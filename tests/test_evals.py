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
    EvalResult,
    contains,
    exact_match,
    json_valid,
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
