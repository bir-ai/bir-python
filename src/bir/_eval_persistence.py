"""Experiment persistence, codecs, and upload transport.

Dependency direction is deliberately one-way: this module depends on the
evaluation value objects in :mod:`bir._eval_models` and a narrow set of shared
SDK safety, validation, and retry helpers.  It never imports :mod:`bir.evals`;
the public module owns orchestration and compatibility wrappers.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from ._eval_models import (
    _EXPERIMENT_SCHEMA_VERSION,
    EvalResult,
    ExperimentExampleResult,
    ExperimentResult,
    ExperimentSummary,
    SendExperimentResult,
    _json_line,
)
from ._sdk import (
    _is_retryable_status,
    _read_http_error_body,
    _safe_capture,
    _safe_error,
    _send_with_retry,
    _TransientSendError,
    _validate_non_negative_int,
    _validate_non_negative_number,
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

    timeout = float(_validate_non_negative_number(timeout, "timeout"))
    retries = _validate_non_negative_int(retries, "retries")
    backoff = float(_validate_non_negative_number(backoff, "backoff"))

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

    Used by the async evaluator runner, which collects results by dataset index
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
    path.write_text(
        json.dumps(summary.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8"
    )


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
        raise ValueError(
            f"Experiment {experiment_path} line {line_number} field 'trace_id' must be a non-empty string or null"
        )
    for field_name in ("input", "expected", "output"):
        if field_name not in payload:
            raise ValueError(
                f"Experiment {experiment_path} line {line_number} is missing required field '{field_name}'"
            )
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
        raise ValueError(
            f"Experiment {experiment_path} line {line_number} score field 'name' must be a non-empty string"
        )
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
        body = _read_http_error_body(exc)
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
