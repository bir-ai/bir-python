from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from bir import configure, load_events, load_traces
from bir._sdk import _reset_config_for_tests
from bir.integrations.llamaindex import BirLlamaIndexHandler


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


class FakePayloadKey:
    def __init__(self, value: str) -> None:
        self.value = value

    def __str__(self) -> str:
        return f"EventPayload.{self.value.upper()}"


class FakeEventType:
    def __init__(self, value: str) -> None:
        self.value = value

    def __str__(self) -> str:
        return f"CBEventType.{self.value.upper()}"


class FakeRawResponse:
    def __init__(self, usage: dict[str, int]) -> None:
        self.usage = usage


class FakeResponse:
    def __init__(self, text: str, usage: dict[str, int]) -> None:
        self.response = text
        self.raw = FakeRawResponse(usage)


class FakeNode:
    def __init__(self, node_id: str, text: str) -> None:
        self.node_id = node_id
        self._text = text

    def get_content(self) -> str:
        return self._text


class FakeNodeWithScore:
    def __init__(self, node: FakeNode, score: float) -> None:
        self.node = node
        self.score = score


class LlamaIndexIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_handler_records_trace_llm_chat_and_retrieval_events(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            handler = BirLlamaIndexHandler()

            handler.start_trace("query")
            llm_event_id = handler.on_event_start(
                FakeEventType("llm"),
                {
                    FakePayloadKey("prompt"): "Answer using the retrieved context.",
                    FakePayloadKey("model_name"): "local-demo",
                },
                event_id="llm-1",
            )
            handler.on_event_end(
                FakeEventType("llm"),
                {
                    FakePayloadKey("response"): FakeResponse(
                        "Bir records local traces.",
                        {"prompt_tokens": 8, "completion_tokens": 5, "total_tokens": 13},
                    )
                },
                event_id=llm_event_id,
            )
            chat_event_id = handler.on_event_start(
                FakeEventType("chat"),
                {FakePayloadKey("messages"): [{"role": "user", "content": "What is Bir?"}]},
                event_id="chat-1",
            )
            handler.on_event_end(
                FakeEventType("chat"),
                {FakePayloadKey("completion"): "Bir is an observability SDK."},
                event_id=chat_event_id,
            )
            retrieve_event_id = handler.on_event_start(
                FakeEventType("retrieve"),
                {FakePayloadKey("query"): "What is Bir?"},
                event_id="retrieve-1",
            )
            handler.on_event_end(
                FakeEventType("retrieve"),
                {
                    FakePayloadKey("nodes"): [
                        FakeNodeWithScore(FakeNode("doc-1", "Bir records local traces."), 0.91)
                    ]
                },
                event_id=retrieve_event_id,
            )
            handler.on_event_start("embedding", {FakePayloadKey("chunks"): ["ignored"]}, event_id="ignored-1")
            handler.on_event_end("embedding", {FakePayloadKey("response"): "ignored"}, event_id="ignored-1")
            handler.end_trace("query")

            traces = load_traces()
            events = load_events()

            self.assertEqual(len(traces), 1)
            self.assertEqual(traces[0].name, "query")
            self.assertEqual(traces[0].status, "success")
            self.assertEqual([event.type for event in events], ["generation", "generation", "tool_call", "trace"])

            llm_event = next(event for event in events if event.name == "llamaindex.llm")
            self.assertEqual(llm_event.metadata["integration"], "llamaindex")
            self.assertEqual(llm_event.metadata["kind"], "llm")
            self.assertEqual(llm_event.metadata["llamaindex_kind"], "llm")
            self.assertEqual(llm_event.model, "local-demo")
            self.assertEqual(llm_event.input, {"prompt": "Answer using the retrieved context."})
            self.assertEqual(llm_event.output, {"text": "Bir records local traces."})
            self.assertEqual(llm_event.usage, {"input_tokens": 8, "output_tokens": 5, "total_tokens": 13})

            chat_event = next(event for event in events if event.name == "llamaindex.chat")
            self.assertEqual(chat_event.input, {"messages": [{"role": "user", "content": "What is Bir?"}]})
            self.assertEqual(chat_event.output, {"text": "Bir is an observability SDK."})

            retrieval_event = next(event for event in events if event.name == "llamaindex.retrieve")
            self.assertEqual(retrieval_event.metadata["integration"], "llamaindex")
            self.assertEqual(retrieval_event.metadata["kind"], "retrieval")
            self.assertEqual(retrieval_event.metadata["llamaindex_kind"], "retrieve")
            self.assertEqual(retrieval_event.input, {"query": "What is Bir?"})
            self.assertEqual(
                retrieval_event.output,
                {"documents": [{"id": "doc-1", "text": "Bir records local traces.", "score": 0.91}]},
            )

    def test_handler_records_direct_llm_invocation_without_active_trace(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            handler = BirLlamaIndexHandler()

            event_id = handler.on_event_start("llm", {"prompt": "Say hello"}, event_id="llm-root")
            handler.on_event_end("llm", {"completion": "hello"}, event_id=event_id)

            traces = load_traces()
            events = load_events()

            self.assertEqual(len(traces), 1)
            self.assertEqual(traces[0].name, "llamaindex.llm")
            self.assertEqual([event.type for event in events], ["generation", "trace"])
            self.assertEqual(events[0].name, "llamaindex.llm")
            self.assertEqual(events[0].input, {"prompt": "Say hello"})
            self.assertEqual(events[0].output, {"text": "hello"})
            self.assertEqual(events[1].metadata["kind"], "implicit_root")

    def test_handler_records_error_on_event_end(self) -> None:
        with temporary_workdir():
            handler = BirLlamaIndexHandler(capture_inputs=True, capture_outputs=True)

            event_id = handler.on_event_start("llm", {"prompt": "Say hello"}, event_id="llm-error")
            handler.on_event_end(
                "llm",
                {"completion": "partial api_key=sk-secret"},
                event_id=event_id,
                error=RuntimeError("llamaindex failed api_key=sk-secret"),
            )

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "llamaindex failed api_key=[redacted]")
            self.assertEqual(generation_event.output, {"text": "partial api_key=[redacted]"})

    def test_handler_extracts_input_output_token_usage_from_raw_response(self) -> None:
        with temporary_workdir():
            handler = BirLlamaIndexHandler()

            event_id = handler.on_event_start("llm", {"prompt": "Say hello"}, event_id="llm-usage")
            handler.on_event_end(
                "llm",
                {"response": FakeResponse("hello", {"input_tokens": 9, "output_tokens": 4})},
                event_id=event_id,
            )

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.usage, {"input_tokens": 9, "output_tokens": 4, "total_tokens": 13})


if __name__ == "__main__":
    unittest.main()
