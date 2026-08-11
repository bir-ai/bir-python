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
    _FAILED_EXAMPLES_IGNORE,
    _FAILED_EXAMPLES_REGRESS,
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
    _logger,
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
_ExperimentResultWriter = _eval_persistence_helpers._ExperimentResultWriter
_finalize_experiment = _eval_persistence_helpers._finalize_experiment
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

# Failed-example policy vocabulary for compare_experiments(). ``regress`` is the
# default: an example whose task raised is scored by nobody, so it leaves the
# aggregate mean's denominator instead of lowering it, and a candidate that broke
# on half its dataset can report a higher mean than the baseline that answered
# every example badly. ``ignore`` restores the pre-0.4.0 behavior of deciding the
# gate on aggregate means alone.
_FAILED_EXAMPLES_POLICIES = (_FAILED_EXAMPLES_IGNORE, _FAILED_EXAMPLES_REGRESS)

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
    total_timeout: float | None = None,
) -> ExperimentResult:
    """Run a task over a dataset and persist per-example evaluator results.

    ``evaluators`` must not name the same evaluator twice. Every score is filed
    under its evaluator's name -- the aggregate mean, the report's rows, the
    gate's deltas -- so two sharing one name would be averaged together into a
    number no example was given. Because each factory defaults ``name`` to its
    own, the ordinary pairing ``field_equals("answer")`` beside
    ``field_equals("citation")`` collides; pass ``name=`` to tell them apart. A
    repeat raises before the run writes anything.

    When ``max_workers`` is greater than 1, examples run concurrently inside a
    :class:`concurrent.futures.ThreadPoolExecutor` with up to ``max_workers``
    threads. Results, JSONL rows, and summary aggregates are always written in
    dataset order regardless of completion order. Every other behavior matches
    the sequential path: ``raise_on_error`` persists through the first failing
    example in dataset order and re-raises that exception; ``record_traces``
    isolation is preserved because each worker thread inherits its own copy of
    the context-var state, so trace trees never bleed across examples.

    Each finished example's row is flushed to the operating system before the
    next one is written, so a run stopped without unwinding — ``SIGTERM`` from a
    pod eviction, ``docker stop``, or a cancelled CI job — leaves the rows for
    the examples that had completed, in dataset order. The ``.summary.json``
    sibling is written only when the run ends, so an interrupted run has result
    rows and no summary; :func:`load_experiment` reads those rows.

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

    ``total_timeout`` is an optional limit on the *run* (a positive, finite
    number of seconds; default ``None`` means unlimited). ``timeout`` bounds an
    example and does not bound the run: a task that outran its timeout keeps its
    worker until it returns, so a run against a backend that stopped answering
    takes as long as the tasks do however small ``timeout`` is. When
    ``total_timeout`` passes, the run starts no further examples and finalizes
    with the ones that ran -- the same shape ``raise_on_error`` already produces
    when it stops a run early, so ``example_count`` counts the rows the run
    produced rather than the dataset. The examples it did not reach are absent
    rather than recorded as failures: they did not fail, and calling them errors
    would make the experiment look worse than the code it measured. Stopping is
    reported on the ``bir`` logger, and honors ``raise_on_error``: ``True`` (the
    default) raises ``TimeoutError`` so a truncated run cannot pass unnoticed,
    ``False`` returns what ran.
    """

    if not name:
        raise ValueError("experiment name must not be empty")
    max_workers = _validate_positive_int(max_workers, "max_workers")
    timeout = None if timeout is None else _validate_positive_number(timeout, "timeout")
    total_timeout = None if total_timeout is None else _validate_positive_number(total_timeout, "total_timeout")

    experiment_id = str(uuid4())
    examples = list(dataset.examples if isinstance(dataset, Dataset) else dataset)
    evaluator_list = list(evaluators)
    _validate_distinct_evaluator_names(evaluator_list)
    start_time = _now()
    deadline = _RunDeadline(total_timeout)
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
            deadline=deadline,
        )

    results: list[ExperimentExampleResult] = []
    # One budget for the whole run, so the bound is on the run rather than on any
    # one example. Unused when ``timeout`` is None, where nothing is abandoned.
    budget = _LiveWorkerBudget(_MAX_LIVE_TIMED_WORKERS)

    with _ExperimentResultWriter(
        output_path,
        experiment_id=experiment_id,
        name=name,
        stop_after_error=raise_on_error,
    ) as writer:
        for example in examples:
            if deadline.expired():
                break
            result, error = _run_example_capturing_sync(
                experiment_id=experiment_id,
                experiment_name=name,
                example=example,
                task=task,
                evaluators=evaluator_list,
                record_traces=record_traces,
                timeout=timeout,
                budget=budget,
            )
            results.append(result)
            writer.write(result, failed=error is not None)
            if error is not None and raise_on_error:
                _finalize_experiment(
                    output_path=output_path,
                    experiment_id=experiment_id,
                    name=name,
                    start_time=start_time,
                    end_time=_now(),
                    results=results,
                )
                _report_live_timed_workers(name, budget.live, limit=_MAX_LIVE_TIMED_WORKERS)
                raise error

    _report_live_timed_workers(name, budget.live, limit=_MAX_LIVE_TIMED_WORKERS)
    experiment_result = _finalize_experiment(
        output_path=output_path,
        experiment_id=experiment_id,
        name=name,
        start_time=start_time,
        end_time=_now(),
        results=results,
    )
    _stop_or_return(name, deadline, ran=len(results), of=len(examples), raise_on_error=raise_on_error)
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
    total_timeout: float | None = None,
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
    the requirement that evaluator names be distinct, task input binding,
    redaction, ``raise_on_error`` semantics, ``total_timeout`` bounding the run
    rather than an example, and the persisted JSONL/summary schema are
    identical. Each example runs in its own
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

    Like :func:`run_experiment`, rows are flushed as the run proceeds, so an
    interrupted run keeps the leading examples that had finished. Because rows
    follow dataset order, an example that completes while an earlier one is still
    running is written only once that earlier one lands.

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

    total_timeout = None if total_timeout is None else _validate_positive_number(total_timeout, "total_timeout")

    experiment_id = str(uuid4())
    examples = list(dataset.examples if isinstance(dataset, Dataset) else dataset)
    evaluator_list = list(evaluators)
    _validate_distinct_evaluator_names(evaluator_list)
    start_time = _now()
    deadline = _RunDeadline(total_timeout)
    output_path = Path(path) if path is not None else _default_experiment_path(name, experiment_id)

    semaphore = asyncio.Semaphore(max_concurrency)
    results_by_index: dict[int, ExperimentExampleResult] = {}
    errors_by_index: dict[int, Exception] = {}
    next_unwritten_index = 0

    with _ExperimentResultWriter(
        output_path,
        experiment_id=experiment_id,
        name=name,
        stop_after_error=raise_on_error,
    ) as writer:

        async def run_one(index: int, example: DatasetExample) -> None:
            nonlocal next_unwritten_index
            async with semaphore:
                # Checked after the semaphore, so an example that waited its turn
                # is judged on when it would start rather than on when it was
                # scheduled. A cut example records nothing; the writer already
                # stops at the first gap, so the rows stay a contiguous prefix.
                if deadline.expired():
                    return
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
                # Rows are persisted in dataset order, so an example that finishes
                # early waits for the ones before it; this drains however much of
                # that order is now complete. It never awaits, so no other example
                # can interleave between reading the cursor and writing the row.
                while next_unwritten_index in results_by_index:
                    writer.write(
                        results_by_index[next_unwritten_index],
                        failed=next_unwritten_index in errors_by_index,
                    )
                    next_unwritten_index += 1

        tasks = [asyncio.create_task(run_one(index, example)) for index, example in enumerate(examples)]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            # Includes CancelledError: cancel and await the in-flight example tasks
            # so they clean up, then re-raise. The examples that had already
            # finished keep their rows; no summary is written, so nothing claims the
            # run completed.
            for pending in tasks:
                pending.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    ordered_results = []
    while len(ordered_results) in results_by_index:
        ordered_results.append(results_by_index[len(ordered_results)])
    end_time = _now()

    if raise_on_error and errors_by_index:
        first_error_index = min(errors_by_index)
        _finalize_experiment(
            output_path=output_path,
            experiment_id=experiment_id,
            name=name,
            start_time=start_time,
            end_time=end_time,
            results=ordered_results[: first_error_index + 1],
        )
        raise errors_by_index[first_error_index]

    experiment_result = _finalize_experiment(
        output_path=output_path,
        experiment_id=experiment_id,
        name=name,
        start_time=start_time,
        end_time=end_time,
        results=ordered_results,
    )
    _stop_or_return(name, deadline, ran=len(ordered_results), of=len(examples), raise_on_error=raise_on_error)
    return experiment_result


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
    failed_examples: str = _FAILED_EXAMPLES_REGRESS,
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

    ``failed_examples`` selects how examples that failed are treated. An
    aggregate score is a mean over the examples an evaluator actually scored, and
    a failed example carries no scores, so failures leave that denominator rather
    than lowering it: a candidate that broke on half its dataset reports a
    *higher* mean than a baseline that answered every example badly.
    ``"regress"`` (the default) fails the gate when the candidate failed a larger
    share of its examples than the baseline did, compared exactly and as a share
    so datasets of different sizes stay comparable. ``"ignore"`` reports the
    counts without failing on them, which is how the gate behaved before 0.4.0.
    Either way the diff records ``baseline_example_count``,
    ``baseline_error_count``, ``candidate_example_count``, and
    ``candidate_error_count``, and ``failed_example_regression`` states the
    comparison. A run that scored nothing at all -- an empty dataset, or every
    example failing -- has no shared evaluator to compare and is the
    ``missing_score`` policy's case rather than this one.

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
    validated_failed_examples = _validate_failed_examples(failed_examples)

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
        failed_examples=validated_failed_examples,
        baseline_example_count=baseline_result.example_count,
        baseline_error_count=baseline_result.error_count,
        candidate_example_count=candidate_result.example_count,
        candidate_error_count=candidate_result.error_count,
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


