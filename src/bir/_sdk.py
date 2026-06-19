"""Core tracing primitives and local JSONL persistence for the Bir SDK."""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import math
import os
import random
import re
import time
import urllib.error
import urllib.request
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from types import TracebackType
from typing import Any, Callable, Iterable, Mapping, TypeVar, cast
from uuid import uuid4

F = TypeVar("F", bound=Callable[..., Any])
T = TypeVar("T")

_DEFAULT_TRACE_PATH = Path(".bir/traces.jsonl")
# Sidecar suffix appended to the trace file name to record the IDs the server has
# already accepted, so an opt-in ``send_events(mark_sent=True)`` can cheaply skip
# them on a later send. SDK-local bookkeeping only; never part of the event schema.
_SENT_IDS_SUFFIX = ".sent"
_SCHEMA_VERSION = "1.0"
_MAX_CAPTURE_DEPTH = 6
_MAX_DEPTH_REACHED = "[max_depth]"
_REDACTED = "[redacted]"
_EVENT_TYPES = {"trace", "span", "generation", "tool_call", "score"}
_EVENT_STATUSES = {"success", "error"}
_EVENT_SORT_PRIORITY = {
    "trace": 0,
    "span": 1,
    "generation": 1,
    "tool_call": 1,
    "score": 2,
}
_SECRET_KEY_PARTS = (
    "access_key",
    "api_key",
    "apikey",
    "authorization",
    "auth_header",
    "client_secret",
    "password",
    "private_key",
    "secret",
    "token",
)
_SECRET_KEY_NAMES = {
    "auth",
    "credential",
    "credentials",
    "creds",
}
_current_trace_id: ContextVar[str | None] = ContextVar("bir_current_trace_id", default=None)
_current_parent_id: ContextVar[str | None] = ContextVar("bir_current_parent_id", default=None)
_current_capture_inputs: ContextVar[bool | None] = ContextVar("bir_current_capture_inputs", default=None)
_current_capture_outputs: ContextVar[bool | None] = ContextVar("bir_current_capture_outputs", default=None)
# Set once at trace-root creation so every descendant event of a sampled-out
# trace is skipped. False means "keep this trace"; the default keeps everything.
_current_trace_dropped: ContextVar[bool] = ContextVar("bir_current_trace_dropped", default=False)


@dataclass(frozen=True)
class _Config:
    trace_path: Path = _DEFAULT_TRACE_PATH
    capture_inputs: bool = False
    capture_outputs: bool = False
    service_name: str | None = None
    environment: str | None = None
    sample_rate: float = 1.0
    # ``max_bytes is None`` keeps the historical behavior of a single trace file
    # that grows without bound. When set, the active file is rotated before a
    # write that would push it past the cap, keeping at most ``backup_count``
    # rotated siblings (``traces.jsonl.1`` .. ``traces.jsonl.N``).
    max_bytes: int | None = None
    backup_count: int = 3


@dataclass(frozen=True)
class TraceEvent:
    """A single trace, span, generation, tool call, or score loaded from storage."""

    id: str
    trace_id: str
    parent_id: str | None
    name: str
    type: str
    start_time: str
    end_time: str
    status: str
    metadata: dict[str, Any]
    input: Any
    output: Any
    error: str | None
    raw: dict[str, Any]
    value: int | float | None = None
    model: str | None = None
    usage: dict[str, int | float] | None = None
    cost: dict[str, int | float] | None = None
    currency: str | None = None

    @property
    def duration_ms(self) -> float:
        """Return the event duration in milliseconds."""

        return _duration_ms(self.start_time, self.end_time)


@dataclass(frozen=True)
class LoadedTrace:
    """A trace root event with all events that share its trace ID."""

    id: str
    name: str
    start_time: str
    end_time: str
    status: str
    events: list[TraceEvent]
    root: TraceEvent

    @property
    def duration_ms(self) -> float:
        """Return the root trace duration in milliseconds."""

        return self.root.duration_ms


@dataclass(frozen=True)
class SendEventsResult:
    """Result returned after sending local events to a Bir server."""

    accepted: int
    event_ids: list[str]
    attempted: int = 0

    @property
    def skipped(self) -> int:
        """Return events the server did not newly accept, usually duplicates."""

        return max(self.attempted - self.accepted, 0)


@dataclass(frozen=True)
class PromptRecord:
    """Prompt metadata attached to a generation event."""

    name: str
    version: str | None
    template: str | None
    variables: dict[str, Any]
    rendered: str | None
    metadata: dict[str, Any]
    capture_template: bool
    capture_variables: bool
    capture_rendered: bool

    def to_metadata(self) -> dict[str, Any]:
        """Return the redacted metadata representation stored on a generation event."""

        payload: dict[str, Any] = {"name": self.name}
        if self.version is not None:
            payload["version"] = self.version
        if self.template is not None:
            payload["template_sha256"] = hashlib.sha256(self.template.encode("utf-8")).hexdigest()
            if self.capture_template:
                payload["template"] = _safe_capture(self.template)
        if self.capture_variables:
            payload["variables"] = _safe_capture(self.variables)
        if self.capture_rendered:
            payload["rendered"] = _safe_capture(self.render())
        if self.metadata:
            payload["metadata"] = _safe_capture(self.metadata)
        return payload

    def render(self) -> str | None:
        """Render the prompt template with variables when no explicit rendered value exists."""

        if self.rendered is not None:
            return self.rendered
        if self.template is None:
            return None
        if not self.variables:
            return self.template
        return self.template.format(**self.variables)


# ``_config`` holds the active configuration. It is initialized from the BIR_*
# environment variables at import time by ``_config_from_env`` near the bottom of
# this module (defined there so the validators it reuses already exist) and is
# then replaced wholesale by ``configure``.
_write_lock = Lock()


def configure(
    *,
    trace_path: str | Path | None = None,
    capture_inputs: bool | None = None,
    capture_outputs: bool | None = None,
    service_name: str | None = None,
    environment: str | None = None,
    sample_rate: float | None = None,
    max_bytes: int | None = None,
    backup_count: int | None = None,
) -> None:
    """Configure local SDK behavior.

    ``service_name`` and ``environment`` are recorded on trace root events
    under ``metadata.service`` so traces can be filtered by deployment later.

    ``sample_rate`` is the probability (``0.0`` to ``1.0``) that a trace is
    recorded. It is decided once per trace root; when a trace is sampled out the
    function still runs and still raises, but the trace and every event under it
    write nothing. The default ``1.0`` records every trace.

    ``max_bytes`` enables opt-in size-based rotation of the local trace file. It
    defaults to ``None`` (unlimited), which keeps the historical single-file
    behavior. When set to a non-negative integer, the active file is rotated
    before any write that would push it past the cap: ``traces.jsonl`` becomes
    ``traces.jsonl.1``, the previous ``.1`` becomes ``.2``, and so on, keeping at
    most ``backup_count`` rotated files and dropping the oldest. Rotation always
    happens on whole-line boundaries, so every file stays valid JSONL and a JSON
    object is never split across files (a single line larger than ``max_bytes``
    is still written whole). ``backup_count`` defaults to ``3``; ``0`` keeps no
    rotated files and simply drops the active file when it fills.

    Note that a single logical trace may be split across rotated files when
    rotation happens mid-trace, so reading with ``include_rotated=True`` can
    surface incomplete traces near a rotation boundary.

    Any field left unset falls back to the value supplied by the matching
    environment variable (``BIR_TRACE_PATH``, ``BIR_CAPTURE_INPUTS``,
    ``BIR_CAPTURE_OUTPUTS``, ``BIR_SAMPLE_RATE``, ``BIR_SERVICE_NAME``,
    ``BIR_ENVIRONMENT``), which is read once at import time, and otherwise to the
    hardcoded default. Explicit arguments to this function take precedence over
    the environment.
    """

    global _config

    updates: dict[str, Any] = {}
    if trace_path is not None:
        updates["trace_path"] = Path(trace_path)
    if capture_inputs is not None:
        updates["capture_inputs"] = capture_inputs
    if capture_outputs is not None:
        updates["capture_outputs"] = capture_outputs
    if service_name is not None:
        updates["service_name"] = _validate_event_name(service_name, "service_name")
    if environment is not None:
        updates["environment"] = _validate_event_name(environment, "environment")
    if sample_rate is not None:
        updates["sample_rate"] = _validate_sample_rate(sample_rate)
    if max_bytes is not None:
        updates["max_bytes"] = _validate_non_negative_int(max_bytes, "max_bytes")
    if backup_count is not None:
        updates["backup_count"] = _validate_non_negative_int(backup_count, "backup_count")

    _config = replace(_config, **updates)


