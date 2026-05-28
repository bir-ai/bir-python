from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from bir import configure, generation, observe, score, span
from bir._sdk import _reset_config_for_tests


def read_events(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


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


class SdkTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_observe_creates_trace_event(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer(question: str) -> str:
                return "ok"

            self.assertEqual(answer("hello"), "ok")

            events = read_events(workdir / ".bir" / "traces.jsonl")
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["schema_version"], "1.0")
            self.assertEqual(event["type"], "trace")
            self.assertEqual(event["name"], "answer")
            self.assertEqual(event["id"], event["trace_id"])
            self.assertIsNone(event["parent_id"])
            self.assertEqual(event["status"], "success")
            self.assertIsNone(event["input"])
            self.assertIsNone(event["output"])
            self.assertIsNone(event["error"])
            self.assertIsInstance(event["start_time"], str)
            self.assertIsInstance(event["end_time"], str)

    def test_span_creates_nested_event(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer() -> None:
                with span("retrieve_context"):
                    pass

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            span_event = next(event for event in events if event["type"] == "span")
            trace_event = next(event for event in events if event["type"] == "trace")
            self.assertEqual(span_event["trace_id"], trace_event["trace_id"])
            self.assertEqual(span_event["parent_id"], trace_event["id"])
            self.assertEqual(span_event["name"], "retrieve_context")
            self.assertEqual(span_event["status"], "success")
            self.assertIsNone(span_event["error"])

    def test_score_records_score_event(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer() -> None:
                score("helpfulness", 0.82)

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            score_event = next(event for event in events if event["type"] == "score")
            trace_event = next(event for event in events if event["type"] == "trace")
            self.assertEqual(score_event["trace_id"], trace_event["trace_id"])
            self.assertEqual(score_event["parent_id"], trace_event["id"])
            self.assertEqual(score_event["name"], "helpfulness")
            self.assertEqual(score_event["value"], 0.82)
            self.assertEqual(score_event["status"], "success")

    def test_generation_records_llm_call_event(self) -> None:
        with temporary_workdir() as workdir:

            @observe(capture_inputs=True, capture_outputs=True)
            def answer(question: str) -> str:
                with generation(
                    "openai.chat",
                    model="gpt-4o-mini",
                    input={"question": question, "api_key": "sk-test"},
                    metadata={"provider": "openai"},
                ) as gen:
                    response = "hello"
                    gen.set_output({"message": response, "token": "response-token"})
                    gen.set_usage(input_tokens=5, output_tokens=7)
                    return response

            answer("hi")

            events = read_events(workdir / ".bir" / "traces.jsonl")
            generation_event = next(event for event in events if event["type"] == "generation")
            trace_event = next(event for event in events if event["type"] == "trace")
            self.assertEqual(generation_event["trace_id"], trace_event["trace_id"])
            self.assertEqual(generation_event["parent_id"], trace_event["id"])
            self.assertEqual(generation_event["name"], "openai.chat")
            self.assertEqual(generation_event["model"], "gpt-4o-mini")
            self.assertEqual(generation_event["metadata"], {"provider": "openai"})
            self.assertEqual(generation_event["input"], {"question": "hi", "api_key": "[redacted]"})
            self.assertEqual(generation_event["output"], {"message": "hello", "token": "[redacted]"})
            self.assertEqual(
                generation_event["usage"],
                {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
            )
            self.assertEqual(generation_event["status"], "success")

    def test_generation_capture_can_be_enabled_per_call(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer() -> None:
                with generation(
                    "local.llm",
                    input={"prompt": "hello"},
                    capture_input=True,
                    capture_output=True,
                ) as gen:
                    gen.set_output("world")

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            generation_event = next(event for event in events if event["type"] == "generation")
            self.assertEqual(generation_event["input"], {"prompt": "hello"})
            self.assertEqual(generation_event["output"], "world")

    def test_generation_exception_is_captured_and_reraised(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def fail() -> None:
                with generation("openai.chat"):
                    raise RuntimeError("provider failed")

            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                fail()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            generation_event = next(event for event in events if event["type"] == "generation")
            trace_event = next(event for event in events if event["type"] == "trace")
            self.assertEqual(generation_event["status"], "error")
            self.assertEqual(generation_event["error"], "provider failed")
            self.assertEqual(trace_event["status"], "error")

    def test_exceptions_are_captured_and_reraised(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def fail() -> None:
                with span("explode"):
                    raise ValueError("boom")

            with self.assertRaisesRegex(ValueError, "boom"):
                fail()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            span_event = next(event for event in events if event["type"] == "span")
            trace_event = next(event for event in events if event["type"] == "trace")
            self.assertEqual(span_event["status"], "error")
            self.assertEqual(span_event["error"], "boom")
            self.assertEqual(trace_event["status"], "error")
            self.assertEqual(trace_event["error"], "boom")

    def test_configure_sets_trace_path(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "custom" / "events.jsonl"
            configure(trace_path=trace_path)

            @observe()
            def answer() -> str:
                return "ok"

            answer()

            self.assertTrue(trace_path.exists())
            events = read_events(trace_path)
            self.assertEqual(events[0]["name"], "answer")

    def test_observe_can_capture_inputs_and_outputs(self) -> None:
        with temporary_workdir() as workdir:

            @observe(capture_inputs=True, capture_outputs=True)
            def answer(question: str, count: int = 1) -> dict[str, object]:
                return {"answer": question.upper(), "count": count}

            answer("hello", count=2)

            events = read_events(workdir / ".bir" / "traces.jsonl")
            event = events[0]
            self.assertEqual(event["input"], {"question": "hello", "count": 2})
            self.assertEqual(event["output"], {"answer": "HELLO", "count": 2})

    def test_configure_can_enable_input_and_output_capture(self) -> None:
        with temporary_workdir() as workdir:
            configure(capture_inputs=True, capture_outputs=True)

            @observe()
            def answer(question: str) -> str:
                return question.upper()

            answer("hello")

            events = read_events(workdir / ".bir" / "traces.jsonl")
            event = events[0]
            self.assertEqual(event["input"], {"question": "hello"})
            self.assertEqual(event["output"], "HELLO")

    def test_capture_redacts_secret_like_inputs_and_outputs(self) -> None:
        with temporary_workdir() as workdir:

            @observe(capture_inputs=True, capture_outputs=True)
            def call_llm(api_key: str, payload: dict[str, object]) -> dict[str, object]:
                return {"token": "output-token", "message": payload["message"]}

            call_llm("sk-test", {"message": "hello", "authorization": "Bearer secret"})

            events = read_events(workdir / ".bir" / "traces.jsonl")
            event = events[0]
            self.assertEqual(
                event["input"],
                {"api_key": "[redacted]", "payload": {"message": "hello", "authorization": "[redacted]"}},
            )
            self.assertEqual(event["output"], {"token": "[redacted]", "message": "hello"})


if __name__ == "__main__":
    unittest.main()
