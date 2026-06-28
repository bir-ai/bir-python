from __future__ import annotations

import asyncio
import json
import math
import tempfile
import threading
import urllib.error
import unittest
from collections.abc import Awaitable, Callable
from http.client import HTTPMessage
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bir import configure, generation, load_events, load_traces, retrieval, span
from bir._sdk import _reset_config_for_tests
from bir.evals import (
    Dataset,
    DatasetExample,
    DeterministicEvaluator,
    EvaluationContext,
    EvalResult,
    ExperimentDiff,
    ExperimentResult,
    answer_contains_citation,
    answer_context_overlap,
    contains,
    compare_experiments,
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
    render_experiment_report,
    retrieved_context_contains,
    run_experiment,
    run_experiment_async,
    send_experiment,
)


class EvalTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_compare_experiments_classifies_aggregate_score_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline = self._run_score_experiment(
                Path(directory) / "baseline.jsonl",
                {"quality": 0.8, "speed": 0.5, "stable": 0.7, "removed": 0.4},
            )
            candidate = self._run_score_experiment(
                Path(directory) / "candidate.jsonl",
                {"quality": 0.6, "speed": 0.8, "stable": 0.7, "added": 0.9},
            )

            diff = compare_experiments(baseline, candidate, tolerance=0.1)

            self.assertIsInstance(diff, ExperimentDiff)
            self.assertAlmostEqual(diff.deltas["quality"], -0.2)
            self.assertAlmostEqual(diff.deltas["speed"], 0.3)
            self.assertEqual(diff.deltas["stable"], 0.0)
            self.assertEqual(diff.regressed, {"quality"})
            self.assertEqual(diff.improved, {"speed"})
            self.assertEqual(diff.unchanged, {"stable"})
            self.assertEqual(diff.baseline_only, {"removed"})
            self.assertEqual(diff.candidate_only, {"added"})
            self.assertTrue(diff.has_regressions)
            self.assertEqual(diff.to_dict()["regressed"], ["quality"])

    def test_compare_experiments_tolerance_boundary_is_not_a_regression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline = self._run_score_experiment(Path(directory) / "baseline.jsonl", {"quality": 0.8})
            candidate = self._run_score_experiment(Path(directory) / "candidate.jsonl", {"quality": 0.7})

            diff = compare_experiments(baseline, candidate, tolerance=0.1)

            self.assertFalse(diff.has_regressions)
            self.assertEqual(diff.regressed, set())
            self.assertEqual(diff.unchanged, {"quality"})

    def test_compare_experiments_loads_result_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline = self._run_score_experiment(Path(directory) / "baseline.jsonl", {"quality": 0.9})
            candidate = self._run_score_experiment(Path(directory) / "candidate.jsonl", {"quality": 0.8})

            diff = compare_experiments(baseline.path or "", Path(candidate.path or ""), tolerance=0.05)

            self.assertEqual(diff.regressed, {"quality"})

    def test_compare_experiments_rejects_invalid_tolerance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment = self._run_score_experiment(Path(directory) / "experiment.jsonl", {"quality": 1.0})
            with self.assertRaisesRegex(ValueError, "tolerance must be non-negative"):
                compare_experiments(experiment, experiment, tolerance=-0.1)
            with self.assertRaisesRegex(ValueError, "tolerance must be finite"):
                compare_experiments(experiment, experiment, tolerance=math.inf)

    def test_compare_experiments_score_tolerance_overrides_global(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline = self._run_score_experiment(
                Path(directory) / "baseline.jsonl", {"quality": 0.9, "speed": 0.9}
            )
            candidate = self._run_score_experiment(
                Path(directory) / "candidate.jsonl", {"quality": 0.7, "speed": 0.7}
            )

            # quality gets a roomy override and stays unchanged; speed keeps the
            # strict global tolerance and regresses on the same 0.2 drop.
            diff = compare_experiments(
                baseline, candidate, tolerance=0.0, score_tolerances={"quality": 0.3}
            )

            self.assertEqual(diff.regressed, {"speed"})
            self.assertEqual(diff.unchanged, {"quality"})
            self.assertEqual(diff.effective_tolerances, {"quality": 0.3, "speed": 0.0})
            self.assertEqual(diff.regression_reasons, {"speed": "delta_below_tolerance"})
            self.assertTrue(diff.has_regressions)

    def test_compare_experiments_score_tolerance_boundary_is_strict_per_evaluator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline = self._run_score_experiment(Path(directory) / "baseline.jsonl", {"quality": 0.8})
            candidate = self._run_score_experiment(Path(directory) / "candidate.jsonl", {"quality": 0.7})

            # A drop exactly equal to the per-evaluator tolerance is the unchanged
            # boundary, matching the global-tolerance isclose behavior.
            diff = compare_experiments(baseline, candidate, score_tolerances={"quality": 0.1})

            self.assertFalse(diff.has_regressions)
            self.assertEqual(diff.unchanged, {"quality"})
            self.assertEqual(diff.regression_reasons, {})

    def test_compare_experiments_rejects_unknown_score_tolerance_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline = self._run_score_experiment(
                Path(directory) / "baseline.jsonl", {"quality": 0.9, "removed": 0.5}
            )
            candidate = self._run_score_experiment(
                Path(directory) / "candidate.jsonl", {"quality": 0.9, "added": 0.5}
            )

            # A typo and a baseline-only evaluator are both rejected: a tolerance
            # only has meaning for an evaluator shared by both runs.
            with self.assertRaisesRegex(ValueError, "shared evaluators present in both experiments: qualtiy"):
                compare_experiments(baseline, candidate, score_tolerances={"qualtiy": 0.1})
            with self.assertRaisesRegex(ValueError, "shared evaluators present in both experiments: removed"):
                compare_experiments(baseline, candidate, score_tolerances={"removed": 0.1})

    def test_compare_experiments_rejects_invalid_score_tolerance_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment = self._run_score_experiment(Path(directory) / "experiment.jsonl", {"quality": 1.0})

            with self.assertRaisesRegex(ValueError, r"score_tolerances\['quality'\] must be non-negative"):
                compare_experiments(experiment, experiment, score_tolerances={"quality": -0.1})
            with self.assertRaisesRegex(ValueError, r"score_tolerances\['quality'\] must be finite"):
                compare_experiments(experiment, experiment, score_tolerances={"quality": math.inf})
            with self.assertRaisesRegex(ValueError, r"score_tolerances\['quality'\] must be an int or float"):
                compare_experiments(experiment, experiment, score_tolerances={"quality": True})
            with self.assertRaisesRegex(ValueError, "score_tolerances names must be non-empty strings"):
                compare_experiments(experiment, experiment, score_tolerances={"": 0.1})

    def test_compare_experiments_missing_score_regress_flags_baseline_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline = self._run_score_experiment(
                Path(directory) / "baseline.jsonl", {"quality": 0.9, "coverage": 1.0}
            )
            candidate = self._run_score_experiment(Path(directory) / "candidate.jsonl", {"quality": 0.9})

            ignored = compare_experiments(baseline, candidate)
            self.assertFalse(ignored.has_regressions)
            self.assertEqual(ignored.baseline_only, {"coverage"})
            self.assertEqual(ignored.regression_reasons, {})
            self.assertEqual(ignored.missing_score, "ignore")

            strict = compare_experiments(baseline, candidate, missing_score="regress")
            self.assertTrue(strict.has_regressions)
            self.assertEqual(strict.baseline_only, {"coverage"})
            # The delta-based regressed set stays empty; the missing evaluator is a
            # regression only through the policy and the reasons map.
            self.assertEqual(strict.regressed, frozenset())
            self.assertEqual(strict.regression_reasons, {"coverage": "baseline_only"})
            self.assertEqual(strict.missing_score, "regress")

    def test_compare_experiments_rejects_invalid_missing_score(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment = self._run_score_experiment(Path(directory) / "experiment.jsonl", {"quality": 1.0})
            with self.assertRaisesRegex(ValueError, "missing_score must be one of: ignore, regress"):
                compare_experiments(experiment, experiment, missing_score="drop")

    def test_compare_experiments_to_dict_explains_policy_and_tolerances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline = self._run_score_experiment(
                Path(directory) / "baseline.jsonl", {"quality": 0.9, "speed": 0.9, "coverage": 1.0}
            )
            candidate = self._run_score_experiment(
                Path(directory) / "candidate.jsonl", {"quality": 0.6, "speed": 0.6}
            )

            diff = compare_experiments(
                baseline,
                candidate,
                tolerance=0.05,
                score_tolerances={"quality": 0.5},
                missing_score="regress",
            )
            payload = diff.to_dict()

            self.assertEqual(payload["missing_score"], "regress")
            self.assertEqual(payload["effective_tolerances"], {"quality": 0.5, "speed": 0.05})
            self.assertEqual(
                payload["regression_reasons"],
                {"coverage": "baseline_only", "speed": "delta_below_tolerance"},
            )
            self.assertEqual(payload["baseline_only"], ["coverage"])
            self.assertEqual(payload["regressed"], ["speed"])
            self.assertTrue(payload["has_regressions"])
            # Mappings are emitted in deterministic sorted-key order.
            self.assertEqual(list(payload["regression_reasons"]), ["coverage", "speed"])
            self.assertEqual(list(payload["effective_tolerances"]), ["quality", "speed"])
            # The whole payload round-trips through JSON unchanged.
            self.assertEqual(json.loads(json.dumps(payload)), payload)

    def test_compare_experiments_per_example_records_shared_example_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            # Identical aggregate means (0.5 each), but the per-example movement is
            # opposite: example "a" dropped while "b" improved. The aggregate diff
            # hides this; per_example surfaces it.
            baseline = self._run_per_example_experiment(
                Path(directory) / "baseline.jsonl",
                {"a": {"quality": 1.0}, "b": {"quality": 0.0}},
            )
            candidate = self._run_per_example_experiment(
                Path(directory) / "candidate.jsonl",
                {"a": {"quality": 0.0}, "b": {"quality": 1.0}},
            )

            default_diff = compare_experiments(baseline, candidate)
            self.assertEqual(default_diff.example_deltas, {})
            self.assertNotIn("example_deltas", default_diff.to_dict())

            diff = compare_experiments(baseline, candidate, per_example=True)

            # Aggregate comparison is unchanged by the opt-in detail.
            self.assertEqual(diff.deltas["quality"], 0.0)
            self.assertEqual(diff.unchanged, {"quality"})
            self.assertFalse(diff.has_regressions)
            self.assertEqual(set(diff.example_deltas), {"quality"})
            self.assertAlmostEqual(diff.example_deltas["quality"]["a"], -1.0)
            self.assertAlmostEqual(diff.example_deltas["quality"]["b"], 1.0)

            payload = diff.to_dict()
            self.assertIn("example_deltas", payload)
            # Evaluator and example keys serialize in sorted order.
            self.assertEqual(list(payload["example_deltas"]["quality"]), ["a", "b"])
            self.assertEqual(json.loads(json.dumps(payload)), payload)

    def test_compare_experiments_per_example_skips_unshared_examples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline = self._run_per_example_experiment(
                Path(directory) / "baseline.jsonl",
                {"shared": {"quality": 1.0}, "only_baseline": {"quality": 1.0}},
            )
            candidate = self._run_per_example_experiment(
                Path(directory) / "candidate.jsonl",
                {"shared": {"quality": 0.5}, "only_candidate": {"quality": 0.7}},
            )

            diff = compare_experiments(baseline, candidate, per_example=True)

            # Only the example present in both runs gets a delta; ids unique to one
            # run are skipped rather than raising.
            self.assertEqual(set(diff.example_deltas["quality"]), {"shared"})
            self.assertAlmostEqual(diff.example_deltas["quality"]["shared"], -0.5)

    def test_compare_experiments_per_example_omits_evaluators_without_shared_examples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            # The evaluator is shared in aggregate, but the two runs scored entirely
            # disjoint example ids, so there is no per-example delta to report.
            baseline = self._run_per_example_experiment(
                Path(directory) / "baseline.jsonl", {"a": {"quality": 1.0}}
            )
            candidate = self._run_per_example_experiment(
                Path(directory) / "candidate.jsonl", {"b": {"quality": 1.0}}
            )

            diff = compare_experiments(baseline, candidate, per_example=True)

            self.assertEqual(diff.example_deltas, {})
            self.assertNotIn("example_deltas", diff.to_dict())

    @staticmethod
    def _run_score_experiment(path: Path, scores: dict[str, float]) -> ExperimentResult:
        return run_experiment(
            path.stem,
            dataset=Dataset([DatasetExample(id="row", input={"scores": scores})]),
            task=lambda scores: scores,
            evaluators=[
                custom_evaluator(name, lambda output, _expected, key=name: output[key])
                for name in scores
            ],
            path=path,
        )

    @staticmethod
    def _run_per_example_experiment(
        path: Path, rows: dict[str, dict[str, float]]
    ) -> ExperimentResult:
        evaluator_names = sorted({name for scores in rows.values() for name in scores})
        return run_experiment(
            path.stem,
            dataset=Dataset(
                [DatasetExample(id=example_id, input={"scores": scores}) for example_id, scores in rows.items()]
            ),
            task=lambda scores: scores,
            evaluators=[
                custom_evaluator(name, lambda output, _expected, key=name: output[key])
                for name in evaluator_names
            ],
            path=path,
        )

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

    def test_answer_context_overlap_scores_supported_answers(self) -> None:
        supported = answer_context_overlap(0.8).evaluate(
            {
                "answer": "Bir records local traces.",
                "contexts": ["Bir records local traces with JSONL files."],
            }
        )
        self.assertEqual(supported.value, 1.0)
        self.assertEqual(supported.metadata["overlap_ratio"], 1.0)
        self.assertEqual(supported.metadata["answer_word_count"], 4)
        self.assertEqual(supported.metadata["supported_word_count"], 4)
        self.assertNotIn("unsupported_words", supported.metadata)

        unsupported = answer_context_overlap(0.8).evaluate(
            {
                "answer": "Bir deploys Kubernetes clusters automatically.",
                "contexts": ["Bir records local traces with JSONL files."],
            }
        )
        self.assertEqual(unsupported.value, 0.0)
        self.assertEqual(unsupported.metadata["overlap_ratio"], 0.2)
        self.assertEqual(
            unsupported.metadata["unsupported_words"],
            ["automatically", "clusters", "deploys", "kubernetes"],
        )

    def test_answer_context_overlap_is_case_insensitive(self) -> None:
        result = answer_context_overlap(1.0).evaluate(
            {
                "answer": "PARIS is the CAPITAL.",
                "contexts": ["paris is the capital of France."],
            }
        )
        self.assertEqual(result.value, 1.0)

    def test_answer_context_overlap_reports_missing_and_empty_inputs(self) -> None:
        evaluator = answer_context_overlap(0.5)

        plain_string = evaluator.evaluate("just an answer")
        self.assertEqual(plain_string.value, 0.0)
        self.assertEqual(plain_string.metadata["reason"], "non_object_output")

        missing_answer = evaluator.evaluate({"contexts": ["doc text"]})
        self.assertEqual(missing_answer.value, 0.0)
        self.assertEqual(missing_answer.metadata["reason"], "missing_answer")

        missing_contexts = evaluator.evaluate({"answer": "confident claim"})
        self.assertEqual(missing_contexts.value, 0.0)
        self.assertEqual(missing_contexts.metadata["reason"], "missing_contexts")

        non_string_contexts = evaluator.evaluate({"answer": "claim", "contexts": ["doc", 1]})
        self.assertEqual(non_string_contexts.value, 0.0)
        self.assertEqual(non_string_contexts.metadata["reason"], "missing_contexts")

        empty_contexts = evaluator.evaluate({"answer": "confident claim", "contexts": []})
        self.assertEqual(empty_contexts.value, 0.0)
        self.assertEqual(empty_contexts.metadata["reason"], "empty_contexts")
        self.assertEqual(empty_contexts.metadata["overlap_ratio"], 0.0)

        empty_answer = evaluator.evaluate({"answer": "", "contexts": ["doc text"]})
        self.assertEqual(empty_answer.value, 1.0)
        self.assertEqual(empty_answer.metadata["reason"], "empty_answer")

    def test_answer_context_overlap_rejects_invalid_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 0 and 1"):
            answer_context_overlap(-0.1)
        with self.assertRaisesRegex(ValueError, "between 0 and 1"):
            answer_context_overlap(1.5)
        with self.assertRaisesRegex(ValueError, "finite"):
            answer_context_overlap(math.nan)
        with self.assertRaisesRegex(ValueError, "evaluator name"):
            answer_context_overlap(0.5, name="")

    def test_retrieved_context_contains_scores_hits_and_misses(self) -> None:
        evaluator = retrieved_context_contains("capital of France")

        hit = evaluator.evaluate(
            {
                "answer": "Paris is the capital of France.",
                "contexts": ["France is in Europe.", "Paris is the capital of France."],
                "citations": ["doc-1"],
            }
        )
        self.assertEqual(hit.value, 1.0)
        self.assertEqual(hit.metadata["matched_index"], 1)
        self.assertEqual(hit.metadata["context_count"], 2)
        self.assertNotIn("reason", hit.metadata)

        miss = evaluator.evaluate({"answer": "I am not sure.", "contexts": ["Unrelated document text."]})
        self.assertEqual(miss.value, 0.0)
        self.assertEqual(miss.metadata["context_count"], 1)
        self.assertNotIn("reason", miss.metadata)
        self.assertNotIn("matched_index", miss.metadata)

        json.dumps(hit.to_dict(), allow_nan=False)
        json.dumps(miss.to_dict(), allow_nan=False)

    def test_retrieved_context_contains_reports_missing_and_empty_contexts(self) -> None:
        evaluator = retrieved_context_contains("anything")

        plain_string = evaluator.evaluate("just an answer")
        self.assertEqual(plain_string.value, 0.0)
        self.assertEqual(plain_string.metadata["reason"], "non_object_output")

        missing_contexts = evaluator.evaluate({"answer": "no contexts here"})
        self.assertEqual(missing_contexts.value, 0.0)
        self.assertEqual(missing_contexts.metadata["reason"], "missing_contexts")

        non_string_contexts = evaluator.evaluate({"contexts": ["doc", 1]})
        self.assertEqual(non_string_contexts.value, 0.0)
        self.assertEqual(non_string_contexts.metadata["reason"], "missing_contexts")

        empty_contexts = evaluator.evaluate({"contexts": []})
        self.assertEqual(empty_contexts.value, 0.0)
        self.assertEqual(empty_contexts.metadata["reason"], "empty_contexts")
        self.assertEqual(empty_contexts.metadata["context_count"], 0)

    def test_retrieved_context_contains_supports_case_insensitive_matching(self) -> None:
        output = {"contexts": ["bir stores local traces in jsonl files."]}

        self.assertEqual(retrieved_context_contains("LOCAL TRACES").evaluate(output).value, 0.0)
        self.assertEqual(
            retrieved_context_contains("LOCAL TRACES", case_sensitive=False).evaluate(output).value,
            1.0,
        )

    def test_retrieved_context_contains_rejects_invalid_configuration(self) -> None:
        with self.assertRaisesRegex(TypeError, "expected value must be a string"):
            retrieved_context_contains(123)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "evaluator name"):
            retrieved_context_contains("doc", name="")

    def test_retrieved_context_contains_redacts_failure_metadata(self) -> None:
        result = retrieved_context_contains("api_key=sk-secret").evaluate({"contexts": ["unrelated text"]})

        self.assertEqual(result.value, 0.0)
        self.assertEqual(result.metadata["expected"], "api_key=[redacted]")
        self.assertNotIn("sk-secret", json.dumps(result.to_dict(), allow_nan=False))

    def test_answer_contains_citation_scores_default_bracketed_markers(self) -> None:
        evaluator = answer_contains_citation()

        present = evaluator.evaluate("Paris is the capital of France [1].")
        self.assertEqual(present.value, 1.0)
        self.assertEqual(present.metadata["citation"], "[1]")
        self.assertEqual(present.metadata["pattern"], r"\[[\w-]+\]")
        self.assertNotIn("reason", present.metadata)
        self.assertNotIn("answer_preview", present.metadata)

        labeled = evaluator.evaluate("Bir records local traces [doc-1].")
        self.assertEqual(labeled.value, 1.0)
        self.assertEqual(labeled.metadata["citation"], "[doc-1]")

        absent = evaluator.evaluate("Paris is the capital of France.")
        self.assertEqual(absent.value, 0.0)
        self.assertEqual(absent.metadata["answer_preview"], "Paris is the capital of France.")
        self.assertNotIn("citation", absent.metadata)

        json.dumps(present.to_dict(), allow_nan=False)
        json.dumps(absent.to_dict(), allow_nan=False)

    def test_answer_contains_citation_supports_dict_output(self) -> None:
        evaluator = answer_contains_citation()

        structured = evaluator.evaluate(
            {
                "answer": "Paris is the capital of France [2].",
                "contexts": ["Paris is the capital of France."],
                "citations": ["doc-2"],
            }
        )
        self.assertEqual(structured.value, 1.0)
        self.assertEqual(structured.metadata["citation"], "[2]")

        missing_answer = evaluator.evaluate({"contexts": ["doc text"]})
        self.assertEqual(missing_answer.value, 0.0)
        self.assertEqual(missing_answer.metadata["reason"], "missing_answer")

        non_string_answer = evaluator.evaluate({"answer": 123})
        self.assertEqual(non_string_answer.value, 0.0)
        self.assertEqual(non_string_answer.metadata["reason"], "missing_answer")

        non_text_output = evaluator.evaluate(["not", "an", "answer"])
        self.assertEqual(non_text_output.value, 0.0)
        self.assertEqual(non_text_output.metadata["reason"], "non_text_output")

    def test_answer_contains_citation_supports_custom_pattern(self) -> None:
        evaluator = answer_contains_citation(pattern=r"\(\d+\)")

        hit = evaluator.evaluate("Paris is the capital of France (1).")
        self.assertEqual(hit.value, 1.0)
        self.assertEqual(hit.metadata["citation"], "(1)")
        self.assertEqual(hit.metadata["pattern"], r"\(\d+\)")

        miss = evaluator.evaluate("Paris is the capital of France [1].")
        self.assertEqual(miss.value, 0.0)
        self.assertEqual(miss.metadata["answer_preview"], "Paris is the capital of France [1].")

    def test_answer_contains_citation_rejects_invalid_pattern(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid regex"):
            answer_contains_citation(pattern="[")

    def test_answer_contains_citation_redacts_failure_metadata(self) -> None:
        result = answer_contains_citation().evaluate("Token api_key=sk-secret has no citation marker.")

        self.assertEqual(result.value, 0.0)
        self.assertEqual(result.metadata["answer_preview"], "Token api_key=[redacted] has no citation marker.")
        self.assertNotIn("sk-secret", json.dumps(result.to_dict(), allow_nan=False))

    def test_answer_contains_citation_truncates_long_answer_preview(self) -> None:
        answer = "No citation here. " + "padding " * 60
        result = answer_contains_citation().evaluate(answer)

        self.assertEqual(result.value, 0.0)
        self.assertEqual(len(result.metadata["answer_preview"]), 203)
        self.assertTrue(result.metadata["answer_preview"].endswith("..."))

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
            answer_contains_citation(name="")
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

    def test_dataset_jsonl_export_can_preserve_raw_values_when_opted_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dataset_path = Path(directory) / "dataset.jsonl"
            dataset = Dataset(
                [
                    DatasetExample(
                        id="q1",
                        input={"question": "Capital of France?", "api_key": "sk-secret"},
                        expected={"answer": "Paris", "token": "secret-token"},
                        metadata={"split": "smoke", "authorization": "Bearer secret-token"},
                    )
                ]
            )

            dataset.to_jsonl(dataset_path, redact=False)
            exported_rows = [json.loads(line) for line in dataset_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(
                exported_rows,
                [
                    {
                        "id": "q1",
                        "input": {"question": "Capital of France?", "api_key": "sk-secret"},
                        "expected": {"answer": "Paris", "token": "secret-token"},
                        "metadata": {"split": "smoke", "authorization": "Bearer secret-token"},
                    }
                ],
            )

            loaded = Dataset.from_jsonl(dataset_path)
            self.assertEqual(loaded.examples[0].input, {"question": "Capital of France?", "api_key": "sk-secret"})
            self.assertEqual(loaded.examples[0].expected, {"answer": "Paris", "token": "secret-token"})
            self.assertEqual(
                loaded.examples[0].metadata,
                {"split": "smoke", "authorization": "Bearer secret-token"},
            )

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
            configure(trace_path=trace_path, capture_inputs=True, capture_outputs=True)
            dataset = Dataset([DatasetExample(id="q1", input={"question": "hello"}, expected="HELLO")])

            def answer(question: str) -> str:
                with span("retrieve_context"):
                    with retrieval("search_docs", query=question) as result:
                        result.add_document(id="doc-1", text="local context")
                output = question.upper()
                with generation("local.llm", model="demo", input={"question": question}) as gen:
                    gen.set_output(output)
                    gen.set_usage(input_tokens=1, output_tokens=1)
                return output

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

            trace = load_traces(trace_path)[0]
            events = trace.events
            trace_event = trace.root
            score_events = [event for event in events if event.type == "score"]
            self.assertEqual(trace_event.id, trace_id)
            self.assertEqual(trace_event.name, "experiment.uppercase.q1")
            self.assertEqual(trace_event.metadata["kind"], "experiment")
            self.assertEqual(trace_event.metadata["experiment_id"], result.id)
            self.assertEqual(trace_event.metadata["experiment_name"], "uppercase")
            self.assertEqual(trace_event.metadata["example_id"], "q1")
            self.assertIsNone(trace_event.input)
            self.assertIsNone(trace_event.output)
            self.assertEqual(
                [(event.type, event.name) for event in events[:4]],
                [
                    ("trace", "experiment.uppercase.q1"),
                    ("span", "retrieve_context"),
                    ("tool_call", "search_docs"),
                    ("generation", "local.llm"),
                ],
            )
            self.assertEqual([event.type for event in events[4:]], ["score", "score"])
            self.assertEqual({event.name for event in events[4:]}, {"exact_match", "contains"})
            self.assertEqual(events[1].parent_id, trace_id)
            self.assertEqual(events[2].parent_id, events[1].id)
            self.assertEqual(events[2].input, {"query": "hello"})
            self.assertEqual(events[2].output, {"documents": [{"id": "doc-1", "text": "local context"}]})
            self.assertEqual(events[3].parent_id, trace_id)
            self.assertEqual(events[3].model, "demo")
            self.assertEqual(events[3].input, {"question": "hello"})
            self.assertEqual(events[3].output, "HELLO")
            self.assertEqual({event.name for event in score_events}, {"exact_match", "contains"})
            for score_event in score_events:
                self.assertEqual(score_event.trace_id, trace_id)
                self.assertEqual(score_event.parent_id, trace_id)

    def test_run_experiment_record_traces_writes_error_trace_when_task_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            trace_path = Path(directory) / "traces.jsonl"
            configure(trace_path=trace_path)
            dataset = Dataset([DatasetExample(id="q1", input={"token": "raw-token"})])

            def fail(token: str) -> None:
                with span("failing_step"):
                    raise RuntimeError(f"provider failed token={token}")

            result = run_experiment(
                "failure",
                dataset=dataset,
                task=fail,
                evaluators=[json_valid()],
                path=experiment_path,
                raise_on_error=False,
                record_traces=True,
            )

            trace_id = result.results[0].trace_id
            self.assertIsNotNone(trace_id)
            self.assertEqual(result.status, "error")
            self.assertEqual(result.results[0].error, "provider failed token=[redacted]")

            trace = load_traces(trace_path)[0]
            self.assertEqual(trace.id, trace_id)
            self.assertEqual(trace.status, "error")
            self.assertEqual(trace.root.error, "provider failed token=[redacted]")
            self.assertEqual([(event.type, event.name, event.status) for event in trace.events], [
                ("trace", "experiment.failure.q1", "error"),
                ("span", "failing_step", "error"),
            ])
            self.assertEqual(trace.events[1].parent_id, trace_id)
            self.assertNotIn("raw-token", experiment_path.read_text(encoding="utf-8"))
            self.assertNotIn("raw-token", trace_path.read_text(encoding="utf-8"))

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

    def test_run_experiment_supports_retrieved_context_contains(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            dataset = Dataset(
                [
                    DatasetExample(id="q1", input={"question": "capital"}),
                    DatasetExample(id="q2", input={"question": "country"}),
                ]
            )

            def answer(question: str) -> dict[str, object]:
                if question == "capital":
                    return {
                        "answer": "Paris is the capital of France.",
                        "contexts": ["Paris is the capital of France.", "France is in Europe."],
                    }
                return {"answer": "I am not sure.", "contexts": ["Unrelated document text."]}

            result = run_experiment(
                "rag",
                dataset=dataset,
                task=answer,
                evaluators=[retrieved_context_contains("capital of France")],
                path=experiment_path,
            )

            self.assertEqual(result.status, "success")
            self.assertEqual(result.aggregate_scores["retrieved_context_contains"], 0.5)
            self.assertEqual(result.results[0].scores[0].metadata["matched_index"], 0)
            self.assertNotIn("reason", result.results[1].scores[0].metadata)

    def test_run_experiment_supports_answer_contains_citation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            dataset = Dataset(
                [
                    DatasetExample(id="q1", input={"question": "capital"}),
                    DatasetExample(id="q2", input={"question": "country"}),
                ]
            )

            def answer(question: str) -> dict[str, object]:
                if question == "capital":
                    return {
                        "answer": "Paris is the capital of France [1].",
                        "contexts": ["Paris is the capital of France."],
                    }
                return {"answer": "France is in Europe.", "contexts": ["France is in Europe."]}

            result = run_experiment(
                "rag-citation",
                dataset=dataset,
                task=answer,
                evaluators=[answer_contains_citation()],
                path=experiment_path,
            )

            self.assertEqual(result.status, "success")
            self.assertEqual(result.aggregate_scores["answer_contains_citation"], 0.5)
            self.assertEqual(result.results[0].scores[0].metadata["citation"], "[1]")
            self.assertEqual(result.results[1].scores[0].metadata["answer_preview"], "France is in Europe.")

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
                HTTPMessage(),
                BytesIO(b'{"detail":"failed"}'),
            )
            # retries=0 keeps this a single-attempt check (the retry paths are
            # covered by the dedicated tests below) so it stays fast and never sleeps.
            with patch("urllib.request.urlopen", side_effect=http_error):
                with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                    send_experiment(experiment_path, retries=0)

            with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
                with self.assertRaisesRegex(RuntimeError, "could not send experiment"):
                    send_experiment(experiment_path, retries=0)

    def test_send_experiment_succeeds_on_first_attempt_without_sleeping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            build_sendable_experiment(experiment_path)
            attempts: list[object] = []
            sleeps: list[float] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                attempts.append(request)
                return FakeHttpResponse(b'{"accepted":1,"id":"experiment-1"}')

            with patch("bir._sdk.time.sleep", side_effect=lambda seconds: sleeps.append(seconds)):
                with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    result = send_experiment(experiment_path)

            # A healthy send is one request with no backoff sleep.
            self.assertEqual(len(attempts), 1)
            self.assertEqual(sleeps, [])
            self.assertEqual(result.accepted, 1)
            self.assertEqual(result.experiment_id, "experiment-1")

    def test_send_experiment_retries_network_error_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            build_sendable_experiment(experiment_path)
            attempts: list[object] = []
            sleeps: list[float] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                attempts.append(request)
                if len(attempts) == 1:
                    raise urllib.error.URLError("temporary network blip")
                return FakeHttpResponse(b'{"accepted":1,"id":"experiment-1"}')

            with patch("bir._sdk.time.sleep", side_effect=lambda seconds: sleeps.append(seconds)):
                with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    result = send_experiment(experiment_path)

            self.assertEqual(len(attempts), 2)
            self.assertEqual(sleeps, [0.5])
            self.assertEqual(result.accepted, 1)
            self.assertEqual(result.experiment_id, "experiment-1")

    def test_send_experiment_retries_timeout_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            build_sendable_experiment(experiment_path)
            attempts: list[object] = []
            sleeps: list[float] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                attempts.append(request)
                if len(attempts) == 1:
                    raise TimeoutError("read timed out")
                return FakeHttpResponse(b'{"accepted":1,"id":"experiment-1"}')

            with patch("bir._sdk.time.sleep", side_effect=lambda seconds: sleeps.append(seconds)):
                with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    result = send_experiment(experiment_path)

            self.assertEqual(len(attempts), 2)
            self.assertEqual(sleeps, [0.5])
            self.assertEqual(result.accepted, 1)

    def test_send_experiment_retries_server_5xx_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            build_sendable_experiment(experiment_path)
            attempts: list[object] = []
            sleeps: list[float] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                attempts.append(request)
                if len(attempts) == 1:
                    raise http_error(503, b'{"detail":"Server Error"}')
                return FakeHttpResponse(b'{"accepted":1,"id":"experiment-1"}')

            with patch("bir._sdk.time.sleep", side_effect=lambda seconds: sleeps.append(seconds)):
                with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    result = send_experiment(experiment_path)

            self.assertEqual(len(attempts), 2)
            self.assertEqual(sleeps, [0.5])
            self.assertEqual(result.accepted, 1)

    def test_send_experiment_raises_after_exhausting_retries_with_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            build_sendable_experiment(experiment_path)
            attempts: list[object] = []
            sleeps: list[float] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                attempts.append(request)
                raise urllib.error.URLError("network down")

            with patch("bir._sdk.time.sleep", side_effect=lambda seconds: sleeps.append(seconds)):
                with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    with self.assertRaisesRegex(RuntimeError, "could not send experiment"):
                        send_experiment(experiment_path, retries=2, backoff=0.5)

            # One initial attempt plus two retries, with exponential backoff between.
            self.assertEqual(len(attempts), 3)
            self.assertEqual(sleeps, [0.5, 1.0])

    def test_send_experiment_does_not_retry_client_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            build_sendable_experiment(experiment_path)
            attempts: list[object] = []
            sleeps: list[float] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                attempts.append(request)
                raise http_error(422, b'{"detail":"rejected"}')

            with patch("bir._sdk.time.sleep", side_effect=lambda seconds: sleeps.append(seconds)):
                with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    with self.assertRaisesRegex(RuntimeError, "HTTP 422"):
                        send_experiment(experiment_path)

            self.assertEqual(len(attempts), 1)
            self.assertEqual(sleeps, [])

    def test_send_experiment_does_not_retry_invalid_response_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            build_sendable_experiment(experiment_path)
            attempts: list[object] = []
            sleeps: list[float] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                attempts.append(request)
                return FakeHttpResponse(b"not json")

            with patch("bir._sdk.time.sleep", side_effect=lambda seconds: sleeps.append(seconds)):
                with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    with self.assertRaisesRegex(RuntimeError, "invalid experiment response"):
                        send_experiment(experiment_path)

            # A malformed 2xx body is a permanent failure: one request, no sleep.
            self.assertEqual(len(attempts), 1)
            self.assertEqual(sleeps, [])

    def test_send_experiment_does_not_retry_malformed_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            # Build a valid experiment (so the summary sidecar exists), then
            # corrupt the result file: loading must fail before any network call.
            build_sendable_experiment(experiment_path)
            experiment_path.write_text("{not valid json\n", encoding="utf-8")
            sleeps: list[float] = []

            def must_not_send(*_args: Any, **_kwargs: Any) -> None:
                raise AssertionError("a malformed local file must fail before any request")

            with patch("bir._sdk.time.sleep", side_effect=lambda seconds: sleeps.append(seconds)):
                with patch("urllib.request.urlopen", side_effect=must_not_send):
                    with self.assertRaisesRegex(ValueError, "Invalid JSON in experiment"):
                        send_experiment(experiment_path)

            self.assertEqual(sleeps, [])

    def test_send_experiment_validates_retries_and_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "experiment.jsonl"
            build_sendable_experiment(experiment_path)

            def must_not_send(*_args: Any, **_kwargs: Any) -> None:
                raise AssertionError("invalid retry/backoff must fail before any request")

            with patch("urllib.request.urlopen", side_effect=must_not_send):
                with self.assertRaises(ValueError):
                    send_experiment(experiment_path, retries=-1)
                with self.assertRaises(TypeError):
                    send_experiment(experiment_path, retries=True)
                with self.assertRaises(TypeError):
                    send_experiment(experiment_path, retries=1.5)  # type: ignore[arg-type]
                with self.assertRaises(ValueError):
                    send_experiment(experiment_path, backoff=-0.5)
                with self.assertRaises(ValueError):
                    send_experiment(experiment_path, backoff=float("nan"))
                with self.assertRaises(ValueError):
                    send_experiment(experiment_path, backoff=float("inf"))
                with self.assertRaises(TypeError):
                    send_experiment(experiment_path, backoff=True)
                with self.assertRaises(TypeError):
                    send_experiment(experiment_path, backoff="fast")  # type: ignore[arg-type]


class RunExperimentAsyncTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_matches_run_experiment_for_async_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dataset = Dataset(
                [
                    DatasetExample(id="q1", input={"question": "hello"}, expected="HELLO"),
                    DatasetExample(id="q2", input={"question": "bir"}, expected="BIR"),
                ]
            )

            async def answer(question: str) -> str:
                await asyncio.sleep(0)
                return question.upper()

            def sync_answer(question: str) -> str:
                return question.upper()

            async_path = Path(directory) / "async.jsonl"
            sync_path = Path(directory) / "sync.jsonl"
            result = asyncio.run(
                run_experiment_async(
                    "uppercase",
                    dataset=dataset,
                    task=answer,
                    evaluators=[exact_match(), contains("H", case_sensitive=True)],
                    path=async_path,
                    max_concurrency=2,
                )
            )
            sync_result = run_experiment(
                "uppercase",
                dataset=dataset,
                task=sync_answer,
                evaluators=[exact_match(), contains("H", case_sensitive=True)],
                path=sync_path,
            )

            self.assertEqual(result.status, "success")
            self.assertEqual(result.aggregate_scores, sync_result.aggregate_scores)
            self.assertEqual(
                [row.example_id for row in result.results],
                [row.example_id for row in sync_result.results],
            )
            records = [json.loads(line) for line in async_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([record["example_id"] for record in records], ["q1", "q2"])
            self.assertEqual(records[0]["experiment_name"], "uppercase")
            self.assertEqual(records[0]["scores"][0]["name"], "exact_match")
            self.assertNotIn("trace_id", records[0])

    def test_supports_sync_callables_and_awaitable_returns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dataset = Dataset([DatasetExample(id="q1", input={"question": "hello"}, expected="HELLO")])

            def sync_task(question: str) -> str:
                return question.upper()

            def returns_awaitable(question: str) -> Awaitable[str]:
                async def inner() -> str:
                    await asyncio.sleep(0)
                    return question.upper()

                return inner()

            cases: list[tuple[str, Callable[..., Any]]] = [
                ("sync", sync_task),
                ("awaitable", returns_awaitable),
            ]
            for label, task in cases:
                path = Path(directory) / f"{label}.jsonl"
                result = asyncio.run(
                    run_experiment_async(
                        label,
                        dataset=dataset,
                        task=task,
                        evaluators=[exact_match()],
                        path=path,
                    )
                )
                self.assertEqual(result.status, "success", label)
                self.assertEqual(result.aggregate_scores, {"exact_match": 1.0}, label)

    def test_preserves_dataset_order_under_out_of_order_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ordered.jsonl"
            dataset = Dataset([DatasetExample(id=f"q{index}", input={"n": index}, expected=index) for index in range(8)])

            async def task(n: int) -> int:
                # Later examples finish first, so completion order reverses dataset order.
                await asyncio.sleep((8 - n) * 0.005)
                return n

            result = asyncio.run(
                run_experiment_async(
                    "ordered",
                    dataset=dataset,
                    task=task,
                    evaluators=[exact_match()],
                    path=path,
                    max_concurrency=8,
                )
            )

            expected_ids = [f"q{index}" for index in range(8)]
            self.assertEqual([row.example_id for row in result.results], expected_ids)
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([record["example_id"] for record in records], expected_ids)
            self.assertEqual(result.aggregate_scores, {"exact_match": 1.0})

    def test_bounds_observed_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bounded.jsonl"
            dataset = Dataset([DatasetExample(id=f"q{index}", input={"n": index}) for index in range(12)])
            tracker = {"active": 0, "peak": 0}

            async def task(n: int) -> int:
                tracker["active"] += 1
                tracker["peak"] = max(tracker["peak"], tracker["active"])
                await asyncio.sleep(0.01)
                tracker["active"] -= 1
                return n

            asyncio.run(
                run_experiment_async(
                    "bounded",
                    dataset=dataset,
                    task=task,
                    evaluators=[json_valid()],
                    path=path,
                    max_concurrency=3,
                )
            )

            self.assertGreater(tracker["peak"], 1)  # examples really ran concurrently
            self.assertLessEqual(tracker["peak"], 3)

    def test_default_max_concurrency_runs_one_example_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sequential.jsonl"
            dataset = Dataset([DatasetExample(id=f"q{index}", input={"n": index}) for index in range(5)])
            tracker = {"active": 0, "peak": 0}

            async def task(n: int) -> int:
                tracker["active"] += 1
                tracker["peak"] = max(tracker["peak"], tracker["active"])
                await asyncio.sleep(0)
                tracker["active"] -= 1
                return n

            asyncio.run(
                run_experiment_async(
                    "sequential",
                    dataset=dataset,
                    task=task,
                    evaluators=[json_valid()],
                    path=path,
                )
            )

            self.assertEqual(tracker["peak"], 1)

    def test_rejects_invalid_max_concurrency(self) -> None:
        dataset = Dataset([DatasetExample(id="q1", input={"n": 1})])

        async def task(n: int) -> int:
            return n

        for value in (0, -1):
            with self.assertRaisesRegex(ValueError, "max_concurrency must be a positive integer"):
                asyncio.run(
                    run_experiment_async("v", dataset=dataset, task=task, evaluators=[json_valid()], max_concurrency=value)
                )
        with self.assertRaisesRegex(TypeError, "max_concurrency must be an int"):
            asyncio.run(
                run_experiment_async("v", dataset=dataset, task=task, evaluators=[json_valid()], max_concurrency=True)
            )
        for bad_type in (1.5, "2"):
            with self.assertRaisesRegex(TypeError, "max_concurrency must be an int"):
                asyncio.run(
                    run_experiment_async(
                        "v",
                        dataset=dataset,
                        task=task,
                        evaluators=[json_valid()],
                        max_concurrency=bad_type,  # type: ignore[arg-type]
                    )
                )

    def test_record_traces_isolates_concurrent_examples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "traced.jsonl"
            trace_path = Path(directory) / "traces.jsonl"
            configure(trace_path=trace_path, capture_inputs=True, capture_outputs=True)
            dataset = Dataset(
                [DatasetExample(id=f"q{index}", input={"question": str(index)}, expected=str(index)) for index in range(6)]
            )

            async def answer(question: str) -> str:
                with span("retrieve_context"):
                    await asyncio.sleep(0.005)
                    with retrieval("search_docs", query=question) as result:
                        result.add_document(id=f"doc-{question}", text=question)
                return question

            result = asyncio.run(
                run_experiment_async(
                    "iso",
                    dataset=dataset,
                    task=answer,
                    evaluators=[exact_match()],
                    path=experiment_path,
                    record_traces=True,
                    max_concurrency=4,
                )
            )

            self.assertEqual(result.status, "success")
            traces = load_traces(trace_path)
            self.assertEqual(len(traces), 6)
            self.assertEqual(
                sorted(trace.name for trace in traces),
                [f"experiment.iso.q{index}" for index in range(6)],
            )
            for trace in traces:
                # Every event in a trace shares that trace's id, and the retrieval query
                # matches the trace's own example: concurrent examples never cross-talk.
                self.assertTrue(all(event.trace_id == trace.id for event in trace.events))
                types = [event.type for event in trace.events]
                self.assertEqual(types.count("trace"), 1)
                self.assertEqual(types.count("score"), 1)
                span_event = next(event for event in trace.events if event.type == "span")
                self.assertEqual(span_event.parent_id, trace.id)
                tool_event = next(event for event in trace.events if event.type == "tool_call")
                self.assertEqual(tool_event.parent_id, span_event.id)
                example_number = trace.name.rsplit(".", 1)[1][1:]
                self.assertEqual(tool_event.input, {"query": example_number})

    def test_raises_and_writes_error_summary_through_first_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "failure.jsonl"
            dataset = Dataset(
                [
                    DatasetExample(id="q0", input={"n": 0}),
                    DatasetExample(id="q1", input={"token": "raw-token"}),
                    DatasetExample(id="q2", input={"n": 2}),
                ]
            )

            async def task(**kwargs: object) -> object:
                await asyncio.sleep(0.005)
                if "token" in kwargs:
                    raise RuntimeError(f"provider failed token={kwargs['token']}")
                return kwargs["n"]

            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                asyncio.run(
                    run_experiment_async(
                        "failure",
                        dataset=dataset,
                        task=task,
                        evaluators=[json_valid()],
                        path=experiment_path,
                        max_concurrency=3,
                    )
                )

            summary = load_experiment_summary(experiment_path.with_suffix(".summary.json"))
            self.assertEqual(summary.status, "error")
            self.assertEqual(summary.example_count, 2)  # q0 success + q1 error, in dataset order
            self.assertEqual(summary.error_count, 1)

            loaded = load_experiment(experiment_path)
            self.assertEqual([row.example_id for row in loaded.results], ["q0", "q1"])
            self.assertEqual(loaded.results[1].error, "provider failed token=[redacted]")
            self.assertNotIn("raw-token", experiment_path.read_text(encoding="utf-8"))

    def test_records_errors_when_configured_to_continue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "continue.jsonl"
            dataset = Dataset(
                [
                    DatasetExample(id="q0", input={"n": 0}),
                    DatasetExample(id="q1", input={"token": "raw-token"}),
                    DatasetExample(id="q2", input={"n": 2}),
                ]
            )

            async def task(**kwargs: object) -> object:
                await asyncio.sleep(0)
                if "token" in kwargs:
                    raise RuntimeError(f"provider failed token={kwargs['token']}")
                return kwargs["n"]

            result = asyncio.run(
                run_experiment_async(
                    "continue",
                    dataset=dataset,
                    task=task,
                    evaluators=[json_valid()],
                    path=experiment_path,
                    raise_on_error=False,
                    max_concurrency=3,
                )
            )

            self.assertEqual(result.status, "error")
            self.assertEqual([row.example_id for row in result.results], ["q0", "q1", "q2"])
            self.assertEqual([row.status for row in result.results], ["success", "error", "success"])
            self.assertEqual(result.results[1].error, "provider failed token=[redacted]")
            self.assertNotIn("raw-token", experiment_path.read_text(encoding="utf-8"))

    def test_redacts_secret_inputs_and_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "secrets.jsonl"
            trace_path = Path(directory) / "traces.jsonl"
            configure(trace_path=trace_path)
            dataset = Dataset([DatasetExample(id="q1", input={"api_key": "sk-secret"}, expected="ok")])

            async def answer(api_key: str) -> dict[str, str]:
                await asyncio.sleep(0)
                return {"answer": "ok", "api_key": api_key}

            result = asyncio.run(
                run_experiment_async(
                    "secrets",
                    dataset=dataset,
                    task=answer,
                    evaluators=[json_valid()],
                    path=experiment_path,
                    record_traces=True,
                )
            )

            self.assertEqual(result.results[0].input, {"api_key": "[redacted]"})
            self.assertEqual(result.results[0].output, {"answer": "ok", "api_key": "[redacted]"})
            self.assertNotIn("sk-secret", experiment_path.read_text(encoding="utf-8"))
            self.assertNotIn("sk-secret", trace_path.read_text(encoding="utf-8"))

    def test_empty_dataset_writes_empty_result_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "empty.jsonl"

            async def task(question: str) -> str:
                return question

            result = asyncio.run(
                run_experiment_async(
                    "empty",
                    dataset=Dataset([]),
                    task=task,
                    evaluators=[exact_match()],
                    path=experiment_path,
                )
            )

            self.assertEqual(result.status, "success")
            self.assertEqual(result.results, [])
            self.assertEqual(result.aggregate_scores, {})
            self.assertEqual(experiment_path.read_text(encoding="utf-8"), "")
            summary = load_experiment_summary(experiment_path.with_suffix(".summary.json"))
            self.assertEqual(summary.example_count, 0)

    def test_requires_experiment_name(self) -> None:
        async def task(question: str) -> str:
            return question

        with self.assertRaisesRegex(ValueError, "experiment name must not be empty"):
            asyncio.run(run_experiment_async("", dataset=Dataset([]), task=task, evaluators=[exact_match()]))

    def test_cancellation_cleans_up_children_without_writing_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "cancel.jsonl"
            dataset = Dataset([DatasetExample(id=f"q{index}", input={"n": index}) for index in range(4)])
            cancelled_children: list[int] = []

            async def driver() -> None:
                started = asyncio.Event()

                async def task(n: int) -> int:
                    started.set()
                    try:
                        await asyncio.sleep(10)
                    except asyncio.CancelledError:
                        cancelled_children.append(n)
                        raise
                    return n

                experiment = asyncio.ensure_future(
                    run_experiment_async(
                        "cancel",
                        dataset=dataset,
                        task=task,
                        evaluators=[json_valid()],
                        path=experiment_path,
                        max_concurrency=2,
                    )
                )
                await started.wait()
                experiment.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await experiment

            asyncio.run(driver())
            self.assertTrue(cancelled_children)  # in-flight example tasks were cancelled
            self.assertFalse(experiment_path.exists())
            self.assertFalse(experiment_path.with_suffix(".summary.json").exists())


class RunExperimentMaxWorkersTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_max_workers_1_is_sequential_and_matches_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dataset = Dataset(
                [
                    DatasetExample(id="q1", input={"question": "hello"}, expected="HELLO"),
                    DatasetExample(id="q2", input={"question": "bir"}, expected="BIR"),
                ]
            )

            def answer(question: str) -> str:
                return question.upper()

            path_default = Path(directory) / "default.jsonl"
            path_explicit = Path(directory) / "explicit.jsonl"
            result_default = run_experiment(
                "upper",
                dataset=dataset,
                task=answer,
                evaluators=[exact_match()],
                path=path_default,
            )
            result_explicit = run_experiment(
                "upper",
                dataset=dataset,
                task=answer,
                evaluators=[exact_match()],
                path=path_explicit,
                max_workers=1,
            )

            self.assertEqual(result_default.aggregate_scores, result_explicit.aggregate_scores)
            self.assertEqual(
                [row.example_id for row in result_default.results],
                [row.example_id for row in result_explicit.results],
            )

    def test_preserves_dataset_order_under_out_of_order_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ordered.jsonl"
            n_examples = 8
            dataset = Dataset([DatasetExample(id=f"q{i}", input={"n": i}, expected=i) for i in range(n_examples)])
            # Barrier ensures all workers start before any returns, proving real concurrency.
            barrier = threading.Barrier(n_examples)

            def task(n: int) -> int:
                barrier.wait()
                return n

            result = run_experiment(
                "ordered",
                dataset=dataset,
                task=task,
                evaluators=[exact_match()],
                path=path,
                max_workers=n_examples,
            )

            expected_ids = [f"q{i}" for i in range(n_examples)]
            self.assertEqual([row.example_id for row in result.results], expected_ids)
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([record["example_id"] for record in records], expected_ids)
            self.assertEqual(result.aggregate_scores, {"exact_match": 1.0})

    def test_concurrent_output_matches_sequential_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dataset = Dataset(
                [DatasetExample(id=f"q{i}", input={"question": str(i)}, expected=str(i)) for i in range(6)]
            )

            def answer(question: str) -> str:
                return question

            seq_path = Path(directory) / "seq.jsonl"
            par_path = Path(directory) / "par.jsonl"
            seq_result = run_experiment(
                "equiv",
                dataset=dataset,
                task=answer,
                evaluators=[exact_match(), contains("0", case_sensitive=True)],
                path=seq_path,
            )
            par_result = run_experiment(
                "equiv",
                dataset=dataset,
                task=answer,
                evaluators=[exact_match(), contains("0", case_sensitive=True)],
                path=par_path,
                max_workers=4,
            )

            self.assertEqual(par_result.status, seq_result.status)
            self.assertEqual(par_result.aggregate_scores, seq_result.aggregate_scores)
            self.assertEqual(
                [row.example_id for row in par_result.results],
                [row.example_id for row in seq_result.results],
            )
            for par_row, seq_row in zip(par_result.results, seq_result.results):
                self.assertEqual(par_row.output, seq_row.output)
                self.assertEqual([s.name for s in par_row.scores], [s.name for s in seq_row.scores])
                self.assertEqual([s.value for s in par_row.scores], [s.value for s in seq_row.scores])

            par_records = [json.loads(line) for line in par_path.read_text(encoding="utf-8").splitlines()]
            seq_records = [json.loads(line) for line in seq_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(
                [r["example_id"] for r in par_records],
                [r["example_id"] for r in seq_records],
            )

    def test_raise_on_error_persists_through_first_failure_in_dataset_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "failure.jsonl"
            n_examples = 6
            dataset = Dataset(
                [DatasetExample(id=f"q{i}", input={"n": i, "fail": i == 2}) for i in range(n_examples)]
            )
            barrier = threading.Barrier(n_examples)

            def task(n: int, fail: bool) -> int:
                barrier.wait()
                if fail:
                    raise RuntimeError(f"bad example n={n}")
                return n

            with self.assertRaisesRegex(RuntimeError, "bad example"):
                run_experiment(
                    "fail",
                    dataset=dataset,
                    task=task,
                    evaluators=[json_valid()],
                    path=experiment_path,
                    max_workers=n_examples,
                )

            summary = load_experiment_summary(experiment_path.with_suffix(".summary.json"))
            self.assertEqual(summary.status, "error")
            # Should persist through dataset index 2 (the failing example): q0, q1, q2
            self.assertEqual(summary.example_count, 3)
            self.assertEqual(summary.error_count, 1)

            loaded = load_experiment(experiment_path)
            self.assertEqual([row.example_id for row in loaded.results], ["q0", "q1", "q2"])
            self.assertEqual(loaded.results[2].status, "error")

    def test_raise_on_error_false_records_all_errors_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "continue.jsonl"
            n_examples = 5
            dataset = Dataset(
                [DatasetExample(id=f"q{i}", input={"n": i, "fail": i % 2 == 0}) for i in range(n_examples)]
            )
            barrier = threading.Barrier(n_examples)

            def task(n: int, fail: bool) -> int:
                barrier.wait()
                if fail:
                    raise RuntimeError("deliberate failure")
                return n

            result = run_experiment(
                "continue",
                dataset=dataset,
                task=task,
                evaluators=[json_valid()],
                path=experiment_path,
                raise_on_error=False,
                max_workers=n_examples,
            )

            self.assertEqual(result.status, "error")
            self.assertEqual([row.example_id for row in result.results], [f"q{i}" for i in range(n_examples)])
            expected_statuses = ["error", "success", "error", "success", "error"]
            self.assertEqual([row.status for row in result.results], expected_statuses)

    def test_record_traces_isolates_concurrent_examples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "traced.jsonl"
            trace_path = Path(directory) / "traces.jsonl"
            configure(trace_path=trace_path, capture_inputs=True, capture_outputs=True)
            n_examples = 5
            dataset = Dataset(
                [DatasetExample(id=f"q{i}", input={"question": str(i)}, expected=str(i)) for i in range(n_examples)]
            )
            barrier = threading.Barrier(n_examples)

            def answer(question: str) -> str:
                barrier.wait()
                with span("step"):
                    with retrieval("lookup", query=question) as r:
                        r.add_document(id=f"doc-{question}", text=question)
                return question

            result = run_experiment(
                "iso",
                dataset=dataset,
                task=answer,
                evaluators=[exact_match()],
                path=experiment_path,
                record_traces=True,
                max_workers=n_examples,
            )

            self.assertEqual(result.status, "success")
            traces = load_traces(trace_path)
            self.assertEqual(len(traces), n_examples)
            self.assertEqual(
                sorted(trace.name for trace in traces),
                [f"experiment.iso.q{i}" for i in range(n_examples)],
            )
            for trace in traces:
                # All events in each trace belong to that trace only.
                self.assertTrue(all(event.trace_id == trace.id for event in trace.events))
                types = [event.type for event in trace.events]
                self.assertEqual(types.count("trace"), 1)
                self.assertEqual(types.count("score"), 1)
                # The retrieval query must match the trace's own example number.
                tool_event = next(event for event in trace.events if event.type == "tool_call")
                example_number = trace.name.rsplit(".", 1)[1][1:]
                self.assertEqual(tool_event.input, {"query": example_number})

    def test_redaction_preserved_under_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            experiment_path = Path(directory) / "redact.jsonl"
            dataset = Dataset(
                [DatasetExample(id=f"q{i}", input={"api_key": f"sk-secret-{i}"}) for i in range(4)]
            )

            def answer(api_key: str) -> dict[str, str]:
                return {"result": "ok", "api_key": api_key}

            result = run_experiment(
                "redact",
                dataset=dataset,
                task=answer,
                evaluators=[json_valid()],
                path=experiment_path,
                max_workers=4,
            )

            raw = experiment_path.read_text(encoding="utf-8")
            for i in range(4):
                self.assertNotIn(f"sk-secret-{i}", raw)
            for row in result.results:
                self.assertEqual(row.output, {"result": "ok", "api_key": "[redacted]"})

    def test_rejects_invalid_max_workers(self) -> None:
        dataset = Dataset([DatasetExample(id="q1", input={"n": 1})])

        def task(n: int) -> int:
            return n

        with self.assertRaisesRegex(ValueError, "max_workers must be a positive integer"):
            run_experiment("v", dataset=dataset, task=task, evaluators=[json_valid()], max_workers=0)
        with self.assertRaisesRegex(ValueError, "max_workers must be a positive integer"):
            run_experiment("v", dataset=dataset, task=task, evaluators=[json_valid()], max_workers=-1)
        with self.assertRaisesRegex(TypeError, "max_workers must be an int"):
            run_experiment("v", dataset=dataset, task=task, evaluators=[json_valid()], max_workers=True)  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "max_workers must be an int"):
            run_experiment("v", dataset=dataset, task=task, evaluators=[json_valid()], max_workers=2.5)  # type: ignore[arg-type]


class RenderExperimentReportTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    @staticmethod
    def _run_faq_experiment(path: Path) -> ExperimentResult:
        dataset = Dataset(
            [
                DatasetExample(id="q1", input="hi", expected="ok"),
                DatasetExample(id="q2", input="yo", expected="no"),
            ]
        )
        return run_experiment(
            "faq",
            dataset=dataset,
            task=lambda _question: "ok",
            evaluators=[exact_match(), contains("o")],
            path=path,
        )

    def test_html_report_contains_summary_aggregates_and_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._run_faq_experiment(Path(directory) / "faq.jsonl")

            report = render_experiment_report(result)

            # Standalone, self-contained document with inline styles (no external assets).
            self.assertTrue(report.startswith("<!DOCTYPE html>"))
            self.assertIn("<style>", report)
            self.assertNotIn("<link", report)
            self.assertNotIn("<script", report)
            # Summary header carries name, id, status, and counts.
            self.assertIn("Experiment Report: faq", report)
            self.assertIn(result.id, report)
            self.assertIn("<td>success</td>", report)
            # Evaluator aggregates appear as their own table, sorted by name.
            self.assertIn("<th>Evaluator</th>", report)
            self.assertLess(report.index("<td>contains</td>"), report.index("<td>exact_match</td>"))
            self.assertIn("<td>0.50</td>", report)
            # Per-example rows carry the example id, status, and per-evaluator scores.
            self.assertIn("<td>q1</td>", report)
            self.assertIn("contains=1.00 exact_match=1.00", report)
            self.assertIn("contains=1.00 exact_match=0.00", report)

    def test_html_report_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._run_faq_experiment(Path(directory) / "faq.jsonl")

            self.assertEqual(render_experiment_report(result), render_experiment_report(result))

    def test_html_report_escapes_example_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dataset = Dataset([DatasetExample(id="<script>alert(1)</script>", input="hi")])

            def boom(_question: str) -> str:
                raise ValueError("<b>boom</b> & <i>fail</i>")

            result = run_experiment(
                "inj<ection>",
                dataset=dataset,
                task=boom,
                evaluators=[exact_match("ok")],
                path=Path(directory) / "inj.jsonl",
                raise_on_error=False,
            )

            report = render_experiment_report(result)

            # User-derived strings are HTML-escaped: no raw injected markup survives.
            self.assertNotIn("<script>alert(1)</script>", report)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", report)
            self.assertNotIn("<b>boom</b>", report)
            self.assertIn("&lt;b&gt;boom&lt;/b&gt; &amp; &lt;i&gt;fail&lt;/i&gt;", report)
            # The experiment name in the heading is escaped too.
            self.assertIn("Experiment Report: inj&lt;ection&gt;", report)

    def test_markdown_report_renders_sections_and_escapes_cells(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dataset = Dataset([DatasetExample(id="a|b", input="hi", expected="ok")])
            result = run_experiment(
                "faq",
                dataset=dataset,
                task=lambda _question: "ok",
                evaluators=[exact_match()],
                path=Path(directory) / "faq.jsonl",
            )

            report = render_experiment_report(result, format="markdown")

            self.assertIn("# Experiment Report: faq", report)
            self.assertIn("## Evaluator aggregates", report)
            self.assertIn("| Evaluator | Mean |", report)
            self.assertIn("| exact_match | 1.00 |", report)
            self.assertIn("## Examples", report)
            self.assertIn("| Example | Status | Scores | Error |", report)
            # A pipe in the example id is escaped so the table structure is preserved.
            self.assertIn(r"| a\|b | success |", report)
            # Deterministic across renders.
            self.assertEqual(report, render_experiment_report(result, format="markdown"))

    def test_no_evaluator_scores_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_experiment(
                "faq",
                dataset=Dataset([DatasetExample(id="q1", input="hi")]),
                task=lambda _question: "ok",
                evaluators=[],
                path=Path(directory) / "faq.jsonl",
            )

            self.assertIn("No evaluator scores.", render_experiment_report(result))
            self.assertIn("No evaluator scores.", render_experiment_report(result, format="markdown"))

    def test_unknown_format_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._run_faq_experiment(Path(directory) / "faq.jsonl")

            with self.assertRaisesRegex(ValueError, "format must be one of"):
                render_experiment_report(result, format="pdf")


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


def posted_request_body(request: object) -> dict[str, Any]:
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


def build_sendable_experiment(path: Path) -> ExperimentResult:
    """Run a tiny deterministic experiment so its result and summary files exist on disk."""

    return run_experiment(
        "retrying",
        dataset=Dataset([DatasetExample(id="q1", input={"question": "hello"})]),
        task=lambda question: question,
        evaluators=[json_valid()],
        path=path,
    )


def http_error(code: int, body: bytes = b"{}") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://127.0.0.1:8000/v1/experiments",
        code,
        "error",
        HTTPMessage(),
        BytesIO(body),
    )


if __name__ == "__main__":
    unittest.main()
