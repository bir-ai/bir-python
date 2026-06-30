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
import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bir import LoadedTrace, TraceEvent, load_traces
from bir.integrations.otel import (
    _event_attributes,
    _iso_to_unix_nano,
    _resolve_traces,
    export_traces_to_otlp,
)

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
        self.assertEqual(
            set(attributes), {"bir.event_type", "bir.event_id", "bir.trace_id"}
        )

    def test_resolve_traces_accepts_single_iterable_and_path(self) -> None:
        loaded = load_traces(str(FIXTURE))
        single = loaded[0]

        self.assertEqual(_resolve_traces(single), [single])
        self.assertEqual(_resolve_traces(loaded), loaded)
        self.assertEqual(_resolve_traces(iter(loaded)), loaded)
        # A path is loaded via ``load_traces``.
        self.assertEqual(len(_resolve_traces(str(FIXTURE))), 1)
        self.assertEqual(len(_resolve_traces(FIXTURE)), 1)


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
        self.assertEqual(
            by_name["retrieve_context"].parent.span_id, by_name["answer_question"].context.span_id
        )
        self.assertEqual(
            by_name["search_docs"].parent.span_id, by_name["retrieve_context"].context.span_id
        )
        self.assertEqual(
            by_name["local.llm"].parent.span_id, by_name["answer_question"].context.span_id
        )
        self.assertEqual(
            by_name["helpfulness"].parent.span_id, by_name["local.llm"].context.span_id
        )

        # Timestamps come straight from the ISO event times.
        self.assertEqual(
            by_name["answer_question"].start_time, _iso_to_unix_nano("2026-01-01T00:00:00+00:00")
        )
        self.assertEqual(
            by_name["answer_question"].end_time, _iso_to_unix_nano("2026-01-01T00:00:01+00:00")
        )

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

        exported = export_traces_to_otlp(
            [_loaded_trace(first), _loaded_trace(second)], span_exporter=exporter
        )

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

    def test_no_environment_source_or_provider_leaves_export_unchanged(self) -> None:
        exporter = _in_memory_exporter()
        trace = self._trace_with(trace_id="t")  # no environment, source, or provider

        export_traces_to_otlp(trace, service_name="svc", span_exporter=exporter)

        spans = exporter.get_finished_spans()
        resource = spans[0].resource.attributes
        self.assertEqual(resource["service.name"], "svc")
        self.assertNotIn("deployment.environment", resource)
        self.assertNotIn("bir.source", resource)
        for span in spans:
            self.assertNotIn("bir.environment", span.attributes)
            self.assertNotIn("bir.source", span.attributes)
            self.assertNotIn("gen_ai.system", span.attributes)


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


if __name__ == "__main__":
    unittest.main()
