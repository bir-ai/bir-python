"""OpenAI Agents SDK tracing-processor integration for recording Bir traces.

The processor intentionally avoids importing the ``openai-agents`` package so the
Bir SDK stays dependency-free. The Agents SDK calls a registered processor's
lifecycle methods by name (``on_trace_start``/``on_trace_end`` and
``on_span_start``/``on_span_end``), so an application can register this processor
with ``agents.add_trace_processor(BirAgentsTracingProcessor())`` when the Agents
SDK is installed in the application environment and have its agent runs recorded
as Bir traces.

An agent run's trace becomes a Bir trace root; spans are mapped by their
``span_data.type``: model/LLM spans (``generation`` and ``response``) become
generation events, tool spans (``function`` and ``mcp_tools``) become tool-call
events, and every other span kind (``agent``, ``handoff``, ``guardrail``,
``custom``, ...) becomes an ordinary span. Active traces and spans are tracked by
their Agents id in a dict exactly as the LangChain handler tracks ``run_id``.
"""

from __future__ import annotations

from typing import Any

from bir import generation, tool_call
from bir._sdk import _current_trace_id, _trace_context
from bir.integrations._common import _response_output, _string_or_none, _usage_tokens, _value

# Agents ``span_data.type`` values mapped to Bir generation events (LLM calls
# carrying a model and, when present, token usage) and to Bir tool-call events
# (function tools and MCP tool listings). Every other type falls through to a
# structural Bir span.
_GENERATION_TYPES = frozenset({"generation", "response"})
_TOOL_TYPES = frozenset({"function", "mcp_tools"})


class BirAgentsTracingProcessor:
    """Record OpenAI Agents SDK trace/span events as Bir trace events.

    Implements the Agents SDK ``TracingProcessor`` interface without importing
    the package. ``capture_inputs``/``capture_outputs`` override Bir's global
    capture settings for the events this processor records, exactly like the
    other Bir callback handlers.
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

    def on_trace_start(self, trace: Any) -> None:
        context = _trace_context(name=_trace_name(trace), metadata=_trace_metadata(trace))
        context.__enter__()
        self._active_runs[_trace_key(trace)] = _ActiveRun("trace", context)

    def on_trace_end(self, trace: Any) -> None:
        self._end_run(_trace_key(trace))

    def on_span_start(self, span: Any) -> None:
        span_data, span_type = _span_data_and_type(span)
        metadata = _base_metadata(span, span_type)
        implicit_trace = _implicit_trace_context(span)

        if span_type in _GENERATION_TYPES:
            context = generation(
                _span_name(span_data, span_type),
                model=_model_name(span_data, span_type),
                input=_generation_input(span_data),
                metadata=metadata,
                capture_input=self.capture_inputs,
                capture_output=self.capture_outputs,
            )
            context.__enter__()
            self._active_runs[_span_key(span)] = _ActiveRun("generation", context, implicit_trace=implicit_trace)
            return

        if span_type in _TOOL_TYPES:
            context = tool_call(
                _span_name(span_data, span_type),
                input=_tool_input(span_data, span_type),
                metadata=metadata,
                capture_input=self.capture_inputs,
                capture_output=self.capture_outputs,
            )
            context.__enter__()
            self._active_runs[_span_key(span)] = _ActiveRun("tool_call", context, implicit_trace=implicit_trace)
            return

        context = _span_context(_span_name(span_data, span_type))
        context.__enter__()
        context.set_metadata(metadata)
        self._active_runs[_span_key(span)] = _ActiveRun("span", context, implicit_trace=implicit_trace)

    def on_span_end(self, span: Any) -> None:
        active_run = self._active_runs.get(_span_key(span))
        if active_run is not None:
            span_data, span_type = _span_data_and_type(span)
            if active_run.kind == "generation":
                _finish_generation(active_run.context, span_data, span_type)
            elif active_run.kind == "tool_call":
                output = _tool_output(span_data, span_type)
                if output is not None and hasattr(active_run.context, "set_output"):
                    active_run.context.set_output(output)
            elif active_run.kind == "span":
                details = _span_details(span_data, span_type)
                if details:
                    active_run.context.set_metadata(details)
        self._end_run(_span_key(span), error=_span_error(span))

    def shutdown(self) -> None:
        # Bir writes every event synchronously when its context manager exits, so
        # there is nothing queued to drain. Defined to satisfy the processor
        # interface.
        return None

    def force_flush(self) -> None:
        # No-op for the same reason as ``shutdown``: nothing is buffered.
        return None

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

    The Agents SDK always wraps spans in a trace, so ``on_trace_start`` normally
    opens the root before any span starts and this returns ``None``. The fallback
    mirrors the LangChain handler so a span recorded without a started trace
    (defensive only) still attaches to a root instead of raising.
    """

    if _current_trace_id.get() is not None:
        return None

    metadata: dict[str, Any] = {"integration": "openai_agents", "kind": "implicit_root"}
    trace_id = _value(span, "trace_id")
    if trace_id is not None:
        metadata["agents_trace_id"] = trace_id

    context = _trace_context(name="openai_agents.trace", metadata=metadata)
    context.__enter__()
    return context


def _span_context(name: str) -> Any:
    # Imported lazily, mirroring the LangChain handler, so the ``span`` builder
    # never collides with the ``span`` parameter on the processor methods.
    from bir import span

    return span(name)


