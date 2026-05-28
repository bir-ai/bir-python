from __future__ import annotations

import functools
import inspect
import json
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
_REDACTED = "[redacted]"
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
            value=value,
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
            usage["input_tokens"] = input_tokens
        if output_tokens is not None:
            usage["output_tokens"] = output_tokens
        if total_tokens is not None:
            usage["total_tokens"] = total_tokens
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            usage["total_tokens"] = input_tokens + output_tokens
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
        trace_file.write(json.dumps(event, sort_keys=True, separators=(",", ":")))
        trace_file.write("\n")


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
    if depth >= _MAX_CAPTURE_DEPTH:
        return repr(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(item_key): _safe_capture(item_value, key=str(item_key), depth=depth + 1)
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_capture(item, depth=depth + 1) for item in value]
    return repr(value)


def _is_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(secret_part in normalized for secret_part in _SECRET_KEY_PARTS)


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