def _list_experiments_skipping_invalid(
    directory: str | Path,
    *,
    on_invalid: Callable[[ValueError], None],
) -> list[ExperimentSummary]:
    """List summaries, handing each unreadable one to ``on_invalid`` and skipping it.

    The public loader stays strict: refusing a directory it cannot fully read is
    the contract a program building on it relies on. A person running the CLI
    against a directory an interrupted write damaged needs the opposite — the
    experiments that are still intact, and a note about what could not be read —
    so the commands that only display experiments use this instead. It mirrors
    ``_load_traces_skipping_invalid`` on the trace side.
    """

    return _eval_persistence_helpers.list_experiments(directory, on_invalid=on_invalid)


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
    deadline: _RunDeadline,
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
    live_workers = 0
    with _ExperimentResultWriter(
        output_path,
        experiment_id=experiment_id,
        name=name,
        stop_after_error=raise_on_error,
    ) as writer:
        if timeout is None:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(run_one, index, example) for index, example in enumerate(examples)]
                # Waiting on futures in dataset order rather than on the whole set
                # keeps the rows in that order while still letting every worker run:
                # each finished example is written while the rest are still going,
                # so an interrupted run keeps the prefix it had completed.
                for future in futures:
                    if deadline.expired():
                        # Cancel what has not begun; the executor's own exit waits
                        # for what has, which cannot be stopped anyway.
                        for pending in futures:
                            pending.cancel()
                        break
                    index, result, error = future.result()
                    results_by_index[index] = result
                    if error is not None:
                        errors_by_index[index] = error
                    writer.write(result, failed=error is not None)
        else:
            live_workers = _collect_threaded_results_with_timeout(
                name=name,
                run_one=run_one,
                examples=examples,
                max_workers=max_workers,
                timeout=timeout,
                results_by_index=results_by_index,
                errors_by_index=errors_by_index,
                writer=writer,
                deadline=deadline,
            )

    # However the run ended, the rows it produced are its leading examples in
    # dataset order, which is what a run stopped early already looks like.
    ordered_results = [results_by_index[i] for i in range(len(results_by_index))]
    end_time = _now()

    if raise_on_error and errors_by_index:
        first_error_index = min(errors_by_index)
        _finalize_experiment(
            output_path=output_path,
            experiment_id=experiment_id,
            name=name,
            start_time=start_time,
            end_time=end_time,
            results=ordered_results[: first_error_index + 1],
        )
        _report_live_timed_workers(name, live_workers, limit=max_workers)
        raise errors_by_index[first_error_index]

    _report_live_timed_workers(name, live_workers, limit=max_workers)
    experiment_result = _finalize_experiment(
        output_path=output_path,
        experiment_id=experiment_id,
        name=name,
        start_time=start_time,
        end_time=end_time,
        results=ordered_results,
    )
    _stop_or_return(name, deadline, ran=len(ordered_results), of=len(examples), raise_on_error=raise_on_error)
    return experiment_result


