"""Tests for the opt-in OpenTelemetry/OTLP exporter.

The exporter is the only Bir integration that imports a third-party package, so
these tests cover both halves of that contract: the pure Bir-to-attribute mapping
and span-tree shape run against an in-memory OpenTelemetry exporter (skipped when
opentelemetry is not installed), while the dependency-isolation and missing-extra
paths run regardless so the local-first guarantees stay enforced even where the
``otel`` extra is present in the dev environment.
"""

from __future__ import annotations

import builtins
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

import bir
from bir import LoadedTrace, TraceEvent, load_traces
from bir._sdk import _count_events_per_trace, _iter_trace_events, _iter_traces_from_events, _reset_config_for_tests
from bir.integrations.otel import (
    _event_attributes,
    _export_traces,
    _iso_to_unix_nano,
    _TraceReader,
    export_traces_to_otlp,
)


@contextmanager
def temporary_workdir() -> Iterator[Path]:
    previous = Path.cwd()
    with tempfile.TemporaryDirectory() as directory:
        workdir = Path(directory)
        os.chdir(workdir)
        try:
            yield workdir
        finally:
            os.chdir(previous)
            _reset_config_for_tests()


def _record_interleaved_traces(trace_path: Path) -> None:
    """Record traces that overlap, so grouping cannot rely on contiguity."""

    bir.configure(trace_path=str(trace_path))
    outer = [bir.trace(f"request-{index}") for index in range(3)]
    for context in outer:
        context.__enter__()
    for context in reversed(outer):
        with bir.span("step"):
            pass
        context.__exit__(None, None, None)


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "valid-events.jsonl"


def _module_available(name: str) -> bool:
    # ``find_spec`` raises ``ModuleNotFoundError`` (rather than returning ``None``)
    # when an intermediate parent package is missing, so the lookup is guarded to
    # keep this module importable — and the otel tests skippable — without the
    # ``otel`` extra installed.
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


_OTEL_AVAILABLE = _module_available("opentelemetry.sdk.trace")
_OTLP_HTTP_AVAILABLE = _module_available("opentelemetry.exporter.otlp.proto.http.trace_exporter")


def _in_memory_exporter() -> Any:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # type: ignore[import-not-found]
        InMemorySpanExporter,
    )

    return InMemorySpanExporter()


def _event(
    *,
    event_id: str,
    parent_id: str | None,
    name: str,
    event_type: str,
    start_time: str,
    end_time: str,
    status: str = "success",
    error: str | None = None,
    trace_id: str = "trace-1",
    metadata: dict[str, Any] | None = None,
    **extra: Any,
) -> TraceEvent:
    """Build a ``TraceEvent`` for the hand-constructed trace tests."""

    return TraceEvent(
        id=event_id,
        trace_id=trace_id,
        parent_id=parent_id,
        name=name,
        type=event_type,
        start_time=start_time,
        end_time=end_time,
        status=status,
        metadata=dict(metadata or {}),
        input=None,
        output=None,
        error=error,
        raw={},
        **extra,
    )


def _loaded_trace(root: TraceEvent, *children: TraceEvent) -> LoadedTrace:
    events = [root, *children]
    return LoadedTrace(
        id=root.trace_id,
        name=root.name,
        start_time=root.start_time,
        end_time=root.end_time,
        status=root.status,
        events=events,
        root=root,
    )


class IsolationTests(unittest.TestCase):
    """Importing Bir must never pull in opentelemetry, even with the extra installed."""

    def test_importing_bir_does_not_import_opentelemetry(self) -> None:
        code = (
            "import sys\n"
            "import bir\n"
            "assert 'opentelemetry' not in sys.modules, 'import bir imported opentelemetry'\n"
            "import bir.integrations\n"
            "assert 'opentelemetry' not in sys.modules, 'import bir.integrations imported opentelemetry'\n"
            "import bir.integrations.otel\n"
            "assert 'opentelemetry' not in sys.modules, 'import bir.integrations.otel imported opentelemetry'\n"
        )
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class MissingExtraTests(unittest.TestCase):
    """Calling the exporter without the extra raises a clear, actionable error."""

    def test_missing_opentelemetry_raises_actionable_importerror(self) -> None:
        trace = load_traces(str(FIXTURE))[0]
        real_import = builtins.__import__

        def blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "opentelemetry" or name.startswith("opentelemetry."):
                raise ImportError(f"No module named {name!r}")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", side_effect=blocked_import):
            with self.assertRaises(ImportError) as caught:
                export_traces_to_otlp([trace])

        message = str(caught.exception)
        self.assertIn("otel", message)
        self.assertIn("pip install", message)
        # The original import failure is chained for debuggability.
        self.assertIsInstance(caught.exception.__cause__, ImportError)


