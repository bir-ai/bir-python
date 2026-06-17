from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from bir import configure, load_events, load_traces, trace
from bir._sdk import _reset_config_for_tests
from bir.integrations.google import trace_generate_content


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


class FakeGenerateContentResponse:
    # Gemini responses carry no top-level ``model``; the integration reads it from
    # the request keyword instead.
    def __init__(
        self,
        *,
        usage_metadata: object | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        self.usage_metadata = usage_metadata
        self._payload = payload or {}

    def model_dump(self) -> dict[str, object]:
        return dict(self._payload)


class GoogleIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_records_generation_with_model_from_request_and_usage_metadata(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            received: dict[str, object] = {}
            created: list[FakeGenerateContentResponse] = []

            def fake_generate(**kwargs: object) -> FakeGenerateContentResponse:
                received.update(kwargs)
                response = FakeGenerateContentResponse(
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
                response = trace_generate_content(
                    fake_generate,
                    model="gemini-2.5-flash",
                    contents="What is Bir?",
                )
                # The wrapper forwards the request and returns the unchanged response.
                self.assertIs(response, created[0])

            self.assertEqual(received["model"], "gemini-2.5-flash")
            self.assertEqual(received["contents"], "What is Bir?")

            traces = load_traces()
            self.assertEqual(len(traces), 1)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "google.generate_content")
            self.assertEqual(generation_event.status, "success")
            # Gemini responses omit a model, so it comes from the request keyword.
            self.assertEqual(generation_event.model, "gemini-2.5-flash")
            self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18})
            self.assertEqual(generation_event.metadata["integration"], "google")
            self.assertEqual(generation_event.input["model"], "gemini-2.5-flash")
            self.assertEqual(generation_event.input["contents"], "What is Bir?")
            self.assertEqual(
                generation_event.output["candidates"][0]["content"]["parts"][0]["text"],
                "Bir traces locally.",
            )

    def test_records_usage_from_mapping(self) -> None:
        with temporary_workdir():
            def fake_generate(**kwargs: object) -> FakeGenerateContentResponse:
                return FakeGenerateContentResponse(
                    usage_metadata={
                        "prompt_token_count": 7,
                        "candidates_token_count": 3,
                        "total_token_count": 10,
                    },
                )

            with trace("chat"):
                trace_generate_content(fake_generate, model="gemini-2.5-flash", contents=[])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.usage, {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})

    def test_uses_request_model_and_omits_usage_when_response_is_sparse(self) -> None:
        with temporary_workdir():
            def fake_generate(**kwargs: object) -> FakeGenerateContentResponse:
                return FakeGenerateContentResponse(usage_metadata=None, payload={"candidates": []})

            with trace("chat"):
                trace_generate_content(fake_generate, model="gemini-2.5-flash", contents=[])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.model, "gemini-2.5-flash")
            self.assertIsNone(generation_event.usage)

    def test_forwards_google_config_and_applies_bir_options(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True)
            received: dict[str, object] = {}

            def fake_generate(**kwargs: object) -> FakeGenerateContentResponse:
                received.update(kwargs)
                return FakeGenerateContentResponse()

            with trace("chat"):
                trace_generate_content(
                    fake_generate,
                    model="gemini-2.5-flash",
                    contents=[],
                    config={"temperature": 0.0},
                    bir_name="chat.turn",
                    bir_metadata={"feature": "qa"},
                )

            # Gemini's own ``config`` kwarg is forwarded to ``generate_content``, not consumed.
            self.assertEqual(received["config"], {"temperature": 0.0})

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "chat.turn")
            self.assertEqual(generation_event.metadata["integration"], "google")
            self.assertEqual(generation_event.metadata["feature"], "qa")
            self.assertEqual(generation_event.input["config"], {"temperature": 0.0})

    def test_records_error_and_redacts_secret_when_generate_raises(self) -> None:
        with temporary_workdir():
            def fake_generate(**kwargs: object) -> FakeGenerateContentResponse:
                raise RuntimeError("request failed api_key=AIzaSyExampleSecret123")

            with trace("chat"):
                with self.assertRaises(RuntimeError):
                    trace_generate_content(fake_generate, model="gemini-2.5-flash", contents=[])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "request failed api_key=[redacted]")

    def test_requires_active_trace(self) -> None:
        with temporary_workdir():
            calls: list[dict[str, object]] = []

            def fake_generate(**kwargs: object) -> FakeGenerateContentResponse:
                calls.append(kwargs)
                return FakeGenerateContentResponse()

            with self.assertRaises(RuntimeError):
                trace_generate_content(fake_generate, model="gemini-2.5-flash", contents=[])

            # The generation guard fires before the request is ever issued.
            self.assertEqual(calls, [])
            self.assertEqual(load_events(), [])


if __name__ == "__main__":
    unittest.main()