def _finish_generation(context: Any, span_data: Any, span_type: str | None) -> None:
    output = _generation_output(span_data, span_type)
    if output is not None and hasattr(context, "set_output"):
        context.set_output(output)
    # A response span's model lives on the response object, which is only
    # populated by span end, so fill it in here when it was unknown at start.
    if getattr(context, "model", None) is None:
        model = _model_name(span_data, span_type)
        if model is not None:
            context.model = model
    usage = _usage(span_data, span_type)
    if usage is not None and hasattr(context, "set_usage"):
        input_tokens, output_tokens, total_tokens = usage
        context.set_usage(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)


def _span_data_and_type(span: Any) -> tuple[Any, str | None]:
    span_data = _value(span, "span_data")
    if span_data is None:
        return None, None
    return span_data, _string_or_none(_value(span_data, "type"))


def _trace_key(trace: Any) -> str:
    trace_id = _value(trace, "trace_id")
    if trace_id is None:
        return "trace:__openai_agents__"
    return f"trace:{trace_id}"


def _span_key(span: Any) -> str:
    return f"span:{_value(span, 'span_id')}"


def _trace_name(trace: Any) -> str:
    name = _string_or_none(_value(trace, "name"))
    if name:
        return name
    return "openai_agents.trace"


def _trace_metadata(trace: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {"integration": "openai_agents", "kind": "trace"}
    trace_id = _value(trace, "trace_id")
    if trace_id is not None:
        metadata["agents_trace_id"] = trace_id
    group_id = _value(trace, "group_id")
    if group_id is not None:
        metadata["group_id"] = group_id
    return metadata


def _span_name(span_data: Any, span_type: str | None) -> str:
    name = _string_or_none(_value(span_data, "name"))
    if name:
        return name
    if span_type:
        return f"openai_agents.{span_type}"
    return "openai_agents.span"


def _base_metadata(span: Any, span_type: str | None) -> dict[str, Any]:
    metadata: dict[str, Any] = {"integration": "openai_agents"}
    if span_type is not None:
        metadata["agents_type"] = span_type
    span_id = _value(span, "span_id")
    if span_id is not None:
        metadata["span_id"] = span_id
    parent_id = _value(span, "parent_id")
    if parent_id is not None:
        metadata["parent_id"] = parent_id
    trace_id = _value(span, "trace_id")
    if trace_id is not None:
        metadata["agents_trace_id"] = trace_id
    return metadata


def _model_name(span_data: Any, span_type: str | None) -> str | None:
    model = _string_or_none(_value(span_data, "model"))
    if model is not None:
        return model
    if span_type == "response":
        response = _value(span_data, "response")
        if response is not None:
            return _string_or_none(_value(response, "model"))
    return None


def _usage(span_data: Any, span_type: str | None) -> tuple[Any, Any, Any] | None:
    usage = _value(span_data, "usage")
    if usage is None and span_type == "response":
        response = _value(span_data, "response")
        usage = _value(response, "usage") if response is not None else None
    if usage is None:
        return None

    input_tokens = _usage_tokens(usage, "input_tokens", "prompt_tokens")
    output_tokens = _usage_tokens(usage, "output_tokens", "completion_tokens")
    total_tokens = _usage_tokens(usage, "total_tokens")
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    return input_tokens, output_tokens, total_tokens


def _generation_input(span_data: Any) -> Any:
    input_value = _value(span_data, "input")
    if input_value is None:
        return None
    return {"input": input_value}


def _generation_output(span_data: Any, span_type: str | None) -> Any:
    if span_type == "response":
        response = _value(span_data, "response")
        if response is not None:
            return _response_output(response)
    return _value(span_data, "output")


def _tool_input(span_data: Any, span_type: str | None) -> Any:
    if span_type == "mcp_tools":
        server = _value(span_data, "server")
        return {"server": server} if server is not None else None
    return _value(span_data, "input")


def _tool_output(span_data: Any, span_type: str | None) -> Any:
    if span_type == "mcp_tools":
        return _value(span_data, "result")
    return _value(span_data, "output")


def _span_details(span_data: Any, span_type: str | None) -> dict[str, Any]:
    """Type-specific fields for a structural span, merged into its metadata.

    Structural Bir spans carry no input/output, so the few useful fields a
    handoff, guardrail, custom, or agent span exposes are recorded as metadata
    (redacted at write time like all metadata).
    """

    if span_data is None:
        return {}

    details: dict[str, Any] = {}
    if span_type == "handoff":
        _set_if_present(details, span_data, ("from_agent", "to_agent"))
    elif span_type == "guardrail":
        triggered = _value(span_data, "triggered")
        if triggered is not None:
            details["triggered"] = triggered
    elif span_type == "custom":
        data = _value(span_data, "data")
        if data is not None:
            details["data"] = data
    elif span_type == "agent":
        _set_if_present(details, span_data, ("tools", "handoffs", "output_type"))
    return details


def _set_if_present(target: dict[str, Any], source: Any, keys: tuple[str, ...]) -> None:
    for key in keys:
        value = _value(source, key)
        if value is not None:
            target[key] = value


def _span_error(span: Any) -> BaseException | None:
    """Build an exception from a span's Agents ``SpanError`` so Bir records error status.

    The Agents SDK reports a span failure as a ``SpanError`` mapping with a
    ``message`` (and optional ``data``), not as a Python exception, so synthesize
    one whose message is redacted by Bir's normal error handling on exit.
    """

    error = _value(span, "error")
    if error is None:
        return None
    message = _value(error, "message")
    text = _string_or_none(message)
    if text is None:
        text = _string_or_none(str(error)) or "openai_agents span error"
    return RuntimeError(text)