class PureMappingTests(unittest.TestCase):
    """The Bir-to-attribute and timestamp mapping needs no opentelemetry."""

    def test_iso_to_unix_nano_matches_epoch_nanoseconds(self) -> None:
        # 2026-01-01T00:00:00+00:00 is 1767225600 seconds since the epoch.
        self.assertEqual(_iso_to_unix_nano("2026-01-01T00:00:00+00:00"), 1767225600 * 1_000_000_000)
        # Fractional seconds survive the conversion to nanoseconds.
        self.assertEqual(
            _iso_to_unix_nano("2026-01-01T00:00:00.400000+00:00"),
            1767225600 * 1_000_000_000 + 400_000_000,
        )

    def test_generation_event_uses_genai_conventions_and_bir_attributes(self) -> None:
        event = _event(
            event_id="gen-1",
            parent_id="trace-1",
            name="local.llm",
            event_type="generation",
            start_time="2026-01-01T00:00:00+00:00",
            end_time="2026-01-01T00:00:01+00:00",
            model="demo-model",
            usage={"input_tokens": 12, "output_tokens": 24, "total_tokens": 36},
            cost={"input_cost": 0.000012, "output_cost": 0.000048, "total_cost": 0.00006},
            currency="USD",
        )

        attributes = _event_attributes(event)

        self.assertEqual(attributes["gen_ai.request.model"], "demo-model")
        self.assertEqual(attributes["gen_ai.usage.input_tokens"], 12)
        self.assertEqual(attributes["gen_ai.usage.output_tokens"], 24)
        self.assertEqual(attributes["bir.usage.total_tokens"], 36)
        self.assertEqual(attributes["bir.cost.input_cost"], 0.000012)
        self.assertEqual(attributes["bir.cost.output_cost"], 0.000048)
        self.assertEqual(attributes["bir.cost.total_cost"], 0.00006)
        self.assertEqual(attributes["bir.currency"], "USD")
        self.assertEqual(attributes["bir.event_type"], "generation")
        self.assertEqual(attributes["bir.event_id"], "gen-1")
        self.assertEqual(attributes["bir.trace_id"], "trace-1")
        self.assertEqual(attributes["bir.parent_id"], "trace-1")
        # Only scalar OpenTelemetry-safe values are emitted.
        for value in attributes.values():
            self.assertIsInstance(value, (str, int, float))

    def test_generation_event_records_gen_ai_system_from_provider(self) -> None:
        event = _event(
            event_id="gen-1",
            parent_id="trace-1",
            name="local.llm",
            event_type="generation",
            start_time="2026-01-01T00:00:00+00:00",
            end_time="2026-01-01T00:00:01+00:00",
            model="gpt-4o",
            metadata={"provider": "openai"},
        )

        attributes = _event_attributes(event)

        self.assertEqual(attributes["gen_ai.system"], "openai")
        self.assertEqual(attributes["gen_ai.provider.name"], "openai")

    def test_generation_event_prefers_gen_ai_system_metadata_over_provider(self) -> None:
        # Pydantic AI records the OTel-native ``gen_ai_system``; when both are
        # present it wins over LiteLLM's ``provider`` hint.
        event = _event(
            event_id="gen-1",
            parent_id="trace-1",
            name="local.llm",
            event_type="generation",
            start_time="2026-01-01T00:00:00+00:00",
            end_time="2026-01-01T00:00:01+00:00",
            model="claude-opus-4-8",
            metadata={"gen_ai_system": "anthropic", "provider": "litellm"},
        )

        attributes = _event_attributes(event)

        self.assertEqual(attributes["gen_ai.system"], "anthropic")
        self.assertEqual(attributes["gen_ai.provider.name"], "anthropic")

    def test_generation_event_omits_gen_ai_system_when_provider_absent(self) -> None:
        event = _event(
            event_id="gen-1",
            parent_id="trace-1",
            name="local.llm",
            event_type="generation",
            start_time="2026-01-01T00:00:00+00:00",
            end_time="2026-01-01T00:00:01+00:00",
            model="demo-model",
        )

        attributes = _event_attributes(event)

        # The provider is never guessed from the model string; omit when unknown.
        self.assertNotIn("gen_ai.system", attributes)
        self.assertNotIn("gen_ai.provider.name", attributes)

    def test_non_generation_event_omits_gen_ai_system(self) -> None:
        # ``gen_ai.system`` is a generation-only attribute; a non-generation event
        # that happens to carry provider metadata does not get it.
        event = _event(
            event_id="tool-1",
            parent_id="trace-1",
            name="search",
            event_type="tool_call",
            start_time="2026-01-01T00:00:00+00:00",
            end_time="2026-01-01T00:00:01+00:00",
            metadata={"provider": "openai"},
        )

        attributes = _event_attributes(event)

        self.assertNotIn("gen_ai.system", attributes)
        self.assertNotIn("gen_ai.provider.name", attributes)

    def test_per_span_environment_and_source_added_only_when_passed(self) -> None:
        event = _event(
            event_id="trace-1",
            parent_id=None,
            name="root",
            event_type="trace",
            start_time="2026-01-01T00:00:00+00:00",
            end_time="2026-01-01T00:00:01+00:00",
        )

        # Defaults add nothing, keeping a no-input export byte-for-byte identical.
        self.assertNotIn("bir.environment", _event_attributes(event))
        self.assertNotIn("bir.source", _event_attributes(event))

        attributes = _event_attributes(event, environment="staging", source="batch")
        self.assertEqual(attributes["bir.environment"], "staging")
        self.assertEqual(attributes["bir.source"], "batch")

    def test_score_event_records_value(self) -> None:
        event = _event(
            event_id="score-1",
            parent_id="gen-1",
            name="helpfulness",
            event_type="score",
            start_time="2026-01-01T00:00:00+00:00",
            end_time="2026-01-01T00:00:00+00:00",
            value=0.82,
        )

        attributes = _event_attributes(event)

        self.assertEqual(attributes["bir.score.value"], 0.82)
        self.assertEqual(attributes["bir.event_type"], "score")

    def test_minimal_event_omits_absent_optional_attributes(self) -> None:
        event = _event(
            event_id="trace-1",
            parent_id=None,
            name="root",
            event_type="trace",
            start_time="2026-01-01T00:00:00+00:00",
            end_time="2026-01-01T00:00:01+00:00",
        )

        attributes = _event_attributes(event)

        self.assertNotIn("bir.parent_id", attributes)
        self.assertNotIn("gen_ai.request.model", attributes)
        self.assertNotIn("bir.score.value", attributes)
        self.assertEqual(set(attributes), {"bir.event_type", "bir.event_id", "bir.trace_id"})

    def test_trace_reader_accepts_single_iterable_and_path(self) -> None:
        loaded = load_traces(str(FIXTURE))
        single = loaded[0]

        def read(traces: LoadedTrace | Iterable[LoadedTrace] | str | Path) -> list[LoadedTrace]:
            return list(_TraceReader(traces, include_rotated=False, on_invalid=None).traces())

        self.assertEqual(read(single), [single])
        self.assertEqual(read(loaded), loaded)
        self.assertEqual(read(iter(loaded)), loaded)
        # A path is streamed from the store rather than loaded whole.
        self.assertEqual(len(read(str(FIXTURE))), 1)
        self.assertEqual(len(read(FIXTURE)), 1)

    def test_trace_reader_reads_a_path_once_per_pass(self) -> None:
        reader = _TraceReader(str(FIXTURE), include_rotated=False, on_invalid=None)

        roots = list(reader.roots())
        traces = list(reader.traces())

        # Each pass re-reads the store, so neither consumes the other: nothing is
        # carried between them, which is what keeps the export bounded.
        self.assertEqual([root.id for root in roots], [trace.root.id for trace in traces])
        self.assertEqual([root.id for root in reader.roots()], [root.id for root in roots])


