"""Opt-in OpenTelemetry/OTLP bridge for forwarding loaded Bir traces.

Bir's defaults are local-first and zero-runtime-dependency: nothing here is
imported by ``import bir`` or ``import bir.integrations``, and the SDK never
exports anything on its own. Teams that already run an observability backend can
call :func:`export_traces_to_otlp` to replay locally recorded traces as
OpenTelemetry spans and ship them to an OTLP endpoint.

The OpenTelemetry packages are imported lazily inside the functions that need
them, so this module imports cleanly without them installed; calling the
exporter without the ``otel`` extra raises a clear, actionable ``ImportError``.
Install the extra with ``pip install 'bir-sdk[otel]'``.

Each Bir trace becomes one OpenTelemetry trace: the trace root maps to a root
span and every other ``TraceEvent`` maps to a child span linked by ``parent_id``.
Span start/end come from the event's ISO timestamps, span status from
``success``/``error``, and attributes follow the GenAI semantic conventions where
they exist (``gen_ai.request.model``, ``gen_ai.usage.input_tokens`` /
``gen_ai.usage.output_tokens``, and ``gen_ai.system`` when the provider was
recorded) with ``bir.*`` attributes for everything else.

The OpenTelemetry ``Resource`` carries ``service.name`` and, when the traces
recorded them, the deployment environment (``deployment.environment``, from
``configure(environment=...)``) and the trace source (``bir.source``, from
``configure(source=...)``). See :func:`export_traces_to_otlp` for how those are
resolved when one export spans more than one environment or source.

The exporter only reads traces; it never writes to or mutates the local JSONL.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from bir import LoadedTrace, TraceEvent, load_traces

# Event types recorded as ``CLIENT`` spans because they represent a call out to
# an external system (a model provider, a tool, a retrieval backend). Everything
# else (the trace root, structural spans, scores) is an ``INTERNAL`` span.
_CLIENT_SPAN_TYPES = frozenset({"generation", "tool_call", "retrieval"})

_INSTALL_HINT = (
    "OpenTelemetry export requires the optional 'otel' extra. Install it with:\n"
    "    pip install 'bir-sdk[otel]'"
)


def export_traces_to_otlp(
    traces: LoadedTrace | Iterable[LoadedTrace] | str | Path,
    *,
    endpoint: str | None = None,
    service_name: str = "bir",
    environment: str | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float | None = None,
    span_exporter: Any | None = None,
) -> int:
    """Convert loaded Bir traces to OpenTelemetry spans and export them via OTLP.

    ``traces`` accepts an already-loaded :class:`~bir.LoadedTrace`, an iterable of
    them, or a path (``str``/``Path``) to a trace file, which is loaded with
    :func:`bir.load_traces`. Loading only reads the local JSONL; this function
    never writes to or alters it.

    ``endpoint`` is the OTLP/HTTP traces endpoint (for example
    ``"http://localhost:4318/v1/traces"``). When ``None`` the underlying exporter
    falls back to its own configuration, including the standard
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` environment variable. ``service_name`` is
    recorded on the OpenTelemetry ``Resource`` as ``service.name``. ``headers``
    and ``timeout`` are forwarded to the default OTLP/HTTP exporter (use
    ``headers`` for backend auth tokens).

    ``environment`` sets ``deployment.environment`` on the ``Resource`` and takes
    precedence over anything recorded in the traces. When it is ``None`` the
    environment is derived from the trace roots' ``metadata.service.environment``
    (recorded by ``configure(environment=...)``); ``bir.source`` is likewise
    derived from ``metadata.source`` (``configure(source=...)``). Each is added to
    the ``Resource`` only when a single value applies to the whole export. If the
    traces in one call disagree (more than one distinct value) and no explicit
    ``environment`` forces a choice, the ``Resource`` attribute is omitted and the
    per-trace value is recorded on each span instead (``bir.environment`` /
    ``bir.source``) so a mixed export never silently drops it. When nothing was
    recorded, nothing is added and the export is byte-for-byte identical to one
    without these inputs.

    ``span_exporter`` injects a ready-made OpenTelemetry ``SpanExporter`` instead
    of building the default OTLP/HTTP one; ``endpoint``, ``headers``, and
    ``timeout`` are then ignored. This is the seam used by tests (an in-memory
    exporter) and by callers who need a different transport. An injected exporter
    is owned by the caller and is not shut down here.

    Returns the number of spans exported (one per Bir event across all traces).

    Raises :class:`ImportError` with an actionable message when the ``otel`` extra
    is not installed.
    """

    loaded = _resolve_traces(traces)
    api = _import_otel_api()
    context = _resolve_resource_context(loaded, environment)

    owns_exporter = span_exporter is None
    exporter = span_exporter if span_exporter is not None else _build_default_exporter(endpoint, headers, timeout)

    resource_attributes: dict[str, Any] = {api.SERVICE_NAME: service_name}
    if context.environment is not None:
        resource_attributes["deployment.environment"] = context.environment
    if context.source is not None:
        resource_attributes["bir.source"] = context.source
    resource = api.Resource.create(resource_attributes)
    provider = api.TracerProvider(resource=resource)
    provider.add_span_processor(api.SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("bir")

    try:
        exported = 0
        for trace in loaded:
            # Per-span environment/source are only filled in when the export spans
            # more than one value, so a single-environment export keeps them on the
            # Resource and adds nothing to the spans themselves.
            span_environment = _trace_environment(trace) if context.per_span_environment else None
            span_source = _trace_source(trace) if context.per_span_source else None
            exported += _export_one_trace(
                trace,
                tracer=tracer,
                api=api,
                span_environment=span_environment,
                span_source=span_source,
            )
        provider.force_flush()
    finally:
        # Only tear down the provider (and, through it, the exporter) when we
        # built the exporter ourselves. An injected exporter belongs to the
        # caller, so we leave it open for them to reuse or shut down.
        if owns_exporter:
            provider.shutdown()
    return exported


def _export_one_trace(
    trace: LoadedTrace,
    *,
    tracer: Any,
    api: _OtelApi,
    span_environment: str | None = None,
    span_source: str | None = None,
) -> int:
    """Build a parent/child OpenTelemetry span tree for one Bir trace.

    Spans are created by walking from the trace root down through ``parent_id``
    links so a parent's span context always exists before its children. A parent
    span is ended before its children are created, which is fine: a child only
    needs the parent's immutable span context, which stays valid after the span
    ends. Any event that is unreachable from the root (an orphan whose
    ``parent_id`` is absent from the trace, or a cycle) is attached under the root
    span so no event is silently dropped.

    ``span_environment`` / ``span_source`` are the per-span fallbacks recorded on
    every span of this trace when the export spans more than one value and they
    could not be placed on the Resource (see :func:`_resolve_resource_context`).
    """

    spans_by_event_id: dict[str, Any] = {}

    children_by_parent: dict[str | None, list[TraceEvent]] = {}
    for event in trace.events:
        children_by_parent.setdefault(event.parent_id, []).append(event)

    root_event = trace.root
    spans_by_event_id[root_event.id] = _emit_span(
        root_event,
        parent_span=None,
        tracer=tracer,
        api=api,
        environment=span_environment,
        source=span_source,
    )

    queue: deque[TraceEvent] = deque([root_event])
    while queue:
        parent_event = queue.popleft()
        for child in children_by_parent.get(parent_event.id, ()):
            if child.id == parent_event.id or child.id in spans_by_event_id:
                continue
            spans_by_event_id[child.id] = _emit_span(
                child,
                parent_span=spans_by_event_id[parent_event.id],
                tracer=tracer,
                api=api,
                environment=span_environment,
                source=span_source,
            )
            queue.append(child)

    root_span = spans_by_event_id[root_event.id]
    for event in trace.events:
        if event.id not in spans_by_event_id:
            spans_by_event_id[event.id] = _emit_span(
                event,
                parent_span=root_span,
                tracer=tracer,
                api=api,
                environment=span_environment,
                source=span_source,
            )

    return len(spans_by_event_id)


def _emit_span(
    event: TraceEvent,
    *,
    parent_span: Any | None,
    tracer: Any,
    api: _OtelApi,
    environment: str | None = None,
    source: str | None = None,
) -> Any:
    """Create, populate, and end a single OpenTelemetry span for ``event``."""

    context = api.trace.set_span_in_context(parent_span) if parent_span is not None else None
    kind = api.SpanKind.CLIENT if event.type in _CLIENT_SPAN_TYPES else api.SpanKind.INTERNAL
    span = tracer.start_span(
        event.name,
        context=context,
        kind=kind,
        start_time=_iso_to_unix_nano(event.start_time),
    )
    for key, value in _event_attributes(event, environment=environment, source=source).items():
        span.set_attribute(key, value)
    if event.status == "error":
        span.set_status(api.Status(api.StatusCode.ERROR, event.error or None))
    else:
        span.set_status(api.Status(api.StatusCode.OK))
    span.end(end_time=_iso_to_unix_nano(event.end_time))
    return span


def _event_attributes(
    event: TraceEvent,
    *,
    environment: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Map a Bir event to OpenTelemetry span attributes.

    GenAI semantic conventions are used where they exist (``gen_ai.request.model``,
    ``gen_ai.usage.input_tokens`` / ``gen_ai.usage.output_tokens``, and
    ``gen_ai.system`` for a generation whose provider was recorded); the remaining
    Bir-specific fields use ``bir.*`` keys that mirror the JSONL field names so a
    span can be correlated back to its local event. Only scalar values are emitted,
    so every value is a valid OpenTelemetry attribute; ``input`` and ``output``
    payloads are intentionally not forwarded.

    ``environment`` and ``source`` are the per-span fallbacks (``bir.environment`` /
    ``bir.source``) added only when a mixed export could not place them on the
    Resource; they default to ``None`` and add nothing, keeping a single-value or
    no-value export byte-for-byte identical to before.
    """

    attributes: dict[str, Any] = {
        "bir.event_type": event.type,
        "bir.event_id": event.id,
        "bir.trace_id": event.trace_id,
    }
    if event.parent_id is not None:
        attributes["bir.parent_id"] = event.parent_id
    if event.model is not None:
        attributes["gen_ai.request.model"] = event.model
    if event.type == "generation":
        system = _gen_ai_system(event)
        if system is not None:
            attributes["gen_ai.system"] = system
    if event.usage:
        input_tokens = event.usage.get("input_tokens")
        output_tokens = event.usage.get("output_tokens")
        total_tokens = event.usage.get("total_tokens")
        if input_tokens is not None:
            attributes["gen_ai.usage.input_tokens"] = input_tokens
        if output_tokens is not None:
            attributes["gen_ai.usage.output_tokens"] = output_tokens
        if total_tokens is not None:
            attributes["bir.usage.total_tokens"] = total_tokens
    if event.cost:
        for source_key, attribute_key in (
            ("input_cost", "bir.cost.input_cost"),
            ("output_cost", "bir.cost.output_cost"),
            ("total_cost", "bir.cost.total_cost"),
        ):
            value = event.cost.get(source_key)
            if value is not None:
                attributes[attribute_key] = value
    if event.currency is not None:
        attributes["bir.currency"] = event.currency
    if event.value is not None:
        attributes["bir.score.value"] = event.value
    if environment is not None:
        attributes["bir.environment"] = environment
    if source is not None:
        attributes["bir.source"] = source
    return attributes


def _gen_ai_system(event: TraceEvent) -> str | None:
    """Return the GenAI provider for a generation event when one was recorded.

    The provider is not a first-class Bir field, so it is read conservatively from
    metadata an integration already populated — ``gen_ai_system`` (Pydantic AI's
    OTel-native value) or ``provider`` (LiteLLM's resolved provider) — and never
    guessed from the model string. When neither is present the attribute is omitted
    so a wrong value is never emitted.
    """

    metadata = event.metadata or {}
    for key in ("gen_ai_system", "provider"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _trace_environment(trace: LoadedTrace) -> str | None:
    """Return the deployment environment recorded on a trace root, if any.

    Bir records it under ``metadata.service.environment`` on the trace root (see
    ``configure(environment=...)``); a trace without it contributes nothing.
    """

    service = trace.root.metadata.get("service")
    if isinstance(service, Mapping):
        environment = service.get("environment")
        if isinstance(environment, str) and environment:
            return environment
    return None


def _trace_source(trace: LoadedTrace) -> str | None:
    """Return the trace source recorded on a trace root, if any (``metadata.source``)."""

    source = trace.root.metadata.get("source")
    if isinstance(source, str) and source:
        return source
    return None


def _resolve_resource_context(loaded: list[LoadedTrace], environment: str | None) -> _ResourceContext:
    """Decide the Resource-level environment/source and any per-span fallbacks.

    ``deployment.environment`` and ``bir.source`` describe the whole exported
    service, so they belong on the OpenTelemetry ``Resource`` and are only set when
    a single value applies to the export. An explicit ``environment`` argument
    always wins for the environment; otherwise the value is taken from the trace
    roots' ``metadata.service.environment`` when they all agree, and ``bir.source``
    from the roots' ``metadata.source`` the same way. When the roots disagree (more
    than one distinct value) and no explicit argument forces the choice, the
    Resource attribute is omitted and the per-trace value is recorded on each span
    instead; when nothing was recorded, nothing is added at all.
    """

    environments: list[str] = []
    sources: list[str] = []
    for trace in loaded:
        env = _trace_environment(trace)
        if env is not None and env not in environments:
            environments.append(env)
        src = _trace_source(trace)
        if src is not None and src not in sources:
            sources.append(src)

    explicit = environment or None
    if explicit is not None:
        resource_environment: str | None = explicit
        per_span_environment = False
    elif len(environments) == 1:
        resource_environment = environments[0]
        per_span_environment = False
    else:
        resource_environment = None
        per_span_environment = len(environments) > 1

    if len(sources) == 1:
        resource_source: str | None = sources[0]
        per_span_source = False
    else:
        resource_source = None
        per_span_source = len(sources) > 1

    return _ResourceContext(
        environment=resource_environment,
        source=resource_source,
        per_span_environment=per_span_environment,
        per_span_source=per_span_source,
    )


def _iso_to_unix_nano(timestamp: str) -> int:
    """Convert a stored ISO-8601 timestamp to integer nanoseconds since the epoch.

    Bir always records timezone-aware UTC timestamps, so ``datetime.timestamp()``
    yields the correct epoch seconds without any local-time ambiguity.
    """

    return int(round(datetime.fromisoformat(timestamp).timestamp() * 1_000_000_000))


def _resolve_traces(traces: LoadedTrace | Iterable[LoadedTrace] | str | Path) -> list[LoadedTrace]:
    """Normalize the ``traces`` argument into a list of :class:`~bir.LoadedTrace`."""

    if isinstance(traces, (str, Path)):
        return load_traces(traces)
    if isinstance(traces, LoadedTrace):
        return [traces]
    return list(traces)


@dataclass(frozen=True)
class _ResourceContext:
    """Resolved Resource-level environment/source plus any per-span fallbacks.

    ``environment`` and ``source`` are placed on the OpenTelemetry ``Resource`` when
    set; the ``per_span_*`` flags request that the per-trace value be recorded on
    each span instead (a mixed export whose value could not go on the Resource).
    """

    environment: str | None
    source: str | None
    per_span_environment: bool
    per_span_source: bool


@dataclass(frozen=True)
class _OtelApi:
    """The OpenTelemetry symbols the exporter needs, imported once and passed around."""

    trace: Any
    SpanKind: Any
    Status: Any
    StatusCode: Any
    TracerProvider: Any
    SimpleSpanProcessor: Any
    Resource: Any
    SERVICE_NAME: Any


def _import_otel_api() -> _OtelApi:
    """Import the OpenTelemetry SDK pieces, raising an actionable error if absent."""

    # ``type: ignore`` keeps pyright green when the opt-in ``otel`` extra is not
    # installed; the imports resolve normally once it is.
    try:
        from opentelemetry import trace as otel_trace  # type: ignore[import-not-found]
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource  # type: ignore[import-not-found]
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-not-found]
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # type: ignore[import-not-found]
        from opentelemetry.trace import SpanKind, Status, StatusCode  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised via patched import in tests
        raise ImportError(_INSTALL_HINT) from exc

    return _OtelApi(
        trace=otel_trace,
        SpanKind=SpanKind,
        Status=Status,
        StatusCode=StatusCode,
        TracerProvider=TracerProvider,
        SimpleSpanProcessor=SimpleSpanProcessor,
        Resource=Resource,
        SERVICE_NAME=SERVICE_NAME,
    )


def _build_default_exporter(endpoint: str | None, headers: Mapping[str, str] | None, timeout: float | None) -> Any:
    """Build the default OTLP/HTTP span exporter, raising an actionable error if absent."""

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
            OTLPSpanExporter,
        )
    except ImportError as exc:  # pragma: no cover - exercised via patched import in tests
        raise ImportError(_INSTALL_HINT) from exc

    kwargs: dict[str, Any] = {}
    if endpoint is not None:
        kwargs["endpoint"] = endpoint
    if headers is not None:
        kwargs["headers"] = dict(headers)
    if timeout is not None:
        kwargs["timeout"] = timeout
    return OTLPSpanExporter(**kwargs)
