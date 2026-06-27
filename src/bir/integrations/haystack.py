"""Haystack 2.x tracing integration for recording Bir traces.

The tracer intentionally avoids importing the ``haystack`` package so the Bir SDK
stays dependency-free. Haystack 2.x exposes a tracing seam: an application
registers a custom tracer with ``haystack.tracing.enable_tracing(tracer)`` and the
framework then drives it as a context manager, calling ``tracer.trace(
operation_name, tags=..., parent_span=...)`` around each pipeline run and each
component run, and ``tracer.current_span()`` so a component can attach tags to the
span it is running inside.

``BirHaystackTracer`` implements that ``Tracer``/``Span`` protocol without importing
Haystack. A pipeline run (operation ``haystack.pipeline.run``) opens a Bir trace
root; each component run (operation ``haystack.component.run``) is mapped by the
component's class name (read from the ``haystack.component.type`` tag): generator
components (class name ending in ``Generator``) become generation events carrying
the model and token usage when present, tool components (``ToolInvoker`` and other
``*Tool*`` components) become tool-call events, and every other component becomes an
ordinary span. A component that raises is recorded with error status. Active spans
are tracked on a context-local stack so nested and concurrent pipeline runs stay
isolated, exactly like the other Bir bridge handlers.

Haystack carries a component's input and output (and, for generators, the model and
token usage living in the output ``meta``) on *content* tags, which the framework
only records when content tracing is enabled. Call
``haystack.tracing.enable_content_tracing()`` (or set
``HAYSTACK_CONTENT_TRACING_ENABLED=true``) so those tags reach the tracer; Bir then
applies its own capture opt-in and redaction to the payloads while always recording
the model and token usage.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from bir import generation, tool_call
from bir._sdk import _current_trace_id, _trace_context
from bir.integrations._common import _string_or_none, _usage_tokens, _value

# Haystack tracing operation names. A pipeline run is the trace root; a component
# run is mapped to a generation, tool call, or span by its component type.
_PIPELINE_RUN = "haystack.pipeline.run"
_COMPONENT_RUN = "haystack.component.run"

# Haystack tag keys read by duck typing. ``input``/``output`` are *content* tags,
# present only when Haystack content tracing is enabled.
_COMPONENT_NAME_TAG = "haystack.component.name"
_COMPONENT_TYPE_TAG = "haystack.component.type"
_COMPONENT_INPUT_TAG = "haystack.component.input"
_COMPONENT_OUTPUT_TAG = "haystack.component.output"
_COMPONENT_VISITS_TAG = "haystack.component.visits"
_PIPELINE_MAX_RUNS_TAG = "haystack.pipeline.max_runs_per_component"

# Active Bir-mapped spans for the current context. A module-level ContextVar gives
# per-thread and per-asyncio-task isolation, so concurrent pipeline runs each see
# their own innermost span through ``current_span`` without leaking across runs.
_active_spans: ContextVar[tuple["_BirSpan", ...]] = ContextVar("bir_haystack_active_spans", default=())


class BirHaystackTracer:
    """Record Haystack 2.x pipeline/component runs as Bir trace events.

    Implements the Haystack ``Tracer`` interface (``trace`` /
    ``current_span``) without importing the package. ``capture_inputs`` /
    ``capture_outputs`` override Bir's global capture settings for the events this
    tracer records, exactly like the other Bir bridge handlers.
    """

    def __init__(
        self,
        *,
        capture_inputs: bool | None = None,
        capture_outputs: bool | None = None,
    ) -> None:
        self.capture_inputs = capture_inputs
        self.capture_outputs = capture_outputs

    @contextmanager
    def trace(
        self,
        operation_name: str,
        tags: dict[str, Any] | None = None,
        parent_span: Any | None = None,
    ) -> Iterator["_BirSpan"]:
        # ``parent_span`` confirms Haystack's own nesting; Bir derives parenting
        # from its task-local context, so the argument is accepted and unused.
        del parent_span

        bir_span = _BirSpan(tags)
        context, kind, implicit_trace = self._open(operation_name, bir_span)
        token = _active_spans.set(_active_spans.get() + (bir_span,))
        error: BaseException | None = None
        try:
            yield bir_span
        except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
            error = exc
            raise
        finally:
            _active_spans.reset(token)
            _close(context, kind, bir_span, implicit_trace, error)

    def current_span(self) -> "_BirSpan | None":
        stack = _active_spans.get()
        return stack[-1] if stack else None

    def _open(self, operation_name: str, bir_span: "_BirSpan") -> tuple[Any, str, Any | None]:
        if operation_name == _PIPELINE_RUN:
            context = _trace_context(name=_PIPELINE_RUN, metadata=_pipeline_metadata(bir_span.tags))
            context.__enter__()
            return context, "trace", None

        implicit_trace = _implicit_trace_context()
        component_type = _component_type(bir_span.tags)
        kind = _component_kind(component_type)
        metadata = _component_metadata(operation_name, bir_span.tags, component_type)
        name = _component_event_name(bir_span.tags, component_type)

        if kind == "generation":
            context = generation(
                name,
                metadata=metadata,
                capture_input=self.capture_inputs,
                capture_output=self.capture_outputs,
            )
            context.__enter__()
            return context, "generation", implicit_trace

        if kind == "tool_call":
            context = tool_call(
                name,
                metadata=metadata,
                capture_input=self.capture_inputs,
                capture_output=self.capture_outputs,
            )
            context.__enter__()
            return context, "tool_call", implicit_trace

        context = _span_context(name)
        context.__enter__()
        context.set_metadata(metadata)
        return context, "span", implicit_trace


class _BirSpan:
    """The Haystack ``Span`` a component writes tags to during its run.

    Tags accumulate here for the duration of the run; the Bir event is populated
    from them when the ``trace`` context manager exits. ``set_content_tag`` records
    the value unconditionally — Haystack guards the *call site* behind its content
    tracing flag, and Bir applies its own capture opt-in and redaction at write
    time, so the tracer never needs to consult Haystack's flag itself.
    """

    def __init__(self, tags: Mapping[str, Any] | None) -> None:
        self.tags: dict[str, Any] = dict(tags or {})

    def set_tag(self, key: str, value: Any) -> None:
        self.tags[key] = value

    def set_tags(self, tags: Mapping[str, Any]) -> None:
        self.tags.update(tags)

    def set_content_tag(self, key: str, value: Any) -> None:
        self.tags[key] = value

    def raw_span(self) -> Any:
        return self

    def get_correlation_data_for_logs(self) -> dict[str, Any]:
        return {}


def _close(
    context: Any,
    kind: str,
    bir_span: "_BirSpan",
    implicit_trace: Any | None,
    error: BaseException | None,
) -> None:
    if kind == "generation":
        _finish_generation(context, bir_span.tags)
    elif kind == "tool_call":
        input_value = _value(bir_span.tags, _COMPONENT_INPUT_TAG)
        if input_value is not None:
            context.input = input_value
        output = _value(bir_span.tags, _COMPONENT_OUTPUT_TAG)
        if output is not None and hasattr(context, "set_output"):
            context.set_output(output)

    exc_info = (None, None, None) if error is None else (type(error), error, None)
    context.__exit__(*exc_info)
    if implicit_trace is not None:
        implicit_trace.__exit__(*exc_info)


def _finish_generation(context: Any, tags: Mapping[str, Any]) -> None:
    input_value = _value(tags, _COMPONENT_INPUT_TAG)
    if input_value is not None:
        # ``_Generation`` reads ``input`` at exit and exposes no setter, so assign
        # it directly, mirroring how the Agents processor fills a late ``model``.
        context.input = input_value

    output = _value(tags, _COMPONENT_OUTPUT_TAG)
    if output is not None and hasattr(context, "set_output"):
        context.set_output(output)

    meta = _component_meta(output)
    model = _string_or_none(_value(meta, "model"))
    if model is not None and getattr(context, "model", None) is None:
        context.model = model

    usage = _usage_triplet(_value(meta, "usage"))
    if usage is not None and hasattr(context, "set_usage"):
        input_tokens, output_tokens, total_tokens = usage
        context.set_usage(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)


def _component_meta(output: Any) -> Any:
    """Locate the per-call ``meta`` mapping carrying a generator's model and usage.

    Non-chat Haystack generators return ``{"replies": [...], "meta": [ {...} ]}``
    while chat generators return ``{"replies": [ChatMessage(...)]}`` with the meta
    on each reply. Both shapes are read by duck typing so the model and token usage
    can be recovered without importing Haystack.
    """

    if output is None:
        return None

    meta = _value(output, "meta")
    if isinstance(meta, (list, tuple)) and meta:
        return meta[0]
    if isinstance(meta, Mapping):
        return meta

    replies = _value(output, "replies")
    if isinstance(replies, (list, tuple)) and replies:
        return _value(replies[0], "meta")
    return None


def _usage_triplet(usage: Any) -> tuple[Any, Any, Any] | None:
    if usage is None:
        return None
    input_tokens = _usage_tokens(usage, "prompt_tokens", "input_tokens")
    output_tokens = _usage_tokens(usage, "completion_tokens", "output_tokens")
    total_tokens = _usage_tokens(usage, "total_tokens")
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    return input_tokens, output_tokens, total_tokens


def _component_kind(component_type: str | None) -> str:
    """Map a Haystack component class name to a Bir event kind.

    Generators (class name ending in ``Generator``) record LLM calls; tool
    components (``ToolInvoker`` and other ``*Tool*`` components) record tool calls;
    everything else (retrievers, prompt builders, routers, ...) is a structural
    span.
    """

    if not component_type:
        return "span"
    if component_type.endswith("Generator"):
        return "generation"
    if "Tool" in component_type:
        return "tool_call"
    return "span"


def _implicit_trace_context() -> Any | None:
    """Open a Bir trace root for a component run that arrives with no active trace.

    Haystack wraps component runs in a ``haystack.pipeline.run`` span, so the root
    is normally already open and this returns ``None``. The fallback mirrors the
    other handlers so a component traced on its own (defensive only) still attaches
    to a root instead of raising.
    """

    if _current_trace_id.get() is not None:
        return None
    context = _trace_context(name=_PIPELINE_RUN, metadata={"integration": "haystack", "kind": "implicit_root"})
    context.__enter__()
    return context


def _span_context(name: str) -> Any:
    # Imported lazily, mirroring the other handlers, so the ``span`` builder never
    # shadows the local ``_BirSpan`` naming in this module.
    from bir import span

    return span(name)


def _component_type(tags: Mapping[str, Any]) -> str | None:
    return _string_or_none(_value(tags, _COMPONENT_TYPE_TAG))


def _component_event_name(tags: Mapping[str, Any], component_type: str | None) -> str:
    name = _string_or_none(_value(tags, _COMPONENT_NAME_TAG))
    if name:
        return name
    if component_type:
        return f"haystack.{component_type}"
    return "haystack.component"


def _component_metadata(
    operation_name: str,
    tags: Mapping[str, Any],
    component_type: str | None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"integration": "haystack", "haystack_operation": operation_name}
    if component_type is not None:
        metadata["haystack_component_type"] = component_type
    name = _string_or_none(_value(tags, _COMPONENT_NAME_TAG))
    if name is not None:
        metadata["haystack_component_name"] = name
    visits = _value(tags, _COMPONENT_VISITS_TAG)
    if visits is not None:
        metadata["visits"] = visits
    return metadata


def _pipeline_metadata(tags: Mapping[str, Any]) -> dict[str, Any]:
    # The pipeline's input/output data is left off the trace root: it is not gated
    # by Bir's input/output capture opt-in the way component payloads are, so
    # recording it as metadata could leak content. Component runs carry the
    # capture-gated payloads instead.
    metadata: dict[str, Any] = {"integration": "haystack", "kind": "pipeline"}
    max_runs = _value(tags, _PIPELINE_MAX_RUNS_TAG)
    if max_runs is not None:
        metadata["max_runs_per_component"] = max_runs
    return metadata
