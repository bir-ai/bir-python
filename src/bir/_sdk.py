from __future__ import annotations

import functools
import inspect
import json
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, TypeVar, cast
from uuid import uuid4

F = TypeVar("F", bound=Callable[..., Any])

_DEFAULT_TRACE_PATH = Path(".llm_observe/traces.jsonl")
_current_trace_id: ContextVar[str | None] = ContextVar("bir_current_trace_id", default=None)
_current_parent_id: ContextVar[str | None] = ContextVar("bir_current_parent_id", default=None)


def observe(name: str | None = None) -> Callable[[F], F]:
    """Decorate a sync function and record one trace event for each call."""

    def decorator(func: F) -> F:
        if inspect.iscoroutinefunction(func):
            raise TypeError("bir.observe supports sync functions only")

        trace_name = name or func.__name__

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            trace_id = _new_id()
            start_time = _now()
            trace_token = _current_trace_id.set(trace_id)
            parent_token = _current_parent_id.set(trace_id)

            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                end_time = _now()
                _reset_context(trace_token, parent_token)
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
                )
                try:
                    _write_event(event)
                except Exception as storage_error:
                    raise exc from storage_error
                raise

            end_time = _now()
            _reset_context(trace_token, parent_token)
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
                )
            )
            return result

        return cast(F, wrapper)

    return decorator


def span(name: str) -> _Span:
    """Create a nested span inside the current trace."""

    return _Span(name)


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
    value: int | float | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "id": event_id,
        "trace_id": trace_id,
        "parent_id": parent_id,
        "name": name,
        "type": event_type,
        "start_time": start_time,
        "end_time": end_time,
        "status": status,
        "metadata": {},
        "input": None,
        "output": None,
        "error": error,
    }
    if value is not None:
        event["value"] = value
    return event


def _write_event(event: dict[str, Any]) -> None:
    _DEFAULT_TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _DEFAULT_TRACE_PATH.open("a", encoding="utf-8") as trace_file:
        trace_file.write(json.dumps(event, sort_keys=True, separators=(",", ":")))
        trace_file.write("\n")


def _reset_context(
    trace_token: Token[str | None],
    parent_token: Token[str | None],
) -> None:
    _current_parent_id.reset(parent_token)
    _current_trace_id.reset(trace_token)


def _new_id() -> str:
    return str(uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
