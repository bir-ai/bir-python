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


class FakeContent:
    def __init__(self, text: str | None) -> None:
        self.text = text


class FakeMessageDelta:
    def __init__(self, content: object) -> None:
        self.content = content


class FakeEventDelta:
    # A streaming event's ``delta`` carries either an incremental ``message``
    # (``content-delta``) or terminal ``usage`` (``message-end``).
    def __init__(self, *, message: object | None = None, usage: object | None = None) -> None:
        self.message = message
        self.usage = usage


class FakeResponseSnapshot:
    def __init__(self, *, usage: object | None = None) -> None:
        self.usage = usage


class FakeStreamEvent:
    def __init__(self, *, delta: object | None = None, response: object | None = None) -> None:
        self.delta = delta
        self.response = response


def _content_delta(text: str | None) -> FakeStreamEvent:
    # A Cohere v2 ``content-delta`` event: text at ``delta.message.content.text``.
    return FakeStreamEvent(delta=FakeEventDelta(message=FakeMessageDelta(FakeContent(text))))


def _message_end(usage: object) -> FakeStreamEvent:
    # A v2 ``message-end`` event carrying usage at ``delta.usage``.
    return FakeStreamEvent(delta=FakeEventDelta(usage=usage))


def _stream_end(usage: object) -> FakeStreamEvent:
    # The older ``stream-end`` event carrying usage at ``response.usage``.
    return FakeStreamEvent(response=FakeResponseSnapshot(usage=usage))


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

    def test_records_streamed_generation_after_events_are_consumed(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            events = [
                _content_delta("Bir "),
                _content_delta("streams"),
                _message_end(FakeUsage(tokens=FakeTokens(input_tokens=12, output_tokens=6))),
            ]
            received: dict[str, object] = {}

            def fake_stream(**kwargs: object) -> list[FakeStreamEvent]:
                received.update(kwargs)
                return events

            consumed: list[FakeStreamEvent] = []
            with trace("chat"):
                stream = trace_chat(
                    fake_stream,
                    model="command-a-03-2025",
                    messages=[{"role": "user", "content": "Stream it"}],
                    stream=True,
                )
                consumed = list(stream)

            # The events are yielded unchanged in order.
            self.assertEqual(consumed, events)
            self.assertIs(received["stream"], True)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "command-a-03-2025")
            self.assertEqual(generation_event.input["stream"], True)
            self.assertEqual(generation_event.output, "Bir streams")
            self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18})

    def test_reads_streamed_usage_from_stream_end_response(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)
            events = [
                _content_delta("Bir"),
                _stream_end(FakeUsage(tokens=FakeTokens(input_tokens=5, output_tokens=2))),
            ]

            def fake_stream(**kwargs: object) -> list[FakeStreamEvent]:
                return events

            with trace("chat"):
                stream = trace_chat(fake_stream, model="command-a-03-2025", messages=[], stream=True)
                self.assertEqual(list(stream), events)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.output, "Bir")
            self.assertEqual(generation_event.usage, {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7})

    def test_stream_falls_back_when_provider_returns_full_response(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)

            def fake_chat(**kwargs: object) -> FakeChatResponse:
                # A provider that ignores ``stream=True`` and returns one response.
                return FakeChatResponse(
                    usage=FakeUsage(tokens=FakeTokens(input_tokens=7, output_tokens=3)),
                    payload={"message": {"content": "One shot."}},
                )

            consumed: list[FakeChatResponse] = []
            with trace("chat"):
                stream = trace_chat(fake_chat, model="command-a-03-2025", messages=[], stream=True)
                consumed = list(stream)

            # The non-streamed response is recorded in one piece; the iterator yields nothing.
            self.assertEqual(consumed, [])
            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "command-a-03-2025")
            self.assertEqual(generation_event.usage, {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})
            self.assertEqual(generation_event.output["message"]["content"], "One shot.")

    def test_records_stream_error_and_redacts_secret(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)

            def failing_stream() -> Iterator[FakeStreamEvent]:
                yield _content_delta("partial api_key=sk-secret123 ")
                raise RuntimeError("stream failed api_key=sk-secret123")

            def fake_stream(**kwargs: object) -> Iterator[FakeStreamEvent]:
                return failing_stream()

            with trace("chat"):
                stream = trace_chat(fake_stream, model="command-a-03-2025", messages=[], stream=True)
                with self.assertRaises(RuntimeError):
                    list(stream)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "stream failed api_key=[redacted]")
            # Partial output collected before the failure is recorded and redacted.
            self.assertEqual(generation_event.output, "partial api_key=[redacted] ")

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
