from __future__ import annotations

import functools
import inspect
import json
import math
import urllib.error
import urllib.request
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Mapping, TypeVar, cast
from uuid import uuid4

F = TypeVar("F", bound=Callable[..., Any])

_DEFAULT_TRACE_PATH = Path(".bir/traces.jsonl")
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
    "api_key",
    "apikey",
    "authorization",
    "auth_header",
    "password",
    "secret",
    "token",
)
_current_trace_id: ContextVar[str | None] = ContextVar("bir_current_trace_id", default=None)
_current_parent_id: ContextVar[str | None] = ContextVar("bir_current_parent_id", default=None)
_current_capture_inputs: ContextVar[bool | None] = ContextVar("bir_current_capture_inputs", default=None)
_current_capture_outputs: ContextVar[bool | None] = ContextVar("bir_current_capture_outputs", default=None)


@dataclass(frozen=True)
class _Config:
    trace_path: Path = _DEFAULT_TRACE_PATH
    capture_inputs: bool = False
    capture_outputs: bool = False


@dataclass(frozen=True)
class TraceEvent:
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

    @property
    def duration_ms(self) -> float:
        return _duration_ms(self.start_time, self.end_time)


@dataclass(frozen=True)
class LoadedTrace:
    id: str
    name: str
    start_time: str
    end_time: str
    status: str
    events: list[TraceEvent]
    root: TraceEvent

    @property
    def duration_ms(self) -> float:
        return self.root.duration_ms


@dataclass(frozen=True)
class SendEventsResult:
    accepted: int
    event_ids: list[str]


_config = _Config()


def configure(
    *,
    trace_path: str | Path | None = None,
    capture_inputs: bool | None = None,
    capture_outputs: bool | None = None,
) -> None:
    """Configure local SDK behavior."""

    global _config

    updates: dict[str, Any] = {}
    if trace_path is not None:
        updates["trace_path"] = Path(trace_path)
    if capture_inputs is not None:
        updates["capture_inputs"] = capture_inputs
    if capture_outputs is not None:
        updates["capture_outputs"] = capture_outputs

    _config = replace(_config, **updates)


def load_events(path: str | Path | None = None) -> list[TraceEvent]:
    """Load local JSONL trace events."""

    trace_path = Path(path) if path is not None else _config.trace_path
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


def load_traces(path: str | Path | None = None) -> list[LoadedTrace]:
    """Load local traces grouped by trace_id."""

    events = load_events(path)
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
) -> SendEventsResult:
    """Send local JSONL trace events to a Bir ingestion server."""

    events = load_events(path)
    event_ids: list[str] = []
    endpoint = _events_endpoint(server_url)

    for event in events:
        _post_event(endpoint, event.raw, timeout=timeout)
        event_ids.append(event.id)

    return SendEventsResult(accepted=len(event_ids), event_ids=event_ids)


def observe(
    name: str | None = None,
    *,
    capture_inputs: bool | None = None,
    capture_outputs: bool | None = None,
) -> Callable[[F], F]:
    """Decorate a sync function and record one trace event for each call."""

    def decorator(func: F) -> F:
        if inspect.iscoroutinefunction(func):
            raise TypeError("bir.observe supports sync functions only")

        trace_name = name or func.__name__
        signature = inspect.signature(func)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            trace_id = _new_id()
            start_time = _now()
            trace_token = _current_trace_id.set(trace_id)
            parent_token = _current_parent_id.set(trace_id)
            capture_inputs_token = _current_capture_inputs.set(_should_capture(capture_inputs, "inputs"))
            capture_outputs_token = _current_capture_outputs.set(_should_capture(capture_outputs, "outputs"))
            input_payload = None

            try:
                if _should_capture(capture_inputs, "inputs"):
                    input_payload = _capture_call_input(signature, args, kwargs)
                result = func(*args, **kwargs)
            except Exception as exc:
                end_time = _now()
                _reset_context(trace_token, parent_token, capture_inputs_token, capture_outputs_token)
                event = _event(
                    event_id=trace_id,
                    trace_id=trace_id,
                    parent_id=None,
                    name=trace_name,
                    event_type="trace",
                    start_time=start_time,
                    end_time=end_time,
                    status="error",
                    error=str(exc),
                    input=input_payload,
                )
                try:
                    _write_event(event)
                except Exception as storage_error:
                    raise exc from storage_error
                raise

            end_time = _now()
            _reset_context(trace_token, parent_token, capture_inputs_token, capture_outputs_token)
            output_payload = _safe_capture(result) if _should_capture(capture_outputs, "outputs") else None
            _write_event(
                _event(
                    event_id=trace_id,
                    trace_id=trace_id,
                    parent_id=None,
                    name=trace_name,
                    event_type="trace",
                    start_time=start_time,
                    end_time=end_time,
                    status="success",
                    error=None,
                    input=input_payload,
                    output=output_payload,
                )
            )
            return result

        return cast(F, wrapper)

    return decorator


def span(name: str) -> _Span:
    """Create a nested span inside the current trace."""

    return _Span(name)


