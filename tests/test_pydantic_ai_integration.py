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
from bir.integrations.pydantic_ai import BirPydanticAIHandler


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


class FakeSpanContext:
    """Mirrors the OpenTelemetry ``SpanContext`` the handler reads ids from."""

    def __init__(self, span_id: int, trace_id: int) -> None:
        self.span_id = span_id
        self.trace_id = trace_id


class StatusCode:
    """Stand-in for the OpenTelemetry ``StatusCode`` enum member (``.name``)."""

    def __init__(self, name: str) -> None:
        self.name = name


class FakeStatus:
    def __init__(self, status_code: Any, description: str | None = None) -> None:
        self.status_code = status_code
        self.description = description


class FakeEvent:
    """Mirrors an OpenTelemetry span event (e.g. the recorded ``exception``)."""

    def __init__(self, name: str, attributes: dict[str, Any]) -> None:
        self.name = name
        self.attributes = attributes


class FakeSpan:
    """Mirrors the OpenTelemetry ``ReadableSpan`` surface the handler reads.

    Pydantic AI's instrumentation emits these via the OTel SDK; attributes are
    populated over the span's lifetime, so the fakes mutate ``attributes`` and
    ``status`` between ``on_start`` and ``on_end`` the way a live span does.
    """

    def __init__(
        self,
        name: str,
        span_id: int,
        *,
        attributes: dict[str, Any] | None = None,
        trace_id: int = 0xAA,
        parent_id: int | None = None,
        status: Any = None,
        events: list[Any] | None = None,
    ) -> None:
        self.name = name
        self.attributes: dict[str, Any] = attributes if attributes is not None else {}
        self.context = FakeSpanContext(span_id, trace_id)
        self.parent = FakeSpanContext(parent_id, trace_id) if parent_id is not None else None
        self.status = status
        self.events = events if events is not None else []


class PydanticAIIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_agent_run_maps_spans_to_generation_and_tool_events(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            handler = BirPydanticAIHandler()

            agent_span = FakeSpan(
                "invoke_agent weather_agent",
                span_id=0x01,
                attributes={
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": "weather_agent",
                },
            )
            handler.on_start(agent_span)

            chat_span = FakeSpan(
                "chat gpt-4o",
                span_id=0x02,
                parent_id=0x01,
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.system": "openai",
                    "gen_ai.request.model": "gpt-4o",
                    "gen_ai.input.messages": [{"role": "user", "content": "weather in SF?"}],
                },
            )
            handler.on_start(chat_span)
            # Usage and the assistant output are only known once the request returns.
            chat_span.attributes["gen_ai.usage.input_tokens"] = 11
            chat_span.attributes["gen_ai.usage.output_tokens"] = 7
            chat_span.attributes["gen_ai.output.messages"] = [{"role": "assistant", "content": "sunny"}]
            handler.on_end(chat_span)

            tool_span = FakeSpan(
                "execute_tool get_weather",
                span_id=0x03,
                parent_id=0x01,
                attributes={
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": "get_weather",
                    "gen_ai.tool.call.id": "call_1",
                    "gen_ai.tool.call.arguments": {"city": "SF"},
                },
            )
            handler.on_start(tool_span)
            tool_span.attributes["gen_ai.tool.call.result"] = "sunny"
            handler.on_end(tool_span)

            handler.on_end(agent_span)

            traces = load_traces()
            events = load_events()

            self.assertEqual(len(traces), 1)
            self.assertEqual(traces[0].name, "weather_agent")
            self.assertEqual(traces[0].status, "success")
            self.assertEqual(
                sorted(event.type for event in events),
                ["generation", "tool_call", "trace"],
            )

            root = next(event for event in events if event.type == "trace")
            self.assertEqual(root.metadata["integration"], "pydantic_ai")
            self.assertEqual(root.metadata["gen_ai_operation"], "invoke_agent")
            self.assertEqual(root.metadata["otel_span_id"], format(0x01, "016x"))

            generation_event = next(event for event in events if event.type == "generation")
            self.assertEqual(generation_event.name, "chat gpt-4o")
            self.assertEqual(generation_event.model, "gpt-4o")
            self.assertEqual(
                generation_event.usage,
                {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
            )
            self.assertEqual(generation_event.input, [{"role": "user", "content": "weather in SF?"}])
            self.assertEqual(generation_event.output, [{"role": "assistant", "content": "sunny"}])
            self.assertEqual(generation_event.metadata["gen_ai_system"], "openai")
            # Nested under the agent-run trace root.
            self.assertEqual(generation_event.parent_id, root.id)

            tool_event = next(event for event in events if event.type == "tool_call")
            self.assertEqual(tool_event.name, "get_weather")
            self.assertEqual(tool_event.input, {"city": "SF"})
            self.assertEqual(tool_event.output, "sunny")
            self.assertEqual(tool_event.metadata["gen_ai_operation"], "execute_tool")
            self.assertEqual(tool_event.metadata["gen_ai_tool_call_id"], "call_1")
            self.assertEqual(tool_event.parent_id, root.id)

    def test_response_model_is_filled_from_span_end(self) -> None:
        with temporary_workdir():
            handler = BirPydanticAIHandler()
            handler.on_start(FakeSpan("agent run", span_id=0x10))

            # Request model unknown at start; the response model arrives at end.
            chat_span = FakeSpan(
                "chat",
                span_id=0x11,
                parent_id=0x10,
                attributes={"gen_ai.operation.name": "chat"},
            )
            handler.on_start(chat_span)
            chat_span.attributes["gen_ai.response.model"] = "gpt-4o-mini"
            chat_span.attributes["gen_ai.usage.total_tokens"] = 9
            handler.on_end(chat_span)
            handler.on_end(FakeSpan("agent run", span_id=0x10))

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.model, "gpt-4o-mini")
            self.assertEqual(generation_event.usage, {"total_tokens": 9})

    def test_legacy_attribute_keys_and_span_name_classification(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            handler = BirPydanticAIHandler()

            # Older Pydantic AI: no operation attribute, legacy tool keys, and the
            # "running tool: {name}" / "agent run" span-name shapes.
            handler.on_start(FakeSpan("agent run", span_id=0x20))
            tool_span = FakeSpan(
                "running tool: lookup",
                span_id=0x21,
                parent_id=0x20,
                attributes={"tool_arguments": "{}"},
            )
            handler.on_start(tool_span)
            tool_span.attributes["tool_response"] = "ok"
            handler.on_end(tool_span)
            handler.on_end(FakeSpan("agent run", span_id=0x20))

            tool_event = next(event for event in load_events() if event.type == "tool_call")
            # Name derived from the span name when no gen_ai.tool.name attribute.
            self.assertEqual(tool_event.name, "lookup")
            self.assertEqual(tool_event.input, "{}")
            self.assertEqual(tool_event.output, "ok")

    def test_unknown_span_type_falls_back_to_span(self) -> None:
        with temporary_workdir():
            handler = BirPydanticAIHandler()
            handler.on_start(FakeSpan("agent run", span_id=0x30))
            other_span = FakeSpan(
                "preparing model request params",
                span_id=0x31,
                parent_id=0x30,
                attributes={"logfire.msg": "prep"},
            )
            handler.on_start(other_span)
            handler.on_end(other_span)
            handler.on_end(FakeSpan("agent run", span_id=0x30))

            span_event = next(event for event in load_events() if event.type == "span")
            self.assertEqual(span_event.name, "preparing model request params")
            self.assertEqual(span_event.metadata["integration"], "pydantic_ai")

    def test_span_error_via_exception_event_records_redacted_error(self) -> None:
        with temporary_workdir():
            handler = BirPydanticAIHandler()
            handler.on_start(FakeSpan("agent run", span_id=0x40))
            failing = FakeSpan(
                "execute_tool get_weather",
                span_id=0x41,
                parent_id=0x40,
                attributes={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "get_weather"},
                status=FakeStatus(StatusCode("ERROR")),
                events=[FakeEvent("exception", {"exception.message": "boom api_key=sk-secret"})],
            )
            handler.on_start(failing)
            handler.on_end(failing)
            handler.on_end(FakeSpan("agent run", span_id=0x40))

            tool_event = next(event for event in load_events() if event.type == "tool_call")
            self.assertEqual(tool_event.status, "error")
            self.assertEqual(tool_event.error, "boom api_key=[redacted]")
            # The failed span closed cleanly, so the trace itself ends successfully.
            self.assertEqual(next(event for event in load_events() if event.type == "trace").status, "success")

    def test_span_error_via_status_code_int(self) -> None:
        with temporary_workdir():
            handler = BirPydanticAIHandler()
            handler.on_start(FakeSpan("agent run", span_id=0x50))
            failing = FakeSpan(
                "chat",
                span_id=0x51,
                parent_id=0x50,
                attributes={"gen_ai.operation.name": "chat"},
                # OTel StatusCode.ERROR == 2, with no exception event.
                status=FakeStatus(2, description="rate limited"),
            )
            handler.on_start(failing)
            handler.on_end(failing)
            handler.on_end(FakeSpan("agent run", span_id=0x50))

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "rate limited")

    def test_orphan_span_without_agent_run_gets_implicit_root(self) -> None:
        with temporary_workdir():
            handler = BirPydanticAIHandler()
            orphan = FakeSpan(
                "chat",
                span_id=0x60,
                trace_id=0xBEEF,
                attributes={"gen_ai.operation.name": "chat", "gen_ai.request.model": "m"},
            )
            handler.on_start(orphan)
            handler.on_end(orphan)

            traces = load_traces()
            events = load_events()
            self.assertEqual(len(traces), 1)
            self.assertEqual([event.type for event in events], ["generation", "trace"])
            root = next(event for event in events if event.type == "trace")
            self.assertEqual(root.name, "pydantic_ai.agent_run")
            self.assertEqual(root.metadata["kind"], "implicit_root")
            self.assertEqual(root.metadata["otel_trace_id"], format(0xBEEF, "016x"))
            generation_event = next(event for event in events if event.type == "generation")
            self.assertEqual(generation_event.parent_id, root.id)

    def test_capture_is_opt_in_and_handler_override_wins(self) -> None:
        with temporary_workdir():
            # Global capture stays off; the run records no input/output payloads.
            handler = BirPydanticAIHandler()
            handler.on_start(FakeSpan("agent run", span_id=0x70))
            chat = FakeSpan(
                "chat",
                span_id=0x71,
                parent_id=0x70,
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": "m",
                    "gen_ai.input.messages": [{"role": "user", "content": "secret prompt"}],
                    "gen_ai.output.messages": "secret answer",
                },
            )
            handler.on_start(chat)
            handler.on_end(chat)
            handler.on_end(FakeSpan("agent run", span_id=0x70))

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertIsNone(generation_event.input)
            self.assertIsNone(generation_event.output)
            # Model is always recorded; only payloads are gated.
            self.assertEqual(generation_event.model, "m")

    def test_handler_capture_override_enables_payloads(self) -> None:
        with temporary_workdir():
            handler = BirPydanticAIHandler(capture_inputs=True, capture_outputs=True)
            handler.on_start(FakeSpan("agent run", span_id=0x80))
            chat = FakeSpan(
                "chat",
                span_id=0x81,
                parent_id=0x80,
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": "m",
                    "gen_ai.input.messages": [{"role": "user", "content": "hi"}],
                    "gen_ai.output.messages": "hello",
                },
            )
            handler.on_start(chat)
            handler.on_end(chat)
            handler.on_end(FakeSpan("agent run", span_id=0x80))

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.input, [{"role": "user", "content": "hi"}])
            self.assertEqual(generation_event.output, "hello")

    def test_nested_agent_run_becomes_a_span_not_a_second_root(self) -> None:
        with temporary_workdir():
            handler = BirPydanticAIHandler()
            handler.on_start(FakeSpan("invoke_agent outer", span_id=0x90, attributes={"gen_ai.operation.name": "invoke_agent"}))
            inner = FakeSpan(
                "invoke_agent inner",
                span_id=0x91,
                parent_id=0x90,
                attributes={"gen_ai.operation.name": "invoke_agent"},
            )
            handler.on_start(inner)
            handler.on_end(inner)
            handler.on_end(FakeSpan("invoke_agent outer", span_id=0x90))

            traces = load_traces()
            self.assertEqual(len(traces), 1)
            span_event = next(event for event in load_events() if event.type == "span")
            self.assertEqual(span_event.name, "invoke_agent inner")
            self.assertEqual(span_event.parent_id, traces[0].root.id)

    def test_sequential_runs_stay_isolated_by_id(self) -> None:
        with temporary_workdir():
            handler = BirPydanticAIHandler()

            for index, suffix in enumerate(("a", "b")):
                handler.on_start(FakeSpan(f"run-{suffix}", span_id=0x100 + index, attributes={"gen_ai.operation.name": "invoke_agent"}))
                chat = FakeSpan(
                    "chat",
                    span_id=0x200 + index,
                    parent_id=0x100 + index,
                    attributes={"gen_ai.operation.name": "chat", "gen_ai.request.model": f"model-{suffix}"},
                )
                handler.on_start(chat)
                handler.on_end(chat)
                handler.on_end(FakeSpan(f"run-{suffix}", span_id=0x100 + index))

            traces = load_traces()
            self.assertEqual(len(traces), 2)
            self.assertEqual({trace.name for trace in traces}, {"run-a", "run-b"})
            for trace in traces:
                generation_event = next(event for event in trace.events if event.type == "generation")
                self.assertEqual(generation_event.trace_id, trace.root.id)
                self.assertEqual(generation_event.parent_id, trace.root.id)

    def test_concurrent_runs_in_threads_do_not_leak_context(self) -> None:
        with temporary_workdir():
            handler = BirPydanticAIHandler()
            barrier = threading.Barrier(2)

            def run(index: int, suffix: str) -> None:
                handler.on_start(
                    FakeSpan(f"run-{suffix}", span_id=0x1000 + index, attributes={"gen_ai.operation.name": "invoke_agent"})
                )
                chat = FakeSpan(
                    "chat",
                    span_id=0x2000 + index,
                    parent_id=0x1000 + index,
                    attributes={"gen_ai.operation.name": "chat", "gen_ai.request.model": f"model-{suffix}"},
                )
                handler.on_start(chat)
                barrier.wait(timeout=5)
                handler.on_end(chat)
                handler.on_end(FakeSpan(f"run-{suffix}", span_id=0x1000 + index))

            threads = [
                threading.Thread(target=run, args=(index, suffix))
                for index, suffix in enumerate(("a", "b"))
            ]
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
                expected_model = "model-a" if trace.name == "run-a" else "model-b"
                self.assertEqual(generation_event.model, expected_model)

    def test_import_and_construct_without_pydantic_ai_installed(self) -> None:
        # The integration must never import Pydantic AI. (OpenTelemetry is not
        # checked: Bir's optional ``otel`` extra may load it into the same test
        # process, and this handler never imports it either way.)
        self.assertNotIn("pydantic_ai", sys.modules)
        handler = BirPydanticAIHandler()
        # Lifecycle no-ops satisfy the OTel SpanProcessor interface.
        self.assertIsNone(handler.shutdown())
        self.assertTrue(handler.force_flush())
        self.assertNotIn("pydantic_ai", sys.modules)


if __name__ == "__main__":
    unittest.main()