@unittest.skipUnless(_OTEL_AVAILABLE, "opentelemetry is not installed")
class SpanTreeTests(unittest.TestCase):
    """The exporter builds a correct parent/child OpenTelemetry span tree."""

    def test_fixture_trace_maps_to_span_tree(self) -> None:
        from opentelemetry.trace import SpanKind, StatusCode  # type: ignore[import-not-found]

        exporter = _in_memory_exporter()
        trace = load_traces(str(FIXTURE))[0]

        exported = export_traces_to_otlp(trace, service_name="rag-api", span_exporter=exporter)

        spans = exporter.get_finished_spans()
        self.assertEqual(exported, 5)
        self.assertEqual(len(spans), 5)

        by_name = {span.name: span for span in spans}
        self.assertEqual(
            set(by_name),
            {"answer_question", "retrieve_context", "search_docs", "local.llm", "helpfulness"},
        )

        # One Bir trace becomes exactly one OpenTelemetry trace.
        self.assertEqual(len({span.context.trace_id for span in spans}), 1)

        # Parent/child links follow Bir ``parent_id``.
        self.assertIsNone(by_name["answer_question"].parent)
        self.assertEqual(by_name["retrieve_context"].parent.span_id, by_name["answer_question"].context.span_id)
        self.assertEqual(by_name["search_docs"].parent.span_id, by_name["retrieve_context"].context.span_id)
        self.assertEqual(by_name["local.llm"].parent.span_id, by_name["answer_question"].context.span_id)
        self.assertEqual(by_name["helpfulness"].parent.span_id, by_name["local.llm"].context.span_id)

        # Timestamps come straight from the ISO event times.
        self.assertEqual(by_name["answer_question"].start_time, _iso_to_unix_nano("2026-01-01T00:00:00+00:00"))
        self.assertEqual(by_name["answer_question"].end_time, _iso_to_unix_nano("2026-01-01T00:00:01+00:00"))

        # Calls out to external systems are CLIENT spans; structure is INTERNAL.
        self.assertEqual(by_name["local.llm"].kind, SpanKind.CLIENT)
        self.assertEqual(by_name["search_docs"].kind, SpanKind.CLIENT)
        self.assertEqual(by_name["answer_question"].kind, SpanKind.INTERNAL)
        self.assertEqual(by_name["retrieve_context"].kind, SpanKind.INTERNAL)

        # All fixture events succeeded.
        for span in spans:
            self.assertEqual(span.status.status_code, StatusCode.OK)

        # GenAI + bir.* attributes on the generation span.
        generation = by_name["local.llm"]
        self.assertEqual(generation.attributes["gen_ai.request.model"], "demo-model")
        self.assertEqual(generation.attributes["gen_ai.usage.input_tokens"], 12)
        self.assertEqual(generation.attributes["gen_ai.usage.output_tokens"], 24)
        self.assertEqual(generation.attributes["bir.usage.total_tokens"], 36)
        self.assertEqual(generation.attributes["bir.cost.total_cost"], 0.00006)
        self.assertEqual(generation.attributes["bir.currency"], "USD")

        # Score value and trace correlation attribute.
        self.assertEqual(by_name["helpfulness"].attributes["bir.score.value"], 0.82)
        self.assertEqual(generation.attributes["bir.trace_id"], "trace-fixture-1")

        # service.name lands on the resource.
        self.assertEqual(spans[0].resource.attributes["service.name"], "rag-api")

    def test_error_event_maps_to_error_status_with_description(self) -> None:
        from opentelemetry.trace import StatusCode  # type: ignore[import-not-found]

        root = _event(
            event_id="t",
            parent_id=None,
            name="root",
            event_type="trace",
            start_time="2026-01-01T00:00:00+00:00",
            end_time="2026-01-01T00:00:01+00:00",
            status="error",
            error="root failed",
            trace_id="t",
        )
        generation = _event(
            event_id="g",
            parent_id="t",
            name="llm",
            event_type="generation",
            start_time="2026-01-01T00:00:00.100000+00:00",
            end_time="2026-01-01T00:00:00.200000+00:00",
            status="error",
            error="generation failed",
            trace_id="t",
            model="m",
        )
        exporter = _in_memory_exporter()

        export_traces_to_otlp(_loaded_trace(root, generation), span_exporter=exporter)

        spans = {span.name: span for span in exporter.get_finished_spans()}
        self.assertEqual(spans["llm"].status.status_code, StatusCode.ERROR)
        self.assertEqual(spans["llm"].status.description, "generation failed")
        self.assertEqual(spans["root"].status.status_code, StatusCode.ERROR)
        self.assertEqual(spans["root"].status.description, "root failed")

    def test_orphan_event_attaches_to_root(self) -> None:
        root = _event(
            event_id="t",
            parent_id=None,
            name="root",
            event_type="trace",
            start_time="2026-01-01T00:00:00+00:00",
            end_time="2026-01-01T00:00:01+00:00",
            trace_id="t",
        )
        orphan = _event(
            event_id="o",
            parent_id="does-not-exist",
            name="orphan",
            event_type="span",
            start_time="2026-01-01T00:00:00.100000+00:00",
            end_time="2026-01-01T00:00:00.200000+00:00",
            trace_id="t",
        )
        exporter = _in_memory_exporter()

        exported = export_traces_to_otlp(_loaded_trace(root, orphan), span_exporter=exporter)

        spans = {span.name: span for span in exporter.get_finished_spans()}
        self.assertEqual(exported, 2)
        self.assertEqual(spans["orphan"].parent.span_id, spans["root"].context.span_id)

    def test_multiple_traces_export_as_separate_otel_traces(self) -> None:
        first = _event(
            event_id="a",
            parent_id=None,
            name="trace-a",
            event_type="trace",
            start_time="2026-01-01T00:00:00+00:00",
            end_time="2026-01-01T00:00:01+00:00",
            trace_id="a",
        )
        second = _event(
            event_id="b",
            parent_id=None,
            name="trace-b",
            event_type="trace",
            start_time="2026-01-01T00:00:02+00:00",
            end_time="2026-01-01T00:00:03+00:00",
            trace_id="b",
        )
        exporter = _in_memory_exporter()

        exported = export_traces_to_otlp([_loaded_trace(first), _loaded_trace(second)], span_exporter=exporter)

        spans = exporter.get_finished_spans()
        self.assertEqual(exported, 2)
        self.assertEqual(len({span.context.trace_id for span in spans}), 2)

    def test_accepts_path_and_leaves_local_jsonl_untouched(self) -> None:
        before = FIXTURE.read_bytes()
        exporter = _in_memory_exporter()

        exported = export_traces_to_otlp(str(FIXTURE), span_exporter=exporter)

        self.assertEqual(exported, 5)
        self.assertEqual(len(exporter.get_finished_spans()), 5)
        self.assertEqual(FIXTURE.read_bytes(), before)