def load_events(path: str | Path | None = None, *, include_rotated: bool = False) -> list[TraceEvent]:
    """Load local JSONL trace events.

    By default only the active trace file is read. Pass ``include_rotated=True``
    to also read size-rotated siblings (``traces.jsonl.1`` ..) created by
    ``configure(max_bytes=...)``. Rotated files are read oldest-first so the
    returned events stay in the same chronological order they were written,
    matching how a single never-rotated file would read. Because rotation can
    occur mid-trace, a single logical trace may be split across files.
    """

    trace_path = Path(path) if path is not None else _config.trace_path
    if not include_rotated:
        return _load_events_from_file(trace_path)

    events: list[TraceEvent] = []
    for file_path in _trace_files_oldest_first(trace_path):
        events.extend(_load_events_from_file(file_path))
    return events


def _load_events_from_file(trace_path: Path) -> list[TraceEvent]:
    if not trace_path.exists():
        return []

    events: list[TraceEvent] = []
    with trace_path.open("r", encoding="utf-8") as trace_file:
        for line_number, line in enumerate(trace_file, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in trace file {trace_path} at line {line_number}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"Trace file {trace_path} line {line_number} must contain a JSON object")
            events.append(_trace_event_from_payload(payload, trace_path=trace_path, line_number=line_number))
    return events


def _trace_files_oldest_first(trace_path: Path) -> list[Path]:
    """Return rotated trace files then the active file, oldest event first.

    Rotated siblings are named ``<trace_path>.<n>`` where a higher ``n`` is
    older, so the original write order is reconstructed by reading the
    highest-numbered backup first, down to ``.1``, and the active file last.
    """

    rotated: list[tuple[int, Path]] = []
    prefix = f"{trace_path.name}."
    try:
        entries = list(trace_path.parent.iterdir())
    except FileNotFoundError:
        entries = []
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        suffix = entry.name[len(prefix):]
        if suffix.isdigit() and int(suffix) >= 1:
            rotated.append((int(suffix), entry))
    rotated.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in rotated] + [trace_path]


def load_traces(path: str | Path | None = None, *, include_rotated: bool = False) -> list[LoadedTrace]:
    """Load local traces grouped by trace_id.

    ``include_rotated`` is forwarded to :func:`load_events`; see its note about
    traces possibly being split across rotated files.
    """

    events = load_events(path, include_rotated=include_rotated)
    events_by_trace_id: dict[str, list[TraceEvent]] = {}
    for event in events:
        events_by_trace_id.setdefault(event.trace_id, []).append(event)

    traces: list[LoadedTrace] = []
    for trace_id, trace_events in events_by_trace_id.items():
        sorted_events = sorted(trace_events, key=_event_sort_key)
        root = next((event for event in sorted_events if event.type == "trace" and event.id == trace_id), None)
        if root is None:
            continue
        traces.append(
            LoadedTrace(
                id=trace_id,
                name=root.name,
                start_time=root.start_time,
                end_time=root.end_time,
                status=root.status,
                events=sorted_events,
                root=root,
            )
        )
    return sorted(traces, key=lambda trace: (trace.start_time, trace.id))


def send_events(
    server_url: str = "http://127.0.0.1:8000",
    *,
    path: str | Path | None = None,
    timeout: float = 10.0,
    retries: int = 2,
    backoff: float = 0.5,
    mark_sent: bool = False,
) -> SendEventsResult:
    """Send local JSONL trace events to a Bir ingestion server.

    Transient failures are retried with exponential backoff: a network error,
    timeout, or HTTP 5xx is retried up to ``retries`` times (default ``2``),
    sleeping ``backoff * 2**attempt`` seconds between tries (``backoff`` defaults to
    ``0.5``). A 4xx response is a permanent rejection and is raised immediately
    without retry, matching the un-retried behavior. A healthy send still makes a
    single attempt, so the default behavior is unchanged.

    ``mark_sent`` is opt-in bookkeeping for cheap re-sends. When ``True``, the IDs
    the server accepts are recorded in a sidecar file next to the trace file
    (``<trace_path>.sent``) and skipped on later sends, so ``attempted`` reflects
    only events not yet recorded as sent. The sidecar is SDK-local: it never
    modifies the trace JSONL or the event schema, and a missing or corrupt sidecar
    is treated as empty so it can never block a send. With the default
    ``mark_sent=False`` nothing is recorded and re-sending the whole file stays
    safe because the server is idempotent on event IDs.
    """

    retries = _validate_non_negative_int(retries, "retries")
    backoff = float(_validate_non_negative_number(backoff, "backoff"))

    events = _events_for_sending(path)
    sent_ids_path = _sent_ids_path(path) if mark_sent else None
    if sent_ids_path is not None:
        already_sent = _load_sent_ids(sent_ids_path)
        if already_sent:
            events = [event for event in events if event.id not in already_sent]

    endpoint = _events_endpoint(server_url)
    if not events:
        return SendEventsResult(accepted=0, event_ids=[], attempted=0)

    result = _post_loaded_events(events, endpoint, timeout=timeout, retries=retries, backoff=backoff)

    if sent_ids_path is not None and result.event_ids:
        _record_sent_ids(sent_ids_path, result.event_ids)
    return result


def _post_loaded_events(
    events: list[TraceEvent],
    endpoint: str,
    *,
    timeout: float,
    retries: int,
    backoff: float,
) -> SendEventsResult:
    """Post already-loaded events, batching first and falling back per-event.

    Both the batch and per-event posts go through :func:`_send_with_retry` so a
    transient failure on either path is retried before it surfaces.
    """

    batch_result = _send_with_retry(
        lambda: _post_event_batch(f"{endpoint}/batch", [event.raw for event in events], timeout=timeout),
        retries=retries,
        backoff=backoff,
    )
    if batch_result is not None:
        return batch_result

    accepted = 0
    event_ids: list[str] = []
    for event in events:
        event_accepted = _send_with_retry(
            lambda event=event: _post_event(endpoint, event.raw, timeout=timeout),
            retries=retries,
            backoff=backoff,
        )
        accepted += event_accepted
        if event_accepted:
            event_ids.append(event.id)

    return SendEventsResult(accepted=accepted, event_ids=event_ids, attempted=len(events))


def _send_with_retry(operation: Callable[[], T], *, retries: int, backoff: float) -> T:
    """Run ``operation`` and retry transient send failures with exponential backoff.

    A transient failure (network error, timeout, or HTTP 5xx) is raised by the
    callers as :class:`_TransientSendError` and retried up to ``retries`` times,
    sleeping ``backoff * 2**attempt`` seconds before each retry. Permanent failures
    (HTTP 4xx, raised as ``RuntimeError``) propagate immediately. When the retries
    are exhausted the failure is surfaced as ``RuntimeError`` so callers see the
    same exception type a single failed attempt raises.
    """

    attempt = 0
    while True:
        try:
            return operation()
        except _TransientSendError as exc:
            if attempt >= retries:
                raise RuntimeError(str(exc)) from exc.cause
            time.sleep(backoff * (2**attempt))
            attempt += 1


def _events_for_sending(path: str | Path | None = None) -> list[TraceEvent]:
    events = load_events(path)
    traces = load_traces(path)
    ordered_events: list[TraceEvent] = []
    ordered_event_ids: set[str] = set()

    for trace in traces:
        for event in trace.events:
            ordered_events.append(event)
            ordered_event_ids.add(event.id)

    ordered_events.extend(event for event in events if event.id not in ordered_event_ids)
    return ordered_events


