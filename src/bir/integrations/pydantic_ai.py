"""Pydantic AI instrumentation integration for recording Bir traces.

Pydantic AI's lowest-coupling, supported observability seam is its OpenTelemetry
instrumentation: constructing an agent with ``Agent(instrument=True)`` (or calling
``Agent.instrument_all()``) makes every run emit OpenTelemetry spans that follow
the GenAI semantic conventions. The OpenTelemetry SDK drives any registered
``SpanProcessor`` by calling its ``on_start``/``on_end`` (and ``shutdown`` /
``force_flush``) methods by name, so an application can register
``BirPydanticAIHandler`` on the tracer provider Pydantic AI uses and have its agent
runs recorded as Bir traces -- without Bir importing ``pydantic_ai`` or
``opentelemetry``.

The spans are read by duck typing, tolerant of the attribute-key changes across
Pydantic AI instrumentation versions. A span is classified by its
``gen_ai.operation.name`` attribute (falling back to its span name): an agent-run
span (``invoke_agent`` / ``agent run``) opens a Bir trace root, a model span
(``chat``) becomes a generation carrying the model and token usage, and a tool span
(``execute_tool`` / ``running tool``) becomes a tool-call event. Every other span
becomes an ordinary Bir span. Active runs are tracked by their OpenTelemetry span id
exactly as the LangChain handler tracks ``run_id``, so concurrent and nested runs
stay isolated, and a failed span (OTel ``ERROR`` status or an ``exception`` event)
is recorded with error status.
"""

from __future__ import annotations

from typing import Any

from bir import generation, tool_call
from bir._sdk import _current_trace_id, _trace_context
from bir.integrations._common import _string_or_none, _usage_tokens, _value

# ``gen_ai.operation.name`` values (and span-name prefixes for older Pydantic AI
# instrumentation versions that predate the operation attribute) mapped to the Bir
# event each span becomes.
_GENERATION_OPERATIONS = frozenset({"chat"})
_TOOL_OPERATIONS = frozenset({"execute_tool", "invoke_tool"})
_AGENT_OPERATIONS = frozenset({"invoke_agent", "create_agent", "agent run"})

_GENERATION_NAME_PREFIXES = ("chat",)
_TOOL_NAME_PREFIXES = ("execute_tool ", "running tool ", "running tool:")
_AGENT_NAME_PREFIXES = ("invoke_agent", "agent run")


class BirPydanticAIHandler:
    """Record Pydantic AI instrumentation spans as Bir trace events.

    Implements the OpenTelemetry ``SpanProcessor`` interface without importing
    ``opentelemetry`` or ``pydantic_ai``. ``capture_inputs``/``capture_outputs``
    override Bir's global capture settings for the events this handler records,
    exactly like the other Bir callback handlers.
    """

    def __init__(
        self,
        *,
        capture_inputs: bool | None = None,
        capture_outputs: bool | None = None,
    ) -> None:
        self.capture_inputs = capture_inputs
        self.capture_outputs = capture_outputs
        self._active_runs: dict[str, _ActiveRun] = {}

    def on_start(self, span: Any, parent_context: Any | None = None) -> None:
        del parent_context  # The Bir parent is the active contextvar, not OTel's.
        attributes = _value(span, "attributes")
        operation = _string_or_none(_value(attributes, "gen_ai.operation.name"))
        kind = _classify(span, operation)
        metadata = _base_metadata(span, attributes, operation)

        if kind == "agent" and _current_trace_id.get() is None:
            context = _trace_context(name=_trace_name(span, attributes), metadata=metadata)
            context.__enter__()
            self._active_runs[_span_key(span)] = _ActiveRun("trace", context)
            return

        implicit_trace = _implicit_trace_context(span)

        if kind == "generation":
            context = generation(
                _event_name(span, "pydantic_ai.chat"),
                model=_model_name(attributes),
                input=_generation_input(attributes),
                metadata=metadata,
                capture_input=self.capture_inputs,
                capture_output=self.capture_outputs,
            )
            context.__enter__()
            self._active_runs[_span_key(span)] = _ActiveRun("generation", context, implicit_trace=implicit_trace)
            return

        if kind == "tool_call":
            context = tool_call(
                _tool_name(span, attributes),
                input=_tool_input(attributes),
                metadata=metadata,
                capture_input=self.capture_inputs,
                capture_output=self.capture_outputs,
            )
            context.__enter__()
            self._active_runs[_span_key(span)] = _ActiveRun("tool_call", context, implicit_trace=implicit_trace)
            return

        context = _span_context(_event_name(span, "pydantic_ai.span"))
        context.__enter__()
        context.set_metadata(metadata)
        self._active_runs[_span_key(span)] = _ActiveRun("span", context, implicit_trace=implicit_trace)

    def on_end(self, span: Any) -> None:
        active_run = self._active_runs.get(_span_key(span))
        if active_run is not None:
            attributes = _value(span, "attributes")
            if active_run.kind == "generation":
                _finish_generation(active_run.context, attributes)
            elif active_run.kind == "tool_call":
                output = _tool_output(attributes)
                if output is not None and hasattr(active_run.context, "set_output"):
                    active_run.context.set_output(output)
        self._end_run(_span_key(span), error=_span_error(span))

    def shutdown(self) -> None:
        # Bir writes every event synchronously when its context manager exits, so
        # there is nothing queued to drain. Defined to satisfy the processor
        # interface.
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        # No-op for the same reason as ``shutdown``: nothing is buffered. The OTel
        # interface expects a bool reporting that the (empty) flush succeeded.
        del timeout_millis
        return True

    def _end_run(self, run_id: str, *, error: BaseException | None = None) -> None:
        active_run = self._active_runs.pop(run_id, None)
        if active_run is None:
            return

        if error is None:
            active_run.context.__exit__(None, None, None)
            if active_run.implicit_trace is not None:
                active_run.implicit_trace.__exit__(None, None, None)
            return
        active_run.context.__exit__(type(error), error, None)
        if active_run.implicit_trace is not None:
            active_run.implicit_trace.__exit__(type(error), error, None)