@unittest.skipUnless(_OTEL_AVAILABLE, "opentelemetry is not installed")
class ResourceAttributeTests(unittest.TestCase):
    """Environment/source reach the Resource (or spans), and gen_ai.system the spans."""

    def _trace_with(
        self,
        *,
        trace_id: str,
        environment: str | None = None,
        source: str | None = None,
        provider: str | None = None,
    ) -> LoadedTrace:
        metadata: dict[str, Any] = {}
        if environment is not None:
            metadata["service"] = {"name": "svc", "environment": environment}
        if source is not None:
            metadata["source"] = source
        root = _event(
            event_id=trace_id,
            parent_id=None,
            name=f"root-{trace_id}",
            event_type="trace",
            start_time="2026-01-01T00:00:00+00:00",
            end_time="2026-01-01T00:00:01+00:00",
            trace_id=trace_id,
            metadata=metadata,
        )
        generation = _event(
            event_id=f"{trace_id}-gen",
            parent_id=trace_id,
            name=f"llm-{trace_id}",
            event_type="generation",
            start_time="2026-01-01T00:00:00.100000+00:00",
            end_time="2026-01-01T00:00:00.200000+00:00",
            trace_id=trace_id,
            model="demo-model",
            metadata={"provider": provider} if provider is not None else None,
        )
        return _loaded_trace(root, generation)

    def test_resource_records_environment_and_source(self) -> None:
        exporter = _in_memory_exporter()
        trace = self._trace_with(trace_id="t", environment="prod", source="checkout")

        export_traces_to_otlp(trace, span_exporter=exporter)

        spans = exporter.get_finished_spans()
        resource = spans[0].resource.attributes
        self.assertEqual(resource["deployment.environment"], "prod")
        self.assertEqual(resource["deployment.environment.name"], "prod")
        self.assertEqual(resource["bir.source"], "checkout")
        # A single value lives on the Resource and is not duplicated onto each span.
        for span in spans:
            self.assertNotIn("bir.environment", span.attributes)
            self.assertNotIn("bir.source", span.attributes)

    def test_explicit_environment_overrides_recorded(self) -> None:
        exporter = _in_memory_exporter()
        trace = self._trace_with(trace_id="t", environment="prod", source="checkout")

        export_traces_to_otlp(trace, environment="canary", span_exporter=exporter)

        resource = exporter.get_finished_spans()[0].resource.attributes
        self.assertEqual(resource["deployment.environment"], "canary")
        self.assertEqual(resource["deployment.environment.name"], "canary")
        # An explicit environment does not disturb the derived source.
        self.assertEqual(resource["bir.source"], "checkout")

    def test_conflicting_environments_fall_back_to_per_span(self) -> None:
        exporter = _in_memory_exporter()
        prod = self._trace_with(trace_id="a", environment="prod")
        staging = self._trace_with(trace_id="b", environment="staging")

        export_traces_to_otlp([prod, staging], span_exporter=exporter)

        spans = {span.name: span for span in exporter.get_finished_spans()}
        # The two traces disagree, so the Resource attribute is omitted...
        self.assertNotIn("deployment.environment", spans["root-a"].resource.attributes)
        self.assertNotIn("deployment.environment.name", spans["root-a"].resource.attributes)
        # ...and the per-trace value is recorded on every span of each trace instead.
        self.assertEqual(spans["root-a"].attributes["bir.environment"], "prod")
        self.assertEqual(spans["llm-a"].attributes["bir.environment"], "prod")
        self.assertEqual(spans["root-b"].attributes["bir.environment"], "staging")
        self.assertEqual(spans["llm-b"].attributes["bir.environment"], "staging")

    def test_generation_span_carries_gen_ai_system_when_provider_recorded(self) -> None:
        exporter = _in_memory_exporter()
        trace = self._trace_with(trace_id="t", provider="openai")

        export_traces_to_otlp(trace, span_exporter=exporter)

        spans = {span.name: span for span in exporter.get_finished_spans()}
        self.assertEqual(spans["llm-t"].attributes["gen_ai.system"], "openai")
        self.assertEqual(spans["llm-t"].attributes["gen_ai.provider.name"], "openai")

    def test_no_environment_source_or_provider_leaves_export_unchanged(self) -> None:
        exporter = _in_memory_exporter()
        trace = self._trace_with(trace_id="t")  # no environment, source, or provider

        export_traces_to_otlp(trace, service_name="svc", span_exporter=exporter)

        spans = exporter.get_finished_spans()
        resource = spans[0].resource.attributes
        self.assertEqual(resource["service.name"], "svc")
        self.assertNotIn("deployment.environment", resource)
        self.assertNotIn("deployment.environment.name", resource)
        self.assertNotIn("bir.source", resource)
        for span in spans:
            self.assertNotIn("bir.environment", span.attributes)
            self.assertNotIn("bir.source", span.attributes)
            self.assertNotIn("gen_ai.system", span.attributes)
            self.assertNotIn("gen_ai.provider.name", span.attributes)


