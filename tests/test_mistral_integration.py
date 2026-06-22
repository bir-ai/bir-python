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
from bir.integrations.mistral import trace_chat, trace_chat_async


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
    # Mistral streams OpenAI-shaped chunks: a ``model``, ``choices[0].delta.content``,
    # and a final ``usage`` when requested.
    def __init__(
        self,
        *,
        content: str | None = None,
        usage: object | None = None,
        model: str | None = "mistral-small-2506",
    ) -> None:
        self.model = model
        self.choices = [FakeChoice(FakeDelta(content))]
        self.usage = usage


class MistralIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_records_generation_with_model_and_usage_from_response(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            received: dict[str, object] = {}
            created: list[FakeChatCompletion] = []

            def fake_complete(**kwargs: object) -> FakeChatCompletion:
                received.update(kwargs)
                completion = FakeChatCompletion(
                    model="mistral-small-2506",
                    usage=FakeUsage(prompt_tokens=12, completion_tokens=6, total_tokens=18),
                    payload={
                        "id": "chatcmpl-1",
                        "model": "mistral-small-2506",
                        "choices": [{"message": {"role": "assistant", "content": "Bir traces locally."}}],
                    },
                )
                created.append(completion)
                return completion

            with trace("chat"):
                response = trace_chat(
                    fake_complete,
                    model="mistral-small-latest",
                    messages=[{"role": "user", "content": "What is Bir?"}],
                )
                # The wrapper forwards the request and returns the unchanged response.
                self.assertIs(response, created[0])

            self.assertEqual(received["model"], "mistral-small-latest")
            self.assertEqual(received["messages"], [{"role": "user", "content": "What is Bir?"}])

            traces = load_traces()
            self.assertEqual(len(traces), 1)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "mistral.chat")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "mistral-small-2506")
            self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18})
            self.assertEqual(generation_event.metadata["integration"], "mistral")
            self.assertEqual(generation_event.input["model"], "mistral-small-latest")
            self.assertEqual(generation_event.input["messages"], [{"role": "user", "content": "What is Bir?"}])
            self.assertEqual(generation_event.output["choices"][0]["message"]["content"], "Bir traces locally.")

    def test_records_usage_from_mapping_and_computes_total(self) -> None:
        with temporary_workdir():
            def fake_complete(**kwargs: object) -> FakeChatCompletion:
                return FakeChatCompletion(
                    model="mistral-small-2506",
                    usage={"prompt_tokens": 7, "completion_tokens": 3},
                )

            with trace("chat"):
                trace_chat(fake_complete, model="mistral-small-latest", messages=[])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.usage, {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})

    def test_falls_back_to_request_model_and_omits_usage_when_response_is_sparse(self) -> None:
        with temporary_workdir():
            def fake_complete(**kwargs: object) -> FakeChatCompletion:
                return FakeChatCompletion(model=None, usage=None, payload={"choices": []})

            with trace("chat"):
                trace_chat(fake_complete, model="mistral-small-latest", messages=[])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.model, "mistral-small-latest")
            self.assertIsNone(generation_event.usage)

    def test_forwards_mistral_metadata_and_applies_bir_options(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True)
            received: dict[str, object] = {}

            def fake_complete(**kwargs: object) -> FakeChatCompletion:
                received.update(kwargs)
                return FakeChatCompletion(model="mistral-small-2506")

            with trace("chat"):
                trace_chat(
                    fake_complete,
                    model="mistral-small-latest",
                    messages=[],
                    metadata={"mistral_request_id": "req-1"},
                    bir_name="chat.turn",
                    bir_metadata={"feature": "qa"},
                )

            # Mistral's own ``metadata`` kwarg is forwarded to ``complete``, not consumed.
            self.assertEqual(received["metadata"], {"mistral_request_id": "req-1"})

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "chat.turn")
            self.assertEqual(generation_event.metadata["integration"], "mistral")
            self.assertEqual(generation_event.metadata["feature"], "qa")
            self.assertEqual(generation_event.input["metadata"], {"mistral_request_id": "req-1"})

    def test_records_error_and_redacts_secret_when_complete_raises(self) -> None:
        with temporary_workdir():
            def fake_complete(**kwargs: object) -> FakeChatCompletion:
                raise RuntimeError("request failed token=sk-secret123")

            with trace("chat"):
                with self.assertRaises(RuntimeError):
                    trace_chat(fake_complete, model="mistral-small-latest", messages=[])

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

            def fake_stream(**kwargs: object) -> list[FakeChatCompletionChunk]:
                received.update(kwargs)
                return chunks

            consumed: list[FakeChatCompletionChunk] = []
            with trace("chat"):
                stream = trace_chat(
                    fake_stream,
                    model="mistral-small-latest",
                    messages=[{"role": "user", "content": "Stream it"}],
                    stream=True,
                )
                consumed = list(stream)

            # The chunks are yielded unchanged in order.
            self.assertEqual(consumed, chunks)
            self.assertIs(received["stream"], True)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "success")
            # The response model from the chunks refines the request model.
            self.assertEqual(generation_event.model, "mistral-small-2506")
            self.assertEqual(generation_event.input["stream"], True)
            self.assertEqual(generation_event.output, "Bir streams")
            self.assertEqual(generation_event.usage, {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15})

    def test_stream_falls_back_when_provider_returns_full_response(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)

            def fake_complete(**kwargs: object) -> FakeChatCompletion:
                # A provider that ignores ``stream=True`` and returns one response.
                return FakeChatCompletion(
                    model="mistral-small-2506",
                    usage=FakeUsage(prompt_tokens=7, completion_tokens=3, total_tokens=10),
                    payload={"choices": [{"message": {"content": "One shot."}}]},
                )

            consumed: list[FakeChatCompletion] = []
            with trace("chat"):
                stream = trace_chat(fake_complete, model="mistral-small-latest", messages=[], stream=True)
                consumed = list(stream)

            # The non-streamed response is recorded in one piece; the iterator yields nothing.
            self.assertEqual(consumed, [])
            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "mistral-small-2506")
            self.assertEqual(generation_event.usage, {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})
            self.assertEqual(generation_event.output["choices"][0]["message"]["content"], "One shot.")

    def test_records_stream_error_and_redacts_secret(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)

            def failing_stream() -> Iterator[FakeChatCompletionChunk]:
                yield FakeChatCompletionChunk(content="partial token=sk-secret123 ")
                raise RuntimeError("stream failed token=sk-secret123")

            def fake_stream(**kwargs: object) -> Iterator[FakeChatCompletionChunk]:
                return failing_stream()

            with trace("chat"):
                stream = trace_chat(fake_stream, model="mistral-small-latest", messages=[], stream=True)
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

            def fake_complete(**kwargs: object) -> FakeChatCompletion:
                calls.append(kwargs)
                return FakeChatCompletion(model="mistral-small-2506")

            with self.assertRaises(RuntimeError):
                trace_chat(fake_complete, model="mistral-small-latest", messages=[])

            # The generation guard fires before the request is ever issued.
            self.assertEqual(calls, [])
            self.assertEqual(load_events(), [])


class MistralAsyncIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_awaits_complete_and_records_model_and_usage(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            created: list[FakeChatCompletion] = []

            async def fake_complete(**kwargs: object) -> FakeChatCompletion:
                completion = FakeChatCompletion(
                    model="mistral-small-2506",
                    usage=FakeUsage(prompt_tokens=12, completion_tokens=6, total_tokens=18),
                    payload={
                        "id": "chatcmpl-1",
                        "model": "mistral-small-2506",
                        "choices": [{"message": {"role": "assistant", "content": "Bir traces locally."}}],
                    },
                )
                created.append(completion)
                return completion

            async def driver() -> object:
                async with trace("chat"):
                    return await trace_chat_async(
                        fake_complete,
                        model="mistral-small-latest",
                        messages=[{"role": "user", "content": "What is Bir?"}],
                    )

            response = asyncio.run(driver())
            self.assertIs(response, created[0])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "mistral.chat")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "mistral-small-2506")
            self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18})
            self.assertEqual(generation_event.metadata["integration"], "mistral")
            self.assertEqual(generation_event.output["choices"][0]["message"]["content"], "Bir traces locally.")

    def test_forwards_metadata_and_consumes_bir_options(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True)
            received: dict[str, object] = {}

            async def fake_complete(**kwargs: object) -> FakeChatCompletion:
                received.update(kwargs)
                return FakeChatCompletion(model="mistral-small-2506")

            async def driver() -> None:
                async with trace("chat"):
                    await trace_chat_async(
                        fake_complete,
                        model="mistral-small-latest",
                        messages=[],
                        metadata={"mistral_request_id": "req-1"},
                        bir_name="chat.turn",
                        bir_metadata={"feature": "qa"},
                    )

            asyncio.run(driver())

            self.assertEqual(received["metadata"], {"mistral_request_id": "req-1"})
            self.assertNotIn("bir_name", received)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "chat.turn")
            self.assertEqual(generation_event.metadata["feature"], "qa")

    def test_records_error_and_redacts_secret_when_complete_raises(self) -> None:
        with temporary_workdir():
            async def fake_complete(**kwargs: object) -> FakeChatCompletion:
                raise RuntimeError("request failed token=sk-secret123")

            async def driver() -> None:
                async with trace("chat"):
                    await trace_chat_async(fake_complete, model="mistral-small-latest", messages=[])

            with self.assertRaises(RuntimeError):
                asyncio.run(driver())

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "request failed token=[redacted]")

    def test_requires_active_trace(self) -> None:
        with temporary_workdir():
            calls: list[dict[str, object]] = []

            async def fake_complete(**kwargs: object) -> FakeChatCompletion:
                calls.append(kwargs)
                return FakeChatCompletion(model="mistral-small-2506")

            async def driver() -> None:
                await trace_chat_async(fake_complete, model="mistral-small-latest", messages=[])

            with self.assertRaises(RuntimeError):
                asyncio.run(driver())

            self.assertEqual(calls, [])
            self.assertEqual(load_events(), [])


if __name__ == "__main__":
    unittest.main()
