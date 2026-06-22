from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from bir import configure, load_events, load_traces, observe, trace
from bir._sdk import _reset_config_for_tests
from bir.integrations.openai import (
    trace_chat_completion,
    trace_chat_completion_async,
    trace_response,
    trace_response_async,
)


class FakeAsyncStream:
    """An async iterator over pre-built chunks, like a provider ``AsyncStream``.

    ``__aiter__`` makes the object an async stream that the wrappers detect, and
    ``__anext__`` replays the chunks. With ``error`` set it raises that exception
    after the chunks are exhausted instead of ending, modeling a mid-stream
    failure; ``aclosed`` records whether ``aclose()`` ran.
    """

    def __init__(self, chunks: Sequence[object], *, error: BaseException | None = None) -> None:
        self._chunks = list(chunks)
        self._index = 0
        self._error = error
        self.aclosed = False

    def __aiter__(self) -> FakeAsyncStream:
        return self

    async def __anext__(self) -> object:
        if self._index < len(self._chunks):
            chunk = self._chunks[self._index]
            self._index += 1
            return chunk
        if self._error is not None:
            raise self._error
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.aclosed = True


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


class FakeUsage:
    def __init__(
        self,
        *,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class FakeChatCompletion:
    def __init__(
        self,
        *,
        model: str | None = None,
        usage: object | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        self.model = model
        self.usage = usage
        self._payload = payload or {}

    def model_dump(self) -> dict[str, object]:
        return dict(self._payload)


class FakeDelta:
    def __init__(self, content: str | None) -> None:
        self.content = content


class FakeChoice:
    def __init__(self, delta: FakeDelta) -> None:
        self.delta = delta


class FakeChatCompletionChunk:
    def __init__(
        self,
        *,
        content: str | None = None,
        usage: object | None = None,
        model: str | None = "gpt-4o-mini-2024-07-18",
    ) -> None:
        self.model = model
        self.choices = [FakeChoice(FakeDelta(content))]
        self.usage = usage


class OpenAIIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_records_generation_with_model_and_usage_from_response(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            received: dict[str, object] = {}
            created: list[FakeChatCompletion] = []

            def fake_create(**kwargs: object) -> FakeChatCompletion:
                received.update(kwargs)
                completion = FakeChatCompletion(
                    model="gpt-4o-mini-2024-07-18",
                    usage=FakeUsage(prompt_tokens=12, completion_tokens=6, total_tokens=18),
                    payload={
                        "id": "chatcmpl-1",
                        "model": "gpt-4o-mini-2024-07-18",
                        "choices": [{"message": {"role": "assistant", "content": "Bir traces locally."}}],
                    },
                )
                created.append(completion)
                return completion

            with trace("chat"):
                response = trace_chat_completion(
                    fake_create,
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": "What is Bir?"}],
                )
                # The wrapper forwards the request and returns the unchanged response.
                self.assertIs(response, created[0])

            self.assertEqual(received["model"], "gpt-4o-mini")
            self.assertEqual(received["messages"], [{"role": "user", "content": "What is Bir?"}])

            traces = load_traces()
            self.assertEqual(len(traces), 1)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "openai.chat.completions")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "gpt-4o-mini-2024-07-18")
            self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18})
            self.assertEqual(generation_event.metadata["integration"], "openai")
            self.assertEqual(generation_event.input["model"], "gpt-4o-mini")
            self.assertEqual(generation_event.input["messages"], [{"role": "user", "content": "What is Bir?"}])
            self.assertEqual(generation_event.output["choices"][0]["message"]["content"], "Bir traces locally.")

    def test_records_usage_from_mapping_and_computes_total(self) -> None:
        with temporary_workdir():
            def fake_create(**kwargs: object) -> FakeChatCompletion:
                return FakeChatCompletion(
                    model="gpt-4o-mini",
                    usage={"prompt_tokens": 7, "completion_tokens": 3},
                )

            with trace("chat"):
                trace_chat_completion(fake_create, model="gpt-4o-mini", messages=[])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.usage, {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})

    def test_falls_back_to_request_model_and_omits_usage_when_response_is_sparse(self) -> None:
        with temporary_workdir():
            def fake_create(**kwargs: object) -> FakeChatCompletion:
                return FakeChatCompletion(model=None, usage=None, payload={"choices": []})

            with trace("chat"):
                trace_chat_completion(fake_create, model="gpt-4o-mini", messages=[])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.model, "gpt-4o-mini")
            self.assertIsNone(generation_event.usage)

    def test_forwards_openai_metadata_and_applies_bir_options(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True)
            received: dict[str, object] = {}

            def fake_create(**kwargs: object) -> FakeChatCompletion:
                received.update(kwargs)
                return FakeChatCompletion(model="gpt-4o-mini")

            with trace("chat"):
                trace_chat_completion(
                    fake_create,
                    model="gpt-4o-mini",
                    messages=[],
                    metadata={"openai_request_id": "req-1"},
                    bir_name="chat.turn",
                    bir_metadata={"feature": "qa"},
                )

            # OpenAI's own ``metadata`` kwarg is forwarded to ``create``, not consumed.
            self.assertEqual(received["metadata"], {"openai_request_id": "req-1"})

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "chat.turn")
            self.assertEqual(generation_event.metadata["integration"], "openai")
            self.assertEqual(generation_event.metadata["feature"], "qa")
            self.assertEqual(generation_event.input["metadata"], {"openai_request_id": "req-1"})

    def test_records_error_and_redacts_secret_when_create_raises(self) -> None:
        with temporary_workdir():
            def fake_create(**kwargs: object) -> FakeChatCompletion:
                raise RuntimeError("request failed token=sk-secret123")

            with trace("chat"):
                with self.assertRaises(RuntimeError):
                    trace_chat_completion(fake_create, model="gpt-4o-mini", messages=[])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "request failed token=[redacted]")

    def test_records_streamed_generation_after_chunks_are_consumed(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            chunks = [
                FakeChatCompletionChunk(content="Bir "),
                FakeChatCompletionChunk(content="streams"),
                FakeChatCompletionChunk(
                    content=None,
                    usage=FakeUsage(prompt_tokens=11, completion_tokens=4, total_tokens=15),
                ),
            ]
            received: dict[str, object] = {}

            def fake_create(**kwargs: object) -> list[FakeChatCompletionChunk]:
                received.update(kwargs)
                return chunks

            consumed: list[FakeChatCompletionChunk] = []
            with trace("chat"):
                stream = trace_chat_completion(
                    fake_create,
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": "Stream it"}],
                    stream=True,
                    stream_options={"include_usage": True},
                )
                consumed = list(stream)

            self.assertEqual(consumed, chunks)
            self.assertIs(received["stream"], True)
            self.assertEqual(received["stream_options"], {"include_usage": True})

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "gpt-4o-mini-2024-07-18")
            self.assertEqual(generation_event.input["stream"], True)
            self.assertEqual(generation_event.output, "Bir streams")
            self.assertEqual(generation_event.usage, {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15})

    def test_records_stream_error_and_redacts_secret(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)

            def failing_stream() -> Iterator[FakeChatCompletionChunk]:
                yield FakeChatCompletionChunk(content="partial api_key=sk-secret123 ")
                raise RuntimeError("stream failed api_key=sk-secret123")

            def fake_create(**kwargs: object) -> Iterator[FakeChatCompletionChunk]:
                return failing_stream()

            with trace("chat"):
                stream = trace_chat_completion(
                    fake_create,
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": "Stream it"}],
                    stream=True,
                )
                with self.assertRaises(RuntimeError):
                    list(stream)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "stream failed api_key=[redacted]")
            self.assertEqual(generation_event.output, "partial api_key=[redacted] ")

    def test_requires_active_trace(self) -> None:
        with temporary_workdir():
            calls: list[dict[str, object]] = []

            def fake_create(**kwargs: object) -> FakeChatCompletion:
                calls.append(kwargs)
                return FakeChatCompletion(model="gpt-4o-mini")

            with self.assertRaises(RuntimeError):
                trace_chat_completion(fake_create, model="gpt-4o-mini", messages=[])

            # The generation guard fires before the request is ever issued.
            self.assertEqual(calls, [])
            self.assertEqual(load_events(), [])


class OpenAIAsyncIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_awaits_create_and_records_model_and_usage(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            received: dict[str, object] = {}
            created: list[FakeChatCompletion] = []

            async def fake_create(**kwargs: object) -> FakeChatCompletion:
                received.update(kwargs)
                completion = FakeChatCompletion(
                    model="gpt-4o-mini-2024-07-18",
                    usage=FakeUsage(prompt_tokens=12, completion_tokens=6, total_tokens=18),
                    payload={
                        "id": "chatcmpl-1",
                        "model": "gpt-4o-mini-2024-07-18",
                        "choices": [{"message": {"role": "assistant", "content": "Bir traces locally."}}],
                    },
                )
                created.append(completion)
                return completion

            async def driver() -> object:
                async with trace("chat"):
                    return await trace_chat_completion_async(
                        fake_create,
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": "What is Bir?"}],
                    )

            response = asyncio.run(driver())
            # The wrapper forwards the request and returns the awaited response.
            self.assertIs(response, created[0])
            self.assertEqual(received["model"], "gpt-4o-mini")

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "openai.chat.completions")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "gpt-4o-mini-2024-07-18")
            self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18})
            self.assertEqual(generation_event.metadata["integration"], "openai")
            self.assertEqual(generation_event.output["choices"][0]["message"]["content"], "Bir traces locally.")

    def test_forwards_metadata_and_consumes_bir_options(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True)
            received: dict[str, object] = {}

            async def fake_create(**kwargs: object) -> FakeChatCompletion:
                received.update(kwargs)
                return FakeChatCompletion(model="gpt-4o-mini")

            async def driver() -> None:
                async with trace("chat"):
                    await trace_chat_completion_async(
                        fake_create,
                        model="gpt-4o-mini",
                        messages=[],
                        metadata={"openai_request_id": "req-1"},
                        bir_name="chat.turn",
                        bir_metadata={"feature": "qa"},
                    )

            asyncio.run(driver())

            # OpenAI's own ``metadata`` kwarg is forwarded; ``bir_*`` options are not.
            self.assertEqual(received["metadata"], {"openai_request_id": "req-1"})
            self.assertNotIn("bir_name", received)
            self.assertNotIn("bir_metadata", received)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "chat.turn")
            self.assertEqual(generation_event.metadata["feature"], "qa")

    def test_records_error_and_redacts_secret_when_create_raises(self) -> None:
        with temporary_workdir():
            async def fake_create(**kwargs: object) -> FakeChatCompletion:
                raise RuntimeError("request failed token=sk-secret123")

            async def driver() -> None:
                async with trace("chat"):
                    await trace_chat_completion_async(fake_create, model="gpt-4o-mini", messages=[])

            with self.assertRaises(RuntimeError):
                asyncio.run(driver())

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "request failed token=[redacted]")

    def test_records_streamed_generation_after_chunks_are_consumed(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            chunks = [
                FakeChatCompletionChunk(content="Bir "),
                FakeChatCompletionChunk(content="streams"),
                FakeChatCompletionChunk(
                    content=None,
                    usage=FakeUsage(prompt_tokens=11, completion_tokens=4, total_tokens=15),
                ),
            ]
            received: dict[str, object] = {}

            async def fake_create(**kwargs: object) -> FakeAsyncStream:
                received.update(kwargs)
                return FakeAsyncStream(chunks)

            async def driver() -> list[object]:
                collected: list[object] = []
                async with trace("chat"):
                    stream = await trace_chat_completion_async(
                        fake_create,
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": "Stream it"}],
                        stream=True,
                    )
                    collected = [chunk async for chunk in stream]
                return collected

            consumed = asyncio.run(driver())

            self.assertEqual(consumed, chunks)
            self.assertIs(received["stream"], True)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "gpt-4o-mini-2024-07-18")
            self.assertEqual(generation_event.output, "Bir streams")
            self.assertEqual(generation_event.usage, {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15})

    def test_records_stream_error_and_redacts_secret(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)

            async def fake_create(**kwargs: object) -> FakeAsyncStream:
                return FakeAsyncStream(
                    [FakeChatCompletionChunk(content="partial api_key=sk-secret123 ")],
                    error=RuntimeError("stream failed api_key=sk-secret123"),
                )

            async def driver() -> None:
                async with trace("chat"):
                    stream = await trace_chat_completion_async(
                        fake_create,
                        model="gpt-4o-mini",
                        messages=[],
                        stream=True,
                    )
                    async for _chunk in stream:
                        pass

            with self.assertRaises(RuntimeError):
                asyncio.run(driver())

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "stream failed api_key=[redacted]")
            self.assertEqual(generation_event.output, "partial api_key=[redacted] ")

    def test_records_partial_output_when_stream_closed_early(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)

            async def fake_create(**kwargs: object) -> FakeAsyncStream:
                return FakeAsyncStream(
                    [FakeChatCompletionChunk(content="Bir "), FakeChatCompletionChunk(content="streams")]
                )

            async def driver() -> None:
                async with trace("chat"):
                    stream = await trace_chat_completion_async(
                        fake_create,
                        model="gpt-4o-mini",
                        messages=[],
                        stream=True,
                    )
                    iterator = stream.__aiter__()
                    await iterator.__anext__()
                    # Closing after one chunk finalizes the accumulated output.
                    await stream.aclose()

            asyncio.run(driver())

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.output, "Bir ")

    def test_stream_falls_back_when_provider_returns_full_response(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)

            async def fake_create(**kwargs: object) -> FakeChatCompletion:
                # A provider that ignores ``stream=True`` and returns one response.
                return FakeChatCompletion(
                    model="gpt-4o-mini-2024-07-18",
                    usage=FakeUsage(prompt_tokens=5, completion_tokens=2, total_tokens=7),
                    payload={"id": "chatcmpl-1", "choices": [{"message": {"content": "Fallback"}}]},
                )

            async def driver() -> list[object]:
                collected: list[object] = []
                async with trace("chat"):
                    stream = await trace_chat_completion_async(
                        fake_create, model="gpt-4o-mini", messages=[], stream=True
                    )
                    collected = [chunk async for chunk in stream]
                return collected

            consumed = asyncio.run(driver())

            # The non-streamed response is recorded in one piece; the iterator yields nothing.
            self.assertEqual(consumed, [])
            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "gpt-4o-mini-2024-07-18")
            self.assertEqual(generation_event.usage, {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7})

    def test_streams_under_observe_async_function(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)

            async def fake_create(**kwargs: object) -> FakeAsyncStream:
                return FakeAsyncStream(
                    [FakeChatCompletionChunk(content="Bir "), FakeChatCompletionChunk(content="streams")]
                )

            @observe()
            async def run() -> list[object]:
                collected: list[object] = []
                stream = await trace_chat_completion_async(
                    fake_create, model="gpt-4o-mini", messages=[], stream=True
                )
                collected = [chunk async for chunk in stream]
                return collected

            consumed = asyncio.run(run())

            self.assertEqual(len(consumed), 2)
            self.assertEqual(len(load_traces()), 1)
            events = load_events()
            trace_event = next(event for event in events if event.type == "trace")
            generation_event = next(event for event in events if event.type == "generation")
            # The streamed generation attaches under the @observe() root trace.
            self.assertEqual(generation_event.trace_id, trace_event.trace_id)
            self.assertEqual(generation_event.output, "Bir streams")

    def test_requires_active_trace(self) -> None:
        with temporary_workdir():
            calls: list[dict[str, object]] = []

            async def fake_create(**kwargs: object) -> FakeChatCompletion:
                calls.append(kwargs)
                return FakeChatCompletion(model="gpt-4o-mini")

            async def driver() -> None:
                await trace_chat_completion_async(fake_create, model="gpt-4o-mini", messages=[])

            with self.assertRaises(RuntimeError):
                asyncio.run(driver())

            # The generation guard fires before the request is ever awaited.
            self.assertEqual(calls, [])
            self.assertEqual(load_events(), [])

    def test_stream_requires_active_trace_before_create(self) -> None:
        with temporary_workdir():
            calls: list[dict[str, object]] = []

            async def fake_create(**kwargs: object) -> FakeAsyncStream:
                calls.append(kwargs)
                return FakeAsyncStream([FakeChatCompletionChunk(content="hi")])

            async def driver() -> None:
                # Awaiting the wrapper returns the lazy async iterator without a trace;
                # draining it must raise and never call create.
                stream = await trace_chat_completion_async(
                    fake_create, model="gpt-4o-mini", messages=[], stream=True
                )
                async for _chunk in stream:
                    pass

            with self.assertRaises(RuntimeError):
                asyncio.run(driver())

            self.assertEqual(calls, [])
            self.assertEqual(load_events(), [])


