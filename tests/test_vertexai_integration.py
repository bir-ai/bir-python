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
from bir.integrations.vertexai import trace_generate_content, trace_generate_content_async


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


class FakeUsageMetadata:
    def __init__(
        self,
        *,
        prompt_token_count: int | None = None,
        candidates_token_count: int | None = None,
        total_token_count: int | None = None,
    ) -> None:
        self.prompt_token_count = prompt_token_count
        self.candidates_token_count = candidates_token_count
        self.total_token_count = total_token_count


class FakeGenerationResponse:
    # Vertex's ``GenerationResponse`` carries the resolved model on
    # ``model_version`` in recent SDK versions; otherwise the model is supplied by
    # the caller through ``bir_model``.
    def __init__(
        self,
        *,
        model_version: str | None = None,
        usage_metadata: object | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        self.model_version = model_version
        self.usage_metadata = usage_metadata
        self._payload = payload or {}

    def model_dump(self) -> dict[str, object]:
        return dict(self._payload)


class FakeStreamChunk:
    # A streaming ``GenerationResponse`` chunk: ``text`` concatenates the candidate
    # parts in the real SDK, with ``model_version`` and ``usage_metadata`` carried
    # on the chunks that resolve them.
    def __init__(
        self,
        *,
        text: str | None = None,
        model_version: str | None = None,
        usage_metadata: object | None = None,
        candidates: object | None = None,
    ) -> None:
        self.text = text
        self.model_version = model_version
        self.usage_metadata = usage_metadata
        self.candidates = candidates


class FakeRaisingTextChunk:
    # A chunk whose ``text`` accessor raises (as the real Vertex accessor does when
    # a chunk carries no single text part), forcing the candidate-parts fallback.
    def __init__(
        self,
        *,
        candidates: object | None = None,
        usage_metadata: object | None = None,
        model_version: str | None = None,
    ) -> None:
        self._candidates = candidates
        self.usage_metadata = usage_metadata
        self.model_version = model_version

    @property
    def text(self) -> str:
        raise ValueError("Content has no parts")

    @property
    def candidates(self) -> object | None:
        return self._candidates


class VertexAIIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_records_generation_with_model_version_and_usage_metadata(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            received_args: list[object] = []
            created: list[FakeGenerationResponse] = []

            def fake_generate(*args: object, **kwargs: object) -> FakeGenerationResponse:
                received_args.extend(args)
                response = FakeGenerationResponse(
                    model_version="gemini-1.5-flash-002",
                    usage_metadata=FakeUsageMetadata(
                        prompt_token_count=12,
                        candidates_token_count=6,
                        total_token_count=18,
                    ),
                    payload={
                        "candidates": [
                            {"content": {"role": "model", "parts": [{"text": "Bir traces locally."}]}}
                        ],
                    },
                )
                created.append(response)
                return response

            with trace("chat"):
                # Vertex binds the model to the GenerativeModel, so contents pass positionally.
                response = trace_generate_content(
                    fake_generate,
                    "What is Bir?",
                    bir_model="gemini-1.5-flash",
                )
                # The wrapper forwards the request and returns the unchanged response.
                self.assertIs(response, created[0])

            self.assertEqual(received_args, ["What is Bir?"])

            traces = load_traces()
            self.assertEqual(len(traces), 1)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "vertexai.generate_content")
            self.assertEqual(generation_event.status, "success")
            # ``bir_model`` seeds the model; the response ``model_version`` refines it.
            self.assertEqual(generation_event.model, "gemini-1.5-flash-002")
            self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18})
            self.assertEqual(generation_event.metadata["integration"], "vertexai")
            self.assertEqual(generation_event.input["args"], ["What is Bir?"])
            self.assertEqual(
                generation_event.output["candidates"][0]["content"]["parts"][0]["text"],
                "Bir traces locally.",
            )

    def test_records_usage_from_mapping_and_computes_total(self) -> None:
        with temporary_workdir():
            # A dict response without ``model_version`` or ``total_token_count``:
            # the model falls back to ``bir_model`` and the total is derived.
            def fake_generate(*args: object, **kwargs: object) -> dict[str, object]:
                return {"usage_metadata": {"prompt_token_count": 7, "candidates_token_count": 3}}

            with trace("chat"):
                trace_generate_content(fake_generate, contents=[], bir_model="gemini-1.5-pro")

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.model, "gemini-1.5-pro")
            self.assertEqual(generation_event.usage, {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})

    def test_uses_bir_model_and_omits_usage_when_response_is_sparse(self) -> None:
        with temporary_workdir():
            def fake_generate(*args: object, **kwargs: object) -> FakeGenerationResponse:
                return FakeGenerationResponse(usage_metadata=None, payload={"candidates": []})

            with trace("chat"):
                trace_generate_content(fake_generate, contents=[], bir_model="gemini-1.5-flash")

            generation_event = next(event for event in load_events() if event.type == "generation")
            # No ``model_version`` on the response, so the caller's ``bir_model`` stands.
            self.assertEqual(generation_event.model, "gemini-1.5-flash")
            self.assertIsNone(generation_event.usage)

    def test_forwards_vertex_config_and_applies_bir_options(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True)
            received: dict[str, object] = {}

            def fake_generate(*args: object, **kwargs: object) -> FakeGenerationResponse:
                received.update(kwargs)
                return FakeGenerationResponse()

            with trace("chat"):
                trace_generate_content(
                    fake_generate,
                    contents=[],
                    generation_config={"temperature": 0.0},
                    bir_name="chat.turn",
                    bir_model="gemini-1.5-flash",
                    bir_metadata={"feature": "qa"},
                )

            # Vertex's own ``generation_config`` kwarg is forwarded to ``generate_content``, not consumed.
            self.assertEqual(received["generation_config"], {"temperature": 0.0})

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "chat.turn")
            self.assertEqual(generation_event.model, "gemini-1.5-flash")
            self.assertEqual(generation_event.metadata["integration"], "vertexai")
            self.assertEqual(generation_event.metadata["feature"], "qa")
            self.assertEqual(generation_event.input["generation_config"], {"temperature": 0.0})

    def test_records_error_and_redacts_secret_when_generate_raises(self) -> None:
        with temporary_workdir():
            def fake_generate(*args: object, **kwargs: object) -> FakeGenerationResponse:
                raise RuntimeError("request failed api_key=AIzaSyExampleSecret123")

            with trace("chat"):
                with self.assertRaises(RuntimeError):
                    trace_generate_content(fake_generate, contents=[], bir_model="gemini-1.5-flash")

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "request failed api_key=[redacted]")

    def test_records_streamed_generation_after_chunks_are_consumed(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            chunks = [
                FakeStreamChunk(text="Bir ", model_version="gemini-1.5-flash-002"),
                FakeStreamChunk(text="streams"),
                FakeStreamChunk(
                    usage_metadata=FakeUsageMetadata(
                        prompt_token_count=12,
                        candidates_token_count=6,
                        total_token_count=18,
                    ),
                ),
            ]
            received: dict[str, object] = {}

            def fake_generate(*args: object, **kwargs: object) -> list[FakeStreamChunk]:
                received.update(kwargs)
                return chunks

            consumed: list[object] = []
            with trace("chat"):
                stream = trace_generate_content(
                    fake_generate,
                    "Stream it",
                    bir_model="gemini-1.5-flash",
                    stream=True,
                )
                consumed = list(stream)

            # The chunks are yielded unchanged and in order.
            self.assertEqual(consumed, chunks)
            self.assertIs(received["stream"], True)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "vertexai.generate_content")
            self.assertEqual(generation_event.status, "success")
            # ``bir_model`` seeds the model; a chunk ``model_version`` refines it.
            self.assertEqual(generation_event.model, "gemini-1.5-flash-002")
            self.assertEqual(generation_event.output, "Bir streams")
            self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18})
            self.assertEqual(generation_event.input["args"], ["Stream it"])
            self.assertEqual(generation_event.input["stream"], True)

    def test_streamed_text_falls_back_to_candidate_parts(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)
            # The first chunk exposes no ``text``; the second's ``text`` accessor
            # raises. Both fall back to reading the candidate parts directly.
            chunks = [
                FakeStreamChunk(
                    text=None,
                    candidates=[{"content": {"parts": [{"text": "Bir"}]}}],
                ),
                FakeRaisingTextChunk(
                    candidates=[{"content": {"parts": [{"text": " parts"}]}}],
                    usage_metadata=FakeUsageMetadata(prompt_token_count=5, candidates_token_count=2),
                ),
            ]

            def fake_generate(*args: object, **kwargs: object) -> list[object]:
                return chunks

            with trace("chat"):
                stream = trace_generate_content(
                    fake_generate, contents=[], bir_model="gemini-1.5-flash", stream=True
                )
                self.assertEqual(list(stream), chunks)

            generation_event = next(event for event in load_events() if event.type == "generation")
            # No chunk carried a ``model_version``, so ``bir_model`` stands.
            self.assertEqual(generation_event.model, "gemini-1.5-flash")
            self.assertEqual(generation_event.output, "Bir parts")
            self.assertEqual(generation_event.usage, {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7})

    def test_stream_falls_back_when_provider_returns_full_response(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)

            def fake_generate(*args: object, **kwargs: object) -> FakeGenerationResponse:
                # A provider that ignored ``stream=True`` and returned one response.
                return FakeGenerationResponse(
                    model_version="gemini-1.5-flash-002",
                    usage_metadata=FakeUsageMetadata(prompt_token_count=7, candidates_token_count=3),
                    payload={"candidates": [{"content": {"parts": [{"text": "One shot."}]}}]},
                )

            consumed: list[object] = []
            with trace("chat"):
                stream = trace_generate_content(
                    fake_generate, contents=[], bir_model="gemini-1.5-flash", stream=True
                )
                consumed = list(stream)

            # The non-streamed response is recorded in one piece; nothing is yielded.
            self.assertEqual(consumed, [])
            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "gemini-1.5-flash-002")
            self.assertEqual(generation_event.usage, {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})
            self.assertEqual(
                generation_event.output["candidates"][0]["content"]["parts"][0]["text"],
                "One shot.",
            )

    def test_records_stream_error_and_redacts_secret(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)

            def failing_stream() -> Iterator[FakeStreamChunk]:
                yield FakeStreamChunk(text="partial api_key=sk-secret123 ")
                raise RuntimeError("stream failed api_key=sk-secret123")

            def fake_generate(*args: object, **kwargs: object) -> Iterator[FakeStreamChunk]:
                return failing_stream()

            with trace("chat"):
                stream = trace_generate_content(
                    fake_generate, contents=[], bir_model="gemini-1.5-flash", stream=True
                )
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

            def fake_generate(*args: object, **kwargs: object) -> FakeGenerationResponse:
                calls.append(kwargs)
                return FakeGenerationResponse()

            with self.assertRaises(RuntimeError):
                trace_generate_content(fake_generate, contents=[], bir_model="gemini-1.5-flash")

            # The generation guard fires before the request is ever issued.
            self.assertEqual(calls, [])
            self.assertEqual(load_events(), [])


class VertexAIAsyncIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_awaits_generate_and_records_model_version_and_usage(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            received_args: list[object] = []
            created: list[FakeGenerationResponse] = []

            async def fake_generate(*args: object, **kwargs: object) -> FakeGenerationResponse:
                received_args.extend(args)
                response = FakeGenerationResponse(
                    model_version="gemini-1.5-flash-002",
                    usage_metadata=FakeUsageMetadata(
                        prompt_token_count=12,
                        candidates_token_count=6,
                        total_token_count=18,
                    ),
                    payload={
                        "candidates": [
                            {"content": {"role": "model", "parts": [{"text": "Bir traces locally."}]}}
                        ],
                    },
                )
                created.append(response)
                return response

            async def driver() -> object:
                async with trace("chat"):
                    # Vertex binds the model to the GenerativeModel, so contents pass positionally.
                    return await trace_generate_content_async(
                        fake_generate,
                        "What is Bir?",
                        bir_model="gemini-1.5-flash",
                    )

            response = asyncio.run(driver())
            # The wrapper forwards the request and returns the unchanged response.
            self.assertIs(response, created[0])

            self.assertEqual(received_args, ["What is Bir?"])

            traces = load_traces()
            self.assertEqual(len(traces), 1)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "vertexai.generate_content")
            self.assertEqual(generation_event.status, "success")
            # ``bir_model`` seeds the model; the response ``model_version`` refines it.
            self.assertEqual(generation_event.model, "gemini-1.5-flash-002")
            self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18})
            self.assertEqual(generation_event.metadata["integration"], "vertexai")
            self.assertEqual(generation_event.input["args"], ["What is Bir?"])
            self.assertEqual(
                generation_event.output["candidates"][0]["content"]["parts"][0]["text"],
                "Bir traces locally.",
            )

    def test_uses_bir_model_and_omits_usage_when_response_is_sparse(self) -> None:
        with temporary_workdir():
            async def fake_generate(*args: object, **kwargs: object) -> FakeGenerationResponse:
                return FakeGenerationResponse(usage_metadata=None, payload={"candidates": []})

            async def driver() -> None:
                async with trace("chat"):
                    await trace_generate_content_async(
                        fake_generate, contents=[], bir_model="gemini-1.5-flash"
                    )

            asyncio.run(driver())

            generation_event = next(event for event in load_events() if event.type == "generation")
            # No ``model_version`` on the response, so the caller's ``bir_model`` stands.
            self.assertEqual(generation_event.model, "gemini-1.5-flash")
            self.assertIsNone(generation_event.usage)

    def test_forwards_vertex_config_and_consumes_bir_options(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True)
            received: dict[str, object] = {}

            async def fake_generate(*args: object, **kwargs: object) -> FakeGenerationResponse:
                received.update(kwargs)
                return FakeGenerationResponse()

            async def driver() -> None:
                async with trace("chat"):
                    await trace_generate_content_async(
                        fake_generate,
                        contents=[],
                        generation_config={"temperature": 0.0},
                        bir_name="chat.turn",
                        bir_model="gemini-1.5-flash",
                        bir_metadata={"feature": "qa"},
                    )

            asyncio.run(driver())

            # Vertex's own ``generation_config`` kwarg is forwarded; the bir_ options are consumed.
            self.assertEqual(received["generation_config"], {"temperature": 0.0})
            self.assertNotIn("bir_model", received)
            self.assertNotIn("bir_name", received)
            self.assertNotIn("bir_metadata", received)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "chat.turn")
            self.assertEqual(generation_event.model, "gemini-1.5-flash")
            self.assertEqual(generation_event.metadata["integration"], "vertexai")
            self.assertEqual(generation_event.metadata["feature"], "qa")
            self.assertEqual(generation_event.input["generation_config"], {"temperature": 0.0})

    def test_does_not_capture_input_or_output_by_default(self) -> None:
        with temporary_workdir():
            # Capture is opt-in: without configure() the request and response are
            # recorded as model + usage only, never the payloads.
            async def fake_generate(*args: object, **kwargs: object) -> FakeGenerationResponse:
                return FakeGenerationResponse(
                    usage_metadata=FakeUsageMetadata(prompt_token_count=7, candidates_token_count=3),
                    payload={"candidates": [{"content": {"parts": [{"text": "secret"}]}}]},
                )

            async def driver() -> None:
                async with trace("chat"):
                    await trace_generate_content_async(
                        fake_generate, contents=[], bir_model="gemini-1.5-flash"
                    )

            asyncio.run(driver())

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertIsNone(generation_event.input)
            self.assertIsNone(generation_event.output)
            self.assertEqual(generation_event.usage, {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})

    def test_records_error_and_redacts_secret_when_generate_raises(self) -> None:
        with temporary_workdir():
            async def fake_generate(*args: object, **kwargs: object) -> FakeGenerationResponse:
                raise RuntimeError("request failed api_key=AIzaSyExampleSecret123")

            async def driver() -> None:
                async with trace("chat"):
                    await trace_generate_content_async(
                        fake_generate, contents=[], bir_model="gemini-1.5-flash"
                    )

            with self.assertRaises(RuntimeError):
                asyncio.run(driver())

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "request failed api_key=[redacted]")

    def test_requires_active_trace(self) -> None:
        with temporary_workdir():
            calls: list[dict[str, object]] = []

            async def fake_generate(*args: object, **kwargs: object) -> FakeGenerationResponse:
                calls.append(kwargs)
                return FakeGenerationResponse()

            async def driver() -> None:
                await trace_generate_content_async(
                    fake_generate, contents=[], bir_model="gemini-1.5-flash"
                )

            with self.assertRaises(RuntimeError):
                asyncio.run(driver())

            # The generation guard fires before the request is ever issued.
            self.assertEqual(calls, [])
            self.assertEqual(load_events(), [])


if __name__ == "__main__":
    unittest.main()