@unittest.skipUnless(_OTLP_HTTP_AVAILABLE, "opentelemetry OTLP/HTTP exporter is not installed")
class DefaultExporterTests(unittest.TestCase):
    """Without an injected exporter, the default OTLP/HTTP exporter is wired up."""

    def test_default_exporter_receives_endpoint_headers_and_timeout(self) -> None:
        from opentelemetry.sdk.trace.export import SpanExportResult  # type: ignore[import-not-found]

        constructed: dict[str, Any] = {}

        class FakeOTLPSpanExporter:
            def __init__(self, **kwargs: Any) -> None:
                constructed.update(kwargs)

            def export(self, spans: Any) -> Any:
                return SpanExportResult.SUCCESS

            def shutdown(self) -> None:
                return None

            def force_flush(self, timeout_millis: int = 30_000) -> bool:
                return True

        trace = load_traces(str(FIXTURE))[0]
        target = "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"
        with mock.patch(target, FakeOTLPSpanExporter):
            exported = export_traces_to_otlp(
                trace,
                endpoint="http://collector.example:4318/v1/traces",
                headers={"x-api-key": "secret"},
                timeout=5.0,
            )

        self.assertEqual(exported, 5)
        self.assertEqual(constructed["endpoint"], "http://collector.example:4318/v1/traces")
        self.assertEqual(constructed["headers"], {"x-api-key": "secret"})
        self.assertEqual(constructed["timeout"], 5.0)


