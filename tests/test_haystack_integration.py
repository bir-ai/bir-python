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
from bir.integrations.haystack import BirHaystackTracer

_PIPELINE_RUN = "haystack.pipeline.run"
_COMPONENT_RUN = "haystack.component.run"


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


class FakeChatMessage:
    """Mirrors a Haystack ``ChatMessage`` carrying per-reply ``meta``."""

    def __init__(self, *, meta: dict[str, object]) -> None:
        self.meta = meta


def run_pipeline(tracer: BirHaystackTracer, components: list[dict[str, Any]]) -> None:
    """Drive ``tracer`` the way Haystack's pipeline run does.

    Haystack opens a ``haystack.pipeline.run`` span, then for each component opens a
    ``haystack.component.run`` span, records the input and (after running) output as
    *content* tags, and lets the component attach its own tags via
    ``current_span()``. This helper replays that contract over synthetic data.
    """

    with tracer.trace(_PIPELINE_RUN, tags={"haystack.pipeline.max_runs_per_component": 100}):
        for component in components:
            tags = {
                "haystack.component.name": component["name"],
                "haystack.component.type": component["type"],
                "haystack.component.visits": component.get("visits", 1),
            }
            with tracer.trace(_COMPONENT_RUN, tags=tags) as span:
                if "input" in component:
                    span.set_content_tag("haystack.component.input", component["input"])
                if component.get("raises") is not None:
                    raise component["raises"]
                if "output" in component:
                    span.set_content_tag("haystack.component.output", component["output"])


class HaystackIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_pipeline_run_maps_components_to_generation_tool_and_span_events(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            tracer = BirHaystackTracer()

            run_pipeline(
                tracer,
                [
                    {
                        "name": "retriever",
                        "type": "InMemoryBM25Retriever",
                        "input": {"query": "What is Bir?"},
                        "output": {"documents": [{"content": "Bir records local traces."}]},
                    },
                    {
                        "name": "prompt_builder",
                        "type": "PromptBuilder",
                        "input": {"documents": ["..."]},
                        "output": {"prompt": "Answer using the docs."},
                    },
                    {
                        "name": "llm",
                        "type": "OpenAIGenerator",
                        "input": {"prompt": "Answer using the docs."},
                        "output": {
                            "replies": ["Bir records local traces."],
                            "meta": [
                                {
                                    "model": "gpt-4o",
                                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                                }
                            ],
                        },
                    },
                    {
                        "name": "tools",
                        "type": "ToolInvoker",
                        "input": {"messages": ["call get_weather"]},
                        "output": {"tool_messages": ["sunny"]},
                    },
                ],
            )

            traces = load_traces()
            events = load_events()

            self.assertEqual(len(traces), 1)
            self.assertEqual(traces[0].name, "haystack.pipeline.run")
            self.assertEqual(traces[0].status, "success")
            self.assertEqual(
                sorted(event.type for event in events),
                ["generation", "span", "span", "tool_call", "trace"],
            )

            root = next(event for event in events if event.type == "trace")
            self.assertEqual(root.metadata["integration"], "haystack")
            self.assertEqual(root.metadata["kind"], "pipeline")
            self.assertEqual(root.metadata["max_runs_per_component"], 100)

            spans = [event for event in events if event.type == "span"]
            retriever_event = next(event for event in spans if event.name == "retriever")
            self.assertEqual(retriever_event.metadata["haystack_component_type"], "InMemoryBM25Retriever")
            self.assertEqual(retriever_event.metadata["haystack_component_name"], "retriever")
            self.assertEqual(retriever_event.metadata["visits"], 1)
            self.assertEqual(retriever_event.parent_id, root.id)
            self.assertTrue(any(event.name == "prompt_builder" for event in spans))

            generation_event = next(event for event in events if event.type == "generation")
            self.assertEqual(generation_event.name, "llm")
            self.assertEqual(generation_event.model, "gpt-4o")
            self.assertEqual(
                generation_event.usage,
                {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            )
            self.assertEqual(generation_event.input, {"prompt": "Answer using the docs."})
            self.assertEqual(generation_event.metadata["haystack_component_type"], "OpenAIGenerator")
            self.assertEqual(generation_event.parent_id, root.id)

            tool_event = next(event for event in events if event.type == "tool_call")
            self.assertEqual(tool_event.name, "tools")
            self.assertEqual(tool_event.input, {"messages": ["call get_weather"]})
            self.assertEqual(tool_event.output, {"tool_messages": ["sunny"]})
            self.assertEqual(tool_event.metadata["haystack_component_type"], "ToolInvoker")

    def test_chat_generator_reads_model_and_usage_from_reply_meta(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            tracer = BirHaystackTracer()

            run_pipeline(
                tracer,
                [
                    {
                        "name": "chat",
                        "type": "OpenAIChatGenerator",
                        "input": {"messages": ["Hi"]},
                        "output": {
                            "replies": [
                                FakeChatMessage(
                                    meta={
                                        "model": "gpt-4o-mini",
                                        "usage": {
                                            "prompt_tokens": 7,
                                            "completion_tokens": 3,
                                            "total_tokens": 10,
                                        },
                                    }
                                )
                            ]
                        },
                    }
                ],
            )

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "chat")
            self.assertEqual(generation_event.model, "gpt-4o-mini")
            self.assertEqual(
                generation_event.usage,
                {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
            )

    def test_component_without_name_falls_back_to_type(self) -> None:
        with temporary_workdir():
            tracer = BirHaystackTracer()
            with tracer.trace(_PIPELINE_RUN):
                with tracer.trace(_COMPONENT_RUN, tags={"haystack.component.type": "DocumentCleaner"}):
                    pass

            span_event = next(event for event in load_events() if event.type == "span")
            self.assertEqual(span_event.name, "haystack.DocumentCleaner")

    def test_failing_component_records_error_status_with_redaction(self) -> None:
        with temporary_workdir():
            tracer = BirHaystackTracer()

            with self.assertRaises(RuntimeError):
                run_pipeline(
                    tracer,
                    [
                        {
                            "name": "llm",
                            "type": "OpenAIGenerator",
                            "raises": RuntimeError("provider failed api_key=sk-secret"),
                        }
                    ],
                )

            events = load_events()
            generation_event = next(event for event in events if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "provider failed api_key=[redacted]")
            # The exception propagates through the pipeline span too, so its trace
            # is recorded with error status.
            trace_event = next(event for event in events if event.type == "trace")
            self.assertEqual(trace_event.status, "error")

    def test_component_without_started_pipeline_gets_implicit_root(self) -> None:
        with temporary_workdir():
            tracer = BirHaystackTracer()
            # No pipeline.run span: a component traced on its own still attaches to
            # an implicit root instead of raising.
            with tracer.trace(_COMPONENT_RUN, tags={"haystack.component.type": "OpenAIGenerator"}):
                pass

            traces = load_traces()
            events = load_events()
            self.assertEqual(len(traces), 1)
            self.assertEqual([event.type for event in events], ["generation", "trace"])
            root = next(event for event in events if event.type == "trace")
            self.assertEqual(root.name, "haystack.pipeline.run")
            self.assertEqual(root.metadata["kind"], "implicit_root")
            generation_event = next(event for event in events if event.type == "generation")
            self.assertEqual(generation_event.parent_id, root.id)

    def test_capture_is_opt_in_and_tracer_override_wins(self) -> None:
        with temporary_workdir():
            # Global capture stays off: the run records no input/output payloads,
            # but model and usage are always recorded.
            tracer = BirHaystackTracer()
            run_pipeline(
                tracer,
                [
                    {
                        "name": "llm",
                        "type": "OpenAIGenerator",
                        "input": {"prompt": "secret prompt"},
                        "output": {
                            "replies": ["secret answer"],
                            "meta": [{"model": "gpt-4o", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}],
                        },
                    }
                ],
            )

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertIsNone(generation_event.input)
            self.assertIsNone(generation_event.output)
            self.assertEqual(generation_event.model, "gpt-4o")
            self.assertEqual(generation_event.usage, {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

    def test_tracer_capture_override_enables_payloads(self) -> None:
        with temporary_workdir():
            tracer = BirHaystackTracer(capture_inputs=True, capture_outputs=True)
            run_pipeline(
                tracer,
                [
                    {
                        "name": "llm",
                        "type": "OpenAIGenerator",
                        "input": {"prompt": "hi"},
                        "output": {"replies": ["hello"], "meta": [{"model": "m"}]},
                    }
                ],
            )

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.input, {"prompt": "hi"})
            self.assertEqual(generation_event.output, {"replies": ["hello"], "meta": [{"model": "m"}]})

    def test_current_span_returns_innermost_active_span(self) -> None:
        with temporary_workdir():
            tracer = BirHaystackTracer()
            self.assertIsNone(tracer.current_span())
            with tracer.trace(_PIPELINE_RUN) as pipeline_span:
                self.assertIs(tracer.current_span(), pipeline_span)
                with tracer.trace(_COMPONENT_RUN, tags={"haystack.component.type": "PromptBuilder"}) as comp_span:
                    self.assertIs(tracer.current_span(), comp_span)
                # The component span is popped, exposing the pipeline span again.
                self.assertIs(tracer.current_span(), pipeline_span)
            self.assertIsNone(tracer.current_span())

    def test_sequential_runs_stay_isolated_by_context(self) -> None:
        with temporary_workdir():
            tracer = BirHaystackTracer()
            for suffix in ("a", "b"):
                run_pipeline(
                    tracer,
                    [{"name": f"llm-{suffix}", "type": "OpenAIGenerator", "output": {"meta": [{"model": f"model-{suffix}"}]}}],
                )

            traces = load_traces()
            self.assertEqual(len(traces), 2)
            for trace in traces:
                generation_event = next(event for event in trace.events if event.type == "generation")
                self.assertEqual(generation_event.trace_id, trace.root.id)
                self.assertEqual(generation_event.parent_id, trace.root.id)

    def test_concurrent_runs_in_threads_do_not_leak_context(self) -> None:
        with temporary_workdir():
            tracer = BirHaystackTracer()
            barrier = threading.Barrier(2)

            def run(suffix: str) -> None:
                with tracer.trace(_PIPELINE_RUN):
                    tags = {"haystack.component.name": f"llm-{suffix}", "haystack.component.type": "OpenAIGenerator"}
                    with tracer.trace(_COMPONENT_RUN, tags=tags) as span:
                        barrier.wait(timeout=5)
                        span.set_content_tag("haystack.component.output", {"meta": [{"model": f"model-{suffix}"}]})

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
                self.assertIn(generation_event.model, {"model-a", "model-b"})

    def test_import_and_construct_without_haystack_installed(self) -> None:
        # The integration must never import Haystack.
        self.assertNotIn("haystack", sys.modules)
        tracer = BirHaystackTracer()
        self.assertIsNone(tracer.current_span())
        self.assertNotIn("haystack", sys.modules)


if __name__ == "__main__":
    unittest.main()
