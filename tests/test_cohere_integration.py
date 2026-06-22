from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from bir import configure, load_events, load_traces, trace
from bir._sdk import _reset_config_for_tests
from bir.integrations.cohere import trace_chat, trace_chat_async


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


class FakeTokens:
    def __init__(
        self,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeUsage:
    def __init__(self, *, tokens: object | None = None) -> None:
        self.tokens = tokens


class FakeChatResponse:
    def __init__(
        self,
        *,
        usage: object | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        self.usage = usage
        self._payload = payload or {}

    def model_dump(self) -> dict[str, object]:
        return dict(self._payload)


class CohereIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_records_generation_with_model_from_request_and_nested_usage_from_response(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            received: dict[str, object] = {}
            created: list[FakeChatResponse] = []

            def fake_chat(**kwargs: object) -> FakeChatResponse:
                received.update(kwargs)
                response = FakeChatResponse(
                    usage=FakeUsage(tokens=FakeTokens(input_tokens=12, output_tokens=6)),
                    payload={
                        "id": "chat-1",
                        "message": {"role": "assistant", "content": "Bir traces locally."},
                    },
                )
                created.append(response)
                return response

            with trace("chat"):
                response = trace_chat(
                    fake_chat,
                    model="command-a-03-2025",
                    messages=[{"role": "user", "content": "What is Bir?"}],
                )
                # The wrapper forwards the request and returns the unchanged response.
                self.assertIs(response, created[0])

            self.assertEqual(received["model"], "command-a-03-2025")
            self.assertEqual(received["messages"], [{"role": "user", "content": "What is Bir?"}])

            traces = load_traces()
            self.assertEqual(len(traces), 1)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "cohere.chat")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "command-a-03-2025")
            self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18})
            self.assertEqual(generation_event.metadata["integration"], "cohere")
            self.assertEqual(generation_event.input["model"], "command-a-03-2025")
            self.assertEqual(generation_event.input["messages"], [{"role": "user", "content": "What is Bir?"}])
            self.assertEqual(generation_event.output["message"]["content"], "Bir traces locally.")

    def test_records_usage_from_nested_mapping_and_computes_total(self) -> None:
        with temporary_workdir():
            def fake_chat(**kwargs: object) -> FakeChatResponse:
                return FakeChatResponse(
                    usage={"tokens": {"input_tokens": 7, "output_tokens": 3}},
                )

            with trace("chat"):
                trace_chat(fake_chat, model="command-a-03-2025", messages=[])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.usage, {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})

    def test_uses_request_model_and_omits_usage_when_response_is_sparse(self) -> None:
        with temporary_workdir():
            def fake_chat(**kwargs: object) -> FakeChatResponse:
                return FakeChatResponse(usage=None, payload={"message": None})

            with trace("chat"):
                trace_chat(fake_chat, model="command-a-03-2025", messages=[])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.model, "command-a-03-2025")
            self.assertIsNone(generation_event.usage)

    def test_forwards_cohere_metadata_and_applies_bir_options(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True)
            received: dict[str, object] = {}

            def fake_chat(**kwargs: object) -> FakeChatResponse:
                received.update(kwargs)
                return FakeChatResponse()

            with trace("chat"):
                trace_chat(
                    fake_chat,
                    model="command-a-03-2025",
                    messages=[],
                    metadata={"cohere_request_id": "req-1"},
                    bir_name="chat.turn",
                    bir_metadata={"feature": "qa"},
                )

            # Cohere's own ``metadata`` kwarg is forwarded to ``chat``, not consumed.
            self.assertEqual(received["metadata"], {"cohere_request_id": "req-1"})

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "chat.turn")
            self.assertEqual(generation_event.metadata["integration"], "cohere")
            self.assertEqual(generation_event.metadata["feature"], "qa")
            self.assertEqual(generation_event.input["metadata"], {"cohere_request_id": "req-1"})

    def test_records_error_and_redacts_secret_when_chat_raises(self) -> None:
        with temporary_workdir():
            def fake_chat(**kwargs: object) -> FakeChatResponse:
                raise RuntimeError("request failed api_key=sk-secret123")

            with trace("chat"):
                with self.assertRaises(RuntimeError):
                    trace_chat(fake_chat, model="command-a-03-2025", messages=[])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "request failed api_key=[redacted]")

    def test_requires_active_trace(self) -> None:
        with temporary_workdir():
            calls: list[dict[str, object]] = []

            def fake_chat(**kwargs: object) -> FakeChatResponse:
                calls.append(kwargs)
                return FakeChatResponse()

            with self.assertRaises(RuntimeError):
                trace_chat(fake_chat, model="command-a-03-2025", messages=[])

            # The generation guard fires before the request is ever issued.
            self.assertEqual(calls, [])
            self.assertEqual(load_events(), [])


class CohereAsyncIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_awaits_chat_and_records_model_from_request_and_nested_usage(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            created: list[FakeChatResponse] = []

            async def fake_chat(**kwargs: object) -> FakeChatResponse:
                response = FakeChatResponse(
                    usage=FakeUsage(tokens=FakeTokens(input_tokens=12, output_tokens=6)),
                    payload={
                        "id": "chat-1",
                        "message": {"role": "assistant", "content": "Bir traces locally."},
                    },
                )
                created.append(response)
                return response

            async def driver() -> object:
                async with trace("chat"):
                    return await trace_chat_async(
                        fake_chat,
                        model="command-a-03-2025",
                        messages=[{"role": "user", "content": "What is Bir?"}],
                    )

            response = asyncio.run(driver())
            self.assertIs(response, created[0])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "cohere.chat")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "command-a-03-2025")
            self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18})
            self.assertEqual(generation_event.metadata["integration"], "cohere")
            self.assertEqual(generation_event.output["message"]["content"], "Bir traces locally.")

    def test_forwards_metadata_and_consumes_bir_options(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True)
            received: dict[str, object] = {}

            async def fake_chat(**kwargs: object) -> FakeChatResponse:
                received.update(kwargs)
                return FakeChatResponse()

            async def driver() -> None:
                async with trace("chat"):
                    await trace_chat_async(
                        fake_chat,
                        model="command-a-03-2025",
                        messages=[],
                        metadata={"cohere_request_id": "req-1"},
                        bir_name="chat.turn",
                        bir_metadata={"feature": "qa"},
                    )

            asyncio.run(driver())

            self.assertEqual(received["metadata"], {"cohere_request_id": "req-1"})
            self.assertNotIn("bir_name", received)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "chat.turn")
            self.assertEqual(generation_event.metadata["feature"], "qa")

    def test_records_error_and_redacts_secret_when_chat_raises(self) -> None:
        with temporary_workdir():
            async def fake_chat(**kwargs: object) -> FakeChatResponse:
                raise RuntimeError("request failed api_key=sk-secret123")

            async def driver() -> None:
                async with trace("chat"):
                    await trace_chat_async(fake_chat, model="command-a-03-2025", messages=[])

            with self.assertRaises(RuntimeError):
                asyncio.run(driver())

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "request failed api_key=[redacted]")

    def test_requires_active_trace(self) -> None:
        with temporary_workdir():
            calls: list[dict[str, object]] = []

            async def fake_chat(**kwargs: object) -> FakeChatResponse:
                calls.append(kwargs)
                return FakeChatResponse()

            async def driver() -> None:
                await trace_chat_async(fake_chat, model="command-a-03-2025", messages=[])

            with self.assertRaises(RuntimeError):
                asyncio.run(driver())

            self.assertEqual(calls, [])
            self.assertEqual(load_events(), [])


if __name__ == "__main__":
    unittest.main()
