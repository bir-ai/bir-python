"""Deterministic evaluation, dataset, and experiment helpers for Bir."""

from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

from ._sdk import _record_score_event, _safe_capture, _safe_error, _trace_context

_USE_EXAMPLE_EXPECTED = object()
_EXPERIMENT_SCHEMA_VERSION = "1.0"
_UNSUPPORTED_WORD_LIMIT = 20
_WORD_TOKEN_PATTERN = re.compile(r"\w+")
_CITATION_ANSWER_PREVIEW_LIMIT = 200
_DEFAULT_CITATION_PATTERN = r"\[[\w-]+\]"

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
    "retrieved_context_contains",
    "run_experiment",
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
    """Aggregate-score differences between two experiment runs."""

    deltas: dict[str, float]
    regressed: frozenset[str]
    improved: frozenset[str]
    unchanged: frozenset[str]
    baseline_only: frozenset[str]
    candidate_only: frozenset[str]
    tolerance: float

    @property
    def has_regressions(self) -> bool:
        """Return whether any shared evaluator regressed beyond tolerance."""

        return bool(self.regressed)

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic JSON-serializable representation of the diff."""

        return {
            "deltas": self.deltas,
            "regressed": sorted(self.regressed),
            "improved": sorted(self.improved),
            "unchanged": sorted(self.unchanged),
            "baseline_only": sorted(self.baseline_only),
            "candidate_only": sorted(self.candidate_only),
            "tolerance": self.tolerance,
            "has_regressions": self.has_regressions,
        }


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
) -> ExperimentResult:
    """Run a task over a dataset and persist per-example evaluator results."""

    if not name:
        raise ValueError("experiment name must not be empty")

    experiment_id = str(uuid4())
    examples = list(dataset.examples if isinstance(dataset, Dataset) else dataset)
    evaluator_list = list(evaluators)
    start_time = _now()
    results: list[ExperimentExampleResult] = []
    output_path = Path(path) if path is not None else _default_experiment_path(name, experiment_id)
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


def compare_experiments(
    baseline: ExperimentResult | str | Path,
    candidate: ExperimentResult | str | Path,
    *,
    tolerance: float = 0.0,
) -> ExperimentDiff:
    """Compare shared aggregate evaluator scores from two experiment runs.

    A shared evaluator regresses when ``candidate - baseline`` is strictly less
    than ``-tolerance``. Evaluators found in only one run are reported but are
    not treated as regressions because no aggregate score can be compared.
    """

    validated_tolerance = _validate_finite_number(tolerance, "tolerance")
    if validated_tolerance < 0:
        raise ValueError("tolerance must be non-negative")

    baseline_result = baseline if isinstance(baseline, ExperimentResult) else load_experiment(baseline)
    candidate_result = candidate if isinstance(candidate, ExperimentResult) else load_experiment(candidate)
    baseline_scores = baseline_result.aggregate_scores
    candidate_scores = candidate_result.aggregate_scores
    shared = baseline_scores.keys() & candidate_scores.keys()
    deltas = {name: candidate_scores[name] - baseline_scores[name] for name in sorted(shared)}

    regressed = frozenset(
        name
        for name, delta in deltas.items()
        if delta < -validated_tolerance
        and not math.isclose(delta, -validated_tolerance, rel_tol=1e-12, abs_tol=1e-12)
    )
    improved = frozenset(
        name
        for name, delta in deltas.items()
        if delta > validated_tolerance
        and not math.isclose(delta, validated_tolerance, rel_tol=1e-12, abs_tol=1e-12)
    )
    unchanged = frozenset(shared - regressed - improved)
    return ExperimentDiff(
        deltas=deltas,
        regressed=regressed,
        improved=improved,
        unchanged=unchanged,
        baseline_only=frozenset(baseline_scores.keys() - candidate_scores.keys()),
        candidate_only=frozenset(candidate_scores.keys() - baseline_scores.keys()),
        tolerance=validated_tolerance,
    )


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
) -> SendExperimentResult:
    """Send a persisted experiment and its summary to a Bir server."""

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
    return _post_experiment(_experiments_endpoint(server_url), payload, timeout=timeout)


def _run_example(
    example: DatasetExample,
    task: Callable[..., Any],
    evaluators: list[DeterministicEvaluator],
) -> ExperimentExampleResult:
    start_time = _now()
    output = _call_task(task, example.input)
    task_end_time = _now()
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
    trace = _trace_context(
        name=f"experiment.{experiment_name}.{example.id}",
        metadata={
            "kind": "experiment",
            "experiment_id": experiment_id,
            "experiment_name": experiment_name,
            "example_id": example.id,
        },
    )
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
        raise RuntimeError(f"bir server rejected experiment with HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"bir could not send experiment to {endpoint}: {exc.reason}") from exc

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


def _json_line(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def _duration_ms(start_time: str, end_time: str) -> float:
    start = datetime.fromisoformat(start_time)
    end = datetime.fromisoformat(end_time)
    return (end - start).total_seconds() * 1000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
