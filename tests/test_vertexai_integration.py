from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from bir import configure, load_events, load_traces, trace
from bir._sdk import _reset_config_for_tests
from bir.integrations.vertexai import trace_generate_content


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


if __name__ == "__main__":
    unittest.main()