class _RecordingExporter:
    """A span exporter that accepts a fixed number of spans and refuses the rest."""

    def __init__(self, *, accept: int | None = None, raises: bool = False) -> None:
        from opentelemetry.sdk.trace.export import SpanExportResult  # type: ignore[import-not-found]

        self._results = SpanExportResult
        self._accept = accept
        self._raises = raises
        self.accepted = 0
        self.shutdown_calls = 0

    def export(self, spans: Any) -> Any:
        if self._raises:
            raise RuntimeError("collector unreachable")
        if self._accept is not None and self.accepted >= self._accept:
            return self._results.FAILURE
        self.accepted += len(spans)
        return self._results.SUCCESS

    def shutdown(self) -> None:
        self.shutdown_calls += 1


@unittest.skipUnless(_OTEL_AVAILABLE, "opentelemetry is not installed")
class ExportDeliveryReportingTests(unittest.TestCase):
    """An export reports what arrived, not what it built.

    ``SimpleSpanProcessor`` discards the ``SpanExportResult`` of every span it
    exports, so a run against an endpoint that accepted nothing still counted
    each span it had constructed and returned that as the number exported. The
    count now comes from the exporter's own answers, and an export that did not
    deliver everything raises -- exporting is invoked for its effect, so a
    failure to produce that effect is the operation failing.
    """

    def setUp(self) -> None:
        _reset_config_for_tests()

    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_a_rejected_export_raises_and_names_the_endpoint(self) -> None:
        exporter = _RecordingExporter(accept=0)
        with self.assertRaises(RuntimeError) as caught:
            export_traces_to_otlp(
                load_traces(str(FIXTURE))[0],
                endpoint="http://collector.example:4318/v1/traces",
                span_exporter=exporter,
            )
        self.assertEqual(
            str(caught.exception),
            "bir could not export traces to http://collector.example:4318/v1/traces: none of 5 span(s) were accepted",
        )

    def test_a_partial_export_reports_how_much_arrived(self) -> None:
        exporter = _RecordingExporter(accept=2)
        with self.assertRaisesRegex(RuntimeError, r"only 2 of 5 span\(s\) were accepted"):
            export_traces_to_otlp(load_traces(str(FIXTURE))[0], span_exporter=exporter)

    def test_an_exporter_that_returns_no_result_is_not_called_a_failure(self) -> None:
        # OpenTelemetry asks ``export`` to return a ``SpanExportResult``, and an
        # exporter that returns something else is not evidence that anything went
        # wrong. Only a stated failure is treated as one, so a working pipeline
        # behind a loosely written exporter is never reported as broken.
        class SilentExporter:
            def export(self, spans: Any) -> None:
                return None

            def shutdown(self) -> None:
                return None

        self.assertEqual(export_traces_to_otlp(load_traces(str(FIXTURE))[0], span_exporter=SilentExporter()), 5)

    def test_an_exporter_that_raises_is_a_failed_export(self) -> None:
        # SimpleSpanProcessor catches and logs the exception, so the run reaches
        # the end either way; nothing was accepted, and that is what decides.
        exporter = _RecordingExporter(raises=True)
        with self.assertRaisesRegex(RuntimeError, r"none of 5 span\(s\) were accepted"):
            with self.assertLogs("opentelemetry.sdk.trace.export", level="ERROR"):
                export_traces_to_otlp(load_traces(str(FIXTURE))[0], span_exporter=exporter)

    def test_a_failure_with_no_endpoint_still_says_what_happened(self) -> None:
        with self.assertRaisesRegex(RuntimeError, r"^bir could not export traces: none of 5"):
            export_traces_to_otlp(load_traces(str(FIXTURE))[0], span_exporter=_RecordingExporter(accept=0))

    def test_a_delivered_export_returns_what_the_exporter_accepted(self) -> None:
        exporter = _RecordingExporter()
        exported = export_traces_to_otlp(load_traces(str(FIXTURE))[0], span_exporter=exporter)
        self.assertEqual(exported, 5)
        self.assertEqual(exporter.accepted, 5)

    def test_an_injected_exporter_is_still_left_open_after_a_failure(self) -> None:
        # The ownership rule does not change because the export failed: a caller's
        # exporter stays theirs to reuse or shut down.
        exporter = _RecordingExporter(accept=0)
        with self.assertRaises(RuntimeError):
            export_traces_to_otlp(load_traces(str(FIXTURE))[0], span_exporter=exporter)
        self.assertEqual(exporter.shutdown_calls, 0)

    def test_cli_reports_a_failed_export_on_stderr_with_a_non_zero_exit(self) -> None:
        from bir.cli import main

        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            trace_path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
            failing = _RecordingExporter(accept=0)

            def fake_export(*args: Any, **kwargs: Any) -> Any:
                kwargs["span_exporter"] = failing
                return _export_traces(*args, **kwargs)

            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch("bir.integrations.otel._export_traces", fake_export):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = main(
                        [
                            "export-otel",
                            "--path",
                            str(trace_path),
                            "--endpoint",
                            "http://collector.example:4318/v1/traces",
                            "--json",
                        ]
                    )

            self.assertEqual(code, 1)
            # Nothing success-shaped on stdout: a pipeline reading the JSON
            # contract must not find a span count for spans that never arrived.
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("bir: bir could not export traces", stderr.getvalue())
            self.assertIn("none of 5 span(s) were accepted", stderr.getvalue())

    def test_cli_reports_a_delivered_export_as_before(self) -> None:
        from bir.cli import main

        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            trace_path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
            accepting = _RecordingExporter()

            def fake_export(*args: Any, **kwargs: Any) -> Any:
                kwargs["span_exporter"] = accepting
                return _export_traces(*args, **kwargs)

            stdout = io.StringIO()
            with mock.patch("bir.integrations.otel._export_traces", fake_export):
                with contextlib.redirect_stdout(stdout):
                    code = main(
                        [
                            "export-otel",
                            "--path",
                            str(trace_path),
                            "--endpoint",
                            "http://collector.example:4318/v1/traces",
                            "--json",
                        ]
                    )

            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["spans"], 5)