def observe(
    name: str | None = None,
    *,
    capture_inputs: bool | None = None,
    capture_outputs: bool | None = None,
) -> Callable[[F], F]:
    """Decorate a sync or async function and record one trace event for each call."""

    if name is not None:
        _validate_event_name(name, "observe name")

    def decorator(func: F) -> F:
        trace_name = name or func.__name__
        signature = inspect.signature(func)

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                state = _begin_observe(capture_inputs, capture_outputs)
                input_payload = None
                try:
                    if state.capture_inputs:
                        input_payload = _capture_call_input(signature, args, kwargs)
                    result = await func(*args, **kwargs)
                except Exception as exc:
                    _finish_observe_error(state, trace_name, exc, input_payload)
                    raise
                _finish_observe_success(state, trace_name, input_payload, result)
                return result

            return cast(F, async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            state = _begin_observe(capture_inputs, capture_outputs)
            input_payload = None
            try:
                if state.capture_inputs:
                    input_payload = _capture_call_input(signature, args, kwargs)
                result = func(*args, **kwargs)
            except Exception as exc:
                _finish_observe_error(state, trace_name, exc, input_payload)
                raise
            _finish_observe_success(state, trace_name, input_payload, result)
            return result

        return cast(F, wrapper)

    return decorator


@dataclass(frozen=True)
class _ObserveState:
    """Per-call state shared by the sync and async ``observe`` wrappers."""

    event_id: str
    trace_id: str
    parent_id: str | None
    event_type: str
    start_time: str
    capture_inputs: bool
    capture_outputs: bool
    dropped: bool
    trace_token: Token[str | None]
    parent_token: Token[str | None]
    capture_inputs_token: Token[bool | None]
    capture_outputs_token: Token[bool | None]
    dropped_token: Token[bool]


def _begin_observe(capture_inputs: bool | None, capture_outputs: bool | None) -> _ObserveState:
    """Open an observation: choose trace-vs-span ids and bind the call contextvars.

    Both the sync and async wrappers call this so the trace decision and
    contextvar bookkeeping live in one place. ContextVars are task-local and each
    asyncio task runs with a copied context, so concurrent observed coroutines
    stay isolated. A new trace root also rolls the sampling decision once here so
    that every nested span inherits it instead of re-rolling.
    """

    active_trace_id = _current_trace_id.get()
    active_parent_id = _current_parent_id.get()
    event_id = _new_id()
    if active_trace_id is not None and active_parent_id is not None:
        trace_id = active_trace_id
        parent_id = active_parent_id
        event_type = "span"
        # Inherit the root's decision so a span never re-rolls sampling.
        dropped = _current_trace_dropped.get()
    else:
        trace_id = event_id
        parent_id = None
        event_type = "trace"
        dropped = _should_drop_trace()
    start_time = _now()
    capture_inputs_for_call = _should_capture(capture_inputs, "inputs")
    capture_outputs_for_call = _should_capture(capture_outputs, "outputs")
    return _ObserveState(
        event_id=event_id,
        trace_id=trace_id,
        parent_id=parent_id,
        event_type=event_type,
        start_time=start_time,
        capture_inputs=capture_inputs_for_call,
        capture_outputs=capture_outputs_for_call,
        dropped=dropped,
        trace_token=_current_trace_id.set(trace_id),
        parent_token=_current_parent_id.set(event_id),
        capture_inputs_token=_current_capture_inputs.set(capture_inputs_for_call),
        capture_outputs_token=_current_capture_outputs.set(capture_outputs_for_call),
        dropped_token=_current_trace_dropped.set(dropped),
    )


def _finish_observe_success(
    state: _ObserveState,
    trace_name: str,
    input_payload: Any,
    result: Any,
) -> None:
    """Reset the call contextvars and write the success event for an observation."""

    end_time = _now()
    _reset_context(
        state.trace_token,
        state.parent_token,
        state.capture_inputs_token,
        state.capture_outputs_token,
        state.dropped_token,
    )
    if state.dropped:
        return
    output_payload = _safe_capture(result) if state.capture_outputs else None
    _write_event(
        _event(
            event_id=state.event_id,
            trace_id=state.trace_id,
            parent_id=state.parent_id,
            name=trace_name,
            event_type=state.event_type,
            start_time=state.start_time,
            end_time=end_time,
            status="success",
            error=None,
            input=input_payload,
            output=output_payload,
        )
    )


def _finish_observe_error(
    state: _ObserveState,
    trace_name: str,
    exc: Exception,
    input_payload: Any,
) -> None:
    """Reset the call contextvars and write the error event for a failed observation.

    A storage failure re-raises the original ``exc`` chained to it so the user's
    exception is never silently swallowed by a write error.
    """

    end_time = _now()
    _reset_context(
        state.trace_token,
        state.parent_token,
        state.capture_inputs_token,
        state.capture_outputs_token,
        state.dropped_token,
    )
    if state.dropped:
        return
    event = _event(
        event_id=state.event_id,
        trace_id=state.trace_id,
        parent_id=state.parent_id,
        name=trace_name,
        event_type=state.event_type,
        start_time=state.start_time,
        end_time=end_time,
        status="error",
        error=_safe_error(exc),
        input=input_payload,
    )
    try:
        _write_event(event)
    except Exception as storage_error:
        raise exc from storage_error


def span(name: str) -> _Span:
    """Create a nested span inside the current trace."""

    _validate_event_name(name, "span name")
    return _Span(name)


def trace(name: str, *, metadata: Mapping[str, Any] | None = None) -> _TraceContext:
    """Create a trace root with a context manager."""

    return _trace_context(name=name, metadata=metadata)


def prompt(
    name: str,
    *,
    version: str | None = None,
    template: str | None = None,
    variables: Mapping[str, Any] | None = None,
    rendered: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    capture_template: bool = False,
    capture_variables: bool = False,
    capture_rendered: bool = False,
) -> PromptRecord:
    """Describe the prompt version used by a generation."""

    if not name:
        raise ValueError("bir prompt name must not be empty")
    if version is not None and not version:
        raise ValueError("bir prompt version must not be empty")
    if template is not None and not isinstance(template, str):
        raise TypeError("bir prompt template must be a string")
    if rendered is not None and not isinstance(rendered, str):
        raise TypeError("bir rendered prompt must be a string")

    return PromptRecord(
        name=name,
        version=version,
        template=template,
        variables=dict(variables or {}),
        rendered=rendered,
        metadata=dict(metadata or {}),
        capture_template=capture_template,
        capture_variables=capture_variables,
        capture_rendered=capture_rendered,
    )


def generation(
    name: str,
    *,
    model: str | None = None,
    input: Any = None,
    metadata: Mapping[str, Any] | None = None,
    prompt: PromptRecord | None = None,
    capture_input: bool | None = None,
    capture_output: bool | None = None,
) -> _Generation:
    """Create a generation event for an LLM call inside the current trace."""

    _validate_event_name(name, "generation name")
    return _Generation(
        name=name,
        model=model,
        input=input,
        metadata=metadata,
        prompt_record=prompt,
        capture_input=capture_input,
        capture_output=capture_output,
    )


def tool_call(
    name: str,
    *,
    input: Any = None,
    metadata: Mapping[str, Any] | None = None,
    capture_input: bool | None = None,
    capture_output: bool | None = None,
) -> _ToolCall:
    """Create a tool call event inside the current trace."""

    _validate_event_name(name, "tool_call name")
    return _ToolCall(
        name=name,
        input=input,
        metadata=metadata,
        capture_input=capture_input,
        capture_output=capture_output,
    )


def retrieval(
    name: str,
    *,
    query: Any,
    metadata: Mapping[str, Any] | None = None,
    capture_input: bool | None = None,
    capture_output: bool | None = None,
) -> _Retrieval:
    """Create a retrieval tool call using Bir's documented RAG event shape."""

    _validate_event_name(name, "retrieval name")
    return _Retrieval(
        name=name,
        query=query,
        metadata=metadata,
        capture_input=capture_input,
        capture_output=capture_output,
    )


def score(name: str, value: int | float, *, metadata: Mapping[str, Any] | None = None) -> None:
    """Attach a score event to the current trace.

    Optional ``metadata`` (for example an evaluator's reasoning or threshold) is
    redacted with the same rules as captured input/output and stored on the
    score event so it can be inspected in the dashboard later.
    """

    _validate_event_name(name, "score name")
    if metadata is not None and not isinstance(metadata, Mapping):
        raise TypeError("bir score metadata must be a mapping")
    trace_id = _current_trace_id.get()
    parent_id = _current_parent_id.get()
    if trace_id is None or parent_id is None:
        raise RuntimeError("bir.score() requires an active trace. Use it inside a @observe() function.")
    score_value = _validate_number(value, "score value")

    timestamp = _now()
    _write_event(
        _event(
            event_id=_new_id(),
            trace_id=trace_id,
            parent_id=parent_id,
            name=name,
            event_type="score",
            start_time=timestamp,
            end_time=timestamp,
            status="success",
            error=None,
            metadata=_safe_capture(dict(metadata or {})),
            value=score_value,
        )
    )


def _trace_context(
    *,
    name: str,
    metadata: Mapping[str, Any] | None = None,
) -> _TraceContext:
    return _TraceContext(name=name, metadata=metadata)


def _record_score_event(
    *,
    trace_id: str,
    parent_id: str,
    name: str,
    value: int | float,
    metadata: Mapping[str, Any] | None = None,
    timestamp: str | None = None,
) -> None:
    _validate_event_name(name, "score name")
    score_value = _validate_number(value, "score value")
    score_time = timestamp or _now()
    _write_event(
        _event(
            event_id=_new_id(),
            trace_id=trace_id,
            parent_id=parent_id,
            name=name,
            event_type="score",
            start_time=score_time,
            end_time=score_time,
            status="success",
            error=None,
            metadata=_safe_capture(dict(metadata or {})),
            value=score_value,
        )
    )


class _TraceContext:
    def __init__(self, *, name: str, metadata: Mapping[str, Any] | None) -> None:
        _validate_event_name(name, "trace name")
        self.name = name
        self.metadata = metadata
        self.id: str | None = None
        self.start_time: str | None = None
        self._dropped = False
        self._trace_token: Token[str | None] | None = None
        self._parent_token: Token[str | None] | None = None
        self._capture_inputs_token: Token[bool | None] | None = None
        self._capture_outputs_token: Token[bool | None] | None = None
        self._dropped_token: Token[bool] | None = None

    def __enter__(self) -> _TraceContext:
        self.id = _new_id()
        self.start_time = _now()
        self._dropped = _should_drop_trace()
        self._trace_token = _current_trace_id.set(self.id)
        self._parent_token = _current_parent_id.set(self.id)
        self._capture_inputs_token = _current_capture_inputs.set(_config.capture_inputs)
        self._capture_outputs_token = _current_capture_outputs.set(_config.capture_outputs)
        self._dropped_token = _current_trace_dropped.set(self._dropped)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc_type, traceback
        self._reset()

        if self.id is None or self.start_time is None:
            raise RuntimeError("bir trace context exited before it was entered")

        if self._dropped:
            return False

        event = _event(
            event_id=self.id,
            trace_id=self.id,
            parent_id=None,
            name=self.name,
            event_type="trace",
            start_time=self.start_time,
            end_time=_now(),
            status="error" if exc is not None else "success",
            error=_safe_error(exc) if exc is not None else None,
            metadata=_safe_capture(dict(self.metadata or {})),
        )
        try:
            _write_event(event)
        except Exception as storage_error:
            if exc is not None:
                raise exc from storage_error
            raise
        return False

    async def __aenter__(self) -> _TraceContext:
        # Delegate to the sync enter so one object works as both ``with trace(...)``
        # and ``async with trace(...)``. The trace and parent_id contextvars are set
        # here with no intervening await, so each asyncio task keeps its own values and
        # concurrent traces stay isolated.
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return self.__exit__(exc_type, exc, traceback)

    def _reset(self) -> None:
        if self._dropped_token is not None:
            _current_trace_dropped.reset(self._dropped_token)
        if self._capture_outputs_token is not None:
            _current_capture_outputs.reset(self._capture_outputs_token)
        if self._capture_inputs_token is not None:
            _current_capture_inputs.reset(self._capture_inputs_token)
        if self._parent_token is not None:
            _current_parent_id.reset(self._parent_token)
        if self._trace_token is not None:
            _current_trace_id.reset(self._trace_token)


class _Span:
    def __init__(self, name: str) -> None:
        self.name = name
        self.id: str | None = None
        self.trace_id: str | None = None
        self.parent_id: str | None = None
        self.start_time: str | None = None
        self._parent_token: Token[str | None] | None = None

    def __enter__(self) -> _Span:
        trace_id = _current_trace_id.get()
        parent_id = _current_parent_id.get()
        if trace_id is None or parent_id is None:
            raise RuntimeError("bir.span() requires an active trace. Use it inside a @observe() function.")

        self.id = _new_id()
        self.trace_id = trace_id
        self.parent_id = parent_id
        self.start_time = _now()
        self._parent_token = _current_parent_id.set(self.id)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if self._parent_token is not None:
            _current_parent_id.reset(self._parent_token)

        if self.id is None or self.trace_id is None or self.start_time is None:
            raise RuntimeError("bir.span() exited before it was entered")

        event = _event(
            event_id=self.id,
            trace_id=self.trace_id,
            parent_id=self.parent_id,
            name=self.name,
            event_type="span",
            start_time=self.start_time,
            end_time=_now(),
            status="error" if exc is not None else "success",
            error=_safe_error(exc) if exc is not None else None,
        )
        try:
            _write_event(event)
        except Exception as storage_error:
            if exc is not None:
                raise exc from storage_error
            raise
        return False

    async def __aenter__(self) -> _Span:
        # Delegate to the sync enter so one object works as both ``with span(...)``
        # and ``async with span(...)``. The parent_id contextvar is set here with no
        # intervening await, so each asyncio task keeps its own value and concurrent
        # spans stay isolated.
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return self.__exit__(exc_type, exc, traceback)


class _Generation:
    def __init__(
        self,
        *,
        name: str,
        model: str | None,
        input: Any,
        metadata: Mapping[str, Any] | None,
        prompt_record: PromptRecord | None,
        capture_input: bool | None,
        capture_output: bool | None,
    ) -> None:
        self.name = name
        self.model = model
        self.input = input
        self.metadata = metadata
        self.prompt_record = prompt_record
        self.capture_input = capture_input
        self.capture_output = capture_output
        self.id: str | None = None
        self.trace_id: str | None = None
        self.parent_id: str | None = None
        self.start_time: str | None = None
        self.output: Any = None
        self.usage: dict[str, int | float] | None = None
        self.cost: dict[str, int | float] | None = None
        self.currency: str | None = None
        self._parent_token: Token[str | None] | None = None

    def __enter__(self) -> _Generation:
        trace_id = _current_trace_id.get()
        parent_id = _current_parent_id.get()
        if trace_id is None or parent_id is None:
            raise RuntimeError("bir.generation() requires an active trace. Use it inside a @observe() function.")

        self.id = _new_id()
        self.trace_id = trace_id
        self.parent_id = parent_id
        self.start_time = _now()
        self._parent_token = _current_parent_id.set(self.id)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if self._parent_token is not None:
            _current_parent_id.reset(self._parent_token)

        if self.id is None or self.trace_id is None or self.start_time is None:
            raise RuntimeError("bir.generation() exited before it was entered")

        input_payload = _safe_capture(self.input) if _should_capture(self.capture_input, "inputs") else None
        output_payload = _safe_capture(self.output) if _should_capture(self.capture_output, "outputs") else None
        metadata_payload = dict(self.metadata or {})
        if self.prompt_record is not None:
            metadata_payload["prompt"] = self.prompt_record.to_metadata()
        event = _event(
            event_id=self.id,
            trace_id=self.trace_id,
            parent_id=self.parent_id,
            name=self.name,
            event_type="generation",
            start_time=self.start_time,
            end_time=_now(),
            status="error" if exc is not None else "success",
            error=_safe_error(exc) if exc is not None else None,
            metadata=_safe_capture(metadata_payload),
            input=input_payload,
            output=output_payload,
            model=self.model,
            usage=self.usage,
            cost=self.cost,
            currency=self.currency,
        )
        try:
            _write_event(event)
        except Exception as storage_error:
            if exc is not None:
                raise exc from storage_error
            raise
        return False

    async def __aenter__(self) -> _Generation:
        # Delegate to the sync enter so one object works as both ``with generation(...)``
        # and ``async with generation(...)``. The parent_id contextvar is set here with no
        # intervening await, so each asyncio task keeps its own value and concurrent
        # generations stay isolated.
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return self.__exit__(exc_type, exc, traceback)

    def set_output(self, output: Any) -> None:
        self.output = output

    def set_usage(
        self,
        *,
        input_tokens: int | float | None = None,
        output_tokens: int | float | None = None,
        total_tokens: int | float | None = None,
    ) -> None:
        if input_tokens is None and output_tokens is None and total_tokens is None:
            raise ValueError("bir usage requires at least one token field")

        usage: dict[str, int | float] = {}
        if input_tokens is not None:
            usage["input_tokens"] = _validate_non_negative_number(input_tokens, "input_tokens")
        if output_tokens is not None:
            usage["output_tokens"] = _validate_non_negative_number(output_tokens, "output_tokens")
        if total_tokens is not None:
            usage["total_tokens"] = _validate_non_negative_number(total_tokens, "total_tokens")
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        self.usage = usage

    def set_cost(
        self,
        *,
        input_cost: int | float | None = None,
        output_cost: int | float | None = None,
        total_cost: int | float | None = None,
        currency: str = "USD",
    ) -> None:
        if input_cost is None and output_cost is None and total_cost is None:
            raise ValueError("bir cost requires at least one cost field")

        cost: dict[str, int | float] = {}
        if input_cost is not None:
            cost["input_cost"] = _validate_non_negative_number(input_cost, "input_cost")
        if output_cost is not None:
            cost["output_cost"] = _validate_non_negative_number(output_cost, "output_cost")
        if total_cost is not None:
            cost["total_cost"] = _validate_non_negative_number(total_cost, "total_cost")
        if total_cost is None and input_cost is not None and output_cost is not None:
            cost["total_cost"] = cost["input_cost"] + cost["output_cost"]
        validated_currency = _validate_currency(currency)
        self.cost = cost
        self.currency = validated_currency


class _ToolCall:
    def __init__(
        self,
        *,
        name: str,
        input: Any,
        metadata: Mapping[str, Any] | None,
        capture_input: bool | None,
        capture_output: bool | None,
    ) -> None:
        self.name = name
        self.input = input
        self.metadata = metadata
        self.capture_input = capture_input
        self.capture_output = capture_output
        self.id: str | None = None
        self.trace_id: str | None = None
        self.parent_id: str | None = None
        self.start_time: str | None = None
        self.output: Any = None
        self._parent_token: Token[str | None] | None = None

    def __enter__(self) -> _ToolCall:
        trace_id = _current_trace_id.get()
        parent_id = _current_parent_id.get()
        if trace_id is None or parent_id is None:
            raise RuntimeError("bir.tool_call() requires an active trace. Use it inside a @observe() function.")

        self.id = _new_id()
        self.trace_id = trace_id
        self.parent_id = parent_id
        self.start_time = _now()
        self._parent_token = _current_parent_id.set(self.id)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if self._parent_token is not None:
            _current_parent_id.reset(self._parent_token)

        if self.id is None or self.trace_id is None or self.start_time is None:
            raise RuntimeError("bir.tool_call() exited before it was entered")

        input_payload = _safe_capture(self.input) if _should_capture(self.capture_input, "inputs") else None
        output_payload = _safe_capture(self.output) if _should_capture(self.capture_output, "outputs") else None
        event = _event(
            event_id=self.id,
            trace_id=self.trace_id,
            parent_id=self.parent_id,
            name=self.name,
            event_type="tool_call",
            start_time=self.start_time,
            end_time=_now(),
            status="error" if exc is not None else "success",
            error=_safe_error(exc) if exc is not None else None,
            metadata=_safe_capture(dict(self.metadata or {})),
            input=input_payload,
            output=output_payload,
        )
        try:
            _write_event(event)
        except Exception as storage_error:
            if exc is not None:
                raise exc from storage_error
            raise
        return False

    async def __aenter__(self) -> _ToolCall:
        # Delegate to the sync enter so one object works as both ``with tool_call(...)``
        # and ``async with tool_call(...)``. The parent_id contextvar is set here with no
        # intervening await, so each asyncio task keeps its own value and concurrent
        # tool calls stay isolated.
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return self.__exit__(exc_type, exc, traceback)

    def set_output(self, output: Any) -> None:
        self.output = output


class _Retrieval(_ToolCall):
    def __init__(
        self,
        *,
        name: str,
        query: Any,
        metadata: Mapping[str, Any] | None,
        capture_input: bool | None,
        capture_output: bool | None,
    ) -> None:
        retrieval_metadata = dict(metadata or {})
        retrieval_metadata["kind"] = "retrieval"
        super().__init__(
            name=name,
            input={"query": query},
            metadata=retrieval_metadata,
            capture_input=capture_input,
            capture_output=capture_output,
        )
        self.output = {"documents": []}

    def __enter__(self) -> _Retrieval:
        super().__enter__()
        return self

    async def __aenter__(self) -> _Retrieval:
        # Override the inherited tool-call ``__aenter__`` only to keep the static
        # return type ``_Retrieval``; the sync delegation it wraps is unchanged.
        await super().__aenter__()
        return self

    def add_document(
        self,
        *,
        id: str | None = None,
        text: str | None = None,
        rank: int | None = None,
        score: int | float | None = None,
        source: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        document: dict[str, Any] = {}
        if id is not None:
            document["id"] = id
        if rank is not None:
            document["rank"] = _validate_non_negative_int(rank, "retrieval document rank")
        if score is not None:
            document["score"] = _validate_non_negative_number(score, "retrieval document score")
        if source is not None:
            document["source"] = source
        if text is not None:
            document["text"] = text
        if metadata is not None:
            document["metadata"] = dict(metadata)
        self.output["documents"].append(document)

    def set_documents(self, documents: Iterable[Mapping[str, Any]]) -> None:
        self.output = {"documents": [_retrieval_document_from_mapping(document) for document in documents]}


def _event(
    *,
    event_id: str,
    trace_id: str,
    parent_id: str | None,
    name: str,
    event_type: str,
    start_time: str,
    end_time: str,
    status: str,
    error: str | None,
    metadata: Mapping[str, Any] | None = None,
    input: Any = None,
    output: Any = None,
    value: int | float | None = None,
    model: str | None = None,
    usage: Mapping[str, int | float] | None = None,
    cost: Mapping[str, int | float] | None = None,
    currency: str | None = None,
) -> dict[str, Any]:
    event_metadata = dict(metadata or {})
    if event_type == "trace":
        service_metadata = _service_metadata()
        if service_metadata is not None:
            event_metadata.setdefault("service", service_metadata)
    event: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "id": event_id,
        "trace_id": trace_id,
        "parent_id": parent_id,
        "name": name,
        "type": event_type,
        "start_time": start_time,
        "end_time": end_time,
        "status": status,
        "metadata": event_metadata,
        "input": input,
        "output": output,
        "error": error,
    }
    if value is not None:
        event["value"] = value
    if model is not None:
        event["model"] = model
    if usage is not None:
        event["usage"] = dict(usage)
    if cost is not None:
        event["cost"] = dict(cost)
    if currency is not None:
        event["currency"] = currency
    return event


def _service_metadata() -> dict[str, str] | None:
    payload: dict[str, str] = {}
    if _config.service_name is not None:
        payload["name"] = _config.service_name
    if _config.environment is not None:
        payload["environment"] = _config.environment
    return payload or None


def _write_event(event: dict[str, Any]) -> None:
    # Child events (spans, generations, tool calls, scores) run while the root's
    # contextvar is still active, so dropping them is centralized here. Trace
    # roots reset their contextvars before writing, so they check their own
    # stored decision instead and never reach this guard while dropped.
    if _current_trace_dropped.get():
        return
    payload = json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    with _write_lock:
        trace_path = _config.trace_path
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_trace_file_if_needed(trace_path, payload)
        with trace_path.open("a", encoding="utf-8") as trace_file:
            trace_file.write(payload)


def _rotate_trace_file_if_needed(trace_path: Path, payload: str) -> None:
    """Rotate the active trace file before a write that would exceed ``max_bytes``.

    A no-op unless ``max_bytes`` is configured. Rotation is decided on the
    already-complete active file (every prior write ended on a newline), so files
    only ever break on whole-line boundaries. The incoming line is never split:
    when an empty active file is about to receive a line larger than the cap, the
    line is still written whole rather than rotated away. Must be called while
    holding ``_write_lock``.
    """

    max_bytes = _config.max_bytes
    if max_bytes is None:
        return
    try:
        current_size = trace_path.stat().st_size
    except FileNotFoundError:
        return
    if current_size == 0:
        return
    if current_size + len(payload.encode("utf-8")) <= max_bytes:
        return
    _rotate_trace_files(trace_path, _config.backup_count)


def _rotate_trace_files(trace_path: Path, backup_count: int) -> None:
    """Shift ``traces.jsonl`` -> ``.1`` -> ``.2`` .., dropping the oldest.

    ``Path.replace`` overwrites its destination atomically, so shifting ``.k``
    onto ``.k+1`` discards the previous oldest backup and keeps at most
    ``backup_count`` rotated files. ``backup_count == 0`` keeps none and just
    drops the filled active file.
    """

    if backup_count <= 0:
        trace_path.unlink(missing_ok=True)
        return
    for index in range(backup_count - 1, 0, -1):
        source = trace_path.with_name(f"{trace_path.name}.{index}")
        if source.exists():
            source.replace(trace_path.with_name(f"{trace_path.name}.{index + 1}"))
    trace_path.replace(trace_path.with_name(f"{trace_path.name}.1"))


def _events_endpoint(server_url: str) -> str:
    normalized_url = server_url.rstrip("/")
    if not normalized_url:
        raise ValueError("bir server_url must not be empty")
    return f"{normalized_url}/v1/events"


class _TransientSendError(Exception):
    """Internal signal that a send attempt failed transiently and may be retried.

    Carries the original cause so :func:`_send_with_retry` can chain it when the
    retries are exhausted and the failure is re-raised as a ``RuntimeError``.
    """

    def __init__(self, message: str, *, cause: BaseException) -> None:
        super().__init__(message)
        self.cause = cause


def _is_retryable_status(status: int) -> bool:
    """Return True for HTTP 5xx, the only status codes worth retrying."""

    return 500 <= status < 600


def _post_event_batch(
    endpoint: str,
    events: list[dict[str, Any]],
    *,
    timeout: float,
) -> SendEventsResult | None:
    """Post all events in one request; return None when the server has no batch endpoint."""

    payload = json.dumps(events, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
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
        if exc.code == 404:
            return None
        body = exc.read().decode("utf-8", errors="replace")
        message = f"bir server rejected event batch with HTTP {exc.code}: {body}"
        if _is_retryable_status(exc.code):
            raise _TransientSendError(message, cause=exc) from exc
        raise RuntimeError(message) from exc
    except urllib.error.URLError as exc:
        raise _TransientSendError(f"bir could not send events to {endpoint}: {exc.reason}", cause=exc) from exc
    except TimeoutError as exc:
        # A socket read timeout surfaces as TimeoutError rather than URLError.
        raise _TransientSendError(f"bir could not send events to {endpoint}: {exc}", cause=exc) from exc

    if status < 200 or status >= 300:
        raise RuntimeError(f"bir server rejected event batch with HTTP {status}: {body}")
    return _batch_result_from_response(body, attempted=len(events))


def _batch_result_from_response(body: str, *, attempted: int) -> SendEventsResult:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"bir server returned an invalid batch response: {body}") from exc
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"bir server returned an invalid batch response: {body}")
    accepted = payload.get("accepted")
    event_ids = payload.get("event_ids")
    if isinstance(accepted, bool) or not isinstance(accepted, int):
        raise RuntimeError(f"bir server returned an invalid batch response: {body}")
    if not isinstance(event_ids, list) or not all(isinstance(event_id, str) for event_id in event_ids):
        raise RuntimeError(f"bir server returned an invalid batch response: {body}")
    return SendEventsResult(accepted=accepted, event_ids=list(event_ids), attempted=attempted)


