"""Deterministic evaluation, dataset, and experiment helpers for Bir."""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import json
import math
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping
from html import escape as _html_escape
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

from ._sdk import (
    _TransientSendError,
    _is_retryable_status,
    _record_score_event,
    _safe_capture,
    _safe_error,
    _send_with_retry,
    _trace_context,
    _validate_non_negative_int,
)
from ._sdk import _validate_non_negative_number as _validate_non_negative_send_number

_USE_EXAMPLE_EXPECTED = object()
_EXPERIMENT_SCHEMA_VERSION = "1.0"
_UNSUPPORTED_WORD_LIMIT = 20
_WORD_TOKEN_PATTERN = re.compile(r"\w+")
_CITATION_ANSWER_PREVIEW_LIMIT = 200
_DEFAULT_CITATION_PATTERN = r"\[[\w-]+\]"

# Self-contained report formats supported by render_experiment_report() and the
# ``bir experiment-report`` CLI command.
_REPORT_FORMATS = ("html", "markdown")

# Missing-score policy vocabulary for compare_experiments(). ``ignore`` keeps the
# historical behavior (baseline-only evaluators are reported but never fail the
# gate); ``regress`` treats a baseline-only evaluator as a regression because a
# removed evaluator silently drops coverage.
_MISSING_SCORE_IGNORE = "ignore"
_MISSING_SCORE_REGRESS = "regress"
_MISSING_SCORE_POLICIES = (_MISSING_SCORE_IGNORE, _MISSING_SCORE_REGRESS)

# Machine-readable explanations recorded in ExperimentDiff.regression_reasons.
_REGRESSION_REASON_DELTA = "delta_below_tolerance"
_REGRESSION_REASON_BASELINE_ONLY = "baseline_only"

__all__ = [
    "Dataset",
    "DatasetExample",
    "DeterministicEvaluator",
    "EvaluationContext",
    "EvalResult",
    "ExperimentDiff",
    "ExperimentExampleResult",
    "ExperimentResult",
    "ExperimentSummary",
    "SendExperimentResult",
    "answer_contains_citation",
    "answer_context_overlap",
    "contains",
    "compare_experiments",
    "cost_under",
    "custom_evaluator",
    "exact_match",
    "field_contains",
    "field_equals",
    "json_valid",
    "latency_under",
    "list_experiments",
    "load_experiment",
    "load_experiment_summary",
    "numeric_between",
    "regex_match",
    "render_experiment_report",
    "retrieved_context_contains",
    "run_experiment",
    "run_experiment_async",
    "send_experiment",
]


