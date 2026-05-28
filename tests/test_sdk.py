from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from bir import configure, generation, load_events, load_traces, observe, score, send_events, span, tool_call
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


class FakeHttpResponse:
    status = 201

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return b'{"accepted":1}'


def posted_request_body(request: object) -> dict[str, object]:
    data = getattr(request, "data")
    if not isinstance(data, bytes):
        raise TypeError("expected request data to be bytes")
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("expected request body to be a JSON object")
    return payload


def request_url(request: object) -> str:
    url = getattr(request, "full_url")
    if not isinstance(url, str):
        raise TypeError("expected request to have a full_url")
    return url


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

    def test_score_uses_current_nested_parent(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer() -> None:
                with span("retrieve_context"):
                    score("context_quality", 0.7)
                    with generation("local.llm"):
                        score("generation_quality", 0.8)

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            span_event = next(event for event in events if event["type"] == "span")
            generation_event = next(event for event in events if event["type"] == "generation")
            context_score = next(event for event in events if event["name"] == "context_quality")
            generation_score = next(event for event in events if event["name"] == "generation_quality")
            self.assertEqual(context_score["parent_id"], span_event["id"])
            self.assertEqual(generation_score["parent_id"], generation_event["id"])

    def test_score_rejects_non_finite_values(self) -> None:
        with temporary_workdir():

            @observe()
            def answer() -> None:
                score("bad", float("nan"))

            with self.assertRaisesRegex(ValueError, "score value must be finite"):
                answer()

    def test_generation_records_llm_call_event(self) -> None:
        with temporary_workdir() as workdir:

            @observe(capture_inputs=True, capture_outputs=True)
            def answer(question: str) -> str:
                response = "hello"
                with generation(
                    "openai.chat",
                    model="gpt-4o-mini",
                    input={"question": question, "api_key": "sk-test"},
                    metadata={"provider": "openai"},
                ) as gen:
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

    def test_generation_usage_rejects_non_finite_values(self) -> None:
        with temporary_workdir():

            @observe()
            def answer() -> None:
                with generation("local.llm") as gen:
                    gen.set_usage(input_tokens=float("inf"))

            with self.assertRaisesRegex(ValueError, "input_tokens must be finite"):
                answer()

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

    def test_tool_call_records_external_call_event(self) -> None:
        with temporary_workdir() as workdir:

            @observe(capture_inputs=True, capture_outputs=True)
            def answer(question: str) -> list[str]:
                results = ["doc-1", "doc-2"]
                with tool_call(
                    "search_docs",
                    input={"query": question, "authorization": "Bearer secret"},
                    metadata={"kind": "retrieval"},
                ) as tool:
                    tool.set_output({"results": results, "token": "tool-token"})
                return results

            answer("hello")

            events = read_events(workdir / ".bir" / "traces.jsonl")
            tool_event = next(event for event in events if event["type"] == "tool_call")
            trace_event = next(event for event in events if event["type"] == "trace")
            self.assertEqual(tool_event["trace_id"], trace_event["trace_id"])
            self.assertEqual(tool_event["parent_id"], trace_event["id"])
            self.assertEqual(tool_event["name"], "search_docs")
            self.assertEqual(tool_event["metadata"], {"kind": "retrieval"})
            self.assertEqual(tool_event["input"], {"query": "hello", "authorization": "[redacted]"})
            self.assertEqual(tool_event["output"], {"results": ["doc-1", "doc-2"], "token": "[redacted]"})
            self.assertEqual(tool_event["status"], "success")

    def test_tool_call_capture_can_be_enabled_per_call(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer() -> None:
                with tool_call(
                    "calculator",
                    input={"expression": "2 + 2"},
                    capture_input=True,
                    capture_output=True,
                ) as tool:
                    tool.set_output(4)

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            tool_event = next(event for event in events if event["type"] == "tool_call")
            self.assertEqual(tool_event["input"], {"expression": "2 + 2"})
            self.assertEqual(tool_event["output"], 4)

    def test_tool_call_exception_is_captured_and_reraised(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def fail() -> None:
                with tool_call("search_docs"):
                    raise LookupError("search failed")

            with self.assertRaisesRegex(LookupError, "search failed"):
                fail()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            tool_event = next(event for event in events if event["type"] == "tool_call")
            trace_event = next(event for event in events if event["type"] == "trace")
            self.assertEqual(tool_event["status"], "error")
            self.assertEqual(tool_event["error"], "search failed")
            self.assertEqual(trace_event["status"], "error")

    def test_load_events_returns_local_trace_events(self) -> None:
        with temporary_workdir() as workdir:

            @observe(capture_inputs=True, capture_outputs=True)
            def answer(question: str) -> str:
                with tool_call("search_docs", input={"query": question}) as tool:
                    tool.set_output(["doc-1"])
                score("helpfulness", 0.9)
                return "ok"

            answer("hello")

            events = load_events()
            self.assertEqual([event.type for event in events], ["tool_call", "score", "trace"])
            self.assertEqual(events[0].name, "search_docs")
            self.assertEqual(events[0].input, {"query": "hello"})
            self.assertGreaterEqual(events[-1].duration_ms, 0)
            self.assertEqual(events[-1].raw["name"], "answer")
            self.assertEqual(load_events(workdir / "missing.jsonl"), [])

    def test_load_events_rejects_invalid_schema(self) -> None:
        with temporary_workdir() as workdir:
            valid_event = {
                "schema_version": "1.0",
                "id": "trace-1",
                "trace_id": "trace-1",
                "parent_id": None,
                "name": "answer",
                "type": "trace",
                "start_time": "2026-01-01T00:00:00+00:00",
                "end_time": "2026-01-01T00:00:01+00:00",
                "status": "success",
                "metadata": {},
                "input": None,
                "output": None,
                "error": None,
            }

            missing_schema_path = workdir / "missing-schema.jsonl"
            event_without_schema = dict(valid_event)
            del event_without_schema["schema_version"]
            missing_schema_path.write_text(json.dumps(event_without_schema) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing required field 'schema_version'"):
                load_events(missing_schema_path)

            invalid_type_path = workdir / "invalid-type.jsonl"
            event_with_invalid_type = dict(valid_event, type="unknown")
            invalid_type_path.write_text(json.dumps(event_with_invalid_type) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported value 'unknown'"):
                load_events(invalid_type_path)

            invalid_time_path = workdir / "invalid-time.jsonl"
            event_with_invalid_time = dict(valid_event, end_time="2025-12-31T23:59:59+00:00")
            invalid_time_path.write_text(json.dumps(event_with_invalid_time) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "end_time before start_time"):
                load_events(invalid_time_path)

    def test_load_traces_groups_events_by_trace_id(self) -> None:
        with temporary_workdir():

            @observe(capture_inputs=True, capture_outputs=True)
            def answer(question: str) -> str:
                with span("retrieve_context"):
                    with tool_call("search_docs", input={"query": question}) as tool:
                        tool.set_output(["doc-1"])
                with generation("local.llm", model="demo", input={"question": question}) as gen:
                    gen.set_output("ok")
                    gen.set_usage(input_tokens=1, output_tokens=2)
                score("helpfulness", 0.9)
                return "ok"

            answer("hello")

            traces = load_traces()
            self.assertEqual(len(traces), 1)
            trace = traces[0]
            self.assertEqual(trace.name, "answer")
            self.assertEqual(trace.status, "success")
            self.assertEqual(trace.id, trace.root.id)
            self.assertGreaterEqual(trace.duration_ms, 0)
            self.assertEqual(
                [event.type for event in trace.events],
                ["trace", "span", "tool_call", "generation", "score"],
            )

    def test_load_traces_orders_root_before_children_with_same_start_time(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "events.jsonl"
            timestamp = "2026-01-01T00:00:00+00:00"
            child = {
                "schema_version": "1.0",
                "id": "score-1",
                "trace_id": "trace-1",
                "parent_id": "trace-1",
                "name": "helpfulness",
                "type": "score",
                "start_time": timestamp,
                "end_time": timestamp,
                "status": "success",
                "metadata": {},
                "input": None,
                "output": None,
                "error": None,
                "value": 1.0,
            }
            root = {
                "schema_version": "1.0",
                "id": "trace-1",
                "trace_id": "trace-1",
                "parent_id": None,
                "name": "answer",
                "type": "trace",
                "start_time": timestamp,
                "end_time": "2026-01-01T00:00:01+00:00",
                "status": "success",
                "metadata": {},
                "input": None,
                "output": None,
                "error": None,
            }
            trace_path.write_text(json.dumps(child) + "\n" + json.dumps(root) + "\n", encoding="utf-8")

            trace = load_traces(trace_path)[0]
            self.assertEqual([event.type for event in trace.events], ["trace", "score"])

    def test_load_traces_skips_events_without_root_trace(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "events.jsonl"
            event = {
                "schema_version": "1.0",
                "id": "score-1",
                "trace_id": "trace-1",
                "parent_id": "trace-1",
                "name": "helpfulness",
                "type": "score",
                "start_time": "2026-01-01T00:00:00+00:00",
                "end_time": "2026-01-01T00:00:00+00:00",
                "status": "success",
                "metadata": {},
                "input": None,
                "output": None,
                "error": None,
                "value": 1.0,
            }
            trace_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

            self.assertEqual(load_traces(trace_path), [])

    def test_load_traces_uses_configured_trace_path(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "custom" / "events.jsonl"
            configure(trace_path=trace_path)

            @observe()
            def answer() -> str:
                return "ok"

            answer()

            traces = load_traces()
            self.assertEqual(len(traces), 1)
            self.assertEqual(traces[0].name, "answer")

    def test_send_events_posts_local_events_to_server(self) -> None:
        with temporary_workdir():

            @observe(capture_inputs=True)
            def answer(question: str) -> str:
                score("helpfulness", 0.9)
                return question.upper()

            answer("hello")
            posted_events: list[dict[str, object]] = []
            posted_urls: list[str] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                self.assertEqual(timeout, 10.0)
                posted_urls.append(request_url(request))
                posted_events.append(posted_request_body(request))
                return FakeHttpResponse()

            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                result = send_events("http://server.test")

            self.assertEqual(result.accepted, 2)
            self.assertEqual(len(result.event_ids), 2)
            self.assertEqual(posted_urls, ["http://server.test/v1/events", "http://server.test/v1/events"])
            posted_types = [event["type"] for event in posted_events]
            self.assertEqual(posted_types, ["score", "trace"])
            trace_event = next(event for event in posted_events if event["type"] == "trace")
            self.assertEqual(trace_event["input"], {"question": "hello"})

    def test_send_events_raises_when_server_rejects_event(self) -> None:
        with temporary_workdir():

            @observe()
            def answer() -> str:
                return "ok"

            answer()
            posted_events: list[dict[str, object]] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                posted_events.append(posted_request_body(request))
                raise urllib.error.HTTPError(
                    url=request_url(request),
                    code=422,
                    msg="Unprocessable Entity",
                    hdrs={},
                    fp=BytesIO(b'{"detail":"rejected"}'),
                )

            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                with self.assertRaisesRegex(RuntimeError, "HTTP 422"):
                    send_events("http://server.test")
            self.assertEqual(len(posted_events), 1)

    def test_load_events_rejects_invalid_jsonl(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "bad.jsonl"
            trace_path.write_text("not-json\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Invalid JSON"):
                load_events(trace_path)

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

    def test_context_is_reset_after_observed_exception(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def fail() -> None:
                raise ValueError("boom")

            with self.assertRaisesRegex(ValueError, "boom"):
                fail()
            with self.assertRaisesRegex(RuntimeError, "requires an active trace"):
                score("outside", 1.0)

            @observe()
            def answer() -> None:
                with span("child"):
                    pass

            answer()
            events = read_events(workdir / ".bir" / "traces.jsonl")
            success_trace = next(event for event in events if event["type"] == "trace" and event["status"] == "success")
            child = next(event for event in events if event["type"] == "span")
            self.assertEqual(child["parent_id"], success_trace["id"])

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

    def test_storage_errors_are_not_swallowed(self) -> None:
        with temporary_workdir() as workdir:
            configure(trace_path=workdir)

            @observe()
            def answer() -> str:
                return "ok"

            with self.assertRaises(IsADirectoryError):
                answer()

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

    def test_capture_is_json_safe_and_depth_limited(self) -> None:
        class BadRepr:
            def __repr__(self) -> str:
                raise RuntimeError("repr failed")

        with temporary_workdir() as workdir:

            @observe(capture_inputs=True, capture_outputs=True)
            def answer(payload: dict[str, object]) -> dict[str, object]:
                return {"not_a_number": float("nan"), "bad": BadRepr()}

            deep_payload: dict[str, object] = {}
            current = deep_payload
            for _ in range(8):
                nested: dict[str, object] = {}
                current["nested"] = nested
                current = nested
            current["authorization"] = "Bearer secret"

            answer(deep_payload)

            trace_path = workdir / ".bir" / "traces.jsonl"
            raw_trace_file = trace_path.read_text(encoding="utf-8")
            self.assertNotIn("Bearer secret", raw_trace_file)
            self.assertNotIn("NaN", raw_trace_file)
            events = read_events(trace_path)
            event = events[0]
            self.assertEqual(event["output"], {"not_a_number": "nan", "bad": "<unrepresentable BadRepr>"})
            self.assertIn("[max_depth]", raw_trace_file)


if __name__ == "__main__":
    unittest.main()