def _post_event(endpoint: str, event: Mapping[str, Any], *, timeout: float) -> int:
    payload = json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
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
        message = f"bir server rejected event with HTTP {exc.code}: {body}"
        if _is_retryable_status(exc.code):
            raise _TransientSendError(message, cause=exc) from exc
        raise RuntimeError(message) from exc
    except urllib.error.URLError as exc:
        raise _TransientSendError(f"bir could not send event to {endpoint}: {exc.reason}", cause=exc) from exc
    except TimeoutError as exc:
        # A socket read timeout surfaces as TimeoutError rather than URLError.
        raise _TransientSendError(f"bir could not send event to {endpoint}: {exc}", cause=exc) from exc

    if status < 200 or status >= 300:
        raise RuntimeError(f"bir server rejected event with HTTP {status}: {body}")
    return _accepted_count_from_response(body)


def _accepted_count_from_response(body: str) -> int:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return 1
    if not isinstance(payload, Mapping):
        return 1
    accepted = payload.get("accepted")
    if isinstance(accepted, int) and not isinstance(accepted, bool):
        return accepted
    return 1


def _sent_ids_path(path: str | Path | None) -> Path:
    """Return the sidecar path that records IDs the server has already accepted.

    The sidecar lives next to the trace file being sent (``<trace_path>.sent``).
    ``path`` is resolved the same way :func:`load_events` resolves it so a custom
    ``send_events(path=...)`` records against the matching file. The ``.sent``
    suffix is non-numeric, so size-based rotation (which only shifts ``.1`` ..
    ``.N`` siblings) never touches it.
    """

    trace_path = Path(path) if path is not None else _config.trace_path
    return trace_path.with_name(trace_path.name + _SENT_IDS_SUFFIX)


