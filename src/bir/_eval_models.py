"""Public evaluation data models behind :mod:`bir.evals`.

This dependency-bottom module owns the evaluation, dataset, and experiment
value objects.  It never imports :mod:`bir.evals`; the public module re-exports
these exact classes and higher-level evaluator, execution, persistence, and
reporting code depends on them.  Only safe capture flows downward into the core
SDK so model construction preserves Bir's redaction guarantees.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ._sdk import _safe_capture, _safe_metadata

_EXPERIMENT_SCHEMA_VERSION = "1.0"
_MISSING_SCORE_IGNORE = "ignore"
_MISSING_SCORE_REGRESS = "regress"

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


def _safe_mapping(value: Mapping[Any, Any]) -> dict[str, Any]:
    """Snapshot and redact a caller-supplied metadata mapping.

    An evaluator's metadata reaches here as whatever object the evaluator
    returned, and reading it runs that object's own code. Building a result must
    not decide whether the experiment run survives, so the snapshot goes through
    the same no-raise contract as every other capture; a value that could not be
    captured at all comes back as a marker string rather than a mapping.
    """

    captured = _safe_capture(_safe_metadata(value, field="metadata"))
    if not isinstance(captured, dict):
        return {}
    return captured


def _validate_finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be an int or float")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{field} must be finite")
    return float(value)


def _validate_evaluator_name(name: str) -> None:
    if not name:
        raise ValueError("evaluator name must not be empty")


def _json_line(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def _set_public_module(cls: type[Any]) -> None:
    """Keep public class and method identities anchored at ``bir.evals``."""

    cls.__module__ = "bir.evals"
    for member in cls.__dict__.values():
        function: Any
        if isinstance(member, (classmethod, staticmethod)):
            function = member.__func__
        elif isinstance(member, property):
            function = member.fget
        else:
            function = member
        # Dataclasses can attach helpers implemented in the stdlib itself (for
        # example ``__replace__`` on Python 3.14). Those functions are shared
        # across dataclasses, so changing their module would mutate process-wide
        # stdlib state. Only definitions generated in or owned by this module
        # need their historical public identity restored.
        if callable(function) and getattr(function, "__module__", None) == __name__:
            function.__module__ = "bir.evals"


for _public_model in (
    EvalResult,
    DeterministicEvaluator,
    EvaluationContext,
    DatasetExample,
    Dataset,
    ExperimentExampleResult,
    ExperimentResult,
    ExperimentDiff,
    ExperimentSummary,
    SendExperimentResult,
):
    _set_public_module(_public_model)

del _public_model
