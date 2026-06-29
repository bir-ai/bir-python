from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from bir import configure, load_events, load_traces, trace
from bir._sdk import _reset_config_for_tests
from bir.integrations.ollama import (
    trace_chat,
    trace_chat_async,
    trace_generate,
    trace_generate_async,
)


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


class FakeMessage:
    def __init__(self, *, role: str = "assistant", content: str | None = None) -> None:
        self.role = role
        self.content = content


class FakeChatResponse:
    """An ``ollama.chat`` response: a model, a ``message``, and top-level usage.

    Mirrors the Ollama client's pydantic ``ChatResponse``: the assistant text is at
    ``message.content`` and the token counts are top-level ``prompt_eval_count`` /
    ``eval_count`` (no nested ``usage`` object). ``model_dump`` returns the JSON
    shape the wrapper records as output.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        content: str | None = None,
        prompt_eval_count: int | None = None,
        eval_count: int | None = None,
    ) -> None:
        self.model = model
        self.message = FakeMessage(content=content)
        self.prompt_eval_count = prompt_eval_count
        self.eval_count = eval_count

    def model_dump(self) -> dict[str, object]:
        return {
            "model": self.model,
            "message": {"role": self.message.role, "content": self.message.content},
            "prompt_eval_count": self.prompt_eval_count,
            "eval_count": self.eval_count,
        }


class FakeGenerateResponse:
    """An ``ollama.generate`` response: the completion text is at ``response``."""

    def __init__(
        self,
        *,
        model: str | None = None,
        response: str | None = None,
        prompt_eval_count: int | None = None,
        eval_count: int | None = None,
    ) -> None:
        self.model = model
        self.response = response
        self.prompt_eval_count = prompt_eval_count
        self.eval_count = eval_count

    def model_dump(self) -> dict[str, object]:
        return {
            "model": self.model,
            "response": self.response,
            "prompt_eval_count": self.prompt_eval_count,
            "eval_count": self.eval_count,
        }


class FakeChatChunk:
    # Ollama streams chat chunks carrying ``model`` and a ``message.content`` delta;
    # the terminal ``done`` chunk also carries ``prompt_eval_count``/``eval_count``.
    def __init__(
        self,
        *,
        content: str | None = None,
        model: str | None = "llama3.2:1b",
        prompt_eval_count: int | None = None,
        eval_count: int | None = None,
    ) -> None:
        self.model = model
        self.message = FakeMessage(content=content)
        self.prompt_eval_count = prompt_eval_count
        self.eval_count = eval_count


class FakeGenerateChunk:
    # Ollama streams generate chunks carrying ``model`` and a ``response`` delta;
    # the terminal ``done`` chunk also carries ``prompt_eval_count``/``eval_count``.
    def __init__(
        self,
        *,
        response: str | None = None,
        model: str | None = "llama3.2:1b",
        prompt_eval_count: int | None = None,
        eval_count: int | None = None,
    ) -> None:
        self.model = model
        self.response = response
        self.prompt_eval_count = prompt_eval_count
        self.eval_count = eval_count


class FakeAsyncStream:
    """An async iterator over pre-built chunks, like the ``AsyncClient`` stream.

    ``__aiter__`` makes the object an async stream that the wrapper detects, and
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


class OllamaChatIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_records_generation_with_model_and_usage_from_response(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            received: dict[str, object] = {}
            created: list[FakeChatResponse] = []

            def fake_chat(**kwargs: object) -> FakeChatResponse:
                received.update(kwargs)
                response = FakeChatResponse(
                    model="llama3.2:1b",
                    content="Bir traces locally.",
                    prompt_eval_count=12,
                    eval_count=6,
                )
                created.append(response)
                return response

            with trace("chat"):
                response = trace_chat(
                    fake_chat,
                    model="llama3.2:1b",
                    messages=[{"role": "user", "content": "What is Bir?"}],
                )
                # The wrapper forwards the request and returns the unchanged response.
                self.assertIs(response, created[0])

            self.assertEqual(received["model"], "llama3.2:1b")
            self.assertEqual(received["messages"], [{"role": "user", "content": "What is Bir?"}])

            traces = load_traces()
            self.assertEqual(len(traces), 1)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "ollama.chat")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "llama3.2:1b")
            self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18})
            self.assertEqual(generation_event.metadata["integration"], "ollama")
            self.assertEqual(generation_event.input["model"], "llama3.2:1b")
            self.assertEqual(generation_event.input["messages"], [{"role": "user", "content": "What is Bir?"}])
            self.assertEqual(generation_event.output["message"]["content"], "Bir traces locally.")

    def test_records_usage_from_mapping_response_and_computes_total(self) -> None:
        with temporary_workdir():
            def fake_chat(**kwargs: object) -> dict[str, object]:
                # Ollama responses also arrive as plain mappings; the wrapper reads
                # token counts tolerantly from either an object or a mapping.
                return {
                    "model": "llama3.2:1b",
                    "message": {"role": "assistant", "content": "ok"},
                    "prompt_eval_count": 7,
                    "eval_count": 3,
                }

            with trace("chat"):
                trace_chat(fake_chat, model="llama3.2:1b", messages=[])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.usage, {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})

    def test_falls_back_to_request_model_and_omits_usage_when_response_is_sparse(self) -> None:
        with temporary_workdir():
            def fake_chat(**kwargs: object) -> FakeChatResponse:
                return FakeChatResponse(model=None, content=None)

            with trace("chat"):
                trace_chat(fake_chat, model="llama3.2:1b", messages=[])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.model, "llama3.2:1b")
            self.assertIsNone(generation_event.usage)

    def test_forwards_ollama_options_and_applies_bir_options(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True)
            received: dict[str, object] = {}

            def fake_chat(**kwargs: object) -> FakeChatResponse:
                received.update(kwargs)
                return FakeChatResponse(model="llama3.2:1b")

            with trace("chat"):
                trace_chat(
                    fake_chat,
                    model="llama3.2:1b",
                    messages=[],
                    options={"temperature": 0},
                    bir_name="chat.turn",
                    bir_metadata={"feature": "qa"},
                )

            # Ollama's own ``options`` kwarg is forwarded to ``chat``, not consumed.
            self.assertEqual(received["options"], {"temperature": 0})
            self.assertNotIn("bir_name", received)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "chat.turn")
            self.assertEqual(generation_event.metadata["integration"], "ollama")
            self.assertEqual(generation_event.metadata["feature"], "qa")
            self.assertEqual(generation_event.input["options"], {"temperature": 0})

    def test_records_error_and_redacts_secret_when_chat_raises(self) -> None:
        with temporary_workdir():
            def fake_chat(**kwargs: object) -> FakeChatResponse:
                raise RuntimeError("request failed token=sk-secret123")

            with trace("chat"):
                with self.assertRaises(RuntimeError):
                    trace_chat(fake_chat, model="llama3.2:1b", messages=[])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "request failed token=[redacted]")

    def test_records_streamed_generation_after_chunks_are_consumed(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            chunks = [
                FakeChatChunk(content="Bir "),
                FakeChatChunk(content="streams"),
                FakeChatChunk(content="", prompt_eval_count=11, eval_count=4),
            ]
            received: dict[str, object] = {}

            def fake_stream(**kwargs: object) -> list[FakeChatChunk]:
                received.update(kwargs)
                return chunks

            consumed: list[FakeChatChunk] = []
            with trace("chat"):
                stream = trace_chat(
                    fake_stream,
                    model="llama3.2:1b",
                    messages=[{"role": "user", "content": "Stream it"}],
                    stream=True,
                )
                consumed = list(stream)

            # The chunks are yielded unchanged in order.
            self.assertEqual(consumed, chunks)
            self.assertIs(received["stream"], True)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "llama3.2:1b")
            self.assertEqual(generation_event.input["stream"], True)
            self.assertEqual(generation_event.output, "Bir streams")
            self.assertEqual(generation_event.usage, {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15})

    def test_stream_falls_back_when_provider_returns_full_response(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)

            def fake_chat(**kwargs: object) -> FakeChatResponse:
                # A provider that ignores ``stream=True`` and returns one response.
                return FakeChatResponse(
                    model="llama3.2:1b",
                    content="One shot.",
                    prompt_eval_count=7,
                    eval_count=3,
                )

            consumed: list[FakeChatResponse] = []
            with trace("chat"):
                stream = trace_chat(fake_chat, model="llama3.2:1b", messages=[], stream=True)
                consumed = list(stream)

            # The non-streamed response is recorded in one piece; the iterator yields nothing.
            self.assertEqual(consumed, [])
            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "llama3.2:1b")
            self.assertEqual(generation_event.usage, {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})
            self.assertEqual(generation_event.output["message"]["content"], "One shot.")

    def test_records_stream_error_and_redacts_secret(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)

            def failing_stream() -> Iterator[FakeChatChunk]:
                yield FakeChatChunk(content="partial token=sk-secret123 ")
                raise RuntimeError("stream failed token=sk-secret123")

            def fake_stream(**kwargs: object) -> Iterator[FakeChatChunk]:
                return failing_stream()

            with trace("chat"):
                stream = trace_chat(fake_stream, model="llama3.2:1b", messages=[], stream=True)
                with self.assertRaises(RuntimeError):
                    list(stream)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "stream failed token=[redacted]")
            # Partial output collected before the failure is recorded and redacted.
            self.assertEqual(generation_event.output, "partial token=[redacted] ")

    def test_requires_active_trace(self) -> None:
        with temporary_workdir():
            calls: list[dict[str, object]] = []

            def fake_chat(**kwargs: object) -> FakeChatResponse:
                calls.append(kwargs)
                return FakeChatResponse(model="llama3.2:1b")

            with self.assertRaises(RuntimeError):
                trace_chat(fake_chat, model="llama3.2:1b", messages=[])

            # The generation guard fires before the request is ever issued.
            self.assertEqual(calls, [])
            self.assertEqual(load_events(), [])


class OllamaGenerateIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_records_generation_with_model_and_usage_from_response(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            received: dict[str, object] = {}

            def fake_generate(**kwargs: object) -> FakeGenerateResponse:
                received.update(kwargs)
                return FakeGenerateResponse(
                    model="llama3.2:1b",
                    response="Bir traces locally.",
                    prompt_eval_count=9,
                    eval_count=5,
                )

            with trace("generate"):
                trace_generate(
                    fake_generate,
                    model="llama3.2:1b",
                    prompt="What is Bir?",
                )

            self.assertEqual(received["prompt"], "What is Bir?")

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "ollama.generate")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "llama3.2:1b")
            self.assertEqual(generation_event.usage, {"input_tokens": 9, "output_tokens": 5, "total_tokens": 14})
            self.assertEqual(generation_event.metadata["integration"], "ollama")
            self.assertEqual(generation_event.output["response"], "Bir traces locally.")

    def test_records_streamed_generation_after_chunks_are_consumed(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            chunks = [
                FakeGenerateChunk(response="Bir "),
                FakeGenerateChunk(response="streams"),
                FakeGenerateChunk(response="", prompt_eval_count=8, eval_count=2),
            ]

            def fake_stream(**kwargs: object) -> list[FakeGenerateChunk]:
                return chunks

            consumed: list[FakeGenerateChunk] = []
            with trace("generate"):
                stream = trace_generate(
                    fake_stream,
                    model="llama3.2:1b",
                    prompt="Stream it",
                    stream=True,
                )
                consumed = list(stream)

            self.assertEqual(consumed, chunks)
            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "llama3.2:1b")
            self.assertEqual(generation_event.output, "Bir streams")
            self.assertEqual(generation_event.usage, {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10})

    def test_records_error_and_redacts_secret_when_generate_raises(self) -> None:
        with temporary_workdir():
            def fake_generate(**kwargs: object) -> FakeGenerateResponse:
                raise RuntimeError("request failed token=sk-secret123")

            with trace("generate"):
                with self.assertRaises(RuntimeError):
                    trace_generate(fake_generate, model="llama3.2:1b", prompt="hi")

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "request failed token=[redacted]")


class OllamaAsyncIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_awaits_chat_and_records_model_and_usage(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            created: list[FakeChatResponse] = []

            async def fake_chat(**kwargs: object) -> FakeChatResponse:
                response = FakeChatResponse(
                    model="llama3.2:1b",
                    content="Bir traces locally.",
                    prompt_eval_count=12,
                    eval_count=6,
                )
                created.append(response)
                return response

            async def driver() -> object:
                async with trace("chat"):
                    return await trace_chat_async(
                        fake_chat,
                        model="llama3.2:1b",
                        messages=[{"role": "user", "content": "What is Bir?"}],
                    )

            response = asyncio.run(driver())
            self.assertIs(response, created[0])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "ollama.chat")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "llama3.2:1b")
            self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18})
            self.assertEqual(generation_event.metadata["integration"], "ollama")
            self.assertEqual(generation_event.output["message"]["content"], "Bir traces locally.")

    def test_awaits_generate_and_records_model_and_usage(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)

            async def fake_generate(**kwargs: object) -> FakeGenerateResponse:
                return FakeGenerateResponse(
                    model="llama3.2:1b",
                    response="ok",
                    prompt_eval_count=4,
                    eval_count=1,
                )

            async def driver() -> None:
                async with trace("generate"):
                    await trace_generate_async(fake_generate, model="llama3.2:1b", prompt="hi")

            asyncio.run(driver())

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "ollama.generate")
            self.assertEqual(generation_event.usage, {"input_tokens": 4, "output_tokens": 1, "total_tokens": 5})
            self.assertEqual(generation_event.output["response"], "ok")

    def test_consumes_bir_options_async(self) -> None:
        with temporary_workdir():
            received: dict[str, object] = {}

            async def fake_chat(**kwargs: object) -> FakeChatResponse:
                received.update(kwargs)
                return FakeChatResponse(model="llama3.2:1b")

            async def driver() -> None:
                async with trace("chat"):
                    await trace_chat_async(
                        fake_chat,
                        model="llama3.2:1b",
                        messages=[],
                        bir_name="chat.turn",
                        bir_metadata={"feature": "qa"},
                    )

            asyncio.run(driver())

            self.assertNotIn("bir_name", received)
            self.assertNotIn("bir_metadata", received)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "chat.turn")
            self.assertEqual(generation_event.metadata["feature"], "qa")

    def test_records_error_and_redacts_secret_when_chat_raises(self) -> None:
        with temporary_workdir():
            async def fake_chat(**kwargs: object) -> FakeChatResponse:
                raise RuntimeError("request failed token=sk-secret123")

            async def driver() -> None:
                async with trace("chat"):
                    await trace_chat_async(fake_chat, model="llama3.2:1b", messages=[])

            with self.assertRaises(RuntimeError):
                asyncio.run(driver())

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "request failed token=[redacted]")

    def test_records_streamed_chat_generation_after_chunks_are_consumed(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            chunks = [
                FakeChatChunk(content="Bir "),
                FakeChatChunk(content="streams"),
                FakeChatChunk(content="", prompt_eval_count=11, eval_count=4),
            ]
            received: dict[str, object] = {}

            async def fake_stream(**kwargs: object) -> FakeAsyncStream:
                received.update(kwargs)
                return FakeAsyncStream(chunks)

            async def driver() -> list[object]:
                collected: list[object] = []
                async with trace("chat"):
                    stream = await trace_chat_async(
                        fake_stream,
                        model="llama3.2:1b",
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
            self.assertEqual(generation_event.model, "llama3.2:1b")
            self.assertEqual(generation_event.input["stream"], True)
            self.assertEqual(generation_event.output, "Bir streams")
            self.assertEqual(generation_event.usage, {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15})

    def test_records_streamed_generate_generation_after_chunks_are_consumed(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)
            chunks = [
                FakeGenerateChunk(response="Bir "),
                FakeGenerateChunk(response="streams"),
                FakeGenerateChunk(response="", prompt_eval_count=8, eval_count=2),
            ]

            async def fake_stream(**kwargs: object) -> FakeAsyncStream:
                return FakeAsyncStream(chunks)

            async def driver() -> list[object]:
                collected: list[object] = []
                async with trace("generate"):
                    stream = await trace_generate_async(
                        fake_stream, model="llama3.2:1b", prompt="Stream it", stream=True
                    )
                    collected = [chunk async for chunk in stream]
                return collected

            consumed = asyncio.run(driver())

            self.assertEqual(consumed, chunks)
            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.output, "Bir streams")
            self.assertEqual(generation_event.usage, {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10})

    def test_records_partial_output_when_stream_closed_early(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)

            async def fake_stream(**kwargs: object) -> FakeAsyncStream:
                return FakeAsyncStream(
                    [FakeChatChunk(content="Bir "), FakeChatChunk(content="streams")]
                )

            async def driver() -> None:
                async with trace("chat"):
                    stream = await trace_chat_async(
                        fake_stream, model="llama3.2:1b", messages=[], stream=True
                    )
                    iterator = stream.__aiter__()
                    await iterator.__anext__()
                    # Closing after one chunk finalizes the accumulated output.
                    await stream.aclose()

            asyncio.run(driver())

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.output, "Bir ")

    def test_records_stream_error_and_redacts_secret(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)

            async def fake_stream(**kwargs: object) -> FakeAsyncStream:
                return FakeAsyncStream(
                    [FakeChatChunk(content="partial token=sk-secret123 ")],
                    error=RuntimeError("stream failed token=sk-secret123"),
                )

            async def driver() -> None:
                async with trace("chat"):
                    stream = await trace_chat_async(
                        fake_stream, model="llama3.2:1b", messages=[], stream=True
                    )
                    async for _chunk in stream:
                        pass

            with self.assertRaises(RuntimeError):
                asyncio.run(driver())

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "stream failed token=[redacted]")
            self.assertEqual(generation_event.output, "partial token=[redacted] ")

    def test_requires_active_trace(self) -> None:
        with temporary_workdir():
            calls: list[dict[str, object]] = []

            async def fake_chat(**kwargs: object) -> FakeChatResponse:
                calls.append(kwargs)
                return FakeChatResponse(model="llama3.2:1b")

            async def driver() -> None:
                await trace_chat_async(fake_chat, model="llama3.2:1b", messages=[])

            with self.assertRaises(RuntimeError):
                asyncio.run(driver())

            self.assertEqual(calls, [])
            self.assertEqual(load_events(), [])


if __name__ == "__main__":
    unittest.main()