def _load_sent_ids(sent_ids_path: Path) -> set[str]:
    """Load the set of already-sent event IDs from the sidecar.

    Bookkeeping must never block a send, so a missing, unreadable, or malformed
    sidecar is treated as empty: the worst case is re-sending events the
    idempotent server already has, exactly as a send without ``mark_sent`` would.
    """

    try:
        raw = sent_ids_path.read_text(encoding="utf-8")
    except OSError:
        return set()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if not isinstance(payload, Mapping):
        return set()
    event_ids = payload.get("event_ids")
    if not isinstance(event_ids, list):
        return set()
    return {event_id for event_id in event_ids if isinstance(event_id, str)}


def _record_sent_ids(sent_ids_path: Path, event_ids: list[str]) -> None:
    """Merge ``event_ids`` into the sidecar of already-sent IDs.

    Writes the union of the previously recorded IDs and the newly accepted ones
    through a temp-file replace so a crash mid-write cannot corrupt the sidecar.
    Only ever touches ``<trace_path>.sent`` — the trace JSONL is never modified.
    """

    merged = _load_sent_ids(sent_ids_path)
    merged.update(event_ids)
    sent_ids_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"event_ids": sorted(merged)}, separators=(",", ":")) + "\n"
    temp_path = sent_ids_path.with_name(sent_ids_path.name + ".tmp")
    temp_path.write_text(payload, encoding="utf-8")
    temp_path.replace(sent_ids_path)


