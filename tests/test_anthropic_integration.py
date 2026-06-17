from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from bir import configure, load_events, load_traces, trace
from bir._sdk import _reset_config_for_tests
from bir.integrations.anthropic import trace_messages


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
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeMessage:
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


class AnthropicIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_records_generation_with_model_and_usage_from_response(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            received: dict[str, object] = {}
            created: list[FakeMessage] = []

            def fake_create(**kwargs: object) -> FakeMessage:
                received.update(kwargs)
                message = FakeMessage(
                    model="claude-haiku-4-5-20251001",
                    usage=FakeUsage(input_tokens=12, output_tokens=6),
                    payload={
                        "id": "msg_1",
                        "model": "claude-haiku-4-5-20251001",
                        "content": [{"type": "text", "text": "Bir traces locally."}],
                    },
                )
                created.append(message)
                return message

            with trace("chat"):
                response = trace_messages(
                    fake_create,
                    model="claude-haiku-4-5",
                    messages=[{"role": "user", "content": "What is Bir?"}],
                )
                # The wrapper forwards the request and returns the unchanged response.
                self.assertIs(response, created[0])

            self.assertEqual(received["model"], "claude-haiku-4-5")
            self.assertEqual(received["messages"], [{"role": "user", "content": "What is Bir?"}])

            traces = load_traces()
            self.assertEqual(len(traces), 1)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "anthropic.messages")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "claude-haiku-4-5-20251001")
            self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18})
            self.assertEqual(generation_event.metadata["integration"], "anthropic")
            self.assertEqual(generation_event.input["model"], "claude-haiku-4-5")
            self.assertEqual(generation_event.input["messages"], [{"role": "user", "content": "What is Bir?"}])
            self.assertEqual(generation_event.output["content"][0]["text"], "Bir traces locally.")

    def test_records_usage_from_mapping_and_computes_total(self) -> None:
        with temporary_workdir():
            def fake_create(**kwargs: object) -> FakeMessage:
                return FakeMessage(
                    model="claude-haiku-4-5",
                    usage={"input_tokens": 7, "output_tokens": 3},
                )

            with trace("chat"):
                trace_messages(fake_create, model="claude-haiku-4-5", messages=[])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.usage, {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})

    def test_falls_back_to_request_model_and_omits_usage_when_response_is_sparse(self) -> None:
        with temporary_workdir():
            def fake_create(**kwargs: object) -> FakeMessage:
                return FakeMessage(model=None, usage=None, payload={"content": []})

            with trace("chat"):
                trace_messages(fake_create, model="claude-haiku-4-5", messages=[])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.model, "claude-haiku-4-5")
            self.assertIsNone(generation_event.usage)

    def test_forwards_anthropic_metadata_and_applies_bir_options(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True)
            received: dict[str, object] = {}

            def fake_create(**kwargs: object) -> FakeMessage:
                received.update(kwargs)
                return FakeMessage(model="claude-haiku-4-5")

            with trace("chat"):
                trace_messages(
                    fake_create,
                    model="claude-haiku-4-5",
                    messages=[],
                    metadata={"user_id": "u-1"},
                    bir_name="chat.turn",
                    bir_metadata={"feature": "qa"},
                )

            # Anthropic's own ``metadata`` kwarg is forwarded to ``create``, not consumed.
            self.assertEqual(received["metadata"], {"user_id": "u-1"})

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "chat.turn")
            self.assertEqual(generation_event.metadata["integration"], "anthropic")
            self.assertEqual(generation_event.metadata["feature"], "qa")
            self.assertEqual(generation_event.input["metadata"], {"user_id": "u-1"})

    def test_records_error_and_redacts_secret_when_create_raises(self) -> None:
        with temporary_workdir():
            def fake_create(**kwargs: object) -> FakeMessage:
                raise RuntimeError("request failed token=sk-ant-secret123")

            with trace("chat"):
                with self.assertRaises(RuntimeError):
                    trace_messages(fake_create, model="claude-haiku-4-5", messages=[])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "request failed token=[redacted]")

    def test_requires_active_trace(self) -> None:
        with temporary_workdir():
            calls: list[dict[str, object]] = []

            def fake_create(**kwargs: object) -> FakeMessage:
                calls.append(kwargs)
                return FakeMessage(model="claude-haiku-4-5")

            with self.assertRaises(RuntimeError):
                trace_messages(fake_create, model="claude-haiku-4-5", messages=[])

            # The generation guard fires before the request is ever issued.
            self.assertEqual(calls, [])
            self.assertEqual(load_events(), [])


if __name__ == "__main__":
    unittest.main()
