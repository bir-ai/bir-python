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

import bir
from bir import configure, load_events, load_traces
from bir._sdk import _reset_config_for_tests
from bir.integrations.autogen import BirAutoGenHandler


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


class FakeAgent:
    """Mirrors the AG2 agent passed as a callback's ``source`` (read by ``name``)."""

    def __init__(self, name: str) -> None:
        self.name = name


class FakeUsage:
    """OpenAI-shaped token usage carried on a chat-completion response."""

    def __init__(self, prompt_tokens: int, completion_tokens: int, total_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class FakeResponse:
    """OpenAI-shaped chat-completion response (``model`` + ``usage`` + ``model_dump``)."""

    def __init__(self, model: str, usage: FakeUsage | None = None, content: str = "ok") -> None:
        self.model = model
        self.usage = usage
        self.content = content

    def model_dump(self) -> dict[str, Any]:
        return {"model": self.model, "content": self.content}


def web_search(query: str) -> str:  # name is read from ``__name__`` by the handler
    return f"results for {query}"


class AutoGenIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_run_maps_completion_function_and_turns(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            handler = BirAutoGenHandler()

            assistant = FakeAgent("assistant")
            user_proxy = FakeAgent("user_proxy")

            self.assertIsInstance(handler.start(), str)

            handler.log_event(assistant, "received_message", sender="user_proxy")
            handler.log_chat_completion(
                source=assistant,
                request={"model": "gpt-4o", "messages": [{"role": "user", "content": "What is Bir?"}]},
                response=FakeResponse("gpt-4o", FakeUsage(12, 4, 16), content="Bir records local traces."),
                is_cached=False,
                cost=0.0012,
            )

            handler.log_event(user_proxy, "received_message", sender="assistant")
            handler.log_function_use(
                source=user_proxy,
                function=web_search,
                args={"query": "Bir"},
                returns="results for Bir",
            )

            handler.stop()

            traces = load_traces()
            events = load_events()

            self.assertEqual(len(traces), 1)
            self.assertEqual(traces[0].name, "autogen.run")
            self.assertEqual(traces[0].status, "success")
            self.assertEqual(
                sorted(event.type for event in events),
                ["generation", "span", "span", "tool_call", "trace"],
            )

            root = next(event for event in events if event.type == "trace")
            self.assertEqual(root.metadata["integration"], "autogen")

            assistant_turn = next(event for event in events if event.type == "span" and event.name == "assistant")
            self.assertEqual(assistant_turn.parent_id, root.id)
            self.assertEqual(assistant_turn.metadata["autogen_event"], "agent_turn")
            self.assertEqual(assistant_turn.metadata["agent"], "assistant")

            user_turn = next(event for event in events if event.type == "span" and event.name == "user_proxy")
            self.assertEqual(user_turn.parent_id, root.id)

            generation = next(event for event in events if event.type == "generation")
            self.assertEqual(generation.name, "autogen.chat_completion")
            self.assertEqual(generation.model, "gpt-4o")
            self.assertEqual(
                generation.usage,
                {"input_tokens": 12, "output_tokens": 4, "total_tokens": 16},
            )
            self.assertEqual(generation.cost, {"total_cost": 0.0012})
            self.assertEqual(generation.input, [{"role": "user", "content": "What is Bir?"}])
            self.assertEqual(generation.output, {"model": "gpt-4o", "content": "Bir records local traces."})
            self.assertEqual(generation.parent_id, assistant_turn.id)

            tool = next(event for event in events if event.type == "tool_call")
            self.assertEqual(tool.name, "web_search")
            self.assertEqual(tool.input, {"query": "Bir"})
            self.assertEqual(tool.output, "results for Bir")
            self.assertEqual(tool.parent_id, user_turn.id)

    def test_model_and_usage_read_from_dict_response(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            handler = BirAutoGenHandler()
            handler.start()
            handler.log_chat_completion(
                source="assistant",
                request={"messages": "Hi"},
                response={
                    "model": "gpt-4o-mini",
                    "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
                },
            )
            handler.stop()

            generation = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation.model, "gpt-4o-mini")
            self.assertEqual(
                generation.usage,
                {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            )
            self.assertIsNone(generation.cost)

    def test_failed_function_records_error_status_with_redaction(self) -> None:
        with temporary_workdir():
            handler = BirAutoGenHandler()
            handler.start()
            handler.log_function_use(
                source="executor",
                function="lookup",
                args={"q": "x"},
                returns={"is_error": True, "content": "tool failed api_key=sk-secret"},
            )
            handler.stop()

            tool = next(event for event in load_events() if event.type == "tool_call")
            self.assertEqual(tool.status, "error")
            self.assertEqual(tool.error, "tool failed api_key=[redacted]")
            # The failed tool still closed cleanly, so the run ends successfully.
            self.assertEqual(next(event for event in load_events() if event.type == "trace").status, "success")

    def test_exception_event_records_error_span_with_redaction(self) -> None:
        with temporary_workdir():
            handler = BirAutoGenHandler()
            handler.start()
            handler.log_event("assistant", "exception", message="boom api_key=sk-secret")
            handler.stop()

            error_span = next(event for event in load_events() if event.type == "span")
            self.assertEqual(error_span.name, "exception")
            self.assertEqual(error_span.status, "error")
            self.assertEqual(error_span.error, "boom api_key=[redacted]")

    def test_capture_is_opt_in_and_handler_override_wins(self) -> None:
        with temporary_workdir():
            # Global capture stays off: no input/output payloads, but model is kept.
            handler = BirAutoGenHandler()
            handler.start()
            handler.log_chat_completion(
                source="assistant",
                request={"messages": [{"role": "user", "content": "secret"}]},
                response=FakeResponse("m"),
            )
            handler.stop()

            generation = next(event for event in load_events() if event.type == "generation")
            self.assertIsNone(generation.input)
            self.assertIsNone(generation.output)
            self.assertEqual(generation.model, "m")

    def test_handler_capture_override_enables_payloads(self) -> None:
        with temporary_workdir():
            handler = BirAutoGenHandler(capture_inputs=True, capture_outputs=True)
            handler.start()
            handler.log_function_use(source="executor", function="t", args={"a": 1}, returns="ok")
            handler.stop()

            tool = next(event for event in load_events() if event.type == "tool_call")
            self.assertEqual(tool.input, {"a": 1})
            self.assertEqual(tool.output, "ok")

    def test_nested_run_becomes_span_subtree(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            handler = BirAutoGenHandler()
            with bir.trace("outer"):
                handler.start()
                handler.log_chat_completion(
                    source="assistant",
                    request={"messages": "Hi"},
                    response=FakeResponse("gpt-4o", FakeUsage(3, 1, 4)),
                )
                handler.stop()

            traces = load_traces()
            events = load_events()
            self.assertEqual(len(traces), 1)
            self.assertEqual(traces[0].name, "outer")

            root = next(event for event in events if event.type == "trace")
            run_span = next(event for event in events if event.type == "span" and event.name == "autogen.run")
            self.assertEqual(run_span.parent_id, root.id)
            self.assertEqual(run_span.metadata["integration"], "autogen")

            turn = next(event for event in events if event.type == "span" and event.name == "assistant")
            self.assertEqual(turn.parent_id, run_span.id)
            generation = next(event for event in events if event.type == "generation")
            self.assertEqual(generation.parent_id, turn.id)

    def test_event_without_started_run_gets_implicit_root(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            handler = BirAutoGenHandler()

            # No start(): a completion arriving without an active run still attaches
            # to an implicit root instead of raising.
            handler.log_chat_completion(
                source="assistant",
                request={"messages": "Hi"},
                response=FakeResponse("m"),
            )

            traces = load_traces()
            events = load_events()
            self.assertEqual(len(traces), 1)
            self.assertEqual([event.type for event in events], ["generation", "trace"])
            root = next(event for event in events if event.type == "trace")
            self.assertEqual(root.name, "autogen.run")
            self.assertEqual(root.metadata["kind"], "implicit_root")
            self.assertEqual(next(event for event in events if event.type == "generation").parent_id, root.id)

    def test_sequential_runs_stay_isolated(self) -> None:
        with temporary_workdir():
            handler = BirAutoGenHandler()
            for suffix in ("a", "b"):
                handler.start()
                handler.log_chat_completion(source=f"agent-{suffix}", response=FakeResponse(f"model-{suffix}"))
                handler.stop()

            traces = load_traces()
            self.assertEqual(len(traces), 2)
            for trace in traces:
                generation = next(event for event in trace.events if event.type == "generation")
                self.assertEqual(generation.trace_id, trace.root.id)

    def test_concurrent_runs_in_threads_do_not_leak_context(self) -> None:
        with temporary_workdir():
            handler = BirAutoGenHandler()
            # Both runs are forced "open" simultaneously so a leaked contextvar or a
            # shared stack would attach one run's call to the other's trace.
            barrier = threading.Barrier(2)

            def run(suffix: str) -> None:
                handler.start()
                handler.log_event(FakeAgent(f"agent-{suffix}"), "received_message")
                barrier.wait(timeout=5)
                handler.log_chat_completion(
                    source=FakeAgent(f"agent-{suffix}"), response=FakeResponse(f"model-{suffix}")
                )
                handler.stop()

            threads = [threading.Thread(target=run, args=(suffix,)) for suffix in ("a", "b")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            traces = load_traces()
            self.assertEqual(len(traces), 2)
            for trace in traces:
                generation = next(event for event in trace.events if event.type == "generation")
                self.assertEqual(generation.trace_id, trace.root.id)
                self.assertIn(generation.model, {"model-a", "model-b"})

    def test_unrelated_events_and_registration_are_ignored(self) -> None:
        with temporary_workdir():
            handler = BirAutoGenHandler()
            handler.start()
            # Non-boundary events, agent/client registration, and a connection probe
            # must neither crash nor record anything.
            handler.log_event("assistant", "group_chat_state", round=1)
            handler.log_new_agent(FakeAgent("assistant"), {"system_message": "x"})
            handler.log_new_wrapper(object(), {})
            handler.log_new_client(object(), object(), {})
            self.assertIsNone(handler.get_connection())
            handler.stop()

            self.assertEqual([event.type for event in load_events()], ["trace"])

    def test_import_and_construct_without_autogen_installed(self) -> None:
        # The integration must never import the AutoGen / AG2 package.
        self.assertNotIn("autogen", sys.modules)
        self.assertNotIn("ag2", sys.modules)
        handler = BirAutoGenHandler()
        self.assertIsNotNone(handler)
        self.assertNotIn("autogen", sys.modules)


if __name__ == "__main__":
    unittest.main()
