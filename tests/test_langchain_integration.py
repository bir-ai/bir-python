from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from bir import configure, load_events, load_traces
from bir._sdk import _reset_config_for_tests
from bir.integrations.langchain import BirCallbackHandler


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


class FakeDocument:
    def __init__(self, page_content: str, metadata: dict[str, object] | None = None, id: str | None = None) -> None:
        self.page_content = page_content
        self.metadata = metadata or {}
        self.id = id


class LangChainIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_callback_handler_records_chain_retriever_llm_and_tool_events(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            handler = BirCallbackHandler()

            handler.on_chain_start(
                {"id": ["langchain", "chains", "RetrievalQA"]},
                {"question": "What is Bir?", "api_key": "sk-secret"},
                run_id="chain-1",
                tags=["demo"],
                metadata={"route": "qa"},
            )
            handler.on_retriever_start(
                {"name": "vector_search"},
                "What is Bir?",
                run_id="retriever-1",
                parent_run_id="chain-1",
            )
            handler.on_retriever_end(
                [FakeDocument("Bir records local traces.", {"source": "docs"}, id="doc-1")],
                run_id="retriever-1",
            )
            handler.on_llm_start(
                {
                    "id": ["langchain", "chat_models", "ChatOpenAI"],
                    "kwargs": {"model": "gpt-4o-mini"},
                },
                ["Answer using the retrieved context."],
                run_id="llm-1",
                parent_run_id="chain-1",
            )
            handler.on_llm_end(
                {
                    "generations": [[{"text": "Bir is an observability toolkit."}]],
                    "llm_output": {
                        "token_usage": {
                            "prompt_tokens": 12,
                            "completion_tokens": 6,
                            "total_tokens": 18,
                        },
                    },
                },
                run_id="llm-1",
            )
            handler.on_tool_start(
                {"name": "calculator"},
                {"expression": "2 + 2"},
                run_id="tool-1",
                parent_run_id="chain-1",
            )
            handler.on_tool_error(RuntimeError("tool failed api_key=sk-secret"), run_id="tool-1")
            handler.on_chain_end({"answer": "Bir is an observability toolkit."}, run_id="chain-1")

            traces = load_traces()
            events = load_events()

            self.assertEqual(len(traces), 1)
            self.assertEqual(traces[0].name, "RetrievalQA")
            self.assertEqual(traces[0].status, "success")
            self.assertEqual([event.type for event in events], ["tool_call", "generation", "tool_call", "trace"])

            retrieval_event = next(event for event in events if event.name == "vector_search")
            self.assertEqual(retrieval_event.metadata["integration"], "langchain")
            self.assertEqual(retrieval_event.metadata["kind"], "retrieval")
            self.assertEqual(retrieval_event.metadata["langchain_kind"], "retriever")
            self.assertEqual(retrieval_event.input, {"query": "What is Bir?"})
            self.assertEqual(retrieval_event.output["documents"][0]["id"], "doc-1")
            self.assertEqual(retrieval_event.output["documents"][0]["rank"], 1)

            generation_event = next(event for event in events if event.name == "ChatOpenAI")
            self.assertEqual(generation_event.model, "gpt-4o-mini")
            self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18})
            self.assertEqual(generation_event.input, {"prompts": ["Answer using the retrieved context."]})

            tool_event = next(event for event in events if event.name == "calculator")
            self.assertEqual(tool_event.status, "error")
            self.assertEqual(tool_event.error, "tool failed api_key=[redacted]")

    def test_callback_handler_records_direct_llm_invocation_without_active_chain(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            handler = BirCallbackHandler()

            handler.on_llm_start(
                {"name": "local.llm", "kwargs": {"model": "demo"}},
                ["Say hello"],
                run_id="llm-root",
            )
            handler.on_llm_end({"generations": [[{"text": "hello"}]]}, run_id="llm-root")

            traces = load_traces()
            events = load_events()

            self.assertEqual(len(traces), 1)
            self.assertEqual(traces[0].name, "local.llm")
            self.assertEqual([event.type for event in events], ["generation", "trace"])
            self.assertEqual(events[0].name, "local.llm")
            self.assertEqual(events[0].input, {"prompts": ["Say hello"]})
            self.assertEqual(events[1].metadata["kind"], "implicit_root")


if __name__ == "__main__":
    unittest.main()