def _trace_event_from_payload(payload: dict[Any, Any], *, trace_path: Path, line_number: int) -> TraceEvent:
    required_fields = (
        "schema_version",
        "id",
        "trace_id",
        "parent_id",
        "name",
        "type",
        "start_time",
        "end_time",
        "status",
        "metadata",
        "input",
        "output",
        "error",
    )
    for field in required_fields:
        if field not in payload:
            raise ValueError(f"Trace file {trace_path} line {line_number} is missing required field {field!r}")

    schema_version = _expect_string(payload["schema_version"], "schema_version", trace_path, line_number)
    if schema_version != _SCHEMA_VERSION:
        raise ValueError(
            f"Trace file {trace_path} line {line_number} has unsupported schema_version {schema_version!r}"
        )
    event_id = _expect_string(payload["id"], "id", trace_path, line_number)
    trace_id = _expect_string(payload["trace_id"], "trace_id", trace_path, line_number)
    parent_id = _expect_optional_string(payload["parent_id"], "parent_id", trace_path, line_number)
    name = _expect_string(payload["name"], "name", trace_path, line_number)
    event_type = _expect_string(payload["type"], "type", trace_path, line_number)
    if event_type not in _EVENT_TYPES:
        raise ValueError(f"Trace file {trace_path} line {line_number} field 'type' has unsupported value {event_type!r}")
    start_time = _expect_datetime_string(payload["start_time"], "start_time", trace_path, line_number)
    end_time = _expect_datetime_string(payload["end_time"], "end_time", trace_path, line_number)
    if datetime.fromisoformat(end_time) < datetime.fromisoformat(start_time):
        raise ValueError(f"Trace file {trace_path} line {line_number} has end_time before start_time")
    status = _expect_string(payload["status"], "status", trace_path, line_number)
    if status not in _EVENT_STATUSES:
        raise ValueError(f"Trace file {trace_path} line {line_number} field 'status' has unsupported value {status!r}")
    metadata = _expect_mapping(payload["metadata"], "metadata", trace_path, line_number)
    error = _expect_optional_string(payload["error"], "error", trace_path, line_number)
    if event_type == "trace" and event_id != trace_id:
        raise ValueError(f"Trace file {trace_path} line {line_number} trace event id must match trace_id")
    if event_type == "trace" and parent_id is not None:
        raise ValueError(f"Trace file {trace_path} line {line_number} trace event parent_id must be null")
    if event_type != "trace" and parent_id is None:
        raise ValueError(f"Trace file {trace_path} line {line_number} {event_type} event requires parent_id")
    event_value = None
    if event_type == "score":
        if "value" not in payload:
            raise ValueError(f"Trace file {trace_path} line {line_number} score event is missing required field 'value'")
        event_value = _validate_number(payload["value"], "score value")
    elif payload.get("value") is not None:
        event_value = _validate_number(payload["value"], "value")
    event_model = None
    if payload.get("model") is not None:
        event_model = _expect_string(payload["model"], "model", trace_path, line_number)
    event_usage = None
    if "usage" in payload:
        usage = payload["usage"]
        if usage is not None:
            if not isinstance(usage, Mapping):
                raise ValueError(f"Trace file {trace_path} line {line_number} field 'usage' must be an object")
            event_usage = {}
            for key, value in usage.items():
                usage_key = _expect_string(key, "usage key", trace_path, line_number)
                event_usage[usage_key] = _validate_non_negative_number(value, f"usage.{key}")
    event_cost = None
    if "cost" in payload:
        cost = payload["cost"]
        if cost is not None:
            if not isinstance(cost, Mapping):
                raise ValueError(f"Trace file {trace_path} line {line_number} field 'cost' must be an object")
            event_cost = {}
            for key, value in cost.items():
                cost_key = _expect_string(key, "cost key", trace_path, line_number)
                event_cost[cost_key] = _validate_non_negative_number(value, f"cost.{cost_key}")
    event_currency = None
    if payload.get("currency") is not None:
        event_currency = _expect_string(payload["currency"], "currency", trace_path, line_number)
    _validate_json_value(metadata, "metadata", trace_path, line_number)
    _validate_json_value(payload["input"], "input", trace_path, line_number)
    _validate_json_value(payload["output"], "output", trace_path, line_number)
    for key, value in payload.items():
        _expect_string(key, "event key", trace_path, line_number)
        if key not in required_fields and key not in {"value", "model", "usage", "cost", "currency"}:
            _validate_json_value(value, key, trace_path, line_number)
    raw = {str(key): value for key, value in payload.items()}

    return TraceEvent(
        id=event_id,
        trace_id=trace_id,
        parent_id=parent_id,
        name=name,
        type=event_type,
        start_time=start_time,
        end_time=end_time,
        status=status,
        metadata=metadata,
        input=payload["input"],
        output=payload["output"],
        error=error,
        raw=raw,
        value=event_value,
        model=event_model,
        usage=event_usage,
        cost=event_cost,
        currency=event_currency,
    )