def _collect_threaded_results_with_timeout(
    *,
    name: str,
    run_one: Callable[[int, DatasetExample], tuple[int, ExperimentExampleResult, Exception | None]],
    examples: list[DatasetExample],
    max_workers: int,
    timeout: float,
    results_by_index: dict[int, ExperimentExampleResult],
    errors_by_index: dict[int, Exception],
    writer: _ExperimentResultWriter,
    deadline: _RunDeadline,
) -> int:
    """Run examples on a thread pool, recording a timeout error per example.

    Every example is submitted up front, but each example's timeout clock starts
    only when its task actually begins running on a worker thread, so time spent
    queued behind other examples never counts against it. The worker records its
    start (a monotonic deadline anchor plus the wall-clock timestamp) and sets a
    started event as its first action; the collector, walking futures in dataset
    order, waits for that event and then allows the task ``timeout`` seconds
    measured from the recorded start. An example whose own runtime exceeds
    ``timeout`` is recorded as a failed example via the same
    :func:`_error_example_result` shape — stamped with the task's real start
    time so ``duration_ms`` reflects the wait — with the timeout exception
    stored so ``raise_on_error`` can re-raise it.

    Python cannot force a thread to stop, so a timed-out task keeps running and
    occupies its pool slot until it returns; a queued example may therefore wait
    for a free worker, but its own clock is not running while it waits. That wait
    is open-ended on purpose. Bounding it by the example's own ``timeout`` was
    tried and measured: two slow examples saturating a two-worker pool made four
    of ten queued fast examples be recorded as failures they would not have had,
    because a worker frees a moment after the bound expires. Refusing an example
    that would have passed is worse than a slow run, so the wait stays, and what
    changed is that a run which is waiting says so.

    The executor is shut down without waiting so the run finishes as soon as
    every example is resolved. Because the collector already walks dataset order,
    each resolved example's row goes straight to ``writer``, so a run stopped
    part-way keeps what it had finished. Returns how many tasks are still running
    when it does, which its results cannot show.
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
        reported_waiting = False
        for index, (future, example) in enumerate(zip(futures, examples)):
            if deadline.expired():
                for pending in futures:
                    pending.cancel()
                break
            if not started_events[index].wait(timeout=timeout):
                # Longer than one example's whole budget without a worker means
                # every one of them is holding a task that already timed out. The
                # run still waits, but it stops looking hung while it does.
                if not reported_waiting:
                    reported_waiting = True
                    _report_waiting_for_worker(name, max_workers)
                # Open-ended unless the run has a limit of its own, which is the
                # only thing that can bound this wait without refusing an example
                # that would have passed.
                if not started_events[index].wait(timeout=deadline.remaining()):
                    for pending in futures:
                        pending.cancel()
                    break
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
            writer.write(result, failed=error is not None)
        # A future that is neither finished nor cancelled is a task still running.
        return sum(1 for future in futures if not future.done())
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
    budget: _LiveWorkerBudget | None = None,
) -> tuple[ExperimentExampleResult, Exception | None]:
    """Run one example for the serial path, optionally bounded by ``timeout``.

    With ``timeout=None`` the example runs inline, exactly as before. Otherwise it
    runs on a dedicated single-worker executor so a timed-out (still-running)
    worker never blocks the next example; a worker that exceeds ``timeout`` is
    recorded as a failed example and the executor is abandoned without waiting.

    ``budget`` bounds how many of those abandoned workers may be alive at once.
    Taking a slot blocks while the bound is full, so an example can wait for a
    worker to return before its own task starts -- the same wait the concurrent
    path already has. Its timeout still starts when its task starts, so a
    slot it waited for costs it none of its time.
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
    budget = budget if budget is not None else _LiveWorkerBudget(_MAX_LIVE_TIMED_WORKERS)
    if not budget.acquire(timeout):
        exc = _no_worker_exc(timeout, _MAX_LIVE_TIMED_WORKERS)
        return _error_example_result(example, exc), exc
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(
            _releasing(budget, _capture_example),
            experiment_id=experiment_id,
            experiment_name=experiment_name,
            example=example,
            task=task,
            evaluators=evaluators,
            record_traces=record_traces,
        )
    except BaseException:
        # Nothing will run, so nothing will hand the slot back.
        budget.release()
        executor.shutdown(wait=False)
        raise
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        exc = _timeout_exc(timeout)
        return _error_example_result(example, exc), exc
    finally:
        executor.shutdown(wait=False)