class _ActiveRun:
    def __init__(self, kind: str, context: Any, *, implicit_trace: Any | None = None) -> None:
        self.kind = kind
        self.context = context
        self.implicit_trace = implicit_trace


def _implicit_trace_context(span: Any) -> Any | None:
    """Open a Bir trace root for a span that arrives with no active Bir trace.

    Pydantic AI always wraps model and tool spans inside an agent-run span, so
    that root normally opens before any child span starts and this returns
    ``None``. The fallback mirrors the LangChain handler so a span recorded
    without a started trace (defensive only) still attaches to a root instead of
    raising.
    """

    if _current_trace_id.get() is not None:
        return None

    metadata: dict[str, Any] = {"integration": "pydantic_ai", "kind": "implicit_root"}
    trace_id = _trace_id(span)
    if trace_id is not None:
        metadata["otel_trace_id"] = trace_id

    context = _trace_context(name="pydantic_ai.agent_run", metadata=metadata)
    context.__enter__()
    return context


def _span_context(name: str) -> Any:
    # Imported lazily, mirroring the LangChain handler, so the ``span`` builder
    # never collides with the ``span`` parameter on the handler methods.
    from bir import span

    return span(name)


def _finish_generation(context: Any, attributes: Any) -> None:
    output = _generation_output(attributes)
    if output is not None and hasattr(context, "set_output"):
        context.set_output(output)
    # The response model is only known once the request completes, so fill it in
    # here when it was unknown at span start.
    if getattr(context, "model", None) is None:
        model = _model_name(attributes)
        if model is not None:
            context.model = model
    usage = _usage(attributes)
    if usage is not None and hasattr(context, "set_usage"):
        input_tokens, output_tokens, total_tokens = usage
        context.set_usage(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)


def _classify(span: Any, operation: str | None) -> str:
    name = _string_or_none(_value(span, "name")) or ""
    if operation in _GENERATION_OPERATIONS or _starts_with(name, _GENERATION_NAME_PREFIXES):
        return "generation"
    if operation in _TOOL_OPERATIONS or _starts_with(name, _TOOL_NAME_PREFIXES):
        return "tool_call"
    if operation in _AGENT_OPERATIONS or name == "agent run" or _starts_with(name, _AGENT_NAME_PREFIXES):
        return "agent"
    return "span"


def _starts_with(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == prefix or name.startswith(prefix) for prefix in prefixes)


def _span_key(span: Any) -> str:
    return f"span:{_span_id(span)}"


def _span_id(span: Any) -> str | None:
    context = _value(span, "context")
    span_id = _value(context, "span_id") if context is not None else None
    if span_id is None:
        getter = getattr(span, "get_span_context", None)
        if callable(getter):
            try:
                fetched = getter()
            except Exception:
                fetched = None
            span_id = _value(fetched, "span_id") if fetched is not None else None
    return _format_id(span_id)


def _parent_span_id(span: Any) -> str | None:
    parent = _value(span, "parent")
    if parent is None:
        return None
    return _format_id(_value(parent, "span_id"))


def _trace_id(span: Any) -> str | None:
    context = _value(span, "context")
    return _format_id(_value(context, "trace_id")) if context is not None else None


