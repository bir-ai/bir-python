from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPMessage
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from bir import configure, generation, load_events, load_traces, observe, score, send_events, span, tool_call
from bir._sdk import _reset_config_for_tests

ROOT = Path(__file__).resolve().parents[3]
CONTRACT_EVENTS_PATH = ROOT / "tests" / "fixtures" / "valid-events.jsonl"


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

    def __init__(self, body: bytes = b'{"accepted":1}') -> None:
        self.body = body

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


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

    def test_nested_observe_creates_span_inside_active_trace(self) -> None:
        with temporary_workdir() as workdir:

            @observe(name="inner_step")
            def inner() -> str:
                return "inner"

            @observe()
            def outer() -> str:
                return inner()

            self.assertEqual(outer(), "inner")

            events = read_events(workdir / ".bir" / "traces.jsonl")
            trace_events = [event for event in events if event["type"] == "trace"]
            span_events = [event for event in events if event["type"] == "span"]
            self.assertEqual(len(trace_events), 1)
            self.assertEqual(len(span_events), 1)

            trace_event = trace_events[0]
            nested_event = span_events[0]
            self.assertEqual(nested_event["name"], "inner_step")
            self.assertEqual(nested_event["trace_id"], trace_event["trace_id"])
            self.assertEqual(nested_event["parent_id"], trace_event["id"])
            self.assertEqual(nested_event["status"], "success")

    def test_nested_observe_uses_current_parent_id(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def inner() -> None:
                pass

            @observe()
            def outer() -> None:
                with span("current_parent"):
                    inner()

            outer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            trace_event = next(event for event in events if event["type"] == "trace")
            parent_span = next(event for event in events if event["name"] == "current_parent")
            nested_event = next(event for event in events if event["name"] == "inner")
            self.assertEqual(nested_event["type"], "span")
            self.assertEqual(nested_event["trace_id"], trace_event["trace_id"])
            self.assertEqual(nested_event["parent_id"], parent_span["id"])

    def test_nested_observe_becomes_parent_for_child_events(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def inner() -> None:
                score("inner_score", 0.8)
                with generation("inner_generation"):
                    pass
                with tool_call("inner_tool"):
                    pass
                with span("inner_span"):
                    pass

            @observe()
            def outer() -> None:
                inner()

            outer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            nested_event = next(event for event in events if event["name"] == "inner")
            for child_name in ("inner_score", "inner_generation", "inner_tool", "inner_span"):
                child_event = next(event for event in events if event["name"] == child_name)
                self.assertEqual(child_event["trace_id"], nested_event["trace_id"])
                self.assertEqual(child_event["parent_id"], nested_event["id"])

    def test_nested_observe_captures_inputs_and_outputs(self) -> None:
        with temporary_workdir() as workdir:

            @observe(capture_inputs=True, capture_outputs=True)
            def inner(question: str) -> dict[str, str]:
                return {"answer": question.upper()}

            @observe()
            def outer() -> dict[str, str]:
                return inner("hello")

            outer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            nested_event = next(event for event in events if event["name"] == "inner")
            self.assertEqual(nested_event["type"], "span")
            self.assertEqual(nested_event["input"], {"question": "hello"})
            self.assertEqual(nested_event["output"], {"answer": "HELLO"})

    def test_nested_observe_records_error_and_reraises(self) -> None:
        with temporary_workdir() as workdir:
            secret_error = "provider failed api_key=sk-secret"

            @observe()
            def inner() -> None:
                raise RuntimeError(secret_error)

            @observe()
            def outer() -> None:
                inner()

            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                outer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            nested_event = next(event for event in events if event["name"] == "inner")
            trace_event = next(event for event in events if event["type"] == "trace")
            self.assertEqual(nested_event["type"], "span")
            self.assertEqual(nested_event["status"], "error")
            self.assertEqual(nested_event["error"], "provider failed api_key=[redacted]")
            self.assertEqual(trace_event["status"], "error")

    def test_context_is_reset_after_nested_observed_exception(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def fail_inner() -> None:
                raise ValueError("boom")

            @observe()
            def recover_outer() -> None:
                with self.assertRaisesRegex(ValueError, "boom"):
                    fail_inner()
                score("after_failure", 1.0)

            recover_outer()
            with self.assertRaisesRegex(RuntimeError, "requires an active trace"):
                score("outside", 1.0)

            @observe()
            def answer() -> None:
                with span("child"):
                    pass

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            recover_trace = next(event for event in events if event["name"] == "recover_outer")
            score_event = next(event for event in events if event["name"] == "after_failure")
            success_trace = next(event for event in events if event["name"] == "answer")
            child = next(event for event in events if event["name"] == "child")
            self.assertEqual(score_event["parent_id"], recover_trace["id"])
            self.assertEqual(child["parent_id"], success_trace["id"])

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

    def test_load_events_accepts_schema_contract_fixtures(self) -> None:
        events = load_events(CONTRACT_EVENTS_PATH)

        self.assertEqual(
            [event.type for event in events],
            ["trace", "span", "tool_call", "generation", "score"],
        )
        generation_event = next(event for event in events if event.type == "generation")
        score_event = next(event for event in events if event.type == "score")
        self.assertEqual(generation_event.parent_id, "trace-fixture-1")
        self.assertEqual(generation_event.model, "demo-model")
        self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 24, "total_tokens": 36})
        self.assertEqual(generation_event.raw["usage"], {"input_tokens": 12, "output_tokens": 24, "total_tokens": 36})
        self.assertEqual(score_event.parent_id, "generation-fixture-1")
        self.assertEqual(score_event.value, 0.82)
        self.assertEqual(score_event.raw["value"], 0.82)

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

            child_without_parent_path = workdir / "child-without-parent.jsonl"
            child_without_parent = dict(
                valid_event,
                id="span-1",
                type="span",
                parent_id=None,
            )
            child_without_parent_path.write_text(json.dumps(child_without_parent) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "span event requires parent_id"):
                load_events(child_without_parent_path)

            non_finite_input_path = workdir / "non-finite-input.jsonl"
            event_with_non_finite_input = dict(valid_event, input={"value": float("nan")})
            non_finite_input_path.write_text(
                json.dumps(event_with_non_finite_input, allow_nan=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "input.value"):
                load_events(non_finite_input_path)

            invalid_usage_path = workdir / "invalid-usage.jsonl"
            event_with_invalid_usage = dict(
                valid_event,
                id="generation-1",
                type="generation",
                parent_id="trace-1",
                usage={"input_tokens": float("inf")},
            )
            invalid_usage_path.write_text(
                json.dumps(event_with_invalid_usage, allow_nan=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "usage.input_tokens"):
                load_events(invalid_usage_path)

            bool_usage_path = workdir / "bool-usage.jsonl"
            event_with_bool_usage = dict(
                valid_event,
                id="generation-1",
                type="generation",
                parent_id="trace-1",
                usage={"input_tokens": True},
            )
            bool_usage_path.write_text(json.dumps(event_with_bool_usage) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(TypeError, "usage.input_tokens"):
                load_events(bool_usage_path)

            bool_score_path = workdir / "bool-score.jsonl"
            event_with_bool_score = dict(
                valid_event,
                id="score-1",
                type="score",
                parent_id="trace-1",
                value=True,
            )
            bool_score_path.write_text(json.dumps(event_with_bool_score) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(TypeError, "score value"):
                load_events(bool_score_path)

            missing_score_value_path = workdir / "missing-score-value.jsonl"
            event_without_score_value = dict(
                valid_event,
                id="score-1",
                type="score",
                parent_id="trace-1",
            )
            missing_score_value_path.write_text(json.dumps(event_without_score_value) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "score event is missing required field 'value'"):
                load_events(missing_score_value_path)

            invalid_model_path = workdir / "invalid-model.jsonl"
            event_with_invalid_model = dict(
                valid_event,
                id="generation-1",
                type="generation",
                parent_id="trace-1",
                model=123,
            )
            invalid_model_path.write_text(json.dumps(event_with_invalid_model) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "model"):
                load_events(invalid_model_path)

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
            self.assertEqual(posted_types, ["trace", "score"])
            trace_event = next(event for event in posted_events if event["type"] == "trace")
            self.assertEqual(trace_event["input"], {"question": "hello"})

    def test_send_events_posts_complete_traces_root_first(self) -> None:
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
            posted_events: list[dict[str, object]] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                posted_events.append(posted_request_body(request))
                return FakeHttpResponse()

            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                result = send_events("http://server.test")

            self.assertEqual(result.accepted, 5)
            self.assertEqual(
                [event["type"] for event in posted_events],
                ["trace", "span", "tool_call", "generation", "score"],
            )
            self.assertEqual(result.event_ids, [event["id"] for event in posted_events])

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
                    hdrs=HTTPMessage(),
                    fp=BytesIO(b'{"detail":"rejected"}'),
                )

            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                with self.assertRaisesRegex(RuntimeError, "HTTP 422"):
                    send_events("http://server.test")
            self.assertEqual(len(posted_events), 1)

    def test_send_events_uses_server_accepted_count(self) -> None:
        with temporary_workdir():

            @observe()
            def answer() -> str:
                return "ok"

            answer()

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                return FakeHttpResponse(b'{"accepted":0,"id":"already-seen"}')

            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                result = send_events("http://server.test")

            self.assertEqual(result.accepted, 0)
            self.assertEqual(result.event_ids, [])

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

    def test_error_capture_redacts_secret_like_values(self) -> None:
        secret_error = "provider failed authorization: Bearer sk-secret api_key=sk-test token=response-token"

        with temporary_workdir() as workdir:

            @observe()
            def fail_trace() -> None:
                raise RuntimeError(secret_error)

            @observe()
            def fail_span() -> None:
                with span("explode"):
                    raise RuntimeError(secret_error)

            @observe()
            def fail_generation() -> None:
                with generation("local.llm"):
                    raise RuntimeError(secret_error)

            @observe()
            def fail_tool() -> None:
                with tool_call("search_docs"):
                    raise RuntimeError(secret_error)

            for func in (fail_trace, fail_span, fail_generation, fail_tool):
                with self.assertRaisesRegex(RuntimeError, "provider failed"):
                    func()

            trace_path = workdir / ".bir" / "traces.jsonl"
            raw_trace_file = trace_path.read_text(encoding="utf-8")
            self.assertNotIn("sk-secret", raw_trace_file)
            self.assertNotIn("sk-test", raw_trace_file)
            self.assertNotIn("response-token", raw_trace_file)

            events = read_events(trace_path)
            errored_events = [event for event in events if event["status"] == "error"]
            self.assertGreaterEqual(len(errored_events), 4)
            for event in errored_events:
                self.assertIn("[redacted]", str(event["error"]))

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

    def test_observe_capture_settings_are_stable_during_call(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer(question: str) -> str:
                configure(capture_inputs=True, capture_outputs=True)
                return question.upper()

            answer("hello")

            events = read_events(workdir / ".bir" / "traces.jsonl")
            event = events[0]
            self.assertIsNone(event["input"])
            self.assertIsNone(event["output"])

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

    def test_capture_redacts_broader_secret_like_values(self) -> None:
        secret_values = {
            "auth": "raw-auth-value",
            "credentials": "raw-credentials-value",
            "client_secret": "raw-client-secret",
            "access_key": "raw-access-key",
            "private_key": "raw-private-key",
            "headers": ["Bearer raw-bearer-token"],
            "openai_key": "sk-rawopenaitoken",
        }

        with temporary_workdir() as workdir:

            @observe(capture_inputs=True, capture_outputs=True)
            def call_provider(payload: dict[str, object]) -> dict[str, object]:
                with generation(
                    "local.llm",
                    metadata={"credential": "raw-generation-credential"},
                    capture_output=True,
                ) as gen:
                    gen.set_output("Authorization=Bearer raw-generation-bearer")
                with tool_call(
                    "provider_call",
                    metadata={"note": "client_secret=raw-tool-client-secret"},
                    capture_output=True,
                ) as tool:
                    tool.set_output("token: raw-tool-token")
                return {"message": "ok", "text": "use sk-rawoutputtoken"}

            call_provider(secret_values)

            trace_path = workdir / ".bir" / "traces.jsonl"
            raw_trace_file = trace_path.read_text(encoding="utf-8")
            for secret in (
                "raw-auth-value",
                "raw-credentials-value",
                "raw-client-secret",
                "raw-access-key",
                "raw-private-key",
                "raw-bearer-token",
                "sk-rawopenaitoken",
                "raw-generation-credential",
                "raw-generation-bearer",
                "raw-tool-client-secret",
                "raw-tool-token",
                "sk-rawoutputtoken",
            ):
                self.assertNotIn(secret, raw_trace_file)

            events = read_events(trace_path)
            trace_event = next(event for event in events if event["type"] == "trace")
            generation_event = next(event for event in events if event["type"] == "generation")
            tool_event = next(event for event in events if event["type"] == "tool_call")
            self.assertEqual(
                trace_event["input"],
                {
                    "payload": {
                        "auth": "[redacted]",
                        "credentials": "[redacted]",
                        "client_secret": "[redacted]",
                        "access_key": "[redacted]",
                        "private_key": "[redacted]",
                        "headers": ["Bearer [redacted]"],
                        "openai_key": "[redacted]",
                    }
                },
            )
            self.assertEqual(trace_event["output"], {"message": "ok", "text": "use [redacted]"})
            self.assertEqual(generation_event["metadata"], {"credential": "[redacted]"})
            self.assertEqual(generation_event["output"], "Authorization=Bearer [redacted]")
            self.assertEqual(tool_event["metadata"], {"note": "client_secret=[redacted]"})
            self.assertEqual(tool_event["output"], "token: [redacted]")

    def test_capture_redacts_secret_like_text_values_and_reprs(self) -> None:
        class SecretRepr:
            def __repr__(self) -> str:
                return "SecretRepr(api_key=sk-repr)"

        with temporary_workdir() as workdir:

            @observe(capture_inputs=True, capture_outputs=True)
            def call_tool(payload: dict[str, object]) -> dict[str, object]:
                with tool_call(
                    "search_docs",
                    input={"query": "authorization: Bearer sk-tool-input"},
                    metadata={"note": "token=tool-metadata-token"},
                    capture_input=True,
                    capture_output=True,
                ) as tool:
                    tool.set_output(["password=tool-output-password"])
                return {"message": "secret=response-secret", "repr": SecretRepr()}

            call_tool(
                {
                    "message": "hello",
                    "headers": ["Authorization: Bearer sk-input-header"],
                    "object": SecretRepr(),
                }
            )

            trace_path = workdir / ".bir" / "traces.jsonl"
            raw_trace_file = trace_path.read_text(encoding="utf-8")
            self.assertNotIn("sk-input-header", raw_trace_file)
            self.assertNotIn("sk-tool-input", raw_trace_file)
            self.assertNotIn("tool-metadata-token", raw_trace_file)
            self.assertNotIn("tool-output-password", raw_trace_file)
            self.assertNotIn("response-secret", raw_trace_file)
            self.assertNotIn("sk-repr", raw_trace_file)

            events = read_events(trace_path)
            trace_event = next(event for event in events if event["type"] == "trace")
            tool_event = next(event for event in events if event["type"] == "tool_call")
            self.assertEqual(
                trace_event["input"],
                {
                    "payload": {
                        "message": "hello",
                        "headers": ["Authorization: Bearer [redacted]"],
                        "object": "SecretRepr(api_key=[redacted])",
                    }
                },
            )
            self.assertEqual(
                trace_event["output"],
                {
                    "message": "secret=[redacted]",
                    "repr": "SecretRepr(api_key=[redacted])",
                },
            )
            self.assertEqual(tool_event["input"], {"query": "authorization: Bearer [redacted]"})
            self.assertEqual(tool_event["metadata"], {"note": "token=[redacted]"})
            self.assertEqual(tool_event["output"], ["password=[redacted]"])

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