# How many workers a serial timed run may have alive at once. Python cannot stop
# a thread, so a task that outran its timeout keeps running until it returns on
# its own; the serial path gave each example its own worker and abandoned it,
# which is one live thread per timed-out example and nothing bounding them. A
# dataset is the only limit, and a hung backend is exactly what the timeout is
# for, so the count grew with the dataset: 400 examples against a hanging task
# held 402 threads, 401 of them still running when the run returned.
#
# Sixteen because the number has two jobs. It has to be high enough that a run
# whose examples occasionally overrun never queues behind them -- sixteen
# outstanding is far past what an occasional slow example produces -- and low
# enough that a run whose examples all hang holds a fixed handful rather than one
# per example. It is not a knob: a caller who wants more concurrency has
# ``max_workers``, which bounds its own pool the same way.
_MAX_LIVE_TIMED_WORKERS = 16


class _LiveWorkerBudget:
    """A count of the timed workers alive at once, and a bounded wait for a slot.

    The wait is bounded by the example's own timeout rather than open-ended, and
    that is the decision this bound turns on. An open-ended wait bounds the
    threads but hands back the thing ``timeout`` exists to prevent: measured on
    400 examples against a task that sleeps 30 s, waiting for a slot took the run
    from 2.70 s to roughly 750 s, because sixteen tasks have to return before the
    seventeenth example can start. Waiting only as long as the example was
    allowed keeps the run bounded by ``examples * timeout`` -- 2 s for that same
    workload -- and keeps the threads bounded too. What it costs is that an
    example can be recorded as failed without having run, which
    :func:`_no_worker_exc` says in as many words rather than calling it a task
    timeout.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._live = 0
        self._free = threading.Condition()

    def acquire(self, timeout: float) -> bool:
        """Take a slot, waiting at most ``timeout``; False if none came free."""

        with self._free:
            if not self._free.wait_for(lambda: self._live < self._limit, timeout=timeout):
                return False
            self._live += 1
            return True

    def release(self) -> None:
        with self._free:
            self._live -= 1
            self._free.notify()

    @property
    def live(self) -> int:
        """Return how many workers are alive right now."""

        with self._free:
            return self._live


def _releasing(budget: _LiveWorkerBudget, work: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap ``work`` so its slot is returned when the thread finally leaves it.

    Released by the worker rather than by the caller, because the caller stops
    waiting at the timeout while the thread keeps running: the slot is occupied
    until the task returns, which is the thing being counted.
    """

    def run(*args: Any, **kwargs: Any) -> Any:
        try:
            return work(*args, **kwargs)
        finally:
            budget.release()

    return run