def _format_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return format(value, "016x")
    return _string_or_none(value)


def _trace_name(span: Any, attributes: Any) -> str:
    agent_name = _string_or_none(_value(attributes, "gen_ai.agent.name"))
    if agent_name:
        return agent_name
    return _event_name(span, "pydantic_ai.agent_run")


def _event_name(span: Any, fallback: str) -> str:
    name = _string_or_none(_value(span, "name"))
    return name if name else fallback


def _base_metadata(span: Any, attributes: Any, operation: str | None) -> dict[str, Any]:
    metadata: dict[str, Any] = {"integration": "pydantic_ai"}
    if operation is not None:
        metadata["gen_ai_operation"] = operation
    span_id = _span_id(span)
    if span_id is not None:
        metadata["otel_span_id"] = span_id
    parent_id = _parent_span_id(span)
    if parent_id is not None:
        metadata["otel_parent_id"] = parent_id
    trace_id = _trace_id(span)
    if trace_id is not None:
        metadata["otel_trace_id"] = trace_id
    system = _string_or_none(_value(attributes, "gen_ai.system")) or _string_or_none(
        _value(attributes, "gen_ai.provider.name")
    )
    if system is not None:
        metadata["gen_ai_system"] = system
    tool_call_id = _string_or_none(_value(attributes, "gen_ai.tool.call.id"))
    if tool_call_id is not None:
        metadata["gen_ai_tool_call_id"] = tool_call_id
    return metadata


def _model_name(attributes: Any) -> str | None:
    for key in ("gen_ai.request.model", "gen_ai.response.model", "gen_ai.model.name"):
        model = _string_or_none(_value(attributes, key))
        if model is not None:
            return model
    return None


def _usage(attributes: Any) -> tuple[Any, Any, Any] | None:
    if attributes is None:
        return None
    input_tokens = _usage_tokens(attributes, "gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens")
    output_tokens = _usage_tokens(attributes, "gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens")
    total_tokens = _usage_tokens(attributes, "gen_ai.usage.total_tokens")
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    return input_tokens, output_tokens, total_tokens


def _generation_input(attributes: Any) -> Any:
    for key in ("gen_ai.input.messages", "gen_ai.prompt"):
        value = _value(attributes, key)
        if value is not None:
            return value
    return None


def _generation_output(attributes: Any) -> Any:
    for key in ("gen_ai.output.messages", "gen_ai.completion"):
        value = _value(attributes, key)
        if value is not None:
            return value
    return None


def _tool_name(span: Any, attributes: Any) -> str:
    name = _string_or_none(_value(attributes, "gen_ai.tool.name"))
    if name is not None:
        return name
    span_name = _string_or_none(_value(span, "name")) or ""
    for prefix in _TOOL_NAME_PREFIXES:
        if span_name.startswith(prefix):
            stripped = span_name[len(prefix):].strip()
            if stripped:
                return stripped
    return span_name or "pydantic_ai.tool"


def _tool_input(attributes: Any) -> Any:
    for key in ("gen_ai.tool.call.arguments", "tool_arguments"):
        value = _value(attributes, key)
        if value is not None:
            return value
    return None


def _tool_output(attributes: Any) -> Any:
    for key in ("gen_ai.tool.call.result", "tool_response"):
        value = _value(attributes, key)
        if value is not None:
            return value
    return None


def _span_error(span: Any) -> BaseException | None:
    """Build an exception from a span's OTel failure so Bir records error status.

    Pydantic AI marks a failed span with the OpenTelemetry ``ERROR`` status and
    records an ``exception`` span event rather than handing back a Python
    exception, so synthesize one whose message is redacted by Bir's normal error
    handling on exit. The exception event's message is preferred over the status
    description when both are present.
    """

    events = _value(span, "events")
    if isinstance(events, (list, tuple)):
        for event in events:
            if _string_or_none(_value(event, "name")) == "exception":
                message = _string_or_none(_value(_value(event, "attributes"), "exception.message"))
                if message is not None:
                    return RuntimeError(message)

    status = _value(span, "status")
    if status is not None and _is_error_status(_value(status, "status_code")):
        description = _string_or_none(_value(status, "description"))
        return RuntimeError(description or "pydantic_ai span error")
    return None


def _is_error_status(code: Any) -> bool:
    if code is None or isinstance(code, bool):
        return False
    name = getattr(code, "name", None)
    if isinstance(name, str):
        return name.upper() == "ERROR"
    if isinstance(code, int):
        return code == 2
    if isinstance(code, str):
        return code.upper().endswith("ERROR")
    return False