@dataclass(frozen=True)
class EvalResult:
    """A numeric evaluator score with optional JSON-safe metadata."""

    name: str
    value: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("eval result name must not be empty")
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise TypeError("eval result value must be an int or float")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("eval result value must be finite")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("eval result metadata must be an object")
        object.__setattr__(self, "value", float(self.value))
        object.__setattr__(self, "metadata", _safe_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of the score."""

        return {
            "name": self.name,
            "value": self.value,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class DeterministicEvaluator:
    """Callable evaluator wrapper used by experiment runs."""

    name: str
    _evaluate: Callable[..., EvalResult]
    _uses_context: bool = False

    def __post_init__(self) -> None:
        _validate_evaluator_name(self.name)
        if not callable(self._evaluate):
            raise TypeError("deterministic evaluator evaluate function must be callable")

    def evaluate(
        self,
        output: Any,
        *,
        expected: Any = None,
        context: EvaluationContext | None = None,
    ) -> EvalResult:
        """Evaluate a task output and return an EvalResult."""

        if self._uses_context:
            if context is None:
                raise ValueError(f"{self.name} requires an evaluation context")
            return self._evaluate(context)
        return self._evaluate(output, expected)


@dataclass(frozen=True)
class EvaluationContext:
    """Runtime context passed to evaluators that need experiment metadata."""

    example: DatasetExample | None
    output: Any
    duration_ms: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "duration_ms", _validate_finite_number(self.duration_ms, "duration_ms"))
        if not isinstance(self.metadata, Mapping):
            raise ValueError("evaluation context metadata must be an object")
        object.__setattr__(self, "metadata", _safe_mapping(self.metadata))


@dataclass(frozen=True)
class DatasetExample:
    """One input, expected output, and metadata row in an evaluation dataset."""

    id: str
    input: Any
    expected: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("dataset example id must not be empty")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("dataset example metadata must be an object")
        object.__setattr__(self, "metadata", {str(key): value for key, value in self.metadata.items()})

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        """Return a JSON-serializable dataset row, redacted by default."""

        input_value = _safe_capture(self.input) if redact else self.input
        expected_value = _safe_capture(self.expected) if redact else self.expected
        metadata = _safe_mapping(self.metadata) if redact else dict(self.metadata)
        return {
            "id": self.id,
            "input": input_value,
            "expected": expected_value,
            "metadata": metadata,
        }


@dataclass(frozen=True)
class Dataset:
    """A collection of uniquely identified examples for experiment runs."""

    examples: list[DatasetExample]

    def __post_init__(self) -> None:
        seen_ids: set[str] = set()
        duplicate_ids: set[str] = set()
        for example in self.examples:
            if example.id in seen_ids:
                duplicate_ids.add(example.id)
            seen_ids.add(example.id)
        if duplicate_ids:
            formatted_ids = ", ".join(sorted(duplicate_ids))
            raise ValueError(f"dataset contains duplicate example IDs: {formatted_ids}")

    @classmethod
    def from_jsonl(cls, path: str | Path) -> Dataset:
        """Load dataset examples from a JSONL file."""

        dataset_path = Path(path)
        examples: list[DatasetExample] = []

        with dataset_path.open("r", encoding="utf-8") as dataset_file:
            for line_number, line in enumerate(dataset_file, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSON in dataset {dataset_path} at line {line_number}") from exc
                if not isinstance(payload, Mapping):
                    raise ValueError(f"Dataset {dataset_path} line {line_number} must contain a JSON object")
                examples.append(_dataset_example_from_payload(payload, dataset_path, line_number))

        return cls(examples)

    def to_jsonl(self, path: str | Path, *, redact: bool = True) -> None:
        """Write dataset examples to a JSONL file.

        Redaction is enabled by default so exported datasets use the same safe
        capture behavior as trace and experiment artifacts. Pass
        ``redact=False`` only when you intentionally want to preserve raw
        example payloads.
        """

        dataset_path = Path(path)
        dataset_path.parent.mkdir(parents=True, exist_ok=True)
        with dataset_path.open("w", encoding="utf-8") as dataset_file:
            for example in self.examples:
                dataset_file.write(_json_line(example.to_dict(redact=redact)))

    def __iter__(self) -> Iterator[DatasetExample]:
        """Iterate over dataset examples."""

        return iter(self.examples)

    def __len__(self) -> int:
        """Return the number of examples in the dataset."""

        return len(self.examples)


@dataclass(frozen=True)
class ExperimentExampleResult:
    """The task output and evaluator scores for one dataset example."""

    id: str
    example_id: str
    input: Any
    expected: Any
    output: Any
    scores: list[EvalResult]
    start_time: str
    end_time: str
    status: str
    error: str | None
    trace_id: str | None = None

    @property
    def duration_ms(self) -> float:
        """Return the example runtime in milliseconds."""

        start = datetime.fromisoformat(self.start_time)
        end = datetime.fromisoformat(self.end_time)
        return (end - start).total_seconds() * 1000

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable experiment result row."""

        payload = {
            "id": self.id,
            "example_id": self.example_id,
            "input": self.input,
            "expected": self.expected,
            "output": self.output,
            "scores": [score.to_dict() for score in self.scores],
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "error": self.error,
        }
        if self.trace_id is not None:
            payload["trace_id"] = self.trace_id
        return payload


@dataclass(frozen=True)
class ExperimentResult:
    """All example results and aggregate scores for one experiment run."""

    id: str
    name: str
    start_time: str
    end_time: str
    status: str
    results: list[ExperimentExampleResult]
    path: str | None

    @property
    def aggregate_scores(self) -> dict[str, float]:
        """Return the mean score for each evaluator name."""

        totals: dict[str, float] = {}
        counts: dict[str, int] = {}
        for result in self.results:
            for score in result.scores:
                totals[score.name] = totals.get(score.name, 0.0) + score.value
                counts[score.name] = counts.get(score.name, 0) + 1
        return {name: totals[name] / counts[name] for name in sorted(totals)}

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable experiment payload."""

        return {
            "id": self.id,
            "name": self.name,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "status": self.status,
            "aggregate_scores": self.aggregate_scores,
            "path": self.path,
            "results": [result.to_dict() for result in self.results],
        }


@dataclass(frozen=True)
class ExperimentDiff:
    """Aggregate-score differences between two experiment runs.

    ``tolerance`` is the global tolerance, while ``effective_tolerances`` records
    the tolerance actually applied to each shared evaluator after per-evaluator
    overrides. ``missing_score`` is the configured policy for evaluators present
    only in the baseline, and ``regression_reasons`` maps every evaluator that
    fails the gate to a machine-readable reason. ``example_deltas`` is the opt-in
    per-example detail: for each shared evaluator it maps an example_id present in
    both runs to the candidate-minus-baseline delta for that example, and is empty
    unless :func:`compare_experiments` was called with ``per_example=True``. All
    mappings are ordered by key so the diff serializes deterministically.
    """

    deltas: dict[str, float]
    regressed: frozenset[str]
    improved: frozenset[str]
    unchanged: frozenset[str]
    baseline_only: frozenset[str]
    candidate_only: frozenset[str]
    tolerance: float
    effective_tolerances: dict[str, float] = field(default_factory=dict)
    missing_score: str = _MISSING_SCORE_IGNORE
    regression_reasons: dict[str, str] = field(default_factory=dict)
    example_deltas: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def has_regressions(self) -> bool:
        """Return whether the configured policy reports any regression.

        A shared evaluator that dropped beyond its effective tolerance always
        counts. When the missing-score policy is ``regress``, evaluators present
        only in the baseline also count: a removed evaluator drops coverage even
        though no aggregate delta can be computed.
        """

        if self.regressed:
            return True
        return self.missing_score == _MISSING_SCORE_REGRESS and bool(self.baseline_only)

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic JSON-serializable representation of the diff.

        ``example_deltas`` is included only when populated (it is empty unless
        per-example detail was requested), so the default aggregate-only output is
        byte-for-byte unchanged from before the field existed.
        """

        payload: dict[str, Any] = {
            "deltas": self.deltas,
            "regressed": sorted(self.regressed),
            "improved": sorted(self.improved),
            "unchanged": sorted(self.unchanged),
            "baseline_only": sorted(self.baseline_only),
            "candidate_only": sorted(self.candidate_only),
            "tolerance": self.tolerance,
            "effective_tolerances": self.effective_tolerances,
            "missing_score": self.missing_score,
            "regression_reasons": self.regression_reasons,
            "has_regressions": self.has_regressions,
        }
        if self.example_deltas:
            payload["example_deltas"] = self.example_deltas
        return payload


@dataclass(frozen=True)
class ExperimentSummary:
    """Compact metadata persisted next to an experiment result file."""

    schema_version: str
    experiment_id: str
    name: str
    start_time: str
    end_time: str
    status: str
    example_count: int
    error_count: int
    aggregate_scores: dict[str, float]
    result_path: str

    def __post_init__(self) -> None:
        if self.schema_version != _EXPERIMENT_SCHEMA_VERSION:
            raise ValueError(f"experiment summary schema_version must be {_EXPERIMENT_SCHEMA_VERSION}")
        if not self.experiment_id:
            raise ValueError("experiment summary experiment_id must not be empty")
        if not self.name:
            raise ValueError("experiment summary name must not be empty")
        if self.status not in {"success", "error"}:
            raise ValueError("experiment summary status must be success or error")
        if isinstance(self.example_count, bool) or not isinstance(self.example_count, int) or self.example_count < 0:
            raise ValueError("experiment summary example_count must be a non-negative integer")
        if isinstance(self.error_count, bool) or not isinstance(self.error_count, int) or self.error_count < 0:
            raise ValueError("experiment summary error_count must be a non-negative integer")
        if not isinstance(self.aggregate_scores, Mapping):
            raise ValueError("experiment summary aggregate_scores must be an object")
        if not self.result_path:
            raise ValueError("experiment summary result_path must not be empty")
        object.__setattr__(
            self,
            "aggregate_scores",
            {
                str(name): _validate_finite_number(value, f"aggregate_scores.{name}")
                for name, value in self.aggregate_scores.items()
            },
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable experiment summary."""

        return {
            "schema_version": self.schema_version,
            "experiment_id": self.experiment_id,
            "name": self.name,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "status": self.status,
            "example_count": self.example_count,
            "error_count": self.error_count,
            "aggregate_scores": self.aggregate_scores,
            "result_path": self.result_path,
        }


@dataclass(frozen=True)
class SendExperimentResult:
    """Result returned after sending an experiment to a Bir server."""

    accepted: int
    experiment_id: str


@dataclass(frozen=True)
class _ResolvedField:
    exists: bool
    value: Any = None
    reason: str | None = None


def exact_match(expected: Any = _USE_EXAMPLE_EXPECTED, *, name: str = "exact_match") -> DeterministicEvaluator:
    """Create an evaluator that scores 1.0 when output equals the expected value."""

    def evaluate(output: Any, example_expected: Any) -> EvalResult:
        target = _expected_value(expected, example_expected, name)
        return EvalResult(
            name=name,
            value=1.0 if output == target else 0.0,
            metadata={"expected": target},
        )

    return DeterministicEvaluator(name=name, _evaluate=evaluate)


def contains(
    expected: str | object = _USE_EXAMPLE_EXPECTED,
    *,
    case_sensitive: bool = True,
    name: str = "contains",
) -> DeterministicEvaluator:
    """Create an evaluator that scores 1.0 when output text contains a string."""

    def evaluate(output: Any, example_expected: Any) -> EvalResult:
        target = _expected_value(expected, example_expected, name)
        if not isinstance(target, str):
            raise TypeError("contains expected value must be a string")
        output_text = "" if output is None else str(output)
        haystack = output_text if case_sensitive else output_text.lower()
        needle = target if case_sensitive else target.lower()
        return EvalResult(name=name, value=1.0 if needle in haystack else 0.0, metadata={"expected": target})

    return DeterministicEvaluator(name=name, _evaluate=evaluate)


def regex_match(pattern: str, *, flags: int = 0, name: str = "regex_match") -> DeterministicEvaluator:
    """Create an evaluator that scores 1.0 when output text matches a regex."""

    compiled = re.compile(pattern, flags)

    def evaluate(output: Any, example_expected: Any) -> EvalResult:
        del example_expected
        output_text = "" if output is None else str(output)
        return EvalResult(
            name=name,
            value=1.0 if compiled.search(output_text) else 0.0,
            metadata={"pattern": pattern},
        )

    return DeterministicEvaluator(name=name, _evaluate=evaluate)


def json_valid(*, name: str = "json_valid") -> DeterministicEvaluator:
    """Create an evaluator that scores 1.0 for JSON-compatible output."""

    def evaluate(output: Any, example_expected: Any) -> EvalResult:
        del example_expected
        try:
            if isinstance(output, str):
                json.loads(output)
            else:
                json.dumps(_safe_capture(output), allow_nan=False)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return EvalResult(name=name, value=0.0, metadata={"error": _safe_error(exc)})
        return EvalResult(name=name, value=1.0)

    return DeterministicEvaluator(name=name, _evaluate=evaluate)


def custom_evaluator(
    name: str,
    evaluate: Callable[..., EvalResult | int | float | bool],
    *,
    uses_context: bool = False,
) -> DeterministicEvaluator:
    """Wrap a user-provided callable as a deterministic evaluator."""

    _validate_evaluator_name(name)
    if not callable(evaluate):
        raise TypeError("custom evaluator function must be callable")

    if uses_context:

        def evaluate_with_context(context: EvaluationContext) -> EvalResult:
            return _coerce_eval_result(name, evaluate(context))

        return DeterministicEvaluator(name=name, _evaluate=evaluate_with_context, _uses_context=True)

    def evaluate_output(output: Any, example_expected: Any) -> EvalResult:
        return _coerce_eval_result(name, evaluate(output, example_expected))

    return DeterministicEvaluator(name=name, _evaluate=evaluate_output)


def field_equals(path: str, expected: Any = _USE_EXAMPLE_EXPECTED, *, name: str = "field_equals") -> DeterministicEvaluator:
    """Create an evaluator that compares a nested output field to an expected value."""

    field_path = _parse_field_path(path)

    def evaluate(output: Any, example_expected: Any) -> EvalResult:
        resolved = _resolve_field_path(output, field_path)
        target = _expected_value(expected, example_expected, name)
        metadata: dict[str, Any] = {
            "path": path,
            "expected": target,
        }
        if not resolved.exists:
            metadata["reason"] = resolved.reason
            return EvalResult(name=name, value=0.0, metadata=metadata)
        metadata["actual"] = resolved.value
        return EvalResult(name=name, value=1.0 if resolved.value == target else 0.0, metadata=metadata)

    return DeterministicEvaluator(name=name, _evaluate=evaluate)


def field_contains(
    path: str,
    expected: str | object = _USE_EXAMPLE_EXPECTED,
    *,
    case_sensitive: bool = True,
    name: str = "field_contains",
) -> DeterministicEvaluator:
    """Create an evaluator that checks whether a nested string field contains text."""

    field_path = _parse_field_path(path)

    def evaluate(output: Any, example_expected: Any) -> EvalResult:
        resolved = _resolve_field_path(output, field_path)
        target = _expected_value(expected, example_expected, name)
        if not isinstance(target, str):
            raise TypeError("field_contains expected value must be a string")
        metadata: dict[str, Any] = {
            "path": path,
            "expected": target,
        }
        if not resolved.exists:
            metadata["reason"] = resolved.reason
            return EvalResult(name=name, value=0.0, metadata=metadata)
        if not isinstance(resolved.value, str):
            metadata["reason"] = "non_string"
            metadata["actual"] = resolved.value
            return EvalResult(name=name, value=0.0, metadata=metadata)
        haystack = resolved.value if case_sensitive else resolved.value.lower()
        needle = target if case_sensitive else target.lower()
        metadata["actual"] = resolved.value
        return EvalResult(name=name, value=1.0 if needle in haystack else 0.0, metadata=metadata)

    return DeterministicEvaluator(name=name, _evaluate=evaluate)


def latency_under(max_ms: float, *, name: str = "latency_under") -> DeterministicEvaluator:
    """Create an evaluator that scores 1.0 when task latency is under a threshold."""

    max_duration = _validate_non_negative_number(max_ms, "max_ms")

    def evaluate(context: EvaluationContext) -> EvalResult:
        return EvalResult(
            name=name,
            value=1.0 if context.duration_ms <= max_duration else 0.0,
            metadata={
                "duration_ms": context.duration_ms,
                "max_ms": max_duration,
            },
        )

    return DeterministicEvaluator(name=name, _evaluate=evaluate, _uses_context=True)


def cost_under(
    max_cost: float,
    *,
    field: str = "total_cost",
    name: str = "cost_under",
) -> DeterministicEvaluator:
    """Create an evaluator that scores 1.0 when a reported cost is under a threshold."""

    max_cost_value = _validate_non_negative_number(max_cost, "max_cost")
    if not field:
        raise ValueError("cost field must not be empty")

    def evaluate(context: EvaluationContext) -> EvalResult:
        value = _extract_cost_value(context.output, field)
        metadata: dict[str, Any] = {
            "field": field,
            "max_cost": max_cost_value,
        }
        if value is None:
            metadata["reason"] = "missing"
            return EvalResult(name=name, value=0.0, metadata=metadata)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            metadata["reason"] = "non_numeric"
            metadata["actual"] = value
            return EvalResult(name=name, value=0.0, metadata=metadata)
        if isinstance(value, float) and not math.isfinite(value):
            metadata["reason"] = "non_finite"
            metadata["actual"] = value
            return EvalResult(name=name, value=0.0, metadata=metadata)
        actual = float(value)
        metadata["actual"] = actual
        return EvalResult(name=name, value=1.0 if actual <= max_cost_value else 0.0, metadata=metadata)

    return DeterministicEvaluator(name=name, _evaluate=evaluate, _uses_context=True)


def numeric_between(
    min_value: float | None = None,
    max_value: float | None = None,
    *,
    field: str | None = None,
    name: str = "numeric_between",
) -> DeterministicEvaluator:
    """Create an evaluator that checks a numeric output or field against bounds."""

    lower_bound = None if min_value is None else _validate_finite_number(min_value, "min_value")
    upper_bound = None if max_value is None else _validate_finite_number(max_value, "max_value")
    if lower_bound is None and upper_bound is None:
        raise ValueError("numeric_between requires min_value or max_value")
    if lower_bound is not None and upper_bound is not None and lower_bound > upper_bound:
        raise ValueError("numeric_between min_value must be less than or equal to max_value")
    field_path = None if field is None else _parse_field_path(field)

    def evaluate(output: Any, example_expected: Any) -> EvalResult:
        del example_expected
        metadata: dict[str, Any] = {
            "min_value": lower_bound,
            "max_value": upper_bound,
        }
        value = output
        if field_path is not None:
            metadata["path"] = field
            resolved = _resolve_field_path(output, field_path)
            if not resolved.exists:
                metadata["reason"] = resolved.reason
                return EvalResult(name=name, value=0.0, metadata=metadata)
            value = resolved.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            metadata["reason"] = "non_numeric"
            metadata["actual"] = value
            return EvalResult(name=name, value=0.0, metadata=metadata)
        if isinstance(value, float) and not math.isfinite(value):
            metadata["reason"] = "non_finite"
            metadata["actual"] = value
            return EvalResult(name=name, value=0.0, metadata=metadata)
        actual = float(value)
        metadata["actual"] = actual
        if lower_bound is not None and actual < lower_bound:
            return EvalResult(name=name, value=0.0, metadata=metadata)
        if upper_bound is not None and actual > upper_bound:
            return EvalResult(name=name, value=0.0, metadata=metadata)
        return EvalResult(name=name, value=1.0, metadata=metadata)

    return DeterministicEvaluator(name=name, _evaluate=evaluate)


def answer_context_overlap(min_ratio: float, *, name: str = "answer_context_overlap") -> DeterministicEvaluator:
    """Create an evaluator that checks how much of an answer is supported by retrieved context.

    The overlap ratio is the fraction of answer word tokens that also appear in
    the retrieved context texts. It is a deterministic heuristic for spotting
    unsupported answers, not proof of faithfulness: paraphrased but faithful
    answers can score low, and unfaithful answers that reuse context words can
    score high.

    The task output must be a mapping with an ``answer`` string and a
    ``contexts`` list of retrieved text strings:

    ``{"answer": "...", "contexts": ["doc text", "other doc text"]}``
    """

    min_ratio_value = _validate_finite_number(min_ratio, "min_ratio")
    if min_ratio_value < 0 or min_ratio_value > 1:
        raise ValueError("min_ratio must be between 0 and 1")

    def evaluate(output: Any, example_expected: Any) -> EvalResult:
        del example_expected
        metadata: dict[str, Any] = {"min_ratio": min_ratio_value}
        if not isinstance(output, Mapping):
            metadata["reason"] = "non_object_output"
            return EvalResult(name=name, value=0.0, metadata=metadata)
        answer = output.get("answer")
        if not isinstance(answer, str):
            metadata["reason"] = "missing_answer"
            return EvalResult(name=name, value=0.0, metadata=metadata)
        contexts = output.get("contexts")
        if not isinstance(contexts, list) or any(not isinstance(item, str) for item in contexts):
            metadata["reason"] = "missing_contexts"
            return EvalResult(name=name, value=0.0, metadata=metadata)

        answer_words = _word_tokens(answer)
        if not answer_words:
            metadata["reason"] = "empty_answer"
            metadata["overlap_ratio"] = 1.0
            return EvalResult(name=name, value=1.0, metadata=metadata)

        context_words: set[str] = set()
        for context_text in contexts:
            context_words.update(_word_tokens(context_text))
        if not context_words:
            metadata["reason"] = "empty_contexts"
            metadata["overlap_ratio"] = 0.0
            metadata["answer_word_count"] = len(answer_words)
            return EvalResult(name=name, value=0.0, metadata=metadata)

        supported_words = answer_words & context_words
        overlap_ratio = len(supported_words) / len(answer_words)
        metadata["overlap_ratio"] = overlap_ratio
        metadata["answer_word_count"] = len(answer_words)
        metadata["supported_word_count"] = len(supported_words)
        unsupported_words = sorted(answer_words - context_words)
        if unsupported_words:
            metadata["unsupported_words"] = unsupported_words[:_UNSUPPORTED_WORD_LIMIT]
        return EvalResult(name=name, value=1.0 if overlap_ratio >= min_ratio_value else 0.0, metadata=metadata)

    return DeterministicEvaluator(name=name, _evaluate=evaluate)


def retrieved_context_contains(
    expected: str,
    *,
    case_sensitive: bool = True,
    name: str = "retrieved_context_contains",
) -> DeterministicEvaluator:
    """Create an evaluator that checks whether retrieved context contains a string.

    This is a deterministic retrieval check, not proof of relevance or
    faithfulness: it only confirms that ``expected`` appears verbatim in one of
    the retrieved context strings, not that the answer relied on it.

    The task output must be a mapping with a ``contexts`` list of retrieved text
    strings:

    ``{"answer": "...", "contexts": ["doc text", "other doc text"]}``
    """

    if not isinstance(expected, str):
        raise TypeError("retrieved_context_contains expected value must be a string")

    def evaluate(output: Any, example_expected: Any) -> EvalResult:
        del example_expected
        metadata: dict[str, Any] = {"expected": expected}
        if not isinstance(output, Mapping):
            metadata["reason"] = "non_object_output"
            return EvalResult(name=name, value=0.0, metadata=metadata)
        contexts = output.get("contexts")
        if not isinstance(contexts, list) or any(not isinstance(item, str) for item in contexts):
            metadata["reason"] = "missing_contexts"
            return EvalResult(name=name, value=0.0, metadata=metadata)
        metadata["context_count"] = len(contexts)
        if not contexts:
            metadata["reason"] = "empty_contexts"
            return EvalResult(name=name, value=0.0, metadata=metadata)

        needle = expected if case_sensitive else expected.lower()
        for index, context_text in enumerate(contexts):
            haystack = context_text if case_sensitive else context_text.lower()
            if needle in haystack:
                metadata["matched_index"] = index
                return EvalResult(name=name, value=1.0, metadata=metadata)
        return EvalResult(name=name, value=0.0, metadata=metadata)

    return DeterministicEvaluator(name=name, _evaluate=evaluate)


def answer_contains_citation(
    *,
    pattern: str | None = None,
    name: str = "answer_contains_citation",
) -> DeterministicEvaluator:
    """Create an evaluator that checks whether an answer includes a citation marker.

    This is a deterministic format check, not proof of grounding or relevance:
    it only confirms that a citation marker is present in the answer text, not
    that the citation is correct or that the cited source supports the answer.

    The task output may be a plain answer string or a structured RAG mapping
    with an ``answer`` string:

    ``"Paris is the capital of France [1]."``
    ``{"answer": "Paris is the capital of France [1].", "contexts": [...]}``

    By default any bracketed marker such as ``[1]`` or ``[doc-1]`` counts as a
    citation. Pass ``pattern`` to override the citation regex, for example
    ``r"\\(\\d+\\)"`` to require parenthetical markers like ``(1)``.
    """

    citation_pattern = _DEFAULT_CITATION_PATTERN if pattern is None else pattern
    try:
        compiled = re.compile(citation_pattern)
    except re.error as exc:
        raise ValueError(f"answer_contains_citation pattern is not a valid regex: {exc}") from exc

    def evaluate(output: Any, example_expected: Any) -> EvalResult:
        del example_expected
        metadata: dict[str, Any] = {"pattern": citation_pattern}
        if isinstance(output, str):
            answer = output
        elif isinstance(output, Mapping):
            answer_value = output.get("answer")
            if not isinstance(answer_value, str):
                metadata["reason"] = "missing_answer"
                return EvalResult(name=name, value=0.0, metadata=metadata)
            answer = answer_value
        else:
            metadata["reason"] = "non_text_output"
            return EvalResult(name=name, value=0.0, metadata=metadata)

        match = compiled.search(answer)
        if match is None:
            metadata["answer_preview"] = _answer_preview(answer)
            return EvalResult(name=name, value=0.0, metadata=metadata)
        metadata["citation"] = match.group(0)
        return EvalResult(name=name, value=1.0, metadata=metadata)

    return DeterministicEvaluator(name=name, _evaluate=evaluate)


def run_experiment(
    name: str,
    *,
    dataset: Dataset | Iterable[DatasetExample],
    task: Callable[..., Any],
    evaluators: Iterable[DeterministicEvaluator],
    path: str | Path | None = None,
    raise_on_error: bool = True,
    record_traces: bool = False,
    max_workers: int = 1,
) -> ExperimentResult:
    """Run a task over a dataset and persist per-example evaluator results.

    When ``max_workers`` is greater than 1, examples run concurrently inside a
    :class:`concurrent.futures.ThreadPoolExecutor` with up to ``max_workers``
    threads. Results, JSONL rows, and summary aggregates are always written in
    dataset order regardless of completion order. Every other behavior matches
    the sequential path: ``raise_on_error`` persists through the first failing
    example in dataset order and re-raises that exception; ``record_traces``
    isolation is preserved because each worker thread inherits its own copy of
    the context-var state, so trace trees never bleed across examples.
    """

    if not name:
        raise ValueError("experiment name must not be empty")
    max_workers = _validate_positive_int(max_workers, "max_workers")

    experiment_id = str(uuid4())
    examples = list(dataset.examples if isinstance(dataset, Dataset) else dataset)
    evaluator_list = list(evaluators)
    start_time = _now()
    output_path = Path(path) if path is not None else _default_experiment_path(name, experiment_id)

    if max_workers > 1:
        return _run_experiment_threaded(
            name=name,
            experiment_id=experiment_id,
            examples=examples,
            task=task,
            evaluator_list=evaluator_list,
            output_path=output_path,
            start_time=start_time,
            raise_on_error=raise_on_error,
            record_traces=record_traces,
            max_workers=max_workers,
        )

    results: list[ExperimentExampleResult] = []
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as experiment_file:
        for example in examples:
            if record_traces:
                result, error = _run_traced_example(
                    experiment_id=experiment_id,
                    experiment_name=name,
                    example=example,
                    task=task,
                    evaluators=evaluator_list,
                )
                if error is not None:
                    results.append(result)
                    _write_experiment_result(experiment_file, experiment_id, name, result)
                    if raise_on_error:
                        end_time = _now()
                        experiment_result = _experiment_result(
                            experiment_id=experiment_id,
                            name=name,
                            start_time=start_time,
                            end_time=end_time,
                            results=results,
                            path=output_path,
                        )
                        _write_experiment_summary(_summary_path(output_path), _summary_from_result(experiment_result))
                        raise error
                    continue
                results.append(result)
                _write_experiment_result(experiment_file, experiment_id, name, result)
                continue

            try:
                result = _run_example(example, task, evaluator_list)
            except Exception as exc:
                result = _error_example_result(example, exc)
                results.append(result)
                _write_experiment_result(experiment_file, experiment_id, name, result)
                if raise_on_error:
                    end_time = _now()
                    experiment_result = _experiment_result(
                        experiment_id=experiment_id,
                        name=name,
                        start_time=start_time,
                        end_time=end_time,
                        results=results,
                        path=output_path,
                    )
                    _write_experiment_summary(_summary_path(output_path), _summary_from_result(experiment_result))
                    raise
                continue
            results.append(result)
            _write_experiment_result(experiment_file, experiment_id, name, result)

    end_time = _now()
    experiment_result = _experiment_result(
        experiment_id=experiment_id,
        name=name,
        start_time=start_time,
        end_time=end_time,
        results=results,
        path=output_path,
    )
    _write_experiment_summary(_summary_path(output_path), _summary_from_result(experiment_result))
    return experiment_result


async def run_experiment_async(
    name: str,
    *,
    dataset: Dataset | Iterable[DatasetExample],
    task: Callable[..., Any],
    evaluators: Iterable[DeterministicEvaluator],
    path: str | Path | None = None,
    raise_on_error: bool = True,
    record_traces: bool = False,
    max_concurrency: int = 1,
) -> ExperimentResult:
    """Run a task over a dataset with bounded concurrency and persist results.

    This is the asynchronous counterpart to :func:`run_experiment`. The ``task``
    may be a coroutine function, a plain sync callable, or a sync callable that
    returns an awaitable; the return value is awaited only when
    :func:`inspect.isawaitable` reports it is awaitable, so the callable itself is
    never inspected. Up to ``max_concurrency`` examples run concurrently, but the
    returned results, the persisted JSONL rows, and the summary aggregates always
    follow dataset order regardless of completion order.

    Every other behavior matches :func:`run_experiment`: evaluator execution,
    task input binding, redaction, ``raise_on_error`` semantics, and the
    persisted JSONL/summary schema are identical. Each example runs in its own
    asyncio task, whose copied context isolates the trace contextvars, so
    ``record_traces=True`` produces a separate trace tree per example even while
    they run concurrently.

    Like :func:`run_experiment`, ``raise_on_error=True`` persists results through
    the first failing example in dataset order, writes the matching error
    summary, and re-raises that example's exception. Because examples run
    concurrently, later examples may already have executed when the failure is
    detected; their results are simply not persisted. If the surrounding
    coroutine is cancelled, the in-flight example tasks are cancelled and awaited
    and ``CancelledError`` propagates without writing a misleading success
    summary.
    """

    if not name:
        raise ValueError("experiment name must not be empty")
    max_concurrency = _validate_positive_int(max_concurrency, "max_concurrency")

    experiment_id = str(uuid4())
    examples = list(dataset.examples if isinstance(dataset, Dataset) else dataset)
    evaluator_list = list(evaluators)
    start_time = _now()
    output_path = Path(path) if path is not None else _default_experiment_path(name, experiment_id)

    semaphore = asyncio.Semaphore(max_concurrency)
    results_by_index: dict[int, ExperimentExampleResult] = {}
    errors_by_index: dict[int, Exception] = {}

    async def run_one(index: int, example: DatasetExample) -> None:
        async with semaphore:
            if record_traces:
                result, error = await _run_traced_example_async(
                    experiment_id=experiment_id,
                    experiment_name=name,
                    example=example,
                    task=task,
                    evaluators=evaluator_list,
                )
            else:
                try:
                    result = await _run_example_async(example, task, evaluator_list)
                    error = None
                except Exception as exc:
                    result = _error_example_result(example, exc)
                    error = exc
            results_by_index[index] = result
            if error is not None:
                errors_by_index[index] = error

    tasks = [asyncio.create_task(run_one(index, example)) for index, example in enumerate(examples)]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        # Includes CancelledError: cancel and await the in-flight example tasks so
        # they clean up, then re-raise without persisting a misleading summary.
        for pending in tasks:
            pending.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    ordered_results = [results_by_index[index] for index in range(len(examples))]
    end_time = _now()

    if raise_on_error and errors_by_index:
        first_error_index = min(errors_by_index)
        _persist_experiment(
            output_path=output_path,
            experiment_id=experiment_id,
            name=name,
            start_time=start_time,
            end_time=end_time,
            results=ordered_results[: first_error_index + 1],
        )
        raise errors_by_index[first_error_index]

    return _persist_experiment(
        output_path=output_path,
        experiment_id=experiment_id,
        name=name,
        start_time=start_time,
        end_time=end_time,
        results=ordered_results,
    )


def load_experiment(path: str | Path) -> ExperimentResult:
    """Load an experiment result JSONL file."""

    experiment_path = Path(path)
    results: list[ExperimentExampleResult] = []
    experiment_id: str | None = None
    experiment_name: str | None = None

    with experiment_path.open("r", encoding="utf-8") as experiment_file:
        for line_number, line in enumerate(experiment_file, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in experiment {experiment_path} at line {line_number}") from exc
            if not isinstance(payload, Mapping):
                raise ValueError(f"Experiment {experiment_path} line {line_number} must contain a JSON object")

            row_experiment_id = _required_string(payload, "experiment_id", experiment_path, line_number)
            row_experiment_name = _required_string(payload, "experiment_name", experiment_path, line_number)
            if experiment_id is None:
                experiment_id = row_experiment_id
            elif experiment_id != row_experiment_id:
                raise ValueError(f"Experiment {experiment_path} contains multiple experiment IDs")
            if experiment_name is None:
                experiment_name = row_experiment_name
            elif experiment_name != row_experiment_name:
                raise ValueError(f"Experiment {experiment_path} contains multiple experiment names")
            results.append(_experiment_example_result_from_payload(payload, experiment_path, line_number))

    if not results:
        summary_path = _summary_path(experiment_path)
        if not summary_path.exists():
            raise ValueError(f"Experiment {experiment_path} does not contain result rows")
        summary = load_experiment_summary(summary_path)
        return ExperimentResult(
            id=summary.experiment_id,
            name=summary.name,
            start_time=summary.start_time,
            end_time=summary.end_time,
            status=summary.status,
            results=[],
            path=str(experiment_path),
        )

    if experiment_id is None or experiment_name is None:
        raise ValueError(f"Experiment {experiment_path} does not contain experiment metadata")

    start_time = min(result.start_time for result in results)
    end_time = max(result.end_time for result in results)
    return _experiment_result(
        experiment_id=experiment_id,
        name=experiment_name,
        start_time=start_time,
        end_time=end_time,
        results=results,
        path=experiment_path,
    )


def render_experiment_report(result: ExperimentResult, *, format: str = "html") -> str:
    """Render one experiment to a self-contained report string.

    The report bundles the run summary, the per-evaluator aggregate means, and a
    per-example table of statuses and scores into a single string with no external
    assets, so a local-first user can share or archive an experiment result
    without standing up the server or dashboard. ``format`` selects ``"html"``
    (the default, a complete standalone HTML document with inline styles) or
    ``"markdown"``.

    Output is deterministic for a given experiment: evaluators are ordered by
    name and examples follow their persisted dataset order, so re-rendering the
    same result yields a byte-identical string. Only already-persisted (already
    redacted) values are rendered, and every experiment-derived string is escaped
    for the chosen format, so example data cannot inject markup. Built with the
    standard library only.
    """

    if format not in _REPORT_FORMATS:
        valid = ", ".join(_REPORT_FORMATS)
        raise ValueError(f"render_experiment_report format must be one of: {valid}")
    if format == "markdown":
        return _render_experiment_report_markdown(result)
    return _render_experiment_report_html(result)


def compare_experiments(
    baseline: ExperimentResult | str | Path,
    candidate: ExperimentResult | str | Path,
    *,
    tolerance: float = 0.0,
    score_tolerances: Mapping[str, float] | None = None,
    missing_score: str = _MISSING_SCORE_IGNORE,
    per_example: bool = False,
) -> ExperimentDiff:
    """Compare shared aggregate evaluator scores from two experiment runs.

    A shared evaluator regresses when ``candidate - baseline`` is strictly less
    than ``-tolerance``. ``score_tolerances`` maps an evaluator name to a
    non-negative, finite tolerance that overrides the global ``tolerance`` for
    that evaluator only; the strict ``math.isclose`` boundary is preserved per
    evaluator. Every override name must be a shared evaluator present in both
    runs, so a typo or a tolerance for a non-comparable evaluator raises a clear
    error instead of being silently ignored.

    ``missing_score`` selects how evaluators present only in the baseline are
    treated. ``"ignore"`` (the default) reports them without failing the gate,
    matching the historical behavior. ``"regress"`` treats each baseline-only
    evaluator as a regression, because a removed evaluator silently drops
    coverage even though no aggregate delta can be computed. Evaluators found
    only in the candidate are always reported but never counted as regressions.

    ``per_example`` is opt-in reporting detail and never changes the aggregate
    comparison or the gate decision. When True, the returned diff's
    ``example_deltas`` records, for each shared evaluator, the
    candidate-minus-baseline score delta of every example_id that both runs
    scored with that evaluator; examples present in only one run (or not scored by
    the evaluator, such as an errored example) are skipped. When False (the
    default) ``example_deltas`` is empty and the diff is identical to before.
    """

    validated_tolerance = _validate_non_negative_number(tolerance, "tolerance")
    validated_missing_score = _validate_missing_score(missing_score)

    baseline_result = baseline if isinstance(baseline, ExperimentResult) else load_experiment(baseline)
    candidate_result = candidate if isinstance(candidate, ExperimentResult) else load_experiment(candidate)
    baseline_scores = baseline_result.aggregate_scores
    candidate_scores = candidate_result.aggregate_scores
    shared = baseline_scores.keys() & candidate_scores.keys()

    overrides = _validate_score_tolerances(score_tolerances, shared)
    deltas = {name: candidate_scores[name] - baseline_scores[name] for name in sorted(shared)}
    effective_tolerances = {name: overrides.get(name, validated_tolerance) for name in sorted(shared)}

    regressed_names: list[str] = []
    improved_names: list[str] = []
    regression_reasons: dict[str, str] = {}
    for name in sorted(shared):
        delta = deltas[name]
        evaluator_tolerance = effective_tolerances[name]
        if delta < -evaluator_tolerance and not math.isclose(
            delta, -evaluator_tolerance, rel_tol=1e-12, abs_tol=1e-12
        ):
            regressed_names.append(name)
            regression_reasons[name] = _REGRESSION_REASON_DELTA
        elif delta > evaluator_tolerance and not math.isclose(
            delta, evaluator_tolerance, rel_tol=1e-12, abs_tol=1e-12
        ):
            improved_names.append(name)

    baseline_only = frozenset(baseline_scores.keys() - candidate_scores.keys())
    candidate_only = frozenset(candidate_scores.keys() - baseline_scores.keys())
    if validated_missing_score == _MISSING_SCORE_REGRESS:
        for name in baseline_only:
            regression_reasons[name] = _REGRESSION_REASON_BASELINE_ONLY

    regressed = frozenset(regressed_names)
    improved = frozenset(improved_names)
    unchanged = frozenset(shared - regressed - improved)
    example_deltas = _per_example_deltas(baseline_result, candidate_result, shared) if per_example else {}
    return ExperimentDiff(
        deltas=deltas,
        regressed=regressed,
        improved=improved,
        unchanged=unchanged,
        baseline_only=baseline_only,
        candidate_only=candidate_only,
        tolerance=validated_tolerance,
        effective_tolerances=effective_tolerances,
        missing_score=validated_missing_score,
        regression_reasons=dict(sorted(regression_reasons.items())),
        example_deltas=example_deltas,
    )


def _per_example_deltas(
    baseline_result: ExperimentResult,
    candidate_result: ExperimentResult,
    shared: Any,
) -> dict[str, dict[str, float]]:
    """Compute candidate-minus-baseline deltas per shared evaluator and example.

    Only evaluators in ``shared`` (present in both runs' aggregate scores) and
    example_ids that both runs scored with that evaluator are included; an
    evaluator with no overlapping examples is omitted entirely. Keys are sorted by
    evaluator then example_id so the result serializes deterministically.
    """

    baseline_scores = _example_scores_by_evaluator(baseline_result)
    candidate_scores = _example_scores_by_evaluator(candidate_result)
    deltas: dict[str, dict[str, float]] = {}
    for name in sorted(shared):
        baseline_examples = baseline_scores.get(name, {})
        candidate_examples = candidate_scores.get(name, {})
        shared_examples = baseline_examples.keys() & candidate_examples.keys()
        if not shared_examples:
            continue
        deltas[name] = {
            example_id: candidate_examples[example_id] - baseline_examples[example_id]
            for example_id in sorted(shared_examples)
        }
    return deltas


def _example_scores_by_evaluator(result: ExperimentResult) -> dict[str, dict[str, float]]:
    """Index one run's scores as ``{evaluator name: {example_id: value}}``.

    If an example_id appears more than once for an evaluator the last row wins,
    matching the order results were persisted; uniquely identified datasets never
    hit that case.
    """

    scores_by_evaluator: dict[str, dict[str, float]] = {}
    for example_result in result.results:
        for score in example_result.scores:
            scores_by_evaluator.setdefault(score.name, {})[example_result.example_id] = score.value
    return scores_by_evaluator


def load_experiment_summary(path: str | Path) -> ExperimentSummary:
    """Load an experiment summary JSON file."""

    summary_path = Path(path)
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in experiment summary {summary_path}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"Experiment summary {summary_path} must contain a JSON object")
    return _experiment_summary_from_payload(payload, summary_path)


def list_experiments(directory: str | Path = Path(".bir") / "experiments") -> list[ExperimentSummary]:
    """List experiment summaries in newest-first order."""

    experiment_directory = Path(directory)
    if not experiment_directory.exists():
        return []
    summaries = [
        load_experiment_summary(summary_path)
        for summary_path in experiment_directory.glob("*.summary.json")
        if summary_path.is_file()
    ]
    return sorted(summaries, key=lambda summary: (summary.start_time, summary.experiment_id), reverse=True)


def send_experiment(
    path: str | Path,
    server_url: str = "http://127.0.0.1:8000",
    *,
    timeout: float = 10.0,
    retries: int = 2,
    backoff: float = 0.5,
) -> SendExperimentResult:
    """Send a persisted experiment and its summary to a Bir server.

    Transient failures are retried with exponential backoff, matching
    ``send_events``: a network error, timeout, or HTTP 5xx is retried up to
    ``retries`` times (default ``2``), sleeping ``backoff * 2**attempt`` seconds
    between tries (``backoff`` defaults to ``0.5``). A 4xx response is a permanent
    rejection raised immediately without retry, as are a missing experiment or
    summary file and an invalid success response body. A healthy send still makes
    a single attempt with no sleep, so the default behavior is unchanged.
    """

    retries = _validate_non_negative_int(retries, "retries")
    backoff = float(_validate_non_negative_send_number(backoff, "backoff"))

    experiment_path = Path(path)
    if not experiment_path.exists():
        raise ValueError(f"Experiment result file {experiment_path} does not exist")
    summary_path = _summary_path(experiment_path)
    if not summary_path.exists():
        raise ValueError(f"Experiment summary file {summary_path} does not exist")
    experiment = load_experiment(experiment_path)
    summary = load_experiment_summary(summary_path)
    payload = {
        "summary": summary.to_dict(),
        "results": [result.to_dict() for result in experiment.results],
    }
    endpoint = _experiments_endpoint(server_url)
    return _send_with_retry(
        lambda: _post_experiment(endpoint, payload, timeout=timeout),
        retries=retries,
        backoff=backoff,
    )


def _run_example(
    example: DatasetExample,
    task: Callable[..., Any],
    evaluators: list[DeterministicEvaluator],
) -> ExperimentExampleResult:
    start_time = _now()
    output = _call_task(task, example.input)
    task_end_time = _now()
    return _evaluate_example_output(
        example,
        output,
        start_time=start_time,
        task_end_time=task_end_time,
        evaluators=evaluators,
    )


async def _run_example_async(
    example: DatasetExample,
    task: Callable[..., Any],
    evaluators: list[DeterministicEvaluator],
) -> ExperimentExampleResult:
    start_time = _now()
    output = await _call_task_async(task, example.input)
    task_end_time = _now()
    return _evaluate_example_output(
        example,
        output,
        start_time=start_time,
        task_end_time=task_end_time,
        evaluators=evaluators,
    )


def _evaluate_example_output(
    example: DatasetExample,
    output: Any,
    *,
    start_time: str,
    task_end_time: str,
    evaluators: list[DeterministicEvaluator],
) -> ExperimentExampleResult:
    context = EvaluationContext(
        example=example,
        output=output,
        duration_ms=_duration_ms(start_time, task_end_time),
        metadata=example.metadata,
    )
    scores = [evaluator.evaluate(output, expected=example.expected, context=context) for evaluator in evaluators]
    end_time = _now()
    return ExperimentExampleResult(
        id=str(uuid4()),
        example_id=example.id,
        input=_safe_capture(example.input),
        expected=_safe_capture(example.expected),
        output=_safe_capture(output),
        scores=scores,
        start_time=start_time,
        end_time=end_time,
        status="success",
        error=None,
    )


def _error_example_result(example: DatasetExample, exc: Exception) -> ExperimentExampleResult:
    timestamp = _now()
    return ExperimentExampleResult(
        id=str(uuid4()),
        example_id=example.id,
        input=_safe_capture(example.input),
        expected=_safe_capture(example.expected),
        output=None,
        scores=[],
        start_time=timestamp,
        end_time=timestamp,
        status="error",
        error=_safe_error(exc),
    )


def _run_traced_example(
    *,
    experiment_id: str,
    experiment_name: str,
    example: DatasetExample,
    task: Callable[..., Any],
    evaluators: list[DeterministicEvaluator],
) -> tuple[ExperimentExampleResult, Exception | None]:
    trace = _experiment_trace_context(experiment_id, experiment_name, example)
    trace.__enter__()
    if trace.id is None:
        raise RuntimeError("bir experiment trace context did not provide a trace id")

    try:
        result = _run_example(example, task, evaluators)
        _record_experiment_scores(trace.id, result)
    except Exception as exc:
        trace.__exit__(type(exc), exc, exc.__traceback__)
        return replace(_error_example_result(example, exc), trace_id=trace.id), exc

    trace.__exit__(None, None, None)
    return replace(result, trace_id=trace.id), None


async def _run_traced_example_async(
    *,
    experiment_id: str,
    experiment_name: str,
    example: DatasetExample,
    task: Callable[..., Any],
    evaluators: list[DeterministicEvaluator],
) -> tuple[ExperimentExampleResult, Exception | None]:
    # ``asyncio.create_task`` copies the current context for each example, so the
    # trace contextvars set here stay isolated from concurrently running examples
    # and the task's nested observations attach to this trace tree only.
    trace = _experiment_trace_context(experiment_id, experiment_name, example)
    trace.__enter__()
    if trace.id is None:
        raise RuntimeError("bir experiment trace context did not provide a trace id")

    try:
        result = await _run_example_async(example, task, evaluators)
        _record_experiment_scores(trace.id, result)
    except Exception as exc:
        trace.__exit__(type(exc), exc, exc.__traceback__)
        return replace(_error_example_result(example, exc), trace_id=trace.id), exc

    trace.__exit__(None, None, None)
    return replace(result, trace_id=trace.id), None


def _experiment_trace_context(
    experiment_id: str,
    experiment_name: str,
    example: DatasetExample,
) -> Any:
    return _trace_context(
        name=f"experiment.{experiment_name}.{example.id}",
        metadata={
            "kind": "experiment",
            "experiment_id": experiment_id,
            "experiment_name": experiment_name,
            "example_id": example.id,
        },
    )


def _record_experiment_scores(trace_id: str, result: ExperimentExampleResult) -> None:
    for score in result.scores:
        _record_score_event(
            trace_id=trace_id,
            parent_id=trace_id,
            name=score.name,
            value=score.value,
            metadata=score.metadata,
            timestamp=result.end_time,
        )


def _call_task(task: Callable[..., Any], input_value: Any) -> Any:
    if isinstance(input_value, Mapping):
        return task(**input_value)
    return task(input_value)


async def _call_task_async(task: Callable[..., Any], input_value: Any) -> Any:
    # Reuse the same input binding as the sync runner, then await only when the
    # call returns an awaitable so plain sync tasks work unchanged.
    result = _call_task(task, input_value)
    if inspect.isawaitable(result):
        return await result
    return result


def _dataset_example_from_payload(
    payload: Mapping[Any, Any],
    dataset_path: Path,
    line_number: int,
) -> DatasetExample:
    example_id = payload.get("id")
    if not isinstance(example_id, str) or not example_id:
        raise ValueError(f"Dataset {dataset_path} line {line_number} field 'id' must be a non-empty string")
    if "input" not in payload:
        raise ValueError(f"Dataset {dataset_path} line {line_number} is missing required field 'input'")
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise ValueError(f"Dataset {dataset_path} line {line_number} field 'metadata' must be an object")
    return DatasetExample(
        id=example_id,
        input=payload["input"],
        expected=payload.get("expected"),
        metadata={str(key): value for key, value in metadata.items()},
    )


def _write_experiment_result(
    experiment_file: TextIO,
    experiment_id: str,
    experiment_name: str,
    result: ExperimentExampleResult,
) -> None:
    record = {
        "experiment_id": experiment_id,
        "experiment_name": experiment_name,
        **result.to_dict(),
    }
    experiment_file.write(_json_line(record))


def _run_experiment_threaded(
    *,
    name: str,
    experiment_id: str,
    examples: list[DatasetExample],
    task: Callable[..., Any],
    evaluator_list: list[DeterministicEvaluator],
    output_path: Path,
    start_time: str,
    raise_on_error: bool,
    record_traces: bool,
    max_workers: int,
) -> ExperimentResult:
    def run_one(index: int, example: DatasetExample) -> tuple[int, ExperimentExampleResult, Exception | None]:
        if record_traces:
            result, error = _run_traced_example(
                experiment_id=experiment_id,
                experiment_name=name,
                example=example,
                task=task,
                evaluators=evaluator_list,
            )
            return index, result, error
        try:
            result = _run_example(example, task, evaluator_list)
            return index, result, None
        except Exception as exc:
            return index, _error_example_result(example, exc), exc

    results_by_index: dict[int, ExperimentExampleResult] = {}
    errors_by_index: dict[int, Exception] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(run_one, index, example) for index, example in enumerate(examples)]
        concurrent.futures.wait(futures)
    for future in futures:
        index, result, error = future.result()
        results_by_index[index] = result
        if error is not None:
            errors_by_index[index] = error

    ordered_results = [results_by_index[i] for i in range(len(examples))]
    end_time = _now()

    if raise_on_error and errors_by_index:
        first_error_index = min(errors_by_index)
        _persist_experiment(
            output_path=output_path,
            experiment_id=experiment_id,
            name=name,
            start_time=start_time,
            end_time=end_time,
            results=ordered_results[: first_error_index + 1],
        )
        raise errors_by_index[first_error_index]

    return _persist_experiment(
        output_path=output_path,
        experiment_id=experiment_id,
        name=name,
        start_time=start_time,
        end_time=end_time,
        results=ordered_results,
    )


def _persist_experiment(
    *,
    output_path: Path,
    experiment_id: str,
    name: str,
    start_time: str,
    end_time: str,
    results: list[ExperimentExampleResult],
) -> ExperimentResult:
    """Write ordered result rows and the summary, returning the experiment result.

    Used by :func:`run_experiment_async`, which collects results by dataset index
    and persists them in one pass once every example has finished.
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as experiment_file:
        for result in results:
            _write_experiment_result(experiment_file, experiment_id, name, result)
    experiment_result = _experiment_result(
        experiment_id=experiment_id,
        name=name,
        start_time=start_time,
        end_time=end_time,
        results=results,
        path=output_path,
    )
    _write_experiment_summary(_summary_path(output_path), _summary_from_result(experiment_result))
    return experiment_result


def _experiment_result(
    *,
    experiment_id: str,
    name: str,
    start_time: str,
    end_time: str,
    results: list[ExperimentExampleResult],
    path: Path,
) -> ExperimentResult:
    status = "error" if any(result.status == "error" for result in results) else "success"
    return ExperimentResult(
        id=experiment_id,
        name=name,
        start_time=start_time,
        end_time=end_time,
        status=status,
        results=results,
        path=str(path),
    )


def _summary_from_result(result: ExperimentResult) -> ExperimentSummary:
    return ExperimentSummary(
        schema_version=_EXPERIMENT_SCHEMA_VERSION,
        experiment_id=result.id,
        name=result.name,
        start_time=result.start_time,
        end_time=result.end_time,
        status=result.status,
        example_count=len(result.results),
        error_count=sum(1 for example_result in result.results if example_result.status == "error"),
        aggregate_scores=result.aggregate_scores,
        result_path=result.path or "",
    )


def _write_experiment_summary(path: Path, summary: ExperimentSummary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8")


def _summary_path(result_path: Path) -> Path:
    return result_path.with_suffix(".summary.json")


def _experiment_example_result_from_payload(
    payload: Mapping[Any, Any],
    experiment_path: Path,
    line_number: int,
) -> ExperimentExampleResult:
    status = _required_string(payload, "status", experiment_path, line_number)
    if status not in {"success", "error"}:
        raise ValueError(f"Experiment {experiment_path} line {line_number} field 'status' must be success or error")
    scores = payload.get("scores")
    if not isinstance(scores, list):
        raise ValueError(f"Experiment {experiment_path} line {line_number} field 'scores' must be a list")
    error = payload.get("error")
    if error is not None and not isinstance(error, str):
        raise ValueError(f"Experiment {experiment_path} line {line_number} field 'error' must be a string or null")
    trace_id = payload.get("trace_id")
    if trace_id is not None and (not isinstance(trace_id, str) or not trace_id):
        raise ValueError(f"Experiment {experiment_path} line {line_number} field 'trace_id' must be a non-empty string or null")
    for field_name in ("input", "expected", "output"):
        if field_name not in payload:
            raise ValueError(f"Experiment {experiment_path} line {line_number} is missing required field '{field_name}'")
    return ExperimentExampleResult(
        id=_required_string(payload, "id", experiment_path, line_number),
        example_id=_required_string(payload, "example_id", experiment_path, line_number),
        input=_safe_capture(payload["input"]),
        expected=_safe_capture(payload["expected"]),
        output=_safe_capture(payload["output"]),
        scores=[_eval_result_from_payload(score, experiment_path, line_number) for score in scores],
        start_time=_required_string(payload, "start_time", experiment_path, line_number),
        end_time=_required_string(payload, "end_time", experiment_path, line_number),
        status=status,
        error=_safe_error(RuntimeError(error)) if error is not None else None,
        trace_id=trace_id,
    )


def _eval_result_from_payload(
    payload: Any,
    experiment_path: Path,
    line_number: int,
) -> EvalResult:
    if not isinstance(payload, Mapping):
        raise ValueError(f"Experiment {experiment_path} line {line_number} score entries must be objects")
    name = payload.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"Experiment {experiment_path} line {line_number} score field 'name' must be a non-empty string")
    if "value" not in payload:
        raise ValueError(f"Experiment {experiment_path} line {line_number} score is missing required field 'value'")
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise ValueError(f"Experiment {experiment_path} line {line_number} score field 'metadata' must be an object")
    return EvalResult(name=name, value=payload["value"], metadata=dict(metadata))


def _experiment_summary_from_payload(payload: Mapping[Any, Any], summary_path: Path) -> ExperimentSummary:
    aggregate_scores = payload.get("aggregate_scores")
    if not isinstance(aggregate_scores, Mapping):
        raise ValueError(f"Experiment summary {summary_path} field 'aggregate_scores' must be an object")
    return ExperimentSummary(
        schema_version=_required_summary_string(payload, "schema_version", summary_path),
        experiment_id=_required_summary_string(payload, "experiment_id", summary_path),
        name=_required_summary_string(payload, "name", summary_path),
        start_time=_required_summary_string(payload, "start_time", summary_path),
        end_time=_required_summary_string(payload, "end_time", summary_path),
        status=_required_summary_string(payload, "status", summary_path),
        example_count=_required_summary_int(payload, "example_count", summary_path),
        error_count=_required_summary_int(payload, "error_count", summary_path),
        aggregate_scores={str(key): value for key, value in aggregate_scores.items()},
        result_path=_required_summary_string(payload, "result_path", summary_path),
    )


def _required_string(payload: Mapping[Any, Any], field_name: str, path: Path, line_number: int) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Experiment {path} line {line_number} field '{field_name}' must be a non-empty string")
    return value


def _required_summary_string(payload: Mapping[Any, Any], field_name: str, path: Path) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Experiment summary {path} field '{field_name}' must be a non-empty string")
    return value


def _required_summary_int(payload: Mapping[Any, Any], field_name: str, path: Path) -> int:
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Experiment summary {path} field '{field_name}' must be a non-negative integer")
    return value


def _default_experiment_path(name: str, experiment_id: str) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip("-") or "experiment"
    return Path(".bir") / "experiments" / f"{safe_name}-{experiment_id}.jsonl"


def _experiments_endpoint(server_url: str) -> str:
    normalized_url = server_url.rstrip("/")
    if not normalized_url:
        raise ValueError("bir experiment server_url must not be empty")
    return f"{normalized_url}/v1/experiments"


def _post_experiment(endpoint: str, experiment: Mapping[str, Any], *, timeout: float) -> SendExperimentResult:
    """Post the experiment once, raising :class:`_TransientSendError` for retryable failures.

    Network errors, timeouts, and HTTP 5xx are surfaced as ``_TransientSendError``
    so :func:`_send_with_retry` can retry them; HTTP 4xx and an invalid success
    body are permanent ``RuntimeError`` failures that propagate immediately.
    """

    payload = json.dumps(experiment, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        message = f"bir server rejected experiment with HTTP {exc.code}: {body}"
        if _is_retryable_status(exc.code):
            raise _TransientSendError(message, cause=exc) from exc
        raise RuntimeError(message) from exc
    except urllib.error.URLError as exc:
        raise _TransientSendError(f"bir could not send experiment to {endpoint}: {exc.reason}", cause=exc) from exc
    except TimeoutError as exc:
        # A socket read timeout surfaces as TimeoutError rather than URLError.
        raise _TransientSendError(f"bir could not send experiment to {endpoint}: {exc}", cause=exc) from exc

    if status < 200 or status >= 300:
        raise RuntimeError(f"bir server rejected experiment with HTTP {status}: {body}")
    return _send_experiment_result_from_response(body)


def _send_experiment_result_from_response(body: str) -> SendExperimentResult:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("bir server returned invalid experiment response JSON") from exc
    if not isinstance(payload, Mapping):
        raise RuntimeError("bir server returned invalid experiment response")
    accepted = payload.get("accepted")
    experiment_id = payload.get("id")
    if isinstance(accepted, bool) or not isinstance(accepted, int):
        raise RuntimeError("bir server experiment response field 'accepted' must be an integer")
    if not isinstance(experiment_id, str) or not experiment_id:
        raise RuntimeError("bir server experiment response field 'id' must be a non-empty string")
    return SendExperimentResult(accepted=accepted, experiment_id=experiment_id)


def _expected_value(configured_expected: Any, example_expected: Any, evaluator_name: str) -> Any:
    if configured_expected is _USE_EXAMPLE_EXPECTED:
        if example_expected is None:
            raise ValueError(f"{evaluator_name} requires an expected value")
        return example_expected
    return configured_expected


def _coerce_eval_result(evaluator_name: str, value: EvalResult | int | float | bool) -> EvalResult:
    if isinstance(value, EvalResult):
        return value
    if isinstance(value, bool):
        return EvalResult(name=evaluator_name, value=1.0 if value else 0.0)
    if isinstance(value, (int, float)):
        return EvalResult(name=evaluator_name, value=value)
    raise TypeError("custom evaluator must return EvalResult, bool, int, or float")


def _extract_cost_value(output: Any, field: str) -> Any:
    if not isinstance(output, Mapping):
        return None
    if field in output:
        return output[field]
    cost = output.get("cost")
    if isinstance(cost, Mapping) and field in cost:
        return cost[field]
    return None


def _parse_field_path(path: str) -> list[str | int]:
    if not isinstance(path, str) or not path:
        raise ValueError("field path must not be empty")

    parts: list[str | int] = []
    index = 0
    while index < len(path):
        if path[index] in ".[":
            raise ValueError(f"invalid field path {path!r}")

        name_start = index
        while index < len(path) and path[index] not in ".[":
            if path[index] == "]":
                raise ValueError(f"invalid field path {path!r}")
            index += 1
        name = path[name_start:index]
        if not name:
            raise ValueError(f"invalid field path {path!r}")
        parts.append(name)

        while index < len(path) and path[index] == "[":
            index += 1
            item_start = index
            while index < len(path) and path[index].isdigit():
                index += 1
            if item_start == index or index >= len(path) or path[index] != "]":
                raise ValueError(f"invalid field path {path!r}")
            parts.append(int(path[item_start:index]))
            index += 1

        if index == len(path):
            break
        if path[index] != ".":
            raise ValueError(f"invalid field path {path!r}")
        index += 1
        if index == len(path):
            raise ValueError(f"invalid field path {path!r}")

    return parts


def _resolve_field_path(output: Any, field_path: list[str | int]) -> _ResolvedField:
    current = output
    for part in field_path:
        if isinstance(part, str):
            if not isinstance(current, Mapping):
                return _ResolvedField(exists=False, reason="non_object")
            if part not in current:
                return _ResolvedField(exists=False, reason="missing_path")
            current = current[part]
            continue
        if not isinstance(current, list):
            return _ResolvedField(exists=False, reason="non_list")
        if part >= len(current):
            return _ResolvedField(exists=False, reason="index_out_of_range")
        current = current[part]
    return _ResolvedField(exists=True, value=current)


def _safe_mapping(value: Mapping[Any, Any]) -> dict[str, Any]:
    captured = _safe_capture({str(key): item for key, item in value.items()})
    if not isinstance(captured, dict):
        return {}
    return captured


def _validate_non_negative_number(value: Any, field: str) -> float:
    numeric_value = _validate_finite_number(value, field)
    if numeric_value < 0:
        raise ValueError(f"{field} must be non-negative")
    return numeric_value


def _validate_missing_score(missing_score: Any) -> str:
    if missing_score not in _MISSING_SCORE_POLICIES:
        valid = ", ".join(_MISSING_SCORE_POLICIES)
        raise ValueError(f"missing_score must be one of: {valid}")
    return missing_score


def _validate_score_tolerances(
    score_tolerances: Mapping[str, float] | None,
    shared_names: Any,
) -> dict[str, float]:
    """Validate per-evaluator tolerance overrides against the shared evaluators.

    Names must be non-empty strings naming a shared evaluator (present in both
    runs); unknown names raise so a typo or a tolerance for a non-comparable
    evaluator fails loudly rather than being silently ignored. Values reuse the
    non-negative finite check, rejecting booleans, negatives, NaN, and infinity.
    """

    if score_tolerances is None:
        return {}
    if not isinstance(score_tolerances, Mapping):
        raise TypeError("score_tolerances must be a mapping of evaluator name to tolerance")

    resolved: dict[str, float] = {}
    unknown: list[str] = []
    for name, value in score_tolerances.items():
        if not isinstance(name, str) or not name:
            raise ValueError("score_tolerances names must be non-empty strings")
        resolved[name] = _validate_non_negative_number(value, f"score_tolerances[{name!r}]")
        if name not in shared_names:
            unknown.append(name)
    if unknown:
        formatted = ", ".join(sorted(unknown))
        raise ValueError(
            f"score_tolerances names must be shared evaluators present in both experiments: {formatted}"
        )
    return resolved


def _validate_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an int")
    if value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _validate_finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be an int or float")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{field} must be finite")
    return float(value)


def _validate_evaluator_name(name: str) -> None:
    if not name:
        raise ValueError("evaluator name must not be empty")


def _word_tokens(text: str) -> set[str]:
    return set(_WORD_TOKEN_PATTERN.findall(text.casefold()))


def _answer_preview(answer: str) -> str:
    if len(answer) <= _CITATION_ANSWER_PREVIEW_LIMIT:
        return answer
    return answer[:_CITATION_ANSWER_PREVIEW_LIMIT] + "..."


_REPORT_CSS = (
    "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;"
    "margin:2rem;color:#1a1a1a;}"
    "h1{font-size:1.5rem;}"
    "h2{font-size:1.1rem;margin-top:2rem;}"
    "table{border-collapse:collapse;margin-top:0.5rem;}"
    "th,td{border:1px solid #d0d0d0;padding:0.35rem 0.6rem;text-align:left;"
    "font-size:0.9rem;vertical-align:top;}"
    "th{background:#f4f4f4;}"
    "table.meta th{width:8rem;}"
)


def _render_experiment_report_html(result: ExperimentResult) -> str:
    """Render ``result`` as a complete standalone HTML document.

    Every experiment-derived string is passed through :func:`html.escape`, so
    example ids, evaluator names, and error text cannot inject markup.
    """

    aggregate_scores = result.aggregate_scores
    example_count = len(result.results)
    error_count = sum(1 for example in result.results if example.status == "error")
    title = f"Experiment Report: {result.name}"

    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{_html_escape(title)}</title>",
        f"<style>{_REPORT_CSS}</style>",
        "</head>",
        "<body>",
        f"<h1>{_html_escape(title)}</h1>",
        '<table class="meta">',
    ]
    for label, value in (
        ("ID", result.id),
        ("Status", result.status),
        ("Examples", str(example_count)),
        ("Errors", str(error_count)),
        ("Start", result.start_time),
        ("End", result.end_time),
    ):
        parts.append(f"<tr><th>{_html_escape(label)}</th><td>{_html_escape(value)}</td></tr>")
    parts.append("</table>")

    parts.append("<h2>Evaluator aggregates</h2>")
    if aggregate_scores:
        parts.append("<table>")
        parts.append("<thead><tr><th>Evaluator</th><th>Mean</th></tr></thead>")
        parts.append("<tbody>")
        for name in sorted(aggregate_scores):
            mean = _format_report_score(aggregate_scores[name])
            parts.append(f"<tr><td>{_html_escape(name)}</td><td>{_html_escape(mean)}</td></tr>")
        parts.append("</tbody>")
        parts.append("</table>")
    else:
        parts.append("<p>No evaluator scores.</p>")

    parts.append("<h2>Examples</h2>")
    parts.append("<table>")
    parts.append("<thead><tr><th>Example</th><th>Status</th><th>Scores</th><th>Error</th></tr></thead>")
    parts.append("<tbody>")
    for example in result.results:
        scores = _format_report_example_scores(example.scores)
        error = example.error or "-"
        parts.append(
            "<tr>"
            f"<td>{_html_escape(example.example_id)}</td>"
            f"<td>{_html_escape(example.status)}</td>"
            f"<td>{_html_escape(scores)}</td>"
            f"<td>{_html_escape(error)}</td>"
            "</tr>"
        )
    parts.append("</tbody>")
    parts.append("</table>")
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts) + "\n"


def _render_experiment_report_markdown(result: ExperimentResult) -> str:
    """Render ``result`` as a self-contained Markdown document.

    Table cells escape the pipe separator and collapse newlines so example text
    cannot break the table structure.
    """

    aggregate_scores = result.aggregate_scores
    example_count = len(result.results)
    error_count = sum(1 for example in result.results if example.status == "error")

    lines: list[str] = [
        f"# Experiment Report: {_markdown_inline(result.name)}",
        "",
        f"- **ID:** {_markdown_inline(result.id)}",
        f"- **Status:** {_markdown_inline(result.status)}",
        f"- **Examples:** {example_count}",
        f"- **Errors:** {error_count}",
        f"- **Start:** {_markdown_inline(result.start_time)}",
        f"- **End:** {_markdown_inline(result.end_time)}",
        "",
        "## Evaluator aggregates",
        "",
    ]
    if aggregate_scores:
        lines.append("| Evaluator | Mean |")
        lines.append("| --- | --- |")
        for name in sorted(aggregate_scores):
            lines.append(f"| {_markdown_cell(name)} | {_format_report_score(aggregate_scores[name])} |")
    else:
        lines.append("No evaluator scores.")
    lines.append("")
    lines.append("## Examples")
    lines.append("")
    lines.append("| Example | Status | Scores | Error |")
    lines.append("| --- | --- | --- | --- |")
    for example in result.results:
        scores = _format_report_example_scores(example.scores)
        error = example.error or "-"
        lines.append(
            f"| {_markdown_cell(example.example_id)} | {_markdown_cell(example.status)} "
            f"| {_markdown_cell(scores)} | {_markdown_cell(error)} |"
        )
    lines.append("")
    return "\n".join(lines)


def _format_report_score(value: float) -> str:
    return f"{value:.2f}"


def _format_report_example_scores(scores: list[EvalResult]) -> str:
    if not scores:
        return "-"
    return " ".join(f"{score.name}={score.value:.2f}" for score in sorted(scores, key=lambda score: score.name))


def _collapse_newlines(text: str) -> str:
    return text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _markdown_inline(text: str) -> str:
    return _collapse_newlines(text)


def _markdown_cell(text: str) -> str:
    return _collapse_newlines(text).replace("|", "\\|")


def _json_line(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def _duration_ms(start_time: str, end_time: str) -> float:
    start = datetime.fromisoformat(start_time)
    end = datetime.fromisoformat(end_time)
    return (end - start).total_seconds() * 1000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
