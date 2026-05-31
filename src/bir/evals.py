from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

from ._sdk import _safe_capture, _safe_error

_USE_EXAMPLE_EXPECTED = object()

__all__ = [
    "Dataset",
    "DatasetExample",
    "DeterministicEvaluator",
    "EvalResult",
    "ExperimentExampleResult",
    "ExperimentResult",
    "contains",
    "exact_match",
    "json_valid",
    "regex_match",
    "run_experiment",
]


@dataclass(frozen=True)
class EvalResult:
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
        object.__setattr__(self, "value", float(self.value))
        object.__setattr__(self, "metadata", _safe_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class DeterministicEvaluator:
    name: str
    _evaluate: Callable[[Any, Any], EvalResult]

    def evaluate(self, output: Any, *, expected: Any = None) -> EvalResult:
        return self._evaluate(output, expected)


@dataclass(frozen=True)
class DatasetExample:
    id: str
    input: Any
    expected: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("dataset example id must not be empty")

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
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
    examples: list[DatasetExample]

    @classmethod
    def from_jsonl(cls, path: str | Path) -> Dataset:
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

    def to_jsonl(self, path: str | Path) -> None:
        dataset_path = Path(path)
        dataset_path.parent.mkdir(parents=True, exist_ok=True)
        with dataset_path.open("w", encoding="utf-8") as dataset_file:
            for example in self.examples:
                dataset_file.write(_json_line(example.to_dict()))

    def __iter__(self) -> Iterator[DatasetExample]:
        return iter(self.examples)

    def __len__(self) -> int:
        return len(self.examples)


@dataclass(frozen=True)
class ExperimentExampleResult:
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

    @property
    def duration_ms(self) -> float:
        start = datetime.fromisoformat(self.start_time)
        end = datetime.fromisoformat(self.end_time)
        return (end - start).total_seconds() * 1000

    def to_dict(self) -> dict[str, Any]:
        return {
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


@dataclass(frozen=True)
class ExperimentResult:
    id: str
    name: str
    start_time: str
    end_time: str
    status: str
    results: list[ExperimentExampleResult]
    path: str | None

    @property
    def aggregate_scores(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        counts: dict[str, int] = {}
        for result in self.results:
            for score in result.scores:
                totals[score.name] = totals.get(score.name, 0.0) + score.value
                counts[score.name] = counts.get(score.name, 0) + 1
        return {name: totals[name] / counts[name] for name in sorted(totals)}

    def to_dict(self) -> dict[str, Any]:
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


def exact_match(expected: Any = _USE_EXAMPLE_EXPECTED, *, name: str = "exact_match") -> DeterministicEvaluator:
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


def run_experiment(
    name: str,
    *,
    dataset: Dataset | Iterable[DatasetExample],
    task: Callable[..., Any],
    evaluators: Iterable[DeterministicEvaluator],
    path: str | Path | None = None,
    raise_on_error: bool = True,
) -> ExperimentResult:
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
            try:
                result = _run_example(example, task, evaluator_list)
            except Exception as exc:
                result = _error_example_result(example, exc)
                results.append(result)
                _write_experiment_result(experiment_file, experiment_id, name, result)
                if raise_on_error:
                    raise
                continue
            results.append(result)
            _write_experiment_result(experiment_file, experiment_id, name, result)

    end_time = _now()
    status = "error" if any(result.status == "error" for result in results) else "success"
    return ExperimentResult(
        id=experiment_id,
        name=name,
        start_time=start_time,
        end_time=end_time,
        status=status,
        results=results,
        path=str(output_path),
    )


def _run_example(
    example: DatasetExample,
    task: Callable[..., Any],
    evaluators: list[DeterministicEvaluator],
) -> ExperimentExampleResult:
    start_time = _now()
    output = _call_task(task, example.input)
    scores = [evaluator.evaluate(output, expected=example.expected) for evaluator in evaluators]
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


def _default_experiment_path(name: str, experiment_id: str) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip("-") or "experiment"
    return Path(".bir") / "experiments" / f"{safe_name}-{experiment_id}.jsonl"


def _expected_value(configured_expected: Any, example_expected: Any, evaluator_name: str) -> Any:
    if configured_expected is _USE_EXAMPLE_EXPECTED:
        if example_expected is None:
            raise ValueError(f"{evaluator_name} requires an expected value")
        return example_expected
    return configured_expected


def _safe_mapping(value: Mapping[Any, Any]) -> dict[str, Any]:
    captured = _safe_capture({str(key): item for key, item in value.items()})
    if not isinstance(captured, dict):
        return {}
    return captured


def _json_line(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