def _expect_string(value: Any, field: str, trace_path: Path, line_number: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Trace file {trace_path} line {line_number} field {field!r} must be a string")
    return value


def _expect_optional_string(value: Any, field: str, trace_path: Path, line_number: int) -> str | None:
    if value is None:
        return None
    return _expect_string(value, field, trace_path, line_number)


def _expect_datetime_string(value: Any, field: str, trace_path: Path, line_number: int) -> str:
    timestamp = _expect_string(value, field, trace_path, line_number)
    try:
        datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise ValueError(
            f"Trace file {trace_path} line {line_number} field {field!r} must be an ISO datetime"
        ) from exc
    return timestamp


def _expect_mapping(value: Any, field: str, trace_path: Path, line_number: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Trace file {trace_path} line {line_number} field {field!r} must be an object")
    return {str(key): item for key, item in value.items()}


def _validate_json_value(value: Any, field: str, trace_path: Path, line_number: int) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        try:
            _validate_number(value, field)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Trace file {trace_path} line {line_number} field {field!r} must be finite") from exc
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{field}[{index}]", trace_path, line_number)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"Trace file {trace_path} line {line_number} field {field!r} keys must be strings")
            _validate_json_value(item, f"{field}.{key}", trace_path, line_number)
        return
    raise ValueError(f"Trace file {trace_path} line {line_number} field {field!r} must be JSON-compatible")


def _duration_ms(start_time: str, end_time: str) -> float:
    start = datetime.fromisoformat(start_time)
    end = datetime.fromisoformat(end_time)
    return (end - start).total_seconds() * 1000


def _should_capture(override: bool | None, target: str) -> bool:
    if override is not None:
        return override
    context_value = _current_capture_inputs.get() if target == "inputs" else _current_capture_outputs.get()
    if context_value is not None:
        return context_value
    if target == "inputs":
        return _config.capture_inputs
    return _config.capture_outputs


def _should_drop_trace() -> bool:
    """Decide whether the trace starting now should be sampled out.

    ``sample_rate`` is the probability of keeping a trace, so the deterministic
    edges (``1.0`` keeps everything, ``0.0`` drops everything) never touch the
    random generator. Only partial rates draw from ``random.random()``.
    """

    sample_rate = _config.sample_rate
    if sample_rate >= 1.0:
        return False
    if sample_rate <= 0.0:
        return True
    return random.random() >= sample_rate


def _capture_call_input(
    signature: inspect.Signature,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    bound = signature.bind_partial(*args, **kwargs)
    bound.apply_defaults()
    return {name: _safe_capture(value, key=name) for name, value in bound.arguments.items()}


def _safe_capture(value: Any, *, key: str | None = None, depth: int = 0) -> Any:
    if key is not None and _is_secret_key(key):
        return _REDACTED
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return _redact_secret_text(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, Path):
        return _redact_secret_text(str(value))
    if depth >= _MAX_CAPTURE_DEPTH:
        return _MAX_DEPTH_REACHED
    if isinstance(value, Mapping):
        captured: dict[str, Any] = {}
        for item_key, item_value in value.items():
            item_key_text = _safe_key(item_key)
            captured[item_key_text] = _safe_capture(item_value, key=item_key_text, depth=depth + 1)
        return captured
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_capture(item, depth=depth + 1) for item in value]
    return _safe_repr(value)


def _is_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in _SECRET_KEY_NAMES or any(secret_part in normalized for secret_part in _SECRET_KEY_PARTS)


def _safe_key(value: Any) -> str:
    try:
        return str(value)
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"


def _safe_repr(value: Any) -> str:
    try:
        return _redact_secret_text(repr(value))
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"


def _safe_error(exc: BaseException) -> str:
    return _redact_secret_text(str(exc))


def _redact_secret_text(value: str) -> str:
    redacted = value
    redacted = re.sub(
        r"(?i)\b(authorization\s*[:=]\s*)(bearer\s+)?(?!\[redacted\])[^\s,;\)\]\}]+",
        _redact_labeled_secret_match,
        redacted,
    )
    redacted = re.sub(
        (
            r"(?i)\b(access[_-]?key|api[_-]?key|apikey|auth|client[_-]?secret|credential|credentials|password|"
            r"private[_-]?key|secret|token)(\s*[:=]\s*)(?!\[redacted\])(?!\{[A-Za-z_][A-Za-z0-9_]*\})"
            r"[^\s,;\)\]\}]+"
        ),
        _redact_labeled_secret_match,
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b(bearer\s+)(?!\[redacted\])[^\s,;\)\]\}]+",
        _redact_bearer_secret_match,
        redacted,
    )
    redacted = re.sub(r"\b(sk-[A-Za-z0-9_-]{4,})\b", _REDACTED, redacted)
    return redacted


