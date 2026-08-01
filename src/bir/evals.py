"""Public evaluators and experiment-execution orchestration for Bir.

Dependency direction: this compatibility surface composes private evaluation
models, persistence, and report rendering. Those focused modules never import
``bir.evals``; evaluator execution may call down into persistence after a run.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import difflib
import inspect
import json
import math
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import _eval_persistence as _eval_persistence_helpers
from . import _eval_reports as _eval_report_helpers
from ._eval_models import (
    _MISSING_SCORE_IGNORE,
    _MISSING_SCORE_REGRESS,
    Dataset,
    DatasetExample,
    DeterministicEvaluator,
    EvalResult,
    EvaluationContext,
    ExperimentDiff,
    ExperimentExampleResult,
    ExperimentResult,
    ExperimentSummary,
    SendExperimentResult,
    _validate_evaluator_name,
    _validate_finite_number,
)
from ._sdk import (
    _duration_ms,
    _now,
    _record_score_event,
    _safe_capture,
    _safe_error,
    _trace_context,
)

_USE_EXAMPLE_EXPECTED = object()
_UNSUPPORTED_WORD_LIMIT = 20
_WORD_TOKEN_PATTERN = re.compile(r"\w+")
_CITATION_ANSWER_PREVIEW_LIMIT = 200
_DEFAULT_CITATION_PATTERN = r"\[[\w-]+\]"

# Private aliases preserve established implementation seams while focused
# modules own persistence and presentation. Public functions below remain real
# ``bir.evals`` wrappers so their observable module identity is unchanged.
_EXPERIMENT_SCHEMA_VERSION = _eval_persistence_helpers._EXPERIMENT_SCHEMA_VERSION
_REPORT_FORMATS = _eval_report_helpers._REPORT_FORMATS
_REPORT_CSS = _eval_report_helpers._REPORT_CSS
_render_experiment_report_html = _eval_report_helpers._render_experiment_report_html
_render_experiment_report_markdown = _eval_report_helpers._render_experiment_report_markdown
_format_report_score = _eval_report_helpers._format_report_score
_format_report_example_scores = _eval_report_helpers._format_report_example_scores
_collapse_newlines = _eval_report_helpers._collapse_newlines
_markdown_inline = _eval_report_helpers._markdown_inline
_markdown_cell = _eval_report_helpers._markdown_cell

_write_experiment_result = _eval_persistence_helpers._write_experiment_result
_persist_experiment = _eval_persistence_helpers._persist_experiment
_experiment_result = _eval_persistence_helpers._experiment_result
_summary_from_result = _eval_persistence_helpers._summary_from_result
_write_experiment_summary = _eval_persistence_helpers._write_experiment_summary
_summary_path = _eval_persistence_helpers._summary_path
_experiment_example_result_from_payload = _eval_persistence_helpers._experiment_example_result_from_payload
_eval_result_from_payload = _eval_persistence_helpers._eval_result_from_payload
_experiment_summary_from_payload = _eval_persistence_helpers._experiment_summary_from_payload
_required_string = _eval_persistence_helpers._required_string
_required_summary_string = _eval_persistence_helpers._required_summary_string
_required_summary_int = _eval_persistence_helpers._required_summary_int
_default_experiment_path = _eval_persistence_helpers._default_experiment_path
_experiments_endpoint = _eval_persistence_helpers._experiments_endpoint
_post_experiment = _eval_persistence_helpers._post_experiment
_send_experiment_result_from_response = _eval_persistence_helpers._send_experiment_result_from_response

# Missing-score policy vocabulary for compare_experiments(). ``ignore`` keeps the
# historical behavior (baseline-only evaluators are reported but never fail the
# gate); ``regress`` treats a baseline-only evaluator as a regression because a
# removed evaluator silently drops coverage.
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
    "similarity_above",
]


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


def similarity_above(
    threshold: float,
    expected: str | object = _USE_EXAMPLE_EXPECTED,
    *,
    case_sensitive: bool = True,
    name: str = "similarity_above",
) -> DeterministicEvaluator:
    """Create an evaluator that scores 1.0 when output text is similar enough to expected.

    Similarity is the normalized :class:`difflib.SequenceMatcher` ratio between
    the output text and the expected text, a deterministic, dependency-free fuzzy
    check that sits between exact equality (:func:`exact_match`) and substring
    presence (:func:`contains`). It tolerates typos, reordering, and minor
    wording differences without an embedding model or any new dependency. The
    score is 1.0 when the achieved ratio is at or above ``threshold`` (the
    boundary is inclusive) and 0.0 otherwise. Pass ``case_sensitive=False`` to
    lowercase both sides before comparing. The achieved ratio and threshold are
    recorded in ``EvalResult.metadata`` so failures are inspectable.
    """

    threshold_value = _validate_finite_number(threshold, "threshold")
    if threshold_value < 0 or threshold_value > 1:
        raise ValueError("threshold must be between 0 and 1")

    def evaluate(output: Any, example_expected: Any) -> EvalResult:
        target = _expected_value(expected, example_expected, name)
        if not isinstance(target, str):
            raise TypeError("similarity_above expected value must be a string")
        output_text = "" if output is None else str(output)
        left = output_text if case_sensitive else output_text.lower()
        right = target if case_sensitive else target.lower()
        ratio = difflib.SequenceMatcher(None, left, right).ratio()
        return EvalResult(
            name=name,
            value=1.0 if ratio >= threshold_value else 0.0,
            metadata={"expected": target, "ratio": ratio, "threshold": threshold_value},
        )

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


def field_equals(
    path: str, expected: Any = _USE_EXAMPLE_EXPECTED, *, name: str = "field_equals"
) -> DeterministicEvaluator:
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

    max_duration = _validate_non_negative_float(max_ms, "max_ms")

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

    max_cost_value = _validate_non_negative_float(max_cost, "max_cost")
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
    timeout: float | None = None,
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

    ``timeout`` is an optional per-example limit in seconds (a positive, finite
    number; default ``None`` means unlimited). When set, an example whose task
    runs longer than ``timeout`` is recorded as an ``"error"``-status result with
    a ``"task timed out after Ns"`` message — the same shape as any other failed
    example, so ``raise_on_error`` is honored — and the run continues with the
    remaining examples. The limit applies to each example's own runtime, measured
    from the moment its task starts executing: with ``max_workers`` greater than
    1, an example queued behind others waiting for a free worker thread accrues
    no timeout while it waits (the serial ``max_workers=1`` path uses a dedicated
    single-worker executor per example, so its task starts immediately). Python
    cannot force a thread to stop, so a timed-out task keeps running in the
    background until it returns on its own; its result is discarded, and it
    occupies its worker slot until then, which can delay — but never time out —
    examples still waiting in the queue. ``timeout=None`` is byte-for-byte
    identical to the previous behavior.
    """

    if not name:
        raise ValueError("experiment name must not be empty")
    max_workers = _validate_positive_int(max_workers, "max_workers")
    timeout = None if timeout is None else _validate_positive_number(timeout, "timeout")

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
            timeout=timeout,
        )

    results: list[ExperimentExampleResult] = []
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as experiment_file:
        for example in examples:
            result, error = _run_example_capturing_sync(
                experiment_id=experiment_id,
                experiment_name=name,
                example=example,
                task=task,
                evaluators=evaluator_list,
                record_traces=record_traces,
                timeout=timeout,
            )
            results.append(result)
            _write_experiment_result(experiment_file, experiment_id, name, result)
            if error is not None and raise_on_error:
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
    timeout: float | None = None,
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

    ``timeout`` is an optional per-example limit in seconds (a positive, finite
    number; default ``None`` means unlimited). When set, each example coroutine
    is wrapped in :func:`asyncio.wait_for`; an example that runs longer than
    ``timeout`` has its task cancelled and awaited (so no pending-task warning
    leaks) and is recorded as an ``"error"``-status result with a ``"task timed
    out after Ns"`` message — the same shape as any other failed example, so
    ``raise_on_error`` is honored and dataset order is preserved. With
    ``record_traces=True``, a timed-out example's trace root is written with
    error status before the cancellation unwinds — its already-recorded child
    events stay attached and loadable — and the error result's ``trace_id``
    links that trace. ``timeout=None`` is byte-for-byte identical to the
    previous behavior.
    """

    if not name:
        raise ValueError("experiment name must not be empty")
    max_concurrency = _validate_positive_int(max_concurrency, "max_concurrency")
    timeout = None if timeout is None else _validate_positive_number(timeout, "timeout")

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
            if timeout is None:
                result, error = await _capture_example_async(
                    experiment_id=experiment_id,
                    experiment_name=name,
                    example=example,
                    task=task,
                    evaluators=evaluator_list,
                    record_traces=record_traces,
                )
            else:
                # The holder lets the traced runner publish its trace id before a
                # timeout cancellation unwinds it, so the error result below can
                # link the closed trace. wait_for awaits the cancelled inner task
                # before raising TimeoutError, so by then the holder is final and
                # the trace root (if any) has been written.
                trace_id_holder: list[str | None] = [None]
                try:
                    result, error = await asyncio.wait_for(
                        _capture_example_async(
                            experiment_id=experiment_id,
                            experiment_name=name,
                            example=example,
                            task=task,
                            evaluators=evaluator_list,
                            record_traces=record_traces,
                            trace_id_holder=trace_id_holder,
                        ),
                        timeout,
                    )
                except asyncio.TimeoutError:
                    exc = _timeout_exc(timeout)
                    result, error = _error_example_result(example, exc), exc
                    if trace_id_holder[0] is not None:
                        result = replace(result, trace_id=trace_id_holder[0])
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

    return _eval_persistence_helpers.load_experiment(path)


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

    return _eval_report_helpers.render_experiment_report(result, format=format)


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

    validated_tolerance = _validate_non_negative_float(tolerance, "tolerance")
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
        if delta < -evaluator_tolerance and not math.isclose(delta, -evaluator_tolerance, rel_tol=1e-12, abs_tol=1e-12):
            regressed_names.append(name)
            regression_reasons[name] = _REGRESSION_REASON_DELTA
        elif delta > evaluator_tolerance and not math.isclose(delta, evaluator_tolerance, rel_tol=1e-12, abs_tol=1e-12):
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

    return _eval_persistence_helpers.load_experiment_summary(path)


def list_experiments(directory: str | Path = Path(".bir") / "experiments") -> list[ExperimentSummary]:
    """List experiment summaries in newest-first order."""

    return _eval_persistence_helpers.list_experiments(directory)


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

    return _eval_persistence_helpers.send_experiment(
        path,
        server_url,
        timeout=timeout,
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


async def _capture_example_async(
    *,
    experiment_id: str,
    experiment_name: str,
    example: DatasetExample,
    task: Callable[..., Any],
    evaluators: list[DeterministicEvaluator],
    record_traces: bool,
    trace_id_holder: list[str | None] | None = None,
) -> tuple[ExperimentExampleResult, Exception | None]:
    """Async counterpart to :func:`_capture_example`.

    Returns the example result paired with any failure. A cancellation (including
    the one :func:`asyncio.wait_for` raises on timeout) is a ``BaseException`` and
    is intentionally not caught here, so it propagates to ``wait_for`` and surfaces
    as a recorded timeout in the caller. ``trace_id_holder`` is a one-element
    mutable cell the caller's timeout path supplies so the traced runner can
    publish its trace id (and close the trace root on cancellation) before that
    cancellation unwinds; the non-traced path never touches it.
    """

    if record_traces:
        return await _run_traced_example_async(
            experiment_id=experiment_id,
            experiment_name=experiment_name,
            example=example,
            task=task,
            evaluators=evaluators,
            trace_id_holder=trace_id_holder,
        )
    try:
        return await _run_example_async(example, task, evaluators), None
    except Exception as exc:
        return _error_example_result(example, exc), exc


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


def _error_example_result(
    example: DatasetExample,
    exc: Exception,
    *,
    start_time: str | None = None,
) -> ExperimentExampleResult:
    end_time = _now()
    return ExperimentExampleResult(
        id=str(uuid4()),
        example_id=example.id,
        input=_safe_capture(example.input),
        expected=_safe_capture(example.expected),
        output=None,
        scores=[],
        start_time=start_time if start_time is not None else end_time,
        end_time=end_time,
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
    trace_id_holder: list[str | None] | None = None,
) -> tuple[ExperimentExampleResult, Exception | None]:
    # ``asyncio.create_task`` copies the current context for each example, so the
    # trace contextvars set here stay isolated from concurrently running examples
    # and the task's nested observations attach to this trace tree only.
    trace = _experiment_trace_context(experiment_id, experiment_name, example)
    trace.__enter__()
    if trace.id is None:
        raise RuntimeError("bir experiment trace context did not provide a trace id")
    if trace_id_holder is not None:
        trace_id_holder[0] = trace.id

    try:
        result = await _run_example_async(example, task, evaluators)
        _record_experiment_scores(trace.id, result)
    except asyncio.CancelledError as exc:
        # A ``trace_id_holder`` marks the caller's ``asyncio.wait_for`` timeout
        # boundary, whose expiry cancels this coroutine. Cancellation is a
        # BaseException, so the ``except Exception`` below never sees it — without
        # this handler the trace root event would never be written and the
        # already-written child events would be orphaned (invisible to
        # ``load_traces``). Close the root with error status, then re-raise so
        # ``wait_for`` still observes the cancellation and raises TimeoutError.
        # Without a holder (``timeout=None``) cancellation propagates untouched,
        # exactly as before.
        if trace_id_holder is not None:
            trace.__exit__(type(exc), exc, exc.__traceback__)
        raise
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
    timeout: float | None,
) -> ExperimentResult:
    def run_one(index: int, example: DatasetExample) -> tuple[int, ExperimentExampleResult, Exception | None]:
        result, error = _capture_example(
            experiment_id=experiment_id,
            experiment_name=name,
            example=example,
            task=task,
            evaluators=evaluator_list,
            record_traces=record_traces,
        )
        return index, result, error

    results_by_index: dict[int, ExperimentExampleResult] = {}
    errors_by_index: dict[int, Exception] = {}
    if timeout is None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(run_one, index, example) for index, example in enumerate(examples)]
            concurrent.futures.wait(futures)
        for future in futures:
            index, result, error = future.result()
            results_by_index[index] = result
            if error is not None:
                errors_by_index[index] = error
    else:
        _collect_threaded_results_with_timeout(
            run_one=run_one,
            examples=examples,
            max_workers=max_workers,
            timeout=timeout,
            results_by_index=results_by_index,
            errors_by_index=errors_by_index,
        )

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


def _collect_threaded_results_with_timeout(
    *,
    run_one: Callable[[int, DatasetExample], tuple[int, ExperimentExampleResult, Exception | None]],
    examples: list[DatasetExample],
    max_workers: int,
    timeout: float,
    results_by_index: dict[int, ExperimentExampleResult],
    errors_by_index: dict[int, Exception],
) -> None:
    """Run examples on a thread pool, recording a timeout error per example.

    Every example is submitted up front, but each example's timeout clock starts
    only when its task actually begins running on a worker thread, so time spent
    queued behind other examples never counts against it. The worker records its
    start (a monotonic deadline anchor plus the wall-clock timestamp) and sets a
    started event as its first action; the collector, walking futures in dataset
    order, waits untimed for that event and then allows the task ``timeout``
    seconds measured from the recorded start. An example whose own runtime
    exceeds ``timeout`` is recorded as a failed example via the same
    :func:`_error_example_result` shape — stamped with the task's real start
    time so ``duration_ms`` reflects the wait — with the timeout exception
    stored so ``raise_on_error`` can re-raise it. Python cannot force a thread
    to stop, so a timed-out task keeps running and occupies its pool slot until
    it returns; a queued example may therefore wait for a free worker, but its
    own clock is not running while it waits. The executor is shut down without
    waiting so the run finishes as soon as every example is resolved.
    """

    started_events = [threading.Event() for _ in examples]
    started_monotonic = [0.0] * len(examples)
    started_wall = [""] * len(examples)

    def run_one_recording_start(
        index: int, example: DatasetExample
    ) -> tuple[int, ExperimentExampleResult, Exception | None]:
        started_monotonic[index] = time.monotonic()
        started_wall[index] = _now()
        started_events[index].set()
        return run_one(index, example)

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = [executor.submit(run_one_recording_start, index, example) for index, example in enumerate(examples)]
        for index, (future, example) in enumerate(zip(futures, examples)):
            started_events[index].wait()
            remaining = timeout - (time.monotonic() - started_monotonic[index])
            try:
                result_index, result, error = future.result(timeout=max(remaining, 0.0))
            except concurrent.futures.TimeoutError:
                exc = _timeout_exc(timeout)
                result = _error_example_result(example, exc, start_time=started_wall[index])
                result_index, error = index, exc
            results_by_index[result_index] = result
            if error is not None:
                errors_by_index[result_index] = error
    finally:
        executor.shutdown(wait=False)


def _capture_example(
    *,
    experiment_id: str,
    experiment_name: str,
    example: DatasetExample,
    task: Callable[..., Any],
    evaluators: list[DeterministicEvaluator],
    record_traces: bool,
) -> tuple[ExperimentExampleResult, Exception | None]:
    """Run one example and return its result paired with any failure.

    A traced example delegates to :func:`_run_traced_example`; an untraced one
    runs :func:`_run_example`, converting an exception into an error result. The
    returned ``error`` is non-``None`` only when the example failed, so callers
    can honor ``raise_on_error`` uniformly.
    """

    if record_traces:
        return _run_traced_example(
            experiment_id=experiment_id,
            experiment_name=experiment_name,
            example=example,
            task=task,
            evaluators=evaluators,
        )
    try:
        return _run_example(example, task, evaluators), None
    except Exception as exc:
        return _error_example_result(example, exc), exc


def _run_example_capturing_sync(
    *,
    experiment_id: str,
    experiment_name: str,
    example: DatasetExample,
    task: Callable[..., Any],
    evaluators: list[DeterministicEvaluator],
    record_traces: bool,
    timeout: float | None,
) -> tuple[ExperimentExampleResult, Exception | None]:
    """Run one example for the serial path, optionally bounded by ``timeout``.

    With ``timeout=None`` the example runs inline, exactly as before. Otherwise it
    runs on a dedicated single-worker executor so a timed-out (still-running)
    worker never blocks the next example; a worker that exceeds ``timeout`` is
    recorded as a failed example and the executor is abandoned without waiting.
    """

    if timeout is None:
        return _capture_example(
            experiment_id=experiment_id,
            experiment_name=experiment_name,
            example=example,
            task=task,
            evaluators=evaluators,
            record_traces=record_traces,
        )
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        _capture_example,
        experiment_id=experiment_id,
        experiment_name=experiment_name,
        example=example,
        task=task,
        evaluators=evaluators,
        record_traces=record_traces,
    )
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        exc = _timeout_exc(timeout)
        return _error_example_result(example, exc), exc
    finally:
        executor.shutdown(wait=False)


def _timeout_exc(timeout: float) -> TimeoutError:
    """Return the exception used for a timed-out example's error result."""

    return TimeoutError(f"task timed out after {timeout}s")


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


def _validate_non_negative_float(value: Any, field: str) -> float:
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
        resolved[name] = _validate_non_negative_float(value, f"score_tolerances[{name!r}]")
        if name not in shared_names:
            unknown.append(name)
    if unknown:
        formatted = ", ".join(sorted(unknown))
        raise ValueError(f"score_tolerances names must be shared evaluators present in both experiments: {formatted}")
    return resolved


def _validate_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an int")
    if value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _validate_positive_number(value: Any, field: str) -> float:
    numeric_value = _validate_finite_number(value, field)
    if numeric_value <= 0:
        raise ValueError(f"{field} must be positive")
    return numeric_value


def _word_tokens(text: str) -> set[str]:
    return set(_WORD_TOKEN_PATTERN.findall(text.casefold()))


def _answer_preview(answer: str) -> str:
    if len(answer) <= _CITATION_ANSWER_PREVIEW_LIMIT:
        return answer
    return answer[:_CITATION_ANSWER_PREVIEW_LIMIT] + "..."
