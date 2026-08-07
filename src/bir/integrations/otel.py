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
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from bir import LoadedTrace, TraceEvent
from bir._sdk import _count_events_per_trace, _iter_trace_events, _iter_traces_from_events

# Event types recorded as ``CLIENT`` spans because they represent a call out to
# an external system (a model provider, a tool, a retrieval backend). Everything
# else (the trace root, structural spans, scores) is an ``INTERNAL`` span.
_CLIENT_SPAN_TYPES = frozenset({"generation", "tool_call", "retrieval"})

_INSTALL_HINT = (
    "OpenTelemetry export requires the optional 'otel' extra. Install it with:\n    pip install 'bir-sdk[otel]'"
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
    them, or a path (``str``/``Path``) to a trace file. Reading only touches the
    local JSONL; this function never writes to or alters it.

    A path is read in two streaming passes and only one trace is held at a time,
    so exporting a large store costs memory proportional to a trace rather than
    to the store. An already-loaded argument is exported as given — it was
    already read by whoever loaded it — so pass the path when the store is large.
    Traces read from a path arrive in completion order rather than sorted by
    start time, which is what lets them be released one at a time; each trace's
    own events are ordered exactly as :func:`bir.load_traces` orders them.

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

    Returns the number of spans the exporter accepted (one per Bir event across
    all traces when everything arrives).

    Raises :class:`RuntimeError` when the exporter did not accept every span,
    naming the endpoint and how much of the export arrived. Exporting is an
    operation invoked for its effect, like
    ``send_events`` and ``prune``, so a failure to produce that effect is raised
    rather than reported — a caller replaying a store into a collector has no
    other way to learn the data is not there. The count previously returned was
    the number of spans *built*, which stayed the same whether or not anything
    reached the endpoint.

    Raises :class:`ImportError` with an actionable message when the ``otel`` extra
    is not installed.
    """

    return _export_traces(
        traces,
        endpoint=endpoint,
        service_name=service_name,
        environment=environment,
        headers=headers,
        timeout=timeout,
        span_exporter=span_exporter,
    ).spans


@dataclass(frozen=True)
class _ExportCounts:
    """How much one export delivered: whole traces read, and spans accepted.

    :func:`export_traces_to_otlp` returns the span count alone, which is its
    documented contract. The CLI reports both, and counting the traces itself
    would mean a third pass over the store, so the internal entry point returns
    the pair. Both are only ever produced by an export that fully succeeded; a
    partial one raises instead, so neither number can overstate what arrived.
    """

    traces: int
    spans: int


def _export_traces(
    traces: LoadedTrace | Iterable[LoadedTrace] | str | Path,
    *,
    endpoint: str | None = None,
    service_name: str = "bir",
    environment: str | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float | None = None,
    span_exporter: Any | None = None,
    include_rotated: bool = False,
    on_invalid: Callable[[ValueError], None] | None = None,
) -> _ExportCounts:
    """Export in two passes, holding no more of the store than one trace at a time.

    ``include_rotated`` and ``on_invalid`` are the store-reading options the CLI
    needs and are meaningful only when ``traces`` is a path; an already-loaded
    argument was read by whoever loaded it.
    """

    reader = _TraceReader(traces, include_rotated=include_rotated, on_invalid=on_invalid)
    api = _import_otel_api()
    # First pass: only the trace roots, because that is all the Resource-level
    # environment and source are read from.
    context = _resolve_resource_context(reader.roots(), environment)

    owns_exporter = span_exporter is None
    delivered = _CountingExporter(
        span_exporter if span_exporter is not None else _build_default_exporter(endpoint, headers, timeout),
        failure=api.SpanExportResult.FAILURE,
    )
    exporter = delivered

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
        built = 0
        trace_count = 0
        # Second pass: each trace is built, exported, and released before the
        # next one is read.
        for trace in reader.traces():
            trace_count += 1
            # Per-span environment/source are only filled in when the export spans
            # more than one value, so a single-environment export keeps them on the
            # Resource and adds nothing to the spans themselves.
            span_environment = _trace_environment(trace.root) if context.per_span_environment else None
            span_source = _trace_source(trace.root) if context.per_span_source else None
            built += _export_one_trace(
                trace,
                tracer=tracer,
                api=api,
                span_environment=span_environment,
                span_source=span_source,
            )
        # Kept because it is how a processor is told the batch is over. Its
        # return value is not the delivery signal: ``SimpleSpanProcessor``
        # exports synchronously in ``on_end``, so by here there is nothing left
        # to flush and it answers ``True`` without consulting the exporter. What
        # the exporter accepted is counted below instead.
        provider.force_flush()
    finally:
        # Only tear down the provider (and, through it, the exporter) when we
        # built the exporter ourselves. An injected exporter belongs to the
        # caller, so we leave it open for them to reuse or shut down.
        if owns_exporter:
            provider.shutdown()

    # A span the exporter did not accept is a span that is not there. The caller
    # asked for the data to be somewhere else and it is not, which is a failed
    # operation rather than a recording mishap, so it raises: the same rule
    # ``bir send``, ``prune``, and the loaders follow.
    if delivered.accepted != built:
        raise RuntimeError(_export_failure_message(built, delivered.accepted, endpoint))
    return _ExportCounts(traces=trace_count, spans=delivered.accepted)


def _export_failure_message(built: int, accepted: int, endpoint: str | None) -> str:
    """Say how much of the export arrived, and where it was going."""

    destination = f" to {endpoint}" if endpoint else ""
    arrived = f"none of {built}" if accepted == 0 else f"only {accepted} of {built}"
    return f"bir could not export traces{destination}: {arrived} span(s) were accepted"


class _TraceReader:
    """The export's traces, readable once per pass.

    A path is read again for each pass, so nothing is carried between them: the
    Resource pass needs only trace roots, and the export pass groups events into
    traces as they complete. That is what keeps a large store from being held in
    memory whole.

    An already-loaded argument is a list, a single trace, or an arbitrary
    iterable. An iterable can only be walked once, so it is materialized — the
    caller chose that representation, and nothing here can read it twice
    otherwise.
    """

    def __init__(
        self,
        traces: LoadedTrace | Iterable[LoadedTrace] | str | Path,
        *,
        include_rotated: bool,
        on_invalid: Callable[[ValueError], None] | None,
    ) -> None:
        self._path: str | Path | None = None
        self._loaded: list[LoadedTrace] | None = None
        if isinstance(traces, (str, Path)):
            self._path = traces
        elif isinstance(traces, LoadedTrace):
            self._loaded = [traces]
        else:
            self._loaded = list(traces)
        self._include_rotated = include_rotated
        self._on_invalid = on_invalid
        self._event_counts: dict[str, int] | None = None

    def roots(self) -> Iterator[TraceEvent]:
        """Yield each trace's root event, counting every trace's events on the way.

        The counts are what lets :meth:`traces` release a trace as soon as it is
        complete, so the pass that has to read the whole store anyway pays for
        both.
        """

        if self._loaded is not None:
            for trace in self._loaded:
                yield trace.root
            return
        counts: dict[str, int] = {}
        for event in self._events():
            counts[event.trace_id] = counts.get(event.trace_id, 0) + 1
            if event.type == "trace" and event.id == event.trace_id:
                yield event
        self._event_counts = counts

    def traces(self) -> Iterator[LoadedTrace]:
        """Yield whole traces, one at a time."""

        if self._loaded is not None:
            yield from self._loaded
            return
        counts = self._event_counts
        if counts is None:
            # Reading the traces without the Resource pass having run first: take
            # the counting pass on its own rather than assume anything.
            counts = _count_events_per_trace(self._events())
        yield from _iter_traces_from_events(self._events(), event_counts=counts)

    def _events(self) -> Iterator[TraceEvent]:
        assert self._path is not None
        return _iter_trace_events(self._path, include_rotated=self._include_rotated, on_invalid=self._on_invalid)


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


def _trace_environment(root: TraceEvent) -> str | None:
    """Return the deployment environment recorded on a trace root, if any.

    Bir records it under ``metadata.service.environment`` on the trace root (see
    ``configure(environment=...)``); a trace without it contributes nothing.
    Taking the root rather than the whole trace lets the Resource pass read a
    store without grouping it into traces.
    """

    service = root.metadata.get("service")
    if isinstance(service, Mapping):
        environment = service.get("environment")
        if isinstance(environment, str) and environment:
            return environment
    return None


def _trace_source(root: TraceEvent) -> str | None:
    """Return the trace source recorded on a trace root, if any (``metadata.source``)."""

    source = root.metadata.get("source")
    if isinstance(source, str) and source:
        return source
    return None


def _resolve_resource_context(roots: Iterable[TraceEvent], environment: str | None) -> _ResourceContext:
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
    for root in roots:
        env = _trace_environment(root)
        if env is not None and env not in environments:
            environments.append(env)
        src = _trace_source(root)
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
    SpanExportResult: Any
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
        from opentelemetry.sdk.trace.export import (  # type: ignore[import-not-found]
            SimpleSpanProcessor,
            SpanExportResult,
        )
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
        SpanExportResult=SpanExportResult,
        Resource=Resource,
        SERVICE_NAME=SERVICE_NAME,
    )


class _CountingExporter:
    """Delegate to the real exporter and record what it actually accepted.

    Nothing downstream reported a failed delivery. ``SimpleSpanProcessor`` calls
    ``export`` once per span and discards the ``SpanExportResult`` it returns,
    and ``TracerProvider.force_flush`` returns a bool nobody read, so an export
    that reached no endpoint at all still counted every span it had built and
    reported them as exported. Counting the exporter's own answers is what makes
    the reported number mean *delivered*.

    Wrapping rather than replacing ``SimpleSpanProcessor`` keeps the failure
    handling the OpenTelemetry SDK already has: it logs a rejected batch and
    keeps going, and an injected exporter behaves for its caller exactly as it
    did before.

    Only ``export`` and ``shutdown`` are implemented, because those are the two
    a ``SimpleSpanProcessor`` calls on the exporter it holds. This is an internal
    wrapper around a caller's exporter, never one handed back out.

    A batch counts as delivered unless the exporter *said* it failed. The
    OpenTelemetry contract asks ``export`` to return a ``SpanExportResult``, but
    an exporter that returns something else is not evidence of a failure, and
    reporting one would be its own bug -- it would break a working pipeline over
    a technicality the OpenTelemetry SDK itself ignores. Only a stated failure,
    which is what the OTLP exporter returns once its retries are exhausted, is
    treated as one.
    """

    def __init__(self, exporter: Any, *, failure: Any) -> None:
        self._exporter = exporter
        self._failure = failure
        self.accepted = 0

    def export(self, spans: Any) -> Any:
        # A raising exporter is caught and logged by ``SimpleSpanProcessor``, so
        # the export continues either way. Nothing is counted here because the
        # increment never runs, which is the right answer: the spans are gone.
        result = self._exporter.export(spans)
        if result != self._failure:
            self.accepted += len(spans)
        return result

    def shutdown(self) -> None:
        self._exporter.shutdown()


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