class FakeResponseUsage:
    def __init__(
        self,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_tokens = total_tokens


class FakeResponse:
    def __init__(
        self,
        *,
        model: str | None = None,
        output_text: str | None = None,
        usage: object | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        self.model = model
        self.output_text = output_text
        self.usage = usage
        self._payload = payload or {}

    def model_dump(self) -> dict[str, object]:
        return dict(self._payload)


class FakeResponseStreamEvent:
    """A single Responses API streaming event.

    The Responses API emits typed events. Text arrives only on
    ``response.output_text.delta`` events (at ``delta``); the model and usage
    arrive on the nested ``response`` snapshot carried by lifecycle events such
    as ``response.created`` and ``response.completed``.
    """

    def __init__(
        self,
        *,
        type: str,
        delta: str | None = None,
        text: str | None = None,
        response: object | None = None,
    ) -> None:
        self.type = type
        self.delta = delta
        self.text = text
        self.response = response


class OpenAIResponsesIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_records_generation_with_model_output_text_and_usage(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            received: dict[str, object] = {}
            created: list[FakeResponse] = []

            def fake_create(**kwargs: object) -> FakeResponse:
                received.update(kwargs)
                response = FakeResponse(
                    model="gpt-4o-2024-08-06",
                    output_text="Bir traces locally.",
                    usage=FakeResponseUsage(input_tokens=12, output_tokens=6, total_tokens=18),
                    payload={"id": "resp-1", "model": "gpt-4o-2024-08-06"},
                )
                created.append(response)
                return response

            with trace("resp"):
                response = trace_response(
                    fake_create,
                    model="gpt-4o",
                    input="What is Bir?",
                )
                # The wrapper forwards the request and returns the unchanged response.
                self.assertIs(response, created[0])

            self.assertEqual(received["model"], "gpt-4o")
            self.assertEqual(received["input"], "What is Bir?")

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "openai.responses")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "gpt-4o-2024-08-06")
            self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18})
            self.assertEqual(generation_event.metadata["integration"], "openai")
            self.assertEqual(generation_event.input["model"], "gpt-4o")
            self.assertEqual(generation_event.input["input"], "What is Bir?")
            # ``output_text`` is preferred over the full response shape.
            self.assertEqual(generation_event.output, "Bir traces locally.")

    def test_records_usage_from_mapping_response_and_computes_total(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)

            def fake_create(**kwargs: object) -> dict[str, object]:
                return {
                    "model": "gpt-4o-mini",
                    "output_text": "Bir maps cleanly.",
                    "usage": {"input_tokens": 7, "output_tokens": 3},
                }

            with trace("resp"):
                trace_response(fake_create, model="gpt-4o-mini", input="hi")

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.model, "gpt-4o-mini")
            self.assertEqual(generation_event.output, "Bir maps cleanly.")
            self.assertEqual(generation_event.usage, {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})

    def test_falls_back_to_full_shape_and_omits_usage_when_response_is_sparse(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)

            def fake_create(**kwargs: object) -> FakeResponse:
                # No model, empty ``output_text``, and no usage: the wrapper must
                # fall back to the request model and the full response shape, and
                # must not fabricate usage.
                return FakeResponse(model=None, output_text="", usage=None, payload={"output": []})

            with trace("resp"):
                trace_response(fake_create, model="gpt-4o", input="hi")

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.model, "gpt-4o")
            self.assertEqual(generation_event.output, {"output": []})
            self.assertIsNone(generation_event.usage)

    def test_forwards_openai_metadata_and_applies_bir_options(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True)
            received: dict[str, object] = {}

            def fake_create(**kwargs: object) -> FakeResponse:
                received.update(kwargs)
                return FakeResponse(model="gpt-4o")

            with trace("resp"):
                trace_response(
                    fake_create,
                    model="gpt-4o",
                    input="hi",
                    metadata={"openai_request_id": "req-1"},
                    bir_name="answer.turn",
                    bir_metadata={"feature": "qa"},
                )

            # OpenAI's own ``metadata`` kwarg is forwarded to ``create``, not consumed.
            self.assertEqual(received["metadata"], {"openai_request_id": "req-1"})
            # The wrapper's own ``bir_*`` options are consumed, never forwarded.
            self.assertNotIn("bir_name", received)
            self.assertNotIn("bir_metadata", received)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "answer.turn")
            self.assertEqual(generation_event.metadata["integration"], "openai")
            self.assertEqual(generation_event.metadata["feature"], "qa")
            self.assertEqual(generation_event.input["metadata"], {"openai_request_id": "req-1"})

    def test_capture_disabled_by_default_still_records_model_and_usage(self) -> None:
        with temporary_workdir():
            def fake_create(**kwargs: object) -> FakeResponse:
                return FakeResponse(
                    model="gpt-4o",
                    output_text="secret answer",
                    usage=FakeResponseUsage(input_tokens=4, output_tokens=2, total_tokens=6),
                )

            with trace("resp"):
                trace_response(fake_create, model="gpt-4o", input="hi")

            generation_event = next(event for event in load_events() if event.type == "generation")
            # Capture is opt-in, so input/output stay out of the persisted event.
            self.assertIsNone(generation_event.input)
            self.assertIsNone(generation_event.output)
            self.assertEqual(generation_event.model, "gpt-4o")
            self.assertEqual(generation_event.usage, {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6})

    def test_captured_output_text_is_redacted(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)

            def fake_create(**kwargs: object) -> FakeResponse:
                return FakeResponse(model="gpt-4o", output_text="here it is api_key=sk-secret123")

            with trace("resp"):
                trace_response(fake_create, model="gpt-4o", input="hi")

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.output, "here it is api_key=[redacted]")

    def test_records_error_and_redacts_secret_when_create_raises(self) -> None:
        with temporary_workdir():
            def fake_create(**kwargs: object) -> FakeResponse:
                raise RuntimeError("request failed token=sk-secret123")

            with trace("resp"):
                with self.assertRaises(RuntimeError):
                    trace_response(fake_create, model="gpt-4o", input="hi")

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "request failed token=[redacted]")

    def test_records_streamed_generation_after_events_are_consumed(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            events = [
                FakeResponseStreamEvent(
                    type="response.created",
                    response=FakeResponse(model="gpt-4o-2024-08-06"),
                ),
                FakeResponseStreamEvent(type="response.output_text.delta", delta="Bir "),
                # A non-text delta event that still carries ``delta`` must be ignored.
                FakeResponseStreamEvent(
                    type="response.function_call_arguments.delta",
                    delta='{"city":"Paris"}',
                ),
                FakeResponseStreamEvent(type="response.output_text.delta", delta="streams"),
                # The ``...done`` event repeats the whole text and must not double-count.
                FakeResponseStreamEvent(type="response.output_text.done", text="Bir streams"),
                FakeResponseStreamEvent(
                    type="response.completed",
                    response=FakeResponse(
                        model="gpt-4o-2024-08-06",
                        usage=FakeResponseUsage(input_tokens=11, output_tokens=4, total_tokens=15),
                    ),
                ),
            ]
            received: dict[str, object] = {}

            def fake_create(**kwargs: object) -> list[FakeResponseStreamEvent]:
                received.update(kwargs)
                return events

            consumed: list[FakeResponseStreamEvent] = []
            with trace("resp"):
                stream = trace_response(
                    fake_create,
                    model="gpt-4o",
                    input="Stream it",
                    stream=True,
                )
                consumed = list(stream)

            # Events are yielded unchanged and in order.
            self.assertEqual(consumed, events)
            self.assertIs(received["stream"], True)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "gpt-4o-2024-08-06")
            self.assertEqual(generation_event.input["stream"], True)
            self.assertEqual(generation_event.output, "Bir streams")
            self.assertEqual(generation_event.usage, {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15})

    def test_records_stream_error_and_redacts_secret(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)

            def failing_stream() -> Iterator[FakeResponseStreamEvent]:
                yield FakeResponseStreamEvent(
                    type="response.output_text.delta",
                    delta="partial api_key=sk-secret123 ",
                )
                raise RuntimeError("stream failed api_key=sk-secret123")

            def fake_create(**kwargs: object) -> Iterator[FakeResponseStreamEvent]:
                return failing_stream()

            with trace("resp"):
                stream = trace_response(
                    fake_create,
                    model="gpt-4o",
                    input="Stream it",
                    stream=True,
                )
                with self.assertRaises(RuntimeError):
                    list(stream)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "stream failed api_key=[redacted]")
            self.assertEqual(generation_event.output, "partial api_key=[redacted] ")

    def test_stream_falls_back_when_provider_returns_full_response(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)

            def fake_create(**kwargs: object) -> FakeResponse:
                # A provider that ignores ``stream=True`` and returns one response.
                return FakeResponse(
                    model="gpt-4o-2024-08-06",
                    output_text="Fallback text",
                    usage=FakeResponseUsage(input_tokens=5, output_tokens=2, total_tokens=7),
                )

            consumed: list[FakeResponseStreamEvent] = []
            with trace("resp"):
                stream = trace_response(fake_create, model="gpt-4o", input="hi", stream=True)
                # The non-streamed response is recorded in one piece; like the chat
                # wrapper's fallback, the iterator itself yields nothing.
                consumed = list(stream)

            self.assertEqual(consumed, [])
            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "gpt-4o-2024-08-06")
            self.assertEqual(generation_event.output, "Fallback text")
            self.assertEqual(generation_event.usage, {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7})

    def test_requires_active_trace(self) -> None:
        with temporary_workdir():
            calls: list[dict[str, object]] = []

            def fake_create(**kwargs: object) -> FakeResponse:
                calls.append(kwargs)
                return FakeResponse(model="gpt-4o")

            with self.assertRaises(RuntimeError):
                trace_response(fake_create, model="gpt-4o", input="hi")

            # The generation guard fires before the request is ever issued.
            self.assertEqual(calls, [])
            self.assertEqual(load_events(), [])

    def test_stream_requires_active_trace_before_create(self) -> None:
        with temporary_workdir():
            calls: list[dict[str, object]] = []

            def fake_create(**kwargs: object) -> list[FakeResponseStreamEvent]:
                calls.append(kwargs)
                return [FakeResponseStreamEvent(type="response.output_text.delta", delta="hi")]

            # The lazy stream wrapper only enforces the active-trace guard once it is
            # iterated, so draining it outside a trace must raise and never call create.
            stream = trace_response(fake_create, model="gpt-4o", input="hi", stream=True)
            with self.assertRaises(RuntimeError):
                list(stream)

            self.assertEqual(calls, [])
            self.assertEqual(load_events(), [])


class OpenAIResponsesAsyncIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_awaits_create_and_records_model_output_text_and_usage(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            created: list[FakeResponse] = []

            async def fake_create(**kwargs: object) -> FakeResponse:
                response = FakeResponse(
                    model="gpt-4o-2024-08-06",
                    output_text="Bir traces locally.",
                    usage=FakeResponseUsage(input_tokens=12, output_tokens=6, total_tokens=18),
                    payload={"id": "resp-1", "model": "gpt-4o-2024-08-06"},
                )
                created.append(response)
                return response

            async def driver() -> object:
                async with trace("resp"):
                    return await trace_response_async(fake_create, model="gpt-4o", input="What is Bir?")

            response = asyncio.run(driver())
            self.assertIs(response, created[0])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "openai.responses")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "gpt-4o-2024-08-06")
            self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18})
            self.assertEqual(generation_event.output, "Bir traces locally.")

    def test_records_error_and_redacts_secret_when_create_raises(self) -> None:
        with temporary_workdir():
            async def fake_create(**kwargs: object) -> FakeResponse:
                raise RuntimeError("request failed token=sk-secret123")

            async def driver() -> None:
                async with trace("resp"):
                    await trace_response_async(fake_create, model="gpt-4o", input="hi")

            with self.assertRaises(RuntimeError):
                asyncio.run(driver())

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "request failed token=[redacted]")

    def test_records_streamed_generation_after_events_are_consumed(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            events = [
                FakeResponseStreamEvent(
                    type="response.created",
                    response=FakeResponse(model="gpt-4o-2024-08-06"),
                ),
                FakeResponseStreamEvent(type="response.output_text.delta", delta="Bir "),
                FakeResponseStreamEvent(type="response.output_text.delta", delta="streams"),
                FakeResponseStreamEvent(type="response.output_text.done", text="Bir streams"),
                FakeResponseStreamEvent(
                    type="response.completed",
                    response=FakeResponse(
                        model="gpt-4o-2024-08-06",
                        usage=FakeResponseUsage(input_tokens=11, output_tokens=4, total_tokens=15),
                    ),
                ),
            ]

            async def fake_create(**kwargs: object) -> FakeAsyncStream:
                return FakeAsyncStream(events)

            async def driver() -> list[object]:
                collected: list[object] = []
                async with trace("resp"):
                    stream = await trace_response_async(
                        fake_create, model="gpt-4o", input="Stream it", stream=True
                    )
                    collected = [event async for event in stream]
                return collected

            consumed = asyncio.run(driver())

            self.assertEqual(consumed, events)
            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "gpt-4o-2024-08-06")
            self.assertEqual(generation_event.output, "Bir streams")
            self.assertEqual(generation_event.usage, {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15})

    def test_records_stream_error_and_redacts_secret(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)

            async def fake_create(**kwargs: object) -> FakeAsyncStream:
                return FakeAsyncStream(
                    [FakeResponseStreamEvent(type="response.output_text.delta", delta="partial api_key=sk-secret123 ")],
                    error=RuntimeError("stream failed api_key=sk-secret123"),
                )

            async def driver() -> None:
                async with trace("resp"):
                    stream = await trace_response_async(fake_create, model="gpt-4o", input="hi", stream=True)
                    async for _event in stream:
                        pass

            with self.assertRaises(RuntimeError):
                asyncio.run(driver())

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "stream failed api_key=[redacted]")
            self.assertEqual(generation_event.output, "partial api_key=[redacted] ")

    def test_stream_falls_back_when_provider_returns_full_response(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)

            async def fake_create(**kwargs: object) -> FakeResponse:
                return FakeResponse(
                    model="gpt-4o-2024-08-06",
                    output_text="Fallback text",
                    usage=FakeResponseUsage(input_tokens=5, output_tokens=2, total_tokens=7),
                )

            async def driver() -> list[object]:
                collected: list[object] = []
                async with trace("resp"):
                    stream = await trace_response_async(fake_create, model="gpt-4o", input="hi", stream=True)
                    collected = [event async for event in stream]
                return collected

            consumed = asyncio.run(driver())

            self.assertEqual(consumed, [])
            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.output, "Fallback text")
            self.assertEqual(generation_event.usage, {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7})

    def test_requires_active_trace(self) -> None:
        with temporary_workdir():
            calls: list[dict[str, object]] = []

            async def fake_create(**kwargs: object) -> FakeResponse:
                calls.append(kwargs)
                return FakeResponse(model="gpt-4o")

            async def driver() -> None:
                await trace_response_async(fake_create, model="gpt-4o", input="hi")

            with self.assertRaises(RuntimeError):
                asyncio.run(driver())

            self.assertEqual(calls, [])
            self.assertEqual(load_events(), [])


if __name__ == "__main__":
    unittest.main()