def _redact_labeled_secret_match(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2) or ''}{_REDACTED}"


def _redact_bearer_secret_match(match: re.Match[str]) -> str:
    return f"{match.group(1)}{_REDACTED}"


def _validate_event_name(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"bir {field} must be a string")
    if not value:
        raise ValueError(f"bir {field} must not be empty")
    return value


def _validate_number(value: Any, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"bir {field} must be an int or float")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"bir {field} must be finite")
    return value


def _validate_non_negative_number(value: Any, field: str) -> int | float:
    numeric_value = _validate_number(value, field)
    if numeric_value < 0:
        raise ValueError(f"bir {field} must be non-negative")
    return numeric_value


def _validate_sample_rate(value: Any) -> float:
    numeric_value = _validate_number(value, "sample_rate")
    if numeric_value < 0.0 or numeric_value > 1.0:
        raise ValueError("bir sample_rate must be between 0.0 and 1.0")
    return float(numeric_value)


def _validate_non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"bir {field} must be an int")
    if value < 0:
        raise ValueError(f"bir {field} must be non-negative")
    return value


def _retrieval_document_from_mapping(document: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(document)
    if "rank" in normalized and normalized["rank"] is not None:
        normalized["rank"] = _validate_non_negative_int(normalized["rank"], "retrieval document rank")
    if "score" in normalized and normalized["score"] is not None:
        normalized["score"] = _validate_non_negative_number(normalized["score"], "retrieval document score")
    return normalized


def _validate_currency(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("bir currency must be a string")
    if not value:
        raise ValueError("bir currency must not be empty")
    return value


def _event_sort_key(event: TraceEvent) -> tuple[str, int, str, str]:
    return (event.start_time, _EVENT_SORT_PRIORITY.get(event.type, 99), event.end_time, event.id)


def _reset_context(
    trace_token: Token[str | None],
    parent_token: Token[str | None],
    capture_inputs_token: Token[bool | None],
    capture_outputs_token: Token[bool | None],
    dropped_token: Token[bool],
) -> None:
    _current_trace_dropped.reset(dropped_token)
    _current_capture_outputs.reset(capture_outputs_token)
    _current_capture_inputs.reset(capture_inputs_token)
    _current_parent_id.reset(parent_token)
    _current_trace_id.reset(trace_token)


def _new_id() -> str:
    return str(uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Environment-variable configuration
#
# The BIR_* variables let deployments configure the SDK without code changes.
# They supply the starting defaults for ``_config`` at import time; explicit
# ``configure(...)`` arguments still win. Capture stays opt-in: it is enabled
# only when an env var or a ``configure`` call asks for it.
# ---------------------------------------------------------------------------

_ENV_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_ENV_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _env_value(name: str) -> str | None:
    """Return the stripped value of ``name``, or ``None`` when unset or blank.

    A blank (whitespace-only) value is treated as unset so a deployment template
    that always defines ``BIR_*`` but sometimes leaves it empty falls back to the
    hardcoded default instead of failing at import.
    """

    raw = os.environ.get(name)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def _parse_env_bool(value: str, name: str) -> bool:
    """Parse a boolean-like environment value, rejecting ambiguous input."""

    normalized = value.strip().lower()
    if normalized in _ENV_TRUE_VALUES:
        return True
    if normalized in _ENV_FALSE_VALUES:
        return False
    allowed = ", ".join(sorted(_ENV_TRUE_VALUES | _ENV_FALSE_VALUES))
    raise ValueError(f"bir {name} must be a boolean-like value (one of: {allowed}), got {value!r}")


def _parse_env_sample_rate(value: str) -> float:
    """Parse a float sample rate from the environment and range-check it."""

    try:
        numeric = float(value.strip())
    except ValueError as exc:
        raise ValueError(f"bir BIR_SAMPLE_RATE must be a number between 0.0 and 1.0, got {value!r}") from exc
    return _validate_sample_rate(numeric)


def _config_from_env() -> _Config:
    """Build the starting config from the ``BIR_*`` environment variables.

    Every field falls back to its hardcoded default when the matching variable is
    unset or blank, so with no environment set this returns a pristine
    ``_Config()``. Invalid values raise a clear error, and capture stays disabled
    unless an env var explicitly enables it.
    """

    defaults = _Config()
    trace_path = _env_value("BIR_TRACE_PATH")
    capture_inputs = _env_value("BIR_CAPTURE_INPUTS")
    capture_outputs = _env_value("BIR_CAPTURE_OUTPUTS")
    service_name = _env_value("BIR_SERVICE_NAME")
    environment = _env_value("BIR_ENVIRONMENT")
    sample_rate = _env_value("BIR_SAMPLE_RATE")
    return _Config(
        trace_path=Path(trace_path) if trace_path is not None else defaults.trace_path,
        capture_inputs=(
            _parse_env_bool(capture_inputs, "BIR_CAPTURE_INPUTS")
            if capture_inputs is not None
            else defaults.capture_inputs
        ),
        capture_outputs=(
            _parse_env_bool(capture_outputs, "BIR_CAPTURE_OUTPUTS")
            if capture_outputs is not None
            else defaults.capture_outputs
        ),
        service_name=(
            _validate_event_name(service_name, "BIR_SERVICE_NAME")
            if service_name is not None
            else defaults.service_name
        ),
        environment=(
            _validate_event_name(environment, "BIR_ENVIRONMENT")
            if environment is not None
            else defaults.environment
        ),
        sample_rate=(
            _parse_env_sample_rate(sample_rate) if sample_rate is not None else defaults.sample_rate
        ),
    )


# Apply env-derived defaults at import, now that the validators above exist.
# ``configure(...)`` still overrides these, and tests reset to a pristine
# ``_Config()`` via ``_reset_config_for_tests`` so ambient env never leaks in.
_config = _config_from_env()


def _reset_config_for_tests() -> None:
    """Reset the active config to hardcoded defaults, ignoring ambient env.

    Tests rely on a clean baseline, so this deliberately constructs a pristine
    ``_Config()`` rather than re-reading the ``BIR_*`` variables; otherwise a
    developer's real environment (or another test's monkeypatched env) could leak
    into an unrelated test.
    """

    global _config
    _config = _Config()
