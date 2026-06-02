from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from bir.evals import (
    Dataset,
    DatasetExample,
    DeterministicEvaluator,
    EvaluationContext,
    EvalResult,
    contains,
    cost_under,
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
)


class EvalTests(unittest.TestCase):
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
            self.assertEqual(records[0]["scores"][0]["name"], "exact_match")
            self.assertEqual(records[0]["scores"][0]["value"], 1.0)

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


if __name__ == "__main__":
    unittest.main()