if __name__ == "__main__":
    unittest.main()


class StreamingExportTests(unittest.TestCase):
    """The export reads the store in passes instead of holding it.

    ``export-otel`` was the last read path that loaded whole traces, so a large
    store cost memory proportional to itself rather than to one trace. The two
    passes have to produce exactly what the materializing one did — the same
    traces, with the same events inside them — so these cases pin the equivalence
    and the streaming, separately.
    """

    def test_grouping_a_stream_yields_the_same_traces_as_loading(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            _record_interleaved_traces(trace_path)

            loaded = load_traces(str(trace_path))
            streamed = list(
                _iter_traces_from_events(
                    _iter_trace_events(str(trace_path)),
                    event_counts=_count_events_per_trace(_iter_trace_events(str(trace_path))),
                )
            )

            self.assertEqual(
                sorted((trace.id, [event.id for event in trace.events]) for trace in streamed),
                sorted((trace.id, [event.id for event in trace.events]) for trace in loaded),
            )

    def test_a_trace_whose_root_comes_first_is_still_whole(self) -> None:
        # The SDK writes a trace's root last, but the shared fixture writes it
        # first and the exporter accepts any JSONL path, so completeness cannot
        # be decided by where the root sits.
        counts = _count_events_per_trace(_iter_trace_events(str(FIXTURE)))
        streamed = list(_iter_traces_from_events(_iter_trace_events(str(FIXTURE)), event_counts=counts))

        self.assertEqual(len(streamed), 1)
        self.assertEqual(len(streamed[0].events), 5)

    def test_a_trace_is_released_before_the_store_is_exhausted(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            _record_interleaved_traces(trace_path)
            events = list(_iter_trace_events(str(trace_path)))
            counts = _count_events_per_trace(events)
            consumed = 0

            def counting() -> Any:
                nonlocal consumed
                for event in events:
                    consumed += 1
                    yield event

            first = next(iter(_iter_traces_from_events(counting(), event_counts=counts)))

            # The whole point: a trace is handed over as soon as it is complete,
            # so the reader is still short of the end of the store.
            self.assertIsNotNone(first)
            self.assertLess(consumed, len(events))

    def test_events_whose_trace_has_no_root_are_dropped(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            _record_interleaved_traces(trace_path)
            # Drop one trace's root, leaving its children with nowhere to hang.
            kept = [
                line
                for line in trace_path.read_text(encoding="utf-8").splitlines()
                if not (json.loads(line)["type"] == "trace" and json.loads(line)["name"] == "request-0")
            ]
            trace_path.write_text("\n".join(kept) + "\n", encoding="utf-8")

            counts = _count_events_per_trace(_iter_trace_events(str(trace_path)))
            streamed = list(_iter_traces_from_events(_iter_trace_events(str(trace_path)), event_counts=counts))

            # Same as the materializing loader: a trace is its root, so events
            # without one are not a trace.
            self.assertEqual(
                sorted(trace.name for trace in streamed),
                sorted(trace.name for trace in load_traces(str(trace_path))),
            )
            self.assertNotIn("request-0", {trace.name for trace in streamed})


@unittest.skipUnless(_OTEL_AVAILABLE, "opentelemetry is not installed")
class StreamingExportEquivalenceTests(unittest.TestCase):
    """Streaming a path exports exactly what handing over loaded traces does."""

    def test_a_path_and_the_loaded_traces_export_the_same_spans(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            _record_interleaved_traces(trace_path)

            from_path = _in_memory_exporter()
            from_list = _in_memory_exporter()
            path_counts = _export_traces(str(trace_path), span_exporter=from_path)
            list_spans = export_traces_to_otlp(load_traces(str(trace_path)), span_exporter=from_list)

            self.assertEqual(path_counts.spans, list_spans)
            self.assertEqual(
                sorted(span.name for span in from_path.get_finished_spans()),
                sorted(span.name for span in from_list.get_finished_spans()),
            )

            # Attributes have to survive the change too, not just the span count.
            def attribute_sets(exporter: Any) -> list[tuple[tuple[str, Any], ...]]:
                return sorted(
                    tuple(sorted(dict(span.attributes or {}).items())) for span in exporter.get_finished_spans()
                )

            self.assertEqual(attribute_sets(from_path), attribute_sets(from_list))

    def test_the_trace_count_is_reported_without_holding_the_traces(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            _record_interleaved_traces(trace_path)

            counts = _export_traces(str(trace_path), span_exporter=_in_memory_exporter())

            self.assertEqual(counts.traces, len(load_traces(str(trace_path))))

    def test_a_mixed_environment_export_still_falls_back_per_span(self) -> None:
        # The Resource pass reads only roots now; a store whose roots disagree
        # must still push the value onto each span rather than the Resource.
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            bir.configure(trace_path=str(trace_path), environment="staging")
            with bir.trace("first"):
                pass
            bir.configure(environment="prod")
            with bir.trace("second"):
                pass

            exporter = _in_memory_exporter()
            _export_traces(str(trace_path), span_exporter=exporter)

            spans = exporter.get_finished_spans()
            self.assertEqual(
                {dict(span.attributes or {}).get("bir.environment") for span in spans},
                {"staging", "prod"},
            )


_SEMCONV_AVAILABLE = _module_available("opentelemetry.semconv._incubating.attributes.gen_ai_attributes")


@unittest.skipUnless(_SEMCONV_AVAILABLE, "opentelemetry semantic conventions are not installed")
class SemanticConventionSpellingTests(unittest.TestCase):
    """The exported names are checked against the conventions, not against a memory.

    The exporter writes attribute names as literals, deliberately: the ``otel``
    extra is optional and the constants that define these names live in an
    ``_incubating`` package. That leaves nothing connecting the literals to the
    conventions they claim to follow, which is how two of them came to be
    superseded spellings without anything failing. These cases make the claim
    checkable: they read the constants out of the installed
    ``opentelemetry-semantic-conventions`` and compare.

    Measured against 0.65b0, installed with ``opentelemetry-sdk`` 1.44.0.
    """

    def test_the_current_spellings_are_what_the_conventions_call_them(self) -> None:
        from opentelemetry.semconv._incubating.attributes import (  # type: ignore[import-not-found]
            gen_ai_attributes,
        )
        from opentelemetry.semconv.attributes import (  # type: ignore[import-not-found]
            deployment_attributes,
        )

        self.assertEqual(deployment_attributes.DEPLOYMENT_ENVIRONMENT_NAME, "deployment.environment.name")
        self.assertEqual(gen_ai_attributes.GEN_AI_PROVIDER_NAME, "gen_ai.provider.name")

    def test_the_unrenamed_genai_spellings_still_match(self) -> None:
        # These three were re-checked at the same time as the two renames. Their
        # deprecation notes record a move to the GenAI conventions repository
        # rather than a new spelling, so the names Bir writes are still current.
        from opentelemetry.semconv._incubating.attributes import (  # type: ignore[import-not-found]
            gen_ai_attributes,
        )

        self.assertEqual(gen_ai_attributes.GEN_AI_REQUEST_MODEL, "gen_ai.request.model")
        self.assertEqual(gen_ai_attributes.GEN_AI_USAGE_INPUT_TOKENS, "gen_ai.usage.input_tokens")
        self.assertEqual(gen_ai_attributes.GEN_AI_USAGE_OUTPUT_TOKENS, "gen_ai.usage.output_tokens")

    def test_the_superseded_spellings_are_still_the_ones_being_carried(self) -> None:
        # The transition ends when these constants go: at that point nothing in
        # the supported range reads the old names, and the second half of each
        # pair can be deleted from the exporter along with this case.
        from opentelemetry.semconv._incubating.attributes import (  # type: ignore[import-not-found]
            deployment_attributes,
            gen_ai_attributes,
        )

        self.assertEqual(
            getattr(deployment_attributes, "DEPLOYMENT_ENVIRONMENT", None),
            "deployment.environment",
            "the superseded deployment spelling is gone from the conventions; the transition can end",
        )
        self.assertEqual(
            getattr(gen_ai_attributes, "GEN_AI_SYSTEM", None),
            "gen_ai.system",
            "the superseded provider spelling is gone from the conventions; the transition can end",
        )
