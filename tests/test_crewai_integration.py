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
from bir.integrations.crewai import BirCrewAIHandler


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


class FakeEvent:
    """Mirrors a CrewAI event read by the handler via attribute access.

    Real CrewAI events are pydantic models exposing a ``type`` string plus
    event-specific fields; the handler reads them by name, so a permissive
    attribute bag matches the contract the fakes stand in for.
    """

    def __init__(self, type: str, **fields: Any) -> None:
        self.type = type
        for key, value in fields.items():
            setattr(self, key, value)


class FakeCrew:
    """Mirrors the ``source`` a crew-kickoff event is emitted with."""

    def __init__(self, id: str, name: str | None = None) -> None:
        self.id = id
        self.name = name


class FakeTask:
    def __init__(self, id: str, name: str | None = None) -> None:
        self.id = id
        self.name = name


class FakeAgent:
    def __init__(self, id: str, role: str) -> None:
        self.id = id
        self.role = role


class CrewAIIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_crew_run_maps_events_to_span_generation_and_tool(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            handler = BirCrewAIHandler()

            crew = FakeCrew("crew_1", name="Research crew")
            task = FakeTask("task_1", name="Summarize")
            agent = FakeAgent("agent_1", role="Researcher")

            handler.on_event(crew, FakeEvent("crew_kickoff_started", crew_name="Research crew"))
            handler.on_event(crew, FakeEvent("task_started", task=task))
            handler.on_event(crew, FakeEvent("agent_execution_started", agent=agent, task=task))

            handler.on_event(
                agent,
                FakeEvent(
                    "llm_call_started",
                    messages=[{"role": "user", "content": "Summarize Bir"}],
                    model="gpt-4o",
                    agent_role="Researcher",
                ),
            )
            handler.on_event(
                agent,
                FakeEvent(
                    "llm_call_completed",
                    response="Bir records local traces.",
                    usage={"prompt_tokens": 12, "completion_tokens": 4},
                ),
            )

            handler.on_event(
                agent,
                FakeEvent("tool_usage_started", tool_name="web_search", tool_args={"q": "Bir"}),
            )
            handler.on_event(
                agent,
                FakeEvent("tool_usage_finished", tool_name="web_search", output="result text"),
            )

            handler.on_event(crew, FakeEvent("agent_execution_completed", agent=agent, task=task, output="done"))
            handler.on_event(crew, FakeEvent("task_completed", task=task, output="summary"))
            handler.on_event(crew, FakeEvent("crew_kickoff_completed", crew_name="Research crew"))

            traces = load_traces()
            events = load_events()

            self.assertEqual(len(traces), 1)
            self.assertEqual(traces[0].name, "Research crew")
            self.assertEqual(traces[0].status, "success")
            self.assertEqual(
                sorted(event.type for event in events),
                ["generation", "span", "span", "tool_call", "trace"],
            )

            root = next(event for event in events if event.type == "trace")
            self.assertEqual(root.metadata["integration"], "crewai")

            task_span = next(event for event in events if event.type == "span" and event.name == "Summarize")
            self.assertEqual(task_span.parent_id, root.id)
            self.assertEqual(task_span.metadata["crewai_event"], "task_started")

            agent_span = next(event for event in events if event.type == "span" and event.name == "Researcher")
            self.assertEqual(agent_span.parent_id, task_span.id)
            self.assertEqual(agent_span.metadata["agent_role"], "Researcher")

            generation_event = next(event for event in events if event.type == "generation")
            self.assertEqual(generation_event.name, "crewai.llm_call")
            self.assertEqual(generation_event.model, "gpt-4o")
            self.assertEqual(
                generation_event.usage,
                {"input_tokens": 12, "output_tokens": 4, "total_tokens": 16},
            )
            self.assertEqual(generation_event.input, [{"role": "user", "content": "Summarize Bir"}])
            self.assertEqual(generation_event.output, "Bir records local traces.")
            # Nested under the agent span, which is nested under the task span.
            self.assertEqual(generation_event.parent_id, agent_span.id)

            tool_event = next(event for event in events if event.type == "tool_call")
            self.assertEqual(tool_event.name, "web_search")
            self.assertEqual(tool_event.input, {"q": "Bir"})
            self.assertEqual(tool_event.output, "result text")
            self.assertEqual(tool_event.parent_id, agent_span.id)

    def test_model_and_usage_read_from_response_object(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            handler = BirCrewAIHandler()

            crew = FakeCrew("crew_resp")
            handler.on_event(crew, FakeEvent("crew_kickoff_started"))
            # The started event carries no model; it is only known on completion.
            handler.on_event(crew, FakeEvent("llm_call_started", messages="Hi"))
            handler.on_event(
                crew,
                FakeEvent(
                    "llm_call_completed",
                    response={"model": "gpt-4o-mini", "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}},
                ),
            )
            handler.on_event(crew, FakeEvent("crew_kickoff_completed"))

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.model, "gpt-4o-mini")
            self.assertEqual(
                generation_event.usage,
                {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            )

    def test_failed_event_records_error_status_with_redaction(self) -> None:
        with temporary_workdir():
            handler = BirCrewAIHandler()

            crew = FakeCrew("crew_err")
            handler.on_event(crew, FakeEvent("crew_kickoff_started"))
            handler.on_event(crew, FakeEvent("tool_usage_started", tool_name="lookup"))
            handler.on_event(
                crew,
                FakeEvent("tool_usage_error", tool_name="lookup", error="tool failed api_key=sk-secret"),
            )
            handler.on_event(crew, FakeEvent("crew_kickoff_completed"))

            tool_event = next(event for event in load_events() if event.type == "tool_call")
            self.assertEqual(tool_event.status, "error")
            self.assertEqual(tool_event.error, "tool failed api_key=[redacted]")
            # The failed tool still closed cleanly, so the trace ends successfully.
            self.assertEqual(next(event for event in load_events() if event.type == "trace").status, "success")

    def test_failed_crew_marks_trace_error(self) -> None:
        with temporary_workdir():
            handler = BirCrewAIHandler()
            crew = FakeCrew("crew_fail")
            handler.on_event(crew, FakeEvent("crew_kickoff_started"))
            handler.on_event(crew, FakeEvent("crew_kickoff_failed", error="crew blew up"))

            trace_event = next(event for event in load_events() if event.type == "trace")
            self.assertEqual(trace_event.status, "error")
            self.assertEqual(trace_event.error, "crew blew up")

    def test_event_without_started_trace_gets_implicit_root(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            handler = BirCrewAIHandler()

            # No crew kickoff: an LLM event arriving without an active Bir trace
            # still attaches to an implicit root instead of raising.
            handler.on_event(None, FakeEvent("llm_call_started", messages="Hi", model="m"))
            handler.on_event(None, FakeEvent("llm_call_completed", response="Hello"))

            traces = load_traces()
            events = load_events()
            self.assertEqual(len(traces), 1)
            self.assertEqual([event.type for event in events], ["generation", "trace"])
            root = next(event for event in events if event.type == "trace")
            self.assertEqual(root.name, "crewai.crew")
            self.assertEqual(root.metadata["kind"], "implicit_root")
            self.assertEqual(next(event for event in events if event.type == "generation").parent_id, root.id)

    def test_capture_is_opt_in_and_handler_override_wins(self) -> None:
        with temporary_workdir():
            # Global capture stays off; the run records no input/output payloads.
            handler = BirCrewAIHandler()
            crew = FakeCrew("crew_off")
            handler.on_event(crew, FakeEvent("crew_kickoff_started"))
            handler.on_event(
                crew,
                FakeEvent("llm_call_started", messages=[{"role": "user", "content": "secret"}], model="m"),
            )
            handler.on_event(crew, FakeEvent("llm_call_completed", response="secret answer"))
            handler.on_event(crew, FakeEvent("crew_kickoff_completed"))

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertIsNone(generation_event.input)
            self.assertIsNone(generation_event.output)
            # Model is always recorded; only payloads are gated.
            self.assertEqual(generation_event.model, "m")

    def test_handler_capture_override_enables_payloads(self) -> None:
        with temporary_workdir():
            handler = BirCrewAIHandler(capture_inputs=True, capture_outputs=True)
            crew = FakeCrew("crew_on")
            handler.on_event(crew, FakeEvent("crew_kickoff_started"))
            handler.on_event(crew, FakeEvent("tool_usage_started", tool_name="t", tool_args={"a": 1}))
            handler.on_event(crew, FakeEvent("tool_usage_finished", tool_name="t", output="ok"))
            handler.on_event(crew, FakeEvent("crew_kickoff_completed"))

            tool_event = next(event for event in load_events() if event.type == "tool_call")
            self.assertEqual(tool_event.input, {"a": 1})
            self.assertEqual(tool_event.output, "ok")

    def test_sequential_runs_stay_isolated_by_id(self) -> None:
        with temporary_workdir():
            handler = BirCrewAIHandler()

            for suffix in ("a", "b"):
                crew = FakeCrew(f"crew_{suffix}", name=f"run-{suffix}")
                handler.on_event(crew, FakeEvent("crew_kickoff_started", crew_name=f"run-{suffix}"))
                handler.on_event(crew, FakeEvent("llm_call_started", model=f"model-{suffix}"))
                handler.on_event(crew, FakeEvent("llm_call_completed", response="ok"))
                handler.on_event(crew, FakeEvent("crew_kickoff_completed", crew_name=f"run-{suffix}"))

            traces = load_traces()
            self.assertEqual(len(traces), 2)
            self.assertEqual({trace.name for trace in traces}, {"run-a", "run-b"})
            for trace in traces:
                generation_event = next(event for event in trace.events if event.type == "generation")
                self.assertEqual(generation_event.trace_id, trace.root.id)
                self.assertEqual(generation_event.parent_id, trace.root.id)

    def test_concurrent_runs_in_threads_do_not_leak_context(self) -> None:
        with temporary_workdir():
            handler = BirCrewAIHandler()
            # Both runs are forced "open" simultaneously so a leaked contextvar or
            # a shared call stack would attach one run's call to the other's trace.
            barrier = threading.Barrier(2)

            def run(suffix: str) -> None:
                crew = FakeCrew(f"crew_{suffix}", name=f"run-{suffix}")
                handler.on_event(crew, FakeEvent("crew_kickoff_started", crew_name=f"run-{suffix}"))
                handler.on_event(crew, FakeEvent("llm_call_started", model=f"model-{suffix}"))
                barrier.wait(timeout=5)
                handler.on_event(crew, FakeEvent("llm_call_completed", response="ok"))
                handler.on_event(crew, FakeEvent("crew_kickoff_completed", crew_name=f"run-{suffix}"))

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
                expected_model = "model-a" if trace.name == "run-a" else "model-b"
                self.assertEqual(generation_event.model, expected_model)

    def test_nested_tool_and_llm_calls_pair_by_stack(self) -> None:
        with temporary_workdir():
            handler = BirCrewAIHandler()
            crew = FakeCrew("crew_nested")
            handler.on_event(crew, FakeEvent("crew_kickoff_started"))
            # A tool whose execution itself triggers an LLM call: the inner call
            # must close before the outer tool.
            handler.on_event(crew, FakeEvent("tool_usage_started", tool_name="outer"))
            handler.on_event(crew, FakeEvent("llm_call_started", model="m"))
            handler.on_event(crew, FakeEvent("llm_call_completed", response="inner"))
            handler.on_event(crew, FakeEvent("tool_usage_finished", tool_name="outer", output="outer-done"))
            handler.on_event(crew, FakeEvent("crew_kickoff_completed"))

            events = load_events()
            tool_event = next(event for event in events if event.type == "tool_call")
            generation_event = next(event for event in events if event.type == "generation")
            root = next(event for event in events if event.type == "trace")
            self.assertEqual(tool_event.parent_id, root.id)
            # The generation nests inside the still-open tool call.
            self.assertEqual(generation_event.parent_id, tool_event.id)

    def test_unrelated_events_are_ignored(self) -> None:
        with temporary_workdir():
            handler = BirCrewAIHandler()
            crew = FakeCrew("crew_noise")
            handler.on_event(crew, FakeEvent("crew_kickoff_started"))
            # A non-boundary event (and an event with no type) must not crash or
            # record anything.
            handler.on_event(crew, FakeEvent("llm_stream_chunk", chunk="x"))
            handler.on_event(crew, object())
            handler.on_event(crew, None)
            handler.on_event(crew, FakeEvent("crew_kickoff_completed"))

            self.assertEqual([event.type for event in load_events()], ["trace"])

    def test_import_and_construct_without_crewai_installed(self) -> None:
        # The integration must never import the CrewAI package.
        self.assertNotIn("crewai", sys.modules)
        handler = BirCrewAIHandler()
        self.assertIsNotNone(handler)
        self.assertNotIn("crewai", sys.modules)


if __name__ == "__main__":
    unittest.main()