def _report_waiting_for_worker(name: str, max_workers: int) -> None:
    """Say that a run is waiting for a worker rather than appearing to hang.

    Emitted once per run, on the first example that waits longer than a whole
    timeout for a slot. By then every worker is holding a task that already timed
    out, so the run proceeds only when one of them returns -- which it cannot
    hurry, and which nothing else would have told anyone about.
    """

    _logger.warning(
        "bir experiment %r is waiting for a free worker: all %d are still running tasks from "
        "examples that already timed out, and Python cannot stop a thread. The run continues as "
        "they return.",
        name,
        max_workers,
    )


def _report_live_timed_workers(name: str, live: int, *, limit: int) -> None:
    """Say that a run left tasks running, since its results do not show it."""

    if not live:
        return
    _logger.warning(
        "bir experiment %r returned with %d task(s) from timed-out examples still running; Python "
        "cannot stop a thread, so they end when they return on their own. At most %d run at once.",
        name,
        live,
        limit,
    )


class _RunDeadline:
    """When a run must stop starting examples, or ``None`` for no limit.

    ``timeout`` bounds an example; this bounds the run. The two are not the same
    limit and neither implies the other: a run whose every worker is holding a
    task that already timed out waits for one to return, so per-example limits
    alone leave the run as long as the tasks are -- 60 examples against a 20 s
    task with a 5 ms timeout took 280 s. Bounding *that* by stretching the
    per-example limit was measured and rejected, because it refuses examples that
    would have passed; a run that wants to end on time has to say so.
    """

    def __init__(self, total_timeout: float | None) -> None:
        self.total_timeout = total_timeout
        self._ends_at = None if total_timeout is None else time.monotonic() + total_timeout

    def expired(self) -> bool:
        return self._ends_at is not None and time.monotonic() >= self._ends_at

    def remaining(self) -> float | None:
        """Seconds left, or ``None`` when the run has no limit of its own."""

        if self._ends_at is None:
            return None
        return max(self._ends_at - time.monotonic(), 0.0)