def generation(
    name: str,
    *,
    model: str | None = None,
    input: Any = None,
    metadata: Mapping[str, Any] | None = None,
    capture_input: bool | None = None,
    capture_output: bool | None = None,
) -> _Generation:
    """Create a generation event for an LLM call inside the current trace."""

    return _Generation(
        name=name,
        model=model,
        input=input,
        metadata=metadata,
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

    return _ToolCall(
        name=name,
        input=input,
        metadata=metadata,
        capture_input=capture_input,
        capture_output=capture_output,
    )


def score(name: str, value: int | float) -> None:
    """Attach a score event to the current trace."""

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
            value=score_value,
        )
    )


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
            error=str(exc) if exc is not None else None,
        )
        try:
            _write_event(event)
        except Exception as storage_error:
            if exc is not None:
                raise exc from storage_error
            raise
        return False


class _Generation:
    def __init__(
        self,
        *,
        name: str,
        model: str | None,
        input: Any,
        metadata: Mapping[str, Any] | None,
        capture_input: bool | None,
        capture_output: bool | None,
    ) -> None:
        self.name = name
        self.model = model
        self.input = input
        self.metadata = metadata
        self.capture_input = capture_input
        self.capture_output = capture_output
        self.id: str | None = None
        self.trace_id: str | None = None
        self.parent_id: str | None = None
        self.start_time: str | None = None
        self.output: Any = None
        self.usage: dict[str, int | float] | None = None
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
        event = _event(
            event_id=self.id,
            trace_id=self.trace_id,
            parent_id=self.parent_id,
            name=self.name,
            event_type="generation",
            start_time=self.start_time,
            end_time=_now(),
            status="error" if exc is not None else "success",
            error=str(exc) if exc is not None else None,
            metadata=_safe_capture(dict(self.metadata or {})),
            input=input_payload,
            output=output_payload,
            model=self.model,
            usage=self.usage,
        )
        try:
            _write_event(event)
        except Exception as storage_error:
            if exc is not None:
                raise exc from storage_error
            raise
        return False

    def set_output(self, output: Any) -> None:
        self.output = output

    def set_usage(
        self,
        *,
        input_tokens: int | float | None = None,
        output_tokens: int | float | None = None,
        total_tokens: int | float | None = None,
    ) -> None:
        usage: dict[str, int | float] = {}
        if input_tokens is not None:
            usage["input_tokens"] = _validate_number(input_tokens, "input_tokens")
        if output_tokens is not None:
            usage["output_tokens"] = _validate_number(output_tokens, "output_tokens")
        if total_tokens is not None:
            usage["total_tokens"] = _validate_number(total_tokens, "total_tokens")
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        self.usage = usage


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
            error=str(exc) if exc is not None else None,
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

    def set_output(self, output: Any) -> None:
        self.output = output


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
) -> dict[str, Any]:
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
        "metadata": dict(metadata or {}),
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
    return event


def _write_event(event: dict[str, Any]) -> None:
    _config.trace_path.parent.mkdir(parents=True, exist_ok=True)
    with _config.trace_path.open("a", encoding="utf-8") as trace_file:
        trace_file.write(json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False))
        trace_file.write("\n")


def _events_endpoint(server_url: str) -> str:
    normalized_url = server_url.rstrip("/")
    if not normalized_url:
        raise ValueError("bir server_url must not be empty")
    return f"{normalized_url}/v1/events"


def _post_event(endpoint: str, event: Mapping[str, Any], *, timeout: float) -> None:
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
        raise RuntimeError(f"bir server rejected event with HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"bir could not send event to {endpoint}: {exc.reason}") from exc

    if status < 200 or status >= 300:
        raise RuntimeError(f"bir server rejected event with HTTP {status}: {body}")


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
    if event_type == "score":
        if "value" not in payload:
            raise ValueError(f"Trace file {trace_path} line {line_number} score event is missing required field 'value'")
        _validate_number(payload["value"], "score value")
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
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, Path):
        return str(value)
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
    return any(secret_part in normalized for secret_part in _SECRET_KEY_PARTS)


def _safe_key(value: Any) -> str:
    try:
        return str(value)
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"


def _validate_number(value: Any, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"bir {field} must be an int or float")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"bir {field} must be finite")
    return value


def _event_sort_key(event: TraceEvent) -> tuple[str, int, str, str]:
    return (event.start_time, _EVENT_SORT_PRIORITY.get(event.type, 99), event.end_time, event.id)


def _reset_context(
    trace_token: Token[str | None],
    parent_token: Token[str | None],
    capture_inputs_token: Token[bool | None],
    capture_outputs_token: Token[bool | None],
) -> None:
    _current_capture_outputs.reset(capture_outputs_token)
    _current_capture_inputs.reset(capture_inputs_token)
    _current_parent_id.reset(parent_token)
    _current_trace_id.reset(trace_token)


def _new_id() -> str:
    return str(uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reset_config_for_tests() -> None:
    global _config
    _config = _Config()
