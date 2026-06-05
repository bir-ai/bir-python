from __future__ import annotations

import json
import math
import tempfile
import urllib.error
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from bir import configure, load_events
from bir._sdk import _reset_config_for_tests
from bir.evals import (
    Dataset,
    DatasetExample,
    DeterministicEvaluator,
    EvaluationContext,
    EvalResult,
    contains,
    cost_under,
    custom_evaluator,
    exact_match,
    field_contains,
    field_equals,
    json_valid,
    latency_under,
    list_experiments,
    load_experiment,
    load_experiment_summary,
    numeric_between,
    regex_match,
    run_experiment,
    send_experiment,
)


class EvalTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_deterministic_evaluators_return_numeric_scores(self) -> None:
        self.assertEqual(exact_match("Paris").evaluate("Paris").value, 1.0)
        self.assertEqual(exact_match("Paris").evaluate("Lyon").value, 0.0)
        self.assertEqual(contains("paris", case_sensitive=False).evaluate("Paris, France").value, 1.0)
        self.assertEqual(regex_match(r"Paris").evaluate("The answer is Paris.").value, 1.0)
        self.assertEqual(json_valid().evaluate('{"answer":"Paris"}').value, 1.0)
        self.assertEqual(json_valid().evaluate("{not-json").value, 0.0)
        self.assertEqual(numeric_between(min_value=0.0, max_value=1.0).evaluate(0.5).value, 1.0)
        self.assertEqual(numeric_between(min_value=0.0, max_value=1.0).evaluate(2.0).value, 0.0)
        structured_output = {
            "answer": "Paris is the capital of France.",
            "confidence": 0.86,
            "items": [{"name": "Paris"}],
        }
        self.assertEqual(field_equals("items[0].name", "Paris").evaluate(structured_output).value, 1.0)
        self.assertEqual(field_contains("answer", "capital").evaluate(structured_output).value, 1.0)
        self.assertEqual(numeric_between(min_value=0.8, max_value=1.0, field="confidence").evaluate(structured_output).value, 1.0)
        self.assertEqual(custom_evaluator("has_paris", lambda output, expected: "Paris" in str(output)).evaluate("Paris").value, 1.0)

    def test_context_evaluators_return_numeric_scores(self) -> None:
        context = EvaluationContext(
            example=DatasetExample(id="q1", input={"question": "hello"}),
            output={"cost": {"total_cost": 0.02}},
            duration_ms=12.0,
        )

        latency_result = latency_under(20).evaluate(context.output, context=context)
        cost_result = cost_under(0.05).evaluate(context.output, context=context)

        self.assertEqual(latency_result.value, 1.0)
        self.assertEqual(latency_result.metadata["duration_ms"], 12.0)
        self.assertEqual(cost_result.value, 1.0)
        self.assertEqual(cost_result.metadata["actual"], 0.02)

    def test_eval_result_rejects_invalid_values(self) -> None:
        with self.assertRaisesRegex(TypeError, "int or float"):
            EvalResult(name="score", value=True)
        with self.assertRaisesRegex(TypeError, "int or float"):
            EvalResult(name="score", value="1.0")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "finite"):
            EvalResult(name="score", value=math.nan)
        with self.assertRaisesRegex(ValueError, "finite"):
            EvalResult(name="score", value=math.inf)

    def test_eval_result_metadata_is_redacted_and_json_safe(self) -> None:
        result = EvalResult(
            name="score",
            value=1,
            metadata={"api_key": "sk-secret", "non_json": object()},
        )

        self.assertEqual(result.value, 1.0)
        self.assertEqual(result.metadata["api_key"], "[redacted]")
        self.assertIsInstance(result.metadata["non_json"], str)
        json.dumps(result.to_dict(), allow_nan=False)

    def test_eval_result_rejects_invalid_metadata(self) -> None:
        with self.assertRaisesRegex(ValueError, "metadata must be an object"):
            EvalResult(name="score", value=1.0, metadata=["not", "an", "object"])  # type: ignore[arg-type]

    def test_evaluator_config_rejects_invalid_names(self) -> None:
        with self.assertRaisesRegex(ValueError, "evaluator name"):
            DeterministicEvaluator(name="", _evaluate=lambda output, expected: EvalResult("score", 1.0))
        with self.assertRaisesRegex(ValueError, "evaluator name"):
            exact_match(name="")
        with self.assertRaisesRegex(ValueError, "evaluator name"):
            contains("Paris", name="")
        with self.assertRaisesRegex(ValueError, "evaluator name"):
            regex_match(r"Paris", name="")
        with self.assertRaisesRegex(ValueError, "evaluator name"):
            json_valid(name="")
        with self.assertRaisesRegex(ValueError, "evaluator name"):
            field_equals("answer", "ok", name="")
        with self.assertRaisesRegex(ValueError, "evaluator name"):
            field_contains("answer", "ok", name="")
        with self.assertRaisesRegex(ValueError, "evaluator name"):
            latency_under(10, name="")
        with self.assertRaisesRegex(ValueError, "evaluator name"):
            cost_under(0.01, name="")
        with self.assertRaisesRegex(ValueError, "evaluator name"):
            numeric_between(max_value=1.0, name="")
        with self.assertRaisesRegex(ValueError, "evaluator name"):
            custom_evaluator("", lambda output, expected: 1.0)

    def test_custom_evaluator_supports_numeric_bool_and_eval_result_returns(self) -> None:
        numeric = custom_evaluator("numeric_score", lambda output, expected: 0.5)
        boolean = custom_evaluator("has_expected", lambda output, expected: expected in output)
        rich = custom_evaluator(
            "rich_score",
            lambda output, expected: EvalResult(
                name="rich_score",
                value=1.0,
                metadata={"expected": expected, "api_key": "sk-secret"},
            ),
        )

        self.assertEqual(numeric.evaluate("ok").value, 0.5)
        self.assertEqual(boolean.evaluate("Paris, France", expected="Paris").value, 1.0)
        self.assertEqual(boolean.evaluate("Lyon, France", expected="Paris").value, 0.0)
        rich_result = rich.evaluate("Paris", expected="Paris")
        self.assertEqual(rich_result.value, 1.0)
        self.assertEqual(rich_result.metadata["api_key"], "[redacted]")

    def test_custom_evaluator_supports_context(self) -> None:
        evaluator = custom_evaluator(
            "fast_enough",
            lambda context: EvalResult(
                name="fast_enough",
                value=1.0 if context.duration_ms < 20 else 0.0,
                metadata={"example_id": context.example.id if context.example else None},
            ),
            uses_context=True,
        )
        context = EvaluationContext(
            example=DatasetExample(id="q1", input={"question": "hello"}),
            output="ok",
            duration_ms=12.0,
        )

        result = evaluator.evaluate(context.output, context=context)

        self.assertEqual(result.value, 1.0)
        self.assertEqual(result.metadata["example_id"], "q1")

    def test_custom_evaluator_rejects_invalid_configuration_and_returns(self) -> None:
        with self.assertRaisesRegex(TypeError, "must be callable"):
            custom_evaluator("bad", None)  # type: ignore[arg-type]

        invalid_return = custom_evaluator("invalid_return", lambda output, expected: {"score": 1.0})  # type: ignore[return-value]
        with self.assertRaisesRegex(TypeError, "custom evaluator must return"):
            invalid_return.evaluate("ok")

        non_finite = custom_evaluator("non_finite", lambda output, expected: math.inf)
        with self.assertRaisesRegex(ValueError, "finite"):
            non_finite.evaluate("ok")

    def test_custom_evaluator_exceptions_surface(self) -> None:
        def fail(output: object, expected: object) -> float:
            raise RuntimeError("custom evaluator failed token=raw-token")

        with self.assertRaisesRegex(RuntimeError, "custom evaluator failed"):
            custom_evaluator("failure", fail).evaluate("ok")

    def test_threshold_evaluators_reject_invalid_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_ms must be non-negative"):
            latency_under(-1)
        with self.assertRaisesRegex(ValueError, "max_ms must be an int or float"):
            latency_under(True)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "max_cost must be non-negative"):
            cost_under(-0.01)
        with self.assertRaisesRegex(ValueError, "max_cost must be an int or float"):
            cost_under(True)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "cost field must not be empty"):
            cost_under(0.01, field="")
        with self.assertRaisesRegex(ValueError, "numeric_between requires"):
            numeric_between()
        with self.assertRaisesRegex(ValueError, "min_value must be less than or equal to max_value"):
            numeric_between(min_value=2.0, max_value=1.0)
        with self.assertRaisesRegex(ValueError, "min_value must be an int or float"):
            numeric_between(min_value=True)  # type: ignore[arg-type]

    def test_field_evaluators_reject_invalid_paths(self) -> None:
        for path in ("", ".answer", "answer.", "items[]", "items[-1]", "items[abc]", "items[0"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "field path"):
                    field_equals(path, "ok")

        with self.assertRaisesRegex(ValueError, "field path"):
            field_contains("items..name", "ok")
        with self.assertRaisesRegex(ValueError, "field path"):
            numeric_between(max_value=1.0, field="[0]")

    def test_field_evaluators_report_missing_and_mismatched_values(self) -> None:
        output = {
            "answer": "Paris is the capital of France.",
            "items": [{"name": "Paris"}],
            "scores": [{"value": 0.72}],
            "metadata": {"api_key": "sk-secret"},
        }

        self.assertEqual(field_equals("items[0].name", "Paris").evaluate(output).value, 1.0)
        self.assertEqual(field_equals("items[0].name", "Lyon").evaluate(output).value, 0.0)
        self.assertEqual(field_contains("answer", "paris", case_sensitive=False).evaluate(output).value, 1.0)
        self.assertEqual(numeric_between(min_value=0.7, max_value=0.8, field="scores[0].value").evaluate(output).value, 1.0)

        missing_result = field_equals("items[1].name", "Paris").evaluate(output)
        self.assertEqual(missing_result.value, 0.0)
        self.assertEqual(missing_result.metadata["reason"], "index_out_of_range")

        non_string_result = field_contains("scores[0].value", "0.72").evaluate(output)
        self.assertEqual(non_string_result.value, 0.0)
        self.assertEqual(non_string_result.metadata["reason"], "non_string")

        redacted_result = field_equals("metadata.api_key", "sk-secret").evaluate(output)
        self.assertEqual(redacted_result.metadata["actual"], "[redacted]")
        self.assertEqual(redacted_result.metadata["expected"], "[redacted]")

    def test_field_evaluators_can_use_example_expected_values(self) -> None:
        output = {"answer": "Paris"}

        self.assertEqual(field_equals("answer").evaluate(output, expected="Paris").value, 1.0)
        self.assertEqual(field_contains("answer").evaluate(output, expected="ari").value, 1.0)

    def test_numeric_between_field_reports_missing_and_non_numeric_values(self) -> None:
        output = {"scores": [{"value": "api_key=sk-secret"}]}

        missing_result = numeric_between(max_value=1.0, field="scores[1].value").evaluate(output)
        non_numeric_result = numeric_between(max_value=1.0, field="scores[0].value").evaluate(output)

        self.assertEqual(missing_result.value, 0.0)
        self.assertEqual(missing_result.metadata["reason"], "index_out_of_range")
        self.assertEqual(non_numeric_result.value, 0.0)
        self.assertEqual(non_numeric_result.metadata["reason"], "non_numeric")
        self.assertEqual(non_numeric_result.metadata["actual"], "api_key=[redacted]")

    def test_context_evaluator_requires_context(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires an evaluation context"):
            latency_under(10).evaluate("ok")

    def test_evaluation_context_rejects_invalid_duration_and_metadata(self) -> None:
        with self.assertRaisesRegex(ValueError, "duration_ms must be finite"):
            EvaluationContext(example=None, output="ok", duration_ms=math.inf)
        with self.assertRaisesRegex(ValueError, "metadata must be an object"):
            EvaluationContext(example=None, output="ok", duration_ms=1.0, metadata=["bad"])  # type: ignore[arg-type]

    def test_cost_under_handles_missing_and_non_numeric_values(self) -> None:
        missing_context = EvaluationContext(example=None, output={"answer": "ok"}, duration_ms=1.0)
        non_numeric_context = EvaluationContext(
            example=None,
            output={"cost": {"total_cost": "api_key=sk-secret"}},
            duration_ms=1.0,
        )

        missing_result = cost_under(0.01).evaluate(missing_context.output, context=missing_context)
        non_numeric_result = cost_under(0.01).evaluate(non_numeric_context.output, context=non_numeric_context)

        self.assertEqual(missing_result.value, 0.0)
        self.assertEqual(missing_result.metadata["reason"], "missing")
        self.assertEqual(non_numeric_result.value, 0.0)
        self.assertEqual(non_numeric_result.metadata["reason"], "non_numeric")
        self.assertEqual(non_numeric_result.metadata["actual"], "api_key=[redacted]")

    def test_deterministic_evaluator_requires_callable(self) -> None:
        with self.assertRaisesRegex(TypeError, "must be callable"):
            DeterministicEvaluator(name="score", _evaluate=None)  # type: ignore[arg-type]

    def test_dataset_jsonl_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dataset_path = Path(directory) / "dataset.jsonl"
            dataset = Dataset(
                [
                    DatasetExample(
                        id="q1",
                        input={"question": "Capital of France?", "api_key": "sk-secret"},
                        expected="Paris",
                        metadata={"split": "smoke"},
                    )
                ]
            )

            dataset.to_jsonl(dataset_path)
            loaded = Dataset.from_jsonl(dataset_path)

            self.assertEqual(len(loaded), 1)
            example = loaded.examples[0]
            self.assertEqual(example.id, "q1")
            self.assertEqual(example.input, {"question": "Capital of France?", "api_key": "[redacted]"})
            self.assertEqual(example.expected, "Paris")
            self.assertEqual(example.metadata, {"split": "smoke"})

    def test_dataset_jsonl_rejects_invalid_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dataset_path = Path(directory) / "dataset.jsonl"
            dataset_path.write_text('{"input":{"question":"hello"}}\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "field 'id'"):
                Dataset.from_jsonl(dataset_path)

    def test_dataset_rejects_duplicate_example_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate example IDs.*q1"):
            Dataset(
                [
                    DatasetExample(id="q1", input={"question": "one"}),
                    DatasetExample(id="q1", input={"question": "two"}),
                ]
            )

    def test_dataset_jsonl_rejects_duplicate_example_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dataset_path = Path(directory) / "dataset.jsonl"
            dataset_path.write_text(
                "\n".join(
                    [
                        '{"id":"q1","input":{"question":"one"}}',
                        '{"id":"q1","input":{"question":"two"}}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "duplicate example IDs.*q1"):
                Dataset.from_jsonl(dataset_path)

    def test_empty_dataset_is_valid(self) -> None:
        dataset = Dataset([])

        self.assertEqual(len(dataset), 0)
        self.assertEqual(list(dataset), [])

    def test_dataset_example_rejects_invalid_metadata(self) -> None:
        with self.assertRaisesRegex(ValueError, "metadata must be an object"):
            DatasetExample(id="q1", input={"question": "hello"}, metadata=["not", "an", "object"])  # type: ignore[arg-type]

    def test_run_experiment_writes_jsonl_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            trace_path = Path(directory) / "traces.jsonl"
            configure(trace_path=trace_path)
            dataset = Dataset(
                [
                    DatasetExample(id="q1", input={"question": "hello"}, expected="HELLO"),
                    DatasetExample(id="q2", input={"question": "bir"}, expected="BIR"),
                ]
            )

            def answer(question: str) -> str:
                return question.upper()

            result = run_experiment(
                "uppercase",
                dataset=dataset,
                task=answer,
                evaluators=[exact_match(), contains("H", case_sensitive=True)],
                path=experiment_path,
            )

            self.assertEqual(result.status, "success")
            self.assertEqual(result.aggregate_scores, {"contains": 0.5, "exact_match": 1.0})
            self.assertEqual(result.path, str(experiment_path))

            records = [json.loads(line) for line in experiment_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["experiment_name"], "uppercase")
            self.assertEqual(records[0]["example_id"], "q1")
            self.assertNotIn("trace_id", records[0])
            self.assertEqual(records[0]["scores"][0]["name"], "exact_match")
            self.assertEqual(records[0]["scores"][0]["value"], 1.0)
            self.assertFalse(trace_path.exists())

    def test_run_experiment_record_traces_writes_linked_trace_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            trace_path = Path(directory) / "traces.jsonl"
            configure(trace_path=trace_path)
            dataset = Dataset([DatasetExample(id="q1", input={"question": "hello"}, expected="HELLO")])

            def answer(question: str) -> str:
                return question.upper()

            result = run_experiment(
                "uppercase",
                dataset=dataset,
                task=answer,
                evaluators=[exact_match(), contains("H", case_sensitive=True)],
                path=experiment_path,
                record_traces=True,
            )

            trace_id = result.results[0].trace_id
            self.assertIsNotNone(trace_id)
            records = [json.loads(line) for line in experiment_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(records[0]["trace_id"], trace_id)

            events = load_events(trace_path)
            trace_event = next(event for event in events if event.type == "trace")
            score_events = [event for event in events if event.type == "score"]
            self.assertEqual(trace_event.id, trace_id)
            self.assertEqual(trace_event.name, "experiment.uppercase.q1")
            self.assertEqual(trace_event.metadata["kind"], "experiment")
            self.assertEqual(trace_event.metadata["experiment_id"], result.id)
            self.assertEqual(trace_event.metadata["experiment_name"], "uppercase")
            self.assertEqual(trace_event.metadata["example_id"], "q1")
            self.assertIsNone(trace_event.input)
            self.assertIsNone(trace_event.output)
            self.assertEqual({event.name for event in score_events}, {"exact_match", "contains"})
            for score_event in score_events:
                self.assertEqual(score_event.trace_id, trace_id)
                self.assertEqual(score_event.parent_id, trace_id)

    def test_run_experiment_record_traces_redacts_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            trace_path = Path(directory) / "traces.jsonl"
            configure(trace_path=trace_path)
            dataset = Dataset([DatasetExample(id="q1", input={"api_key": "sk-secret"}, expected="ok")])

            def answer(api_key: str) -> dict[str, str]:
                return {"answer": "ok", "api_key": api_key}

            result = run_experiment(
                "secrets",
                dataset=dataset,
                task=answer,
                evaluators=[
                    custom_evaluator(
                        "secret_metadata",
                        lambda output, expected: EvalResult(
                            name="secret_metadata",
                            value=1.0,
                            metadata={"api_key": "sk-secret"},
                        ),
                    )
                ],
                path=experiment_path,
                record_traces=True,
            )

            self.assertEqual(result.results[0].input, {"api_key": "[redacted]"})
            self.assertEqual(result.results[0].output, {"answer": "ok", "api_key": "[redacted]"})
            self.assertNotIn("sk-secret", experiment_path.read_text(encoding="utf-8"))
            self.assertNotIn("sk-secret", trace_path.read_text(encoding="utf-8"))
            score_event = next(event for event in load_events(trace_path) if event.type == "score")
            self.assertEqual(score_event.metadata["api_key"], "[redacted]")

    def test_load_experiment_supports_rows_without_trace_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "legacy.jsonl"
            experiment_path.write_text(
                json.dumps(
                    {
                        "experiment_id": "experiment-1",
                        "experiment_name": "legacy",
                        "id": "result-1",
                        "example_id": "q1",
                        "input": {"question": "hello"},
                        "expected": "HELLO",
                        "output": "HELLO",
                        "scores": [{"name": "exact_match", "value": 1.0, "metadata": {}}],
                        "start_time": "2026-01-01T00:00:00+00:00",
                        "end_time": "2026-01-01T00:00:01+00:00",
                        "duration_ms": 1000.0,
                        "status": "success",
                        "error": None,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            loaded = load_experiment(experiment_path)

            self.assertIsNone(loaded.results[0].trace_id)

    def test_run_experiment_supports_threshold_evaluators(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            dataset = Dataset(
                [
                    DatasetExample(id="q1", input={"cost": 0.02}, expected=0.02),
                    DatasetExample(id="q2", input={"cost": 0.20}, expected=0.20),
                ]
            )

            def answer(cost: float) -> dict[str, object]:
                return {
                    "value": cost,
                    "cost": {"total_cost": cost},
                }

            result = run_experiment(
                "thresholds",
                dataset=dataset,
                task=answer,
                evaluators=[
                    latency_under(1000),
                    cost_under(0.05),
                ],
                path=experiment_path,
            )

            self.assertEqual(result.status, "success")
            self.assertEqual(result.aggregate_scores["latency_under"], 1.0)
            self.assertEqual(result.aggregate_scores["cost_under"], 0.5)
            self.assertEqual(result.results[0].scores[1].metadata["actual"], 0.02)
            self.assertEqual(result.results[1].scores[1].metadata["actual"], 0.2)

    def test_run_experiment_supports_structured_output_evaluators(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            dataset = Dataset(
                [
                    DatasetExample(id="q1", input={"question": "capital"}, expected="Paris"),
                    DatasetExample(id="q2", input={"question": "country"}, expected="France"),
                ]
            )

            def answer(question: str) -> dict[str, object]:
                if question == "capital":
                    return {
                        "answer": "Paris is the capital of France.",
                        "confidence": 0.91,
                        "citations": [{"id": "doc-1"}],
                    }
                return {
                    "answer": "France is in Europe.",
                    "confidence": 0.62,
                    "citations": [],
                }

            result = run_experiment(
                "structured",
                dataset=dataset,
                task=answer,
                evaluators=[
                    field_contains("answer"),
                    field_equals("citations[0].id", "doc-1"),
                    numeric_between(min_value=0.7, max_value=1.0, field="confidence"),
                ],
                path=experiment_path,
            )

            self.assertEqual(result.status, "success")
            self.assertEqual(result.aggregate_scores["field_contains"], 1.0)
            self.assertEqual(result.aggregate_scores["field_equals"], 0.5)
            self.assertEqual(result.aggregate_scores["numeric_between"], 0.5)
            self.assertEqual(result.results[1].scores[1].metadata["reason"], "index_out_of_range")

    def test_run_experiment_supports_custom_evaluators(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            dataset = Dataset(
                [
                    DatasetExample(id="q1", input={"question": "capital"}, expected="Paris"),
                    DatasetExample(id="q2", input={"question": "country"}, expected="France"),
                ]
            )

            def answer(question: str) -> str:
                return "Paris is the capital of France." if question == "capital" else "France is in Europe."

            result = run_experiment(
                "custom",
                dataset=dataset,
                task=answer,
                evaluators=[
                    custom_evaluator("mentions_expected", lambda output, expected: expected in output),
                    custom_evaluator(
                        "custom_metadata",
                        lambda output, expected: EvalResult(
                            name="custom_metadata",
                            value=1.0,
                            metadata={"expected": expected, "api_key": "sk-secret"},
                        ),
                    ),
                ],
                path=experiment_path,
            )

            self.assertEqual(result.status, "success")
            self.assertEqual(result.aggregate_scores["mentions_expected"], 1.0)
            self.assertEqual(result.aggregate_scores["custom_metadata"], 1.0)
            self.assertEqual(result.results[0].scores[1].metadata["api_key"], "[redacted]")
            self.assertNotIn("sk-secret", experiment_path.read_text(encoding="utf-8"))

    def test_run_experiment_supports_context_custom_evaluator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            dataset = Dataset([DatasetExample(id="q1", input={"question": "hello"})])

            def answer(question: str) -> str:
                return question.upper()

            result = run_experiment(
                "custom-context",
                dataset=dataset,
                task=answer,
                evaluators=[
                    custom_evaluator(
                        "has_example",
                        lambda context: EvalResult(
                            name="has_example",
                            value=1.0 if context.example is not None else 0.0,
                            metadata={"duration_ms": context.duration_ms},
                        ),
                        uses_context=True,
                    )
                ],
                path=experiment_path,
            )

            self.assertEqual(result.results[0].scores[0].value, 1.0)
            self.assertIn("duration_ms", result.results[0].scores[0].metadata)

    def test_run_experiment_redacts_threshold_failure_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            dataset = Dataset([DatasetExample(id="q1", input={"cost": "api_key=sk-secret"})])

            def answer(cost: str) -> dict[str, object]:
                return {"cost": {"total_cost": cost}}

            result = run_experiment(
                "redacted-threshold",
                dataset=dataset,
                task=answer,
                evaluators=[cost_under(0.05)],
                path=experiment_path,
            )

            self.assertEqual(result.results[0].scores[0].metadata["actual"], "api_key=[redacted]")
            raw_store = experiment_path.read_text(encoding="utf-8")
            self.assertNotIn("sk-secret", raw_store)

    def test_run_experiment_writes_summary_and_loads_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            dataset = Dataset(
                [
                    DatasetExample(id="q1", input={"question": "hello"}, expected="HELLO"),
                    DatasetExample(id="q2", input={"question": "bye"}, expected="BYE"),
                ]
            )

            def answer(question: str) -> str:
                return question.upper()

            result = run_experiment(
                "uppercase",
                dataset=dataset,
                task=answer,
                evaluators=[exact_match()],
                path=experiment_path,
            )

            summary_path = experiment_path.with_suffix(".summary.json")
            summary = load_experiment_summary(summary_path)
            self.assertEqual(summary.schema_version, "1.0")
            self.assertEqual(summary.experiment_id, result.id)
            self.assertEqual(summary.name, "uppercase")
            self.assertEqual(summary.status, "success")
            self.assertEqual(summary.example_count, 2)
            self.assertEqual(summary.error_count, 0)
            self.assertEqual(summary.aggregate_scores, {"exact_match": 1.0})
            self.assertEqual(summary.result_path, str(experiment_path))

            loaded = load_experiment(experiment_path)
            self.assertEqual(loaded.id, result.id)
            self.assertEqual(loaded.name, result.name)
            self.assertEqual(loaded.status, "success")
            self.assertEqual(loaded.aggregate_scores, {"exact_match": 1.0})
            self.assertEqual([row.example_id for row in loaded.results], ["q1", "q2"])

            listed = list_experiments(directory)
            self.assertEqual([item.experiment_id for item in listed], [result.id])

    def test_load_experiment_supports_empty_experiment_with_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "empty.jsonl"

            def answer(question: str) -> str:
                return question

            result = run_experiment(
                "empty",
                dataset=Dataset([]),
                task=answer,
                evaluators=[exact_match()],
                path=experiment_path,
            )

            self.assertEqual(result.status, "success")
            self.assertEqual(result.aggregate_scores, {})
            loaded = load_experiment(experiment_path)
            self.assertEqual(loaded.id, result.id)
            self.assertEqual(loaded.name, "empty")
            self.assertEqual(loaded.results, [])
            self.assertEqual(load_experiment_summary(experiment_path.with_suffix(".summary.json")).example_count, 0)

    def test_run_experiment_writes_error_summary_before_reraising(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "failure.jsonl"
            dataset = Dataset([DatasetExample(id="q1", input={"token": "raw-token"})])

            def fail(token: str) -> str:
                raise RuntimeError(f"provider failed token={token}")

            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                run_experiment(
                    "failure",
                    dataset=dataset,
                    task=fail,
                    evaluators=[json_valid()],
                    path=experiment_path,
                )

            summary = load_experiment_summary(experiment_path.with_suffix(".summary.json"))
            self.assertEqual(summary.status, "error")
            self.assertEqual(summary.example_count, 1)
            self.assertEqual(summary.error_count, 1)

            loaded = load_experiment(experiment_path)
            self.assertEqual(loaded.status, "error")
            self.assertEqual(loaded.results[0].error, "provider failed token=[redacted]")

    def test_run_experiment_records_errors_when_configured_to_continue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            dataset = Dataset([DatasetExample(id="q1", input={"token": "raw-token"})])

            def fail(token: str) -> str:
                raise RuntimeError(f"provider failed token={token}")

            result = run_experiment(
                "failures",
                dataset=dataset,
                task=fail,
                evaluators=[json_valid()],
                path=experiment_path,
                raise_on_error=False,
            )

            self.assertEqual(result.status, "error")
            self.assertEqual(result.results[0].error, "provider failed token=[redacted]")

            raw_store = experiment_path.read_text(encoding="utf-8")
            self.assertNotIn("raw-token", raw_store)
            record = json.loads(raw_store)
            self.assertEqual(record["input"], {"token": "[redacted]"})
            self.assertEqual(record["status"], "error")

    def test_send_experiment_posts_summary_and_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            dataset = Dataset([DatasetExample(id="q1", input={"api_key": "sk-secret"}, expected="HELLO")])

            def answer(api_key: str) -> str:
                return f"HELLO {api_key}"

            result = run_experiment(
                "uppercase",
                dataset=dataset,
                task=answer,
                evaluators=[contains("HELLO")],
                path=experiment_path,
            )

            with patch("urllib.request.urlopen", return_value=FakeHttpResponse(b'{"accepted":1,"id":"experiment-1"}')) as urlopen:
                send_result = send_experiment(experiment_path, "http://127.0.0.1:8000/")

            self.assertEqual(send_result.accepted, 1)
            self.assertEqual(send_result.experiment_id, "experiment-1")
            request = urlopen.call_args.args[0]
            self.assertEqual(request_url(request), "http://127.0.0.1:8000/v1/experiments")
            payload = posted_request_body(request)
            self.assertEqual(payload["summary"]["experiment_id"], result.id)
            self.assertEqual(payload["results"][0]["example_id"], "q1")
            self.assertNotIn("sk-secret", json.dumps(payload))

    def test_send_experiment_parses_duplicate_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"

            def answer(question: str) -> str:
                return question

            run_experiment(
                "duplicate",
                dataset=Dataset([DatasetExample(id="q1", input={"question": "hello"})]),
                task=answer,
                evaluators=[json_valid()],
                path=experiment_path,
            )

            with patch("urllib.request.urlopen", return_value=FakeHttpResponse(b'{"accepted":0,"id":"experiment-1"}')):
                send_result = send_experiment(experiment_path)

            self.assertEqual(send_result.accepted, 0)
            self.assertEqual(send_result.experiment_id, "experiment-1")

    def test_send_experiment_rejects_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing_path = Path(directory) / "missing.jsonl"
            with self.assertRaisesRegex(ValueError, "result file"):
                send_experiment(missing_path)

            experiment_path = Path(directory) / "experiment.jsonl"
            experiment_path.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "summary file"):
                send_experiment(experiment_path)

    def test_send_experiment_surfaces_http_and_network_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"

            def answer(question: str) -> str:
                return question

            run_experiment(
                "errors",
                dataset=Dataset([DatasetExample(id="q1", input={"question": "hello"})]),
                task=answer,
                evaluators=[contains("hello")],
                path=experiment_path,
            )

            http_error = urllib.error.HTTPError(
                "http://127.0.0.1:8000/v1/experiments",
                500,
                "Server Error",
                {},
                BytesIO(b'{"detail":"failed"}'),
            )
            with patch("urllib.request.urlopen", side_effect=http_error):
                with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                    send_experiment(experiment_path)

            with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
                with self.assertRaisesRegex(RuntimeError, "could not send experiment"):
                    send_experiment(experiment_path)

class FakeHttpResponse:
    status = 201

    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def posted_request_body(request: object) -> dict[str, object]:
    data = getattr(request, "data")
    if not isinstance(data, bytes):
        raise TypeError("expected request data to be bytes")
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("expected request body to be a JSON object")
    return payload


def request_url(request: object) -> str:
    url = getattr(request, "full_url")
    if not isinstance(url, str):
        raise TypeError("expected request to have a full_url")
    return url


if __name__ == "__main__":
    unittest.main()