def _stop_or_return(name: str, deadline: _RunDeadline, *, ran: int, of: int, raise_on_error: bool) -> None:
    """Report a run that stopped on its own limit, and raise if asked to.

    Called after the results are finalized, so what ran is on disk either way --
    the same order ``raise_on_error`` already uses for a failing example.
    """

    if deadline.total_timeout is None or ran >= of:
        return
    _report_stopped_run(name, deadline.total_timeout, ran, of)
    if raise_on_error:
        raise _stopped_run_exc(deadline.total_timeout, ran, of)


def _stopped_run_exc(total_timeout: float, ran: int, of: int) -> TimeoutError:
    """Return the exception for a run that reached its own limit."""

    return TimeoutError(f"experiment stopped after {total_timeout}s with {ran} of {of} example(s) run")


def _report_stopped_run(name: str, total_timeout: float, ran: int, of: int) -> None:
    """Say that a run ended on its own limit rather than on its dataset."""

    _logger.warning(
        "bir experiment %r stopped after its total_timeout of %ss with %d of %d example(s) run; the "
        "results and summary cover what ran.",
        name,
        total_timeout,
        ran,
        of,
    )


def _timeout_exc(timeout: float) -> TimeoutError:
    """Return the exception used for a timed-out example's error result."""

    return TimeoutError(f"task timed out after {timeout}s")


def _no_worker_exc(timeout: float, limit: int) -> TimeoutError:
    """Return the exception for an example that never got a worker to run on.

    Distinct from :func:`_timeout_exc` because the task did not overrun -- it
    never started. The message says so, and says why: earlier examples timed out
    and their tasks are still running, because Python cannot stop a thread.
    """

    return TimeoutError(
        f"no worker was free within {timeout}s; {limit} task(s) from earlier timed-out examples "
        "are still running and cannot be stopped"
    )


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


def _validate_distinct_evaluator_names(evaluators: list[DeterministicEvaluator]) -> None:
    """Reject an evaluator list that names the same evaluator twice.

    Every score a run produces is filed under its evaluator's name and nothing
    else: the aggregate mean sums by name, the report prints one row per name,
    the gate's deltas are keyed by name, and its per-example detail keeps the
    last score written for a name. Two evaluators sharing one produce a mean over
    both -- a number no example was given by anything -- and a per-example delta
    for only one of them, in the same diff.

    Thirteen of the fourteen evaluator factories default ``name`` to the factory's
    own, so the collision arrives from the most ordinary pairing there is:
    ``field_equals("answer")`` beside ``field_equals("citation")``, or two
    ``regex_match`` patterns. Rejected here, where the list is built and where
    the keyword-only ``name=`` that fixes it is in reach, rather than reported
    later from a number that has already lost which evaluator it came from.

    Raised before the run touches its output file, so a rejected experiment
    cannot truncate a previous one.
    """

    seen: set[str] = set()
    for evaluator in evaluators:
        if evaluator.name in seen:
            raise ValueError(
                f"duplicate evaluator name {evaluator.name!r}: scores are aggregated by name, so every "
                "evaluator in one run must have a distinct one. Pass name= to override a factory's default."
            )
        seen.add(evaluator.name)


def _validate_failed_examples(failed_examples: Any) -> str:
    if failed_examples not in _FAILED_EXAMPLES_POLICIES:
        valid = ", ".join(_FAILED_EXAMPLES_POLICIES)
        raise ValueError(f"failed_examples must be one of: {valid}")
    return failed_examples


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
