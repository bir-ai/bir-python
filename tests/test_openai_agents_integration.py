from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from bir import configure, load_events, load_traces
from bir._sdk import _reset_config_for_tests
from bir.integrations.openai_agents import BirAgentsTracingProcessor


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


class FakeTrace:
    """Mirrors the Agents SDK ``Trace`` surface the processor reads."""

    def __init__(self, trace_id: str, name: str = "agent workflow", group_id: str | None = None) -> None:
        self.trace_id = trace_id
        self.name = name
        self.group_id = group_id


class FakeSpanData:
    """Mirrors an Agents SDK ``SpanData`` subclass via attribute access.

    Real subclasses expose a ``type`` property plus operation-specific
    attributes; the processor reads them by name, so a permissive attribute bag
    matches the contract the fake objects stand in for.
    """

    def __init__(self, type: str, **fields: object) -> None:
        self.type = type
        for key, value in fields.items():
            setattr(self, key, value)


class FakeResponse:
    """Mirrors the OpenAI Responses ``Response`` carried by a response span."""

    def __init__(self, *, id: str, model: str, usage: dict[str, object], payload: dict[str, object]) -> None:
        self.id = id
        self.model = model
        self.usage = usage
        self._payload = payload

    def model_dump(self) -> dict[str, object]:
        return dict(self._payload)


class FakeSpan:
    """Mirrors the Agents SDK ``Span`` surface the processor reads."""

    def __init__(
        self,
        span_id: str,
        span_data: Any,
        *,
        trace_id: str = "trace_1",
        parent_id: str | None = None,
        error: Any = None,
    ) -> None:
        self.span_id = span_id
        self.trace_id = trace_id
        self.parent_id: str | None = parent_id
        # ``Span[Any].span_data`` is generic over the span-data subclass, so the
        # SDK itself types this ``Any``; the fakes mutate it after creation the
        # way the SDK populates ``output``/``response`` by span end.
        self.span_data: Any = span_data
        self.error: Any = error
        self.started_at: str | None = None
        self.ended_at: str | None = None


class OpenAIAgentsIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_agent_run_maps_spans_to_generation_tool_and_span_events(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            processor = BirAgentsTracingProcessor()

            processor.on_trace_start(FakeTrace("trace_1", name="Joke workflow"))

            agent_span = FakeSpan(
                "span_agent",
                FakeSpanData("agent", name="Assistant", tools=["get_weather"], handoffs=[], output_type="str"),
                parent_id=None,
            )
            processor.on_span_start(agent_span)

            generation_span = FakeSpan(
                "span_gen",
                FakeSpanData(
                    "generation",
                    input=[{"role": "user", "content": "Tell a joke"}],
                    model="gpt-4o",
                    usage={"input_tokens": 10, "output_tokens": 5},
                ),
                parent_id="span_agent",
            )
            processor.on_span_start(generation_span)
            generation_span.span_data.output = [{"role": "assistant", "content": "A joke."}]
            processor.on_span_end(generation_span)

            tool_span = FakeSpan(
                "span_fn",
                FakeSpanData("function", name="get_weather", input='{"city": "SF"}'),
                parent_id="span_agent",
            )
            processor.on_span_start(tool_span)
            tool_span.span_data.output = "sunny"
            processor.on_span_end(tool_span)

            processor.on_span_end(agent_span)
            processor.on_trace_end(FakeTrace("trace_1", name="Joke workflow"))

            traces = load_traces()
            events = load_events()

            self.assertEqual(len(traces), 1)
            self.assertEqual(traces[0].name, "Joke workflow")
            self.assertEqual(traces[0].status, "success")
            self.assertEqual(
                sorted(event.type for event in events),
                ["generation", "span", "tool_call", "trace"],
            )

            root = next(event for event in events if event.type == "trace")
            self.assertEqual(root.metadata["integration"], "openai_agents")
            self.assertEqual(root.metadata["agents_trace_id"], "trace_1")

            agent_event = next(event for event in events if event.type == "span")
            self.assertEqual(agent_event.name, "Assistant")
            self.assertEqual(agent_event.metadata["agents_type"], "agent")
            self.assertEqual(agent_event.metadata["tools"], ["get_weather"])
            self.assertEqual(agent_event.metadata["output_type"], "str")
            self.assertEqual(agent_event.parent_id, root.id)

            generation_event = next(event for event in events if event.type == "generation")
            self.assertEqual(generation_event.name, "openai_agents.generation")
            self.assertEqual(generation_event.model, "gpt-4o")
            self.assertEqual(
                generation_event.usage,
                {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            )
            self.assertEqual(generation_event.input, {"input": [{"role": "user", "content": "Tell a joke"}]})
            self.assertEqual(generation_event.output, [{"role": "assistant", "content": "A joke."}])
            self.assertEqual(generation_event.metadata["agents_type"], "generation")
            self.assertEqual(generation_event.metadata["parent_id"], "span_agent")
            # Nested under the agent span, which is nested under the trace root.
            self.assertEqual(generation_event.parent_id, agent_event.id)

            tool_event = next(event for event in events if event.type == "tool_call")
            self.assertEqual(tool_event.name, "get_weather")
            self.assertEqual(tool_event.input, '{"city": "SF"}')
            self.assertEqual(tool_event.output, "sunny")
            self.assertEqual(tool_event.metadata["agents_type"], "function")
            self.assertEqual(tool_event.parent_id, agent_event.id)

    def test_response_span_reads_model_and_usage_from_response_object(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            processor = BirAgentsTracingProcessor()

            processor.on_trace_start(FakeTrace("trace_resp"))
            response_span = FakeSpan(
                "span_resp",
                FakeSpanData("response", input="What is Bir?", response=None, usage=None),
            )
            # The model, usage, and response are only populated by span end.
            processor.on_span_start(response_span)
            response_span.span_data.response = FakeResponse(
                id="resp_1",
                model="gpt-4o-mini",
                usage={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
                payload={"id": "resp_1", "output_text": "Bir records local traces."},
            )
            processor.on_span_end(response_span)
            processor.on_trace_end(FakeTrace("trace_resp"))

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "openai_agents.response")
            self.assertEqual(generation_event.model, "gpt-4o-mini")
            self.assertEqual(
                generation_event.usage,
                {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
            )
            self.assertEqual(generation_event.input, {"input": "What is Bir?"})
            self.assertEqual(
                generation_event.output,
                {"id": "resp_1", "output_text": "Bir records local traces."},
            )

    def test_mcp_tools_span_maps_to_tool_call(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            processor = BirAgentsTracingProcessor()

            processor.on_trace_start(FakeTrace("trace_mcp"))
            mcp_span = FakeSpan("span_mcp", FakeSpanData("mcp_tools", server="weather-mcp", result=None))
            processor.on_span_start(mcp_span)
            mcp_span.span_data.result = ["get_weather", "get_forecast"]
            processor.on_span_end(mcp_span)
            processor.on_trace_end(FakeTrace("trace_mcp"))

            tool_event = next(event for event in load_events() if event.type == "tool_call")
            self.assertEqual(tool_event.name, "openai_agents.mcp_tools")
            self.assertEqual(tool_event.input, {"server": "weather-mcp"})
            self.assertEqual(tool_event.output, ["get_weather", "get_forecast"])
            self.assertEqual(tool_event.metadata["agents_type"], "mcp_tools")

    def test_span_error_records_error_status_with_redaction(self) -> None:
        with temporary_workdir():
            processor = BirAgentsTracingProcessor()

            processor.on_trace_start(FakeTrace("trace_err"))
            failing_span = FakeSpan(
                "span_fn",
                FakeSpanData("function", name="get_weather"),
                error={"message": "tool failed api_key=sk-secret", "data": {"detail": "x"}},
            )
            processor.on_span_start(failing_span)
            processor.on_span_end(failing_span)
            processor.on_trace_end(FakeTrace("trace_err"))

            tool_event = next(event for event in load_events() if event.type == "tool_call")
            self.assertEqual(tool_event.status, "error")
            self.assertEqual(tool_event.error, "tool failed api_key=[redacted]")
            # The failed span still closed cleanly, so the trace ends successfully.
            self.assertEqual(next(event for event in load_events() if event.type == "trace").status, "success")

    def test_unknown_span_type_falls_back_to_span(self) -> None:
        with temporary_workdir():
            processor = BirAgentsTracingProcessor()

            processor.on_trace_start(FakeTrace("trace_other"))
            handoff_span = FakeSpan(
                "span_handoff",
                FakeSpanData("handoff", from_agent="Triage", to_agent="Spanish"),
            )
            processor.on_span_start(handoff_span)
            processor.on_span_end(handoff_span)
            # A span carrying no span_data at all still maps to a structural span.
            bare_span = FakeSpan("span_bare", None)
            processor.on_span_start(bare_span)
            processor.on_span_end(bare_span)
            processor.on_trace_end(FakeTrace("trace_other"))

            spans = [event for event in load_events() if event.type == "span"]
            self.assertEqual(len(spans), 2)
            handoff_event = next(event for event in spans if event.name == "openai_agents.handoff")
            self.assertEqual(handoff_event.metadata["from_agent"], "Triage")
            self.assertEqual(handoff_event.metadata["to_agent"], "Spanish")
            self.assertTrue(any(event.name == "openai_agents.span" for event in spans))

    def test_span_without_started_trace_gets_implicit_root(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            processor = BirAgentsTracingProcessor()

            # No on_trace_start: a span arriving without an active Bir trace still
            # attaches to an implicit root instead of raising.
            orphan_span = FakeSpan("span_gen", FakeSpanData("generation", model="m"), trace_id="trace_orphan")
            processor.on_span_start(orphan_span)
            processor.on_span_end(orphan_span)

            traces = load_traces()
            events = load_events()
            self.assertEqual(len(traces), 1)
            self.assertEqual([event.type for event in events], ["generation", "trace"])
            root = next(event for event in events if event.type == "trace")
            self.assertEqual(root.name, "openai_agents.trace")
            self.assertEqual(root.metadata["kind"], "implicit_root")
            self.assertEqual(root.metadata["agents_trace_id"], "trace_orphan")
            generation_event = next(event for event in events if event.type == "generation")
            self.assertEqual(generation_event.parent_id, root.id)

    def test_capture_is_opt_in_and_handler_override_wins(self) -> None:
        with temporary_workdir():
            # Global capture stays off; the run records no input/output payloads.
            processor = BirAgentsTracingProcessor()
            processor.on_trace_start(FakeTrace("trace_off"))
            off_span = FakeSpan(
                "span_gen",
                FakeSpanData("generation", input=[{"role": "user", "content": "secret prompt"}], model="m"),
            )
            processor.on_span_start(off_span)
            off_span.span_data.output = "secret answer"
            processor.on_span_end(off_span)
            processor.on_trace_end(FakeTrace("trace_off"))

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertIsNone(generation_event.input)
            self.assertIsNone(generation_event.output)
            # Model and usage are always recorded; only payloads are gated.
            self.assertEqual(generation_event.model, "m")

    def test_handler_capture_override_enables_payloads(self) -> None:
        with temporary_workdir():
            processor = BirAgentsTracingProcessor(capture_inputs=True, capture_outputs=True)
            processor.on_trace_start(FakeTrace("trace_on"))
            on_span = FakeSpan(
                "span_gen",
                FakeSpanData("generation", input=[{"role": "user", "content": "hi"}], model="m"),
            )
            processor.on_span_start(on_span)
            on_span.span_data.output = "hello"
            processor.on_span_end(on_span)
            processor.on_trace_end(FakeTrace("trace_on"))

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.input, {"input": [{"role": "user", "content": "hi"}]})
            self.assertEqual(generation_event.output, "hello")

    def test_sequential_runs_stay_isolated_by_id(self) -> None:
        with temporary_workdir():
            processor = BirAgentsTracingProcessor()

            for suffix in ("a", "b"):
                processor.on_trace_start(FakeTrace(f"trace_{suffix}", name=f"run-{suffix}"))
                gen_span = FakeSpan(
                    f"span_gen_{suffix}",
                    FakeSpanData("generation", model=f"model-{suffix}"),
                    trace_id=f"trace_{suffix}",
                )
                processor.on_span_start(gen_span)
                processor.on_span_end(gen_span)
                processor.on_trace_end(FakeTrace(f"trace_{suffix}", name=f"run-{suffix}"))

            traces = load_traces()
            self.assertEqual(len(traces), 2)
            self.assertEqual({trace.name for trace in traces}, {"run-a", "run-b"})
            for trace in traces:
                # Each run's generation is parented to its own root, never the other's.
                generation_event = next(event for event in trace.events if event.type == "generation")
                self.assertEqual(generation_event.trace_id, trace.root.id)
                self.assertEqual(generation_event.parent_id, trace.root.id)

    def test_concurrent_runs_in_threads_do_not_leak_context(self) -> None:
        with temporary_workdir():
            processor = BirAgentsTracingProcessor()
            # Both runs are forced to be "open" simultaneously so a leaked
            # contextvar would attach one run's span to the other's trace.
            barrier = threading.Barrier(2)

            def run(suffix: str) -> None:
                processor.on_trace_start(FakeTrace(f"trace_{suffix}", name=f"run-{suffix}"))
                gen_span = FakeSpan(
                    f"span_gen_{suffix}",
                    FakeSpanData("generation", model=f"model-{suffix}"),
                    trace_id=f"trace_{suffix}",
                )
                processor.on_span_start(gen_span)
                barrier.wait(timeout=5)
                processor.on_span_end(gen_span)
                processor.on_trace_end(FakeTrace(f"trace_{suffix}", name=f"run-{suffix}"))

            threads = [threading.Thread(target=run, args=(suffix,)) for suffix in ("a", "b")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            traces = load_traces()
            self.assertEqual(len(traces), 2)
            for trace in traces:
                generation_event = next(event for event in trace.events if event.type == "generation")
                self.assertEqual(generation_event.trace_id, trace.root.id)
                self.assertEqual(generation_event.parent_id, trace.root.id)
                # The model recorded matches the run's own trace, proving no leak.
                expected_model = "model-a" if trace.name == "run-a" else "model-b"
                self.assertEqual(generation_event.model, expected_model)

    def test_import_and_construct_without_agents_sdk_installed(self) -> None:
        # The integration must never import the Agents SDK.
        self.assertNotIn("agents", sys.modules)
        processor = BirAgentsTracingProcessor()
        # Lifecycle no-ops satisfy the processor interface without buffered state.
        self.assertIsNone(processor.shutdown())
        self.assertIsNone(processor.force_flush())
        self.assertNotIn("agents", sys.modules)


if __name__ == "__main__":
    unittest.main()
