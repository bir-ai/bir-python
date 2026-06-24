from __future__ import annotations

import asyncio
import inspect
import json
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import unittest
from concurrent.futures import ThreadPoolExecutor
from collections.abc import AsyncGenerator, Generator, Iterator
from contextlib import contextmanager
from http.client import HTTPMessage
from io import BytesIO
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import bir
from bir import configure, generation, get_current_span_id, get_current_trace_id, load_events, load_traces, observe, prompt, retrieval, score, send_events, span, tool_call, trace
from bir._sdk import (
    _Config,
    _config_from_env,
    _parse_env_bool,
    _parse_env_sample_rate,
    _record_sent_ids,
    _redact_secret_text,
    _reset_config_for_tests,
    _safe_capture,
)

_BIR_ENV_VARS = (
    "BIR_TRACE_PATH",
    "BIR_CAPTURE_INPUTS",
    "BIR_CAPTURE_OUTPUTS",
    "BIR_SAMPLE_RATE",
    "BIR_SERVICE_NAME",
    "BIR_ENVIRONMENT",
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_EVENTS_PATH = ROOT / "tests" / "fixtures" / "valid-events.jsonl"
CONTRACT_SCHEMA_PATH = ROOT / "tests" / "fixtures" / "event-schema-v1.json"


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


@contextmanager
def env_vars(**values: str) -> Iterator[None]:
    """Run with only the given BIR_* variables set, restoring the prior env.

    All recognized BIR_* variables are cleared first so a developer's real
    environment cannot leak into the assertions, then the provided ones are set.
    """

    saved = {name: os.environ.get(name) for name in _BIR_ENV_VARS}
    for name in _BIR_ENV_VARS:
        os.environ.pop(name, None)
    os.environ.update(values)
    try:
        yield
    finally:
        for name in _BIR_ENV_VARS:
            os.environ.pop(name, None)
            previous = saved[name]
            if previous is not None:
                os.environ[name] = previous


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


def posted_request_batch(request: object) -> list[dict[str, object]]:
    data = getattr(request, "data")
    if not isinstance(data, bytes):
        raise TypeError("expected request data to be bytes")
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, list):
        raise TypeError("expected request body to be a JSON list")
    return payload


def batch_response_accepting(events: list[dict[str, object]]) -> FakeHttpResponse:
    body = json.dumps({"accepted": len(events), "event_ids": [event["id"] for event in events]})
    return FakeHttpResponse(body.encode("utf-8"))


def http_error(request: object, code: int, body: bytes = b"{}") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url=request_url(request),
        code=code,
        msg="error",
        hdrs=HTTPMessage(),
        fp=BytesIO(body),
    )


def request_url(request: object) -> str:
    url = getattr(request, "full_url")
    if not isinstance(url, str):
        raise TypeError("expected request to have a full_url")
    return url


def load_contract_schema() -> dict[str, object]:
    payload = json.loads(CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("expected event schema to be a JSON object")
    return payload


class SdkTests(unittest.TestCase):
    def setUp(self) -> None:
        # Start every test from hardcoded defaults so an ambient BIR_* variable
        # in the developer's environment never changes the import-time config.
        _reset_config_for_tests()

    def tearDown(self) -> None:
        _reset_config_for_tests()

    def _run_synced_subprocesses(
        self,
        workdir: Path,
        code: str,
        process_args: list[list[str]],
    ) -> None:
        """Start workers behind a file barrier and require clean exits."""

        start_path = workdir / "start"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        processes: list[subprocess.Popen[str]] = []
        for index, args in enumerate(process_args):
            ready_path = workdir / f"ready-{index}"
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", code, str(ready_path), str(start_path), *args],
                    cwd=workdir,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )

        deadline = time.monotonic() + 15
        while not all((workdir / f"ready-{index}").exists() for index in range(len(processes))):
            if any(process.poll() is not None for process in processes) or time.monotonic() >= deadline:
                for process in processes:
                    process.kill()
                outputs = [process.communicate() for process in processes]
                self.fail(f"subprocess workers did not reach the start barrier: {outputs}")
            time.sleep(0.01)
        start_path.touch()

        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, f"worker failed\nstdout: {stdout}\nstderr: {stderr}")

    def test_exposes_a_version_string(self) -> None:
        self.assertIn("__version__", bir.__all__)
        self.assertIsInstance(bir.__version__, str)
        self.assertRegex(bir.__version__, r"^\d+\.\d+\.\d+")

    def test_exposes_trace_context_manager(self) -> None:
        self.assertIn("trace", bir.__all__)
        self.assertIs(bir.trace, trace)

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

    def test_sdk_event_contract_matches_schema_artifact(self) -> None:
        schema = load_contract_schema()
        properties = schema["properties"]
        if not isinstance(properties, dict):
            raise TypeError("expected schema properties to be an object")

        required_fields = schema["required"]
        event_type = properties["type"]
        event_status = properties["status"]
        schema_version = properties["schema_version"]
        if not isinstance(required_fields, list):
            raise TypeError("expected schema required fields to be a list")
        if not isinstance(event_type, dict) or not isinstance(event_status, dict) or not isinstance(schema_version, dict):
            raise TypeError("expected schema property definitions to be objects")

        with temporary_workdir() as workdir:

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

            events = read_events(workdir / ".bir" / "traces.jsonl")
            self.assertEqual({event["type"] for event in events}, set(event_type["enum"]))
            for event in events:
                self.assertEqual(event["schema_version"], schema_version["const"])
                self.assertIn(event["type"], event_type["enum"])
                self.assertIn(event["status"], event_status["enum"])
                for field in required_fields:
                    self.assertIn(field, event)

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

    def test_async_observe_records_single_trace(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            async def answer(question: str) -> str:
                await asyncio.sleep(0)
                return "ok"

            self.assertEqual(asyncio.run(answer("hello")), "ok")

            events = read_events(workdir / ".bir" / "traces.jsonl")
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["type"], "trace")
            self.assertEqual(event["name"], "answer")
            self.assertEqual(event["id"], event["trace_id"])
            self.assertIsNone(event["parent_id"])
            self.assertEqual(event["status"], "success")

    def test_nested_async_observe_records_span_under_same_trace(self) -> None:
        with temporary_workdir() as workdir:

            @observe(name="inner_step")
            async def inner() -> str:
                await asyncio.sleep(0)
                return "inner"

            @observe()
            async def outer() -> str:
                return await inner()

            self.assertEqual(asyncio.run(outer()), "inner")

            events = read_events(workdir / ".bir" / "traces.jsonl")
            trace_events = [event for event in events if event["type"] == "trace"]
            span_events = [event for event in events if event["type"] == "span"]
            self.assertEqual(len(trace_events), 1)
            self.assertEqual(len(span_events), 1)

            trace_event = trace_events[0]
            span_event = span_events[0]
            self.assertEqual(span_event["name"], "inner_step")
            self.assertEqual(span_event["trace_id"], trace_event["trace_id"])
            self.assertEqual(span_event["parent_id"], trace_event["id"])
            self.assertEqual(span_event["status"], "success")

    def test_async_observe_records_error_and_reraises(self) -> None:
        with temporary_workdir() as workdir:
            secret_error = "provider failed api_key=sk-secret"

            @observe()
            async def boom() -> None:
                await asyncio.sleep(0)
                raise RuntimeError(secret_error)

            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                asyncio.run(boom())

            events = read_events(workdir / ".bir" / "traces.jsonl")
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["type"], "trace")
            self.assertEqual(event["status"], "error")
            self.assertEqual(event["error"], "provider failed api_key=[redacted]")

    def test_async_observe_captures_inputs_and_outputs(self) -> None:
        with temporary_workdir() as workdir:

            @observe(capture_inputs=True, capture_outputs=True)
            async def answer(question: str) -> dict[str, str]:
                await asyncio.sleep(0)
                return {"answer": question.upper()}

            self.assertEqual(asyncio.run(answer("hello")), {"answer": "HELLO"})

            events = read_events(workdir / ".bir" / "traces.jsonl")
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["input"], {"question": "hello"})
            self.assertEqual(event["output"], {"answer": "HELLO"})

    def test_gather_of_observed_coroutines_records_separate_traces(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            async def task(value: str) -> str:
                # Yield control so the two gathered tasks interleave; if the
                # contextvars leaked across tasks one would nest under the other.
                await asyncio.sleep(0)
                return value

            async def main() -> tuple[str, str]:
                return await asyncio.gather(task("a"), task("b"))

            self.assertEqual(sorted(asyncio.run(main())), ["a", "b"])

            events = read_events(workdir / ".bir" / "traces.jsonl")
            self.assertEqual(len(events), 2)
            self.assertTrue(all(event["type"] == "trace" for event in events))
            self.assertTrue(all(event["parent_id"] is None for event in events))
            self.assertEqual(len({event["trace_id"] for event in events}), 2)

    def test_observe_sync_generator_yields_same_values_and_records_completed_trace(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def stream(count: int) -> Generator[int, None, None]:
                for index in range(count):
                    yield index

            self.assertEqual(list(stream(3)), [0, 1, 2])

            events = read_events(workdir / ".bir" / "traces.jsonl")
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["type"], "trace")
            self.assertEqual(event["name"], "stream")
            self.assertEqual(event["id"], event["trace_id"])
            self.assertIsNone(event["parent_id"])
            self.assertEqual(event["status"], "success")
            self.assertIsNone(event["error"])
            self.assertEqual(event["metadata"], {"generator": {"outcome": "completed"}})

    def test_observe_sync_generator_is_lazy_until_first_iteration(self) -> None:
        with temporary_workdir() as workdir:
            body_ran: list[str] = []

            @observe()
            def stream() -> Generator[int, None, None]:
                body_ran.append("started")
                yield 1

            generator = stream()
            # Creating the generator must not run the body or write any event.
            self.assertEqual(body_ran, [])
            self.assertFalse((workdir / ".bir" / "traces.jsonl").exists())

            self.assertEqual(next(generator), 1)
            self.assertEqual(body_ran, ["started"])
            self.assertEqual(list(generator), [])

            events = read_events(workdir / ".bir" / "traces.jsonl")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["status"], "success")

    def test_observe_sync_generator_child_events_attach_across_iterations(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def stream() -> Generator[int, None, None]:
                with span("before_first"):
                    pass
                yield 1
                # This span is created only after the consumer pulled the first
                # value, proving the trace stays open across the whole iteration.
                with span("after_first"):
                    pass
                yield 2

            self.assertEqual(list(stream()), [1, 2])

            events = read_events(workdir / ".bir" / "traces.jsonl")
            trace_event = next(event for event in events if event["type"] == "trace")
            span_events = [event for event in events if event["type"] == "span"]
            self.assertEqual({event["name"] for event in span_events}, {"before_first", "after_first"})
            for span_event in span_events:
                self.assertEqual(span_event["trace_id"], trace_event["id"])
                self.assertEqual(span_event["parent_id"], trace_event["id"])

    def test_observe_sync_generator_generation_spans_streamed_yields(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def stream_tokens() -> Generator[str, None, None]:
                # The generation is held open across every yielded token, the
                # canonical streaming-LLM shape, and finalized once the stream ends.
                with generation("local.llm", model="demo", capture_output=True) as gen:
                    collected: list[str] = []
                    for token in ("Ans", "wer"):
                        collected.append(token)
                        yield token
                    gen.set_output("".join(collected))

            self.assertEqual(list(stream_tokens()), ["Ans", "wer"])

            events = read_events(workdir / ".bir" / "traces.jsonl")
            trace_event = next(event for event in events if event["type"] == "trace")
            generation_event = next(event for event in events if event["type"] == "generation")
            self.assertEqual(generation_event["trace_id"], trace_event["id"])
            self.assertEqual(generation_event["parent_id"], trace_event["id"])
            self.assertEqual(generation_event["output"], "Answer")
            self.assertEqual(generation_event["status"], "success")

    def test_observe_sync_generator_records_error_and_reraises(self) -> None:
        with temporary_workdir() as workdir:
            secret_error = "provider failed api_key=sk-secret"

            @observe()
            def stream() -> Generator[int, None, None]:
                yield 1
                raise RuntimeError(secret_error)

            generator = stream()
            self.assertEqual(next(generator), 1)
            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                next(generator)

            events = read_events(workdir / ".bir" / "traces.jsonl")
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["type"], "trace")
            self.assertEqual(event["status"], "error")
            self.assertEqual(event["error"], "provider failed api_key=[redacted]")
            self.assertEqual(event["metadata"], {"generator": {"outcome": "error"}})

    def test_observe_sync_generator_close_records_terminal_state_and_runs_finally(self) -> None:
        with temporary_workdir() as workdir:
            finally_ran: list[str] = []

            @observe()
            def stream() -> Generator[int, None, None]:
                try:
                    yield 1
                    yield 2
                finally:
                    finally_ran.append("cleanup")

            generator = stream()
            self.assertEqual(next(generator), 1)
            generator.close()

            # The wrapped generator's finally block must still run on close.
            self.assertEqual(finally_ran, ["cleanup"])

            events = read_events(workdir / ".bir" / "traces.jsonl")
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["status"], "success")
            self.assertEqual(event["metadata"], {"generator": {"outcome": "closed"}})

            # Closing early must reset all contextvars, leaving no active trace.
            with self.assertRaisesRegex(RuntimeError, "requires an active trace"):
                score("after_close", 1.0)

    def test_observe_sync_generator_does_not_leak_context_between_iterations(self) -> None:
        with temporary_workdir():

            @observe()
            def stream() -> Generator[int, None, None]:
                yield 1
                yield 2

            generator = stream()
            self.assertEqual(next(generator), 1)
            # While the generator is suspended the trace context belongs to it, not
            # to the consumer, so a score outside the body has no active trace.
            with self.assertRaisesRegex(RuntimeError, "requires an active trace"):
                score("between", 1.0)
            self.assertEqual(list(generator), [2])

    def test_observe_sync_generator_send_value_round_trips(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def echo() -> Generator[str, str | None, None]:
                received = yield "ready"
                yield f"got {received}"

            generator = echo()
            self.assertEqual(next(generator), "ready")
            self.assertEqual(generator.send("hello"), "got hello")
            self.assertEqual(list(generator), [])

            events = read_events(workdir / ".bir" / "traces.jsonl")
            self.assertEqual(events[0]["status"], "success")

    def test_observe_sync_generator_throw_is_proxied_to_body(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def catcher() -> Generator[str, None, None]:
                try:
                    yield "first"
                except ValueError:
                    yield "handled"

            generator = catcher()
            self.assertEqual(next(generator), "first")
            # A thrown exception the body catches lets iteration continue.
            self.assertEqual(generator.throw(ValueError("boom")), "handled")
            # Exhaust the generator so the trace is finalized.
            self.assertEqual(list(generator), [])

            events = read_events(workdir / ".bir" / "traces.jsonl")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["status"], "success")
            self.assertEqual(events[0]["metadata"], {"generator": {"outcome": "completed"}})

    def test_observe_sync_generator_uncaught_throw_records_error(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def stream() -> Generator[int, None, None]:
                yield 1
                yield 2

            generator = stream()
            self.assertEqual(next(generator), 1)
            with self.assertRaisesRegex(ValueError, "boom"):
                generator.throw(ValueError("boom"))

            events = read_events(workdir / ".bir" / "traces.jsonl")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["status"], "error")
            self.assertEqual(events[0]["error"], "boom")

    def test_observe_sync_generator_capture_records_item_count_not_content(self) -> None:
        with temporary_workdir() as workdir:

            @observe(capture_inputs=True, capture_outputs=True)
            def stream(label: str) -> Iterator[str]:
                yield f"{label}-secretpayload-1"
                yield f"{label}-secretpayload-2"

            self.assertEqual(
                list(stream("topic")),
                ["topic-secretpayload-1", "topic-secretpayload-2"],
            )

            trace_path = workdir / ".bir" / "traces.jsonl"
            event = read_events(trace_path)[0]
            self.assertEqual(event["input"], {"label": "topic"})
            # Output capture records only the bounded yielded-item count.
            self.assertEqual(event["metadata"], {"generator": {"outcome": "completed", "items": 2}})
            self.assertIsNone(event["output"])
            # Yielded content itself is never buffered or persisted.
            self.assertNotIn("secretpayload", trace_path.read_text(encoding="utf-8"))

    def test_observe_sync_generator_without_capture_records_no_item_count(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def stream() -> Generator[int, None, None]:
                yield 1
                yield 2

            self.assertEqual(list(stream()), [1, 2])

            event = read_events(workdir / ".bir" / "traces.jsonl")[0]
            self.assertEqual(event["metadata"], {"generator": {"outcome": "completed"}})
            self.assertIsNone(event["output"])

    def test_observe_preserves_generator_function_nature(self) -> None:
        @observe()
        def stream() -> Iterator[int]:
            yield 1

        self.assertTrue(inspect.isgeneratorfunction(stream))
        self.assertEqual(stream.__name__, "stream")

    def test_nested_observe_generator_records_span_under_same_trace(self) -> None:
        with temporary_workdir() as workdir:

            @observe(name="inner_stream")
            def inner() -> Iterator[str]:
                yield "a"
                yield "b"

            @observe()
            def outer() -> Iterator[str]:
                for value in inner():
                    yield value

            self.assertEqual(list(outer()), ["a", "b"])

            events = read_events(workdir / ".bir" / "traces.jsonl")
            trace_events = [event for event in events if event["type"] == "trace"]
            span_events = [event for event in events if event["type"] == "span"]
            self.assertEqual(len(trace_events), 1)
            self.assertEqual(len(span_events), 1)
            self.assertEqual(span_events[0]["name"], "inner_stream")
            self.assertEqual(span_events[0]["trace_id"], trace_events[0]["id"])
            self.assertEqual(span_events[0]["parent_id"], trace_events[0]["id"])

    def test_sample_rate_zero_drops_sync_generator_but_still_yields(self) -> None:
        with temporary_workdir() as workdir:
            configure(sample_rate=0.0)

            @observe()
            def stream() -> Generator[int, None, None]:
                with span("child"):
                    score("helpfulness", 0.9)
                yield 1
                yield 2

            self.assertEqual(list(stream()), [1, 2])

            self.assertFalse((workdir / ".bir" / "traces.jsonl").exists())
            self.assertEqual(load_events(), [])

    def test_observe_async_generator_yields_same_values_and_records_completed_trace(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            async def stream(count: int) -> AsyncGenerator[int, None]:
                for index in range(count):
                    await asyncio.sleep(0)
                    yield index

            async def collect() -> list[int]:
                return [value async for value in stream(3)]

            self.assertEqual(asyncio.run(collect()), [0, 1, 2])

            events = read_events(workdir / ".bir" / "traces.jsonl")
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["type"], "trace")
            self.assertEqual(event["name"], "stream")
            self.assertEqual(event["status"], "success")
            self.assertEqual(event["metadata"], {"generator": {"outcome": "completed"}})

    def test_observe_async_generator_is_lazy_until_first_iteration(self) -> None:
        with temporary_workdir() as workdir:
            body_ran: list[str] = []

            @observe()
            async def stream() -> AsyncGenerator[int, None]:
                body_ran.append("started")
                await asyncio.sleep(0)
                yield 1

            async def drive() -> None:
                generator = stream()
                self.assertEqual(body_ran, [])
                self.assertFalse((workdir / ".bir" / "traces.jsonl").exists())
                self.assertEqual(await generator.__anext__(), 1)
                await generator.aclose()

            asyncio.run(drive())
            self.assertEqual(body_ran, ["started"])

    def test_observe_async_generator_child_events_attach_to_trace(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            async def stream() -> AsyncGenerator[int, None]:
                async with span("before_first"):
                    await asyncio.sleep(0)
                yield 1
                async with span("after_first"):
                    await asyncio.sleep(0)
                yield 2

            async def collect() -> list[int]:
                return [value async for value in stream()]

            self.assertEqual(asyncio.run(collect()), [1, 2])

            events = read_events(workdir / ".bir" / "traces.jsonl")
            trace_event = next(event for event in events if event["type"] == "trace")
            span_events = [event for event in events if event["type"] == "span"]
            self.assertEqual({event["name"] for event in span_events}, {"before_first", "after_first"})
            for span_event in span_events:
                self.assertEqual(span_event["trace_id"], trace_event["id"])
                self.assertEqual(span_event["parent_id"], trace_event["id"])

    def test_observe_async_generator_records_error_and_reraises(self) -> None:
        with temporary_workdir() as workdir:
            secret_error = "provider failed token=sk-secret"

            @observe()
            async def stream() -> AsyncGenerator[int, None]:
                await asyncio.sleep(0)
                yield 1
                raise RuntimeError(secret_error)

            async def drive() -> None:
                generator = stream()
                self.assertEqual(await generator.__anext__(), 1)
                with self.assertRaisesRegex(RuntimeError, "provider failed"):
                    await generator.__anext__()

            asyncio.run(drive())

            events = read_events(workdir / ".bir" / "traces.jsonl")
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["status"], "error")
            self.assertEqual(event["error"], "provider failed token=[redacted]")
            self.assertEqual(event["metadata"], {"generator": {"outcome": "error"}})

    def test_observe_async_generator_aclose_records_terminal_state_and_runs_finally(self) -> None:
        with temporary_workdir() as workdir:
            finally_ran: list[str] = []

            @observe()
            async def stream() -> AsyncGenerator[int, None]:
                try:
                    yield 1
                    yield 2
                finally:
                    finally_ran.append("cleanup")

            async def drive() -> None:
                generator = stream()
                self.assertEqual(await generator.__anext__(), 1)
                await generator.aclose()

            asyncio.run(drive())

            self.assertEqual(finally_ran, ["cleanup"])
            event = read_events(workdir / ".bir" / "traces.jsonl")[0]
            self.assertEqual(event["status"], "success")
            self.assertEqual(event["metadata"], {"generator": {"outcome": "closed"}})

    def test_observe_async_generator_asend_value_round_trips(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            async def echo() -> AsyncGenerator[str, str | None]:
                received = yield "ready"
                yield f"got {received}"

            async def drive() -> tuple[str, str]:
                generator = echo()
                first = await generator.asend(None)
                second = await generator.asend("hello")
                await generator.aclose()
                return first, second

            self.assertEqual(asyncio.run(drive()), ("ready", "got hello"))
            event = read_events(workdir / ".bir" / "traces.jsonl")[0]
            self.assertEqual(event["metadata"], {"generator": {"outcome": "closed"}})

    def test_observe_async_generator_capture_records_item_count_not_content(self) -> None:
        with temporary_workdir() as workdir:

            @observe(capture_outputs=True)
            async def stream() -> AsyncGenerator[str, None]:
                yield "secretpayload-1"
                yield "secretpayload-2"

            async def collect() -> list[str]:
                return [value async for value in stream()]

            self.assertEqual(asyncio.run(collect()), ["secretpayload-1", "secretpayload-2"])

            trace_path = workdir / ".bir" / "traces.jsonl"
            event = read_events(trace_path)[0]
            self.assertEqual(event["metadata"], {"generator": {"outcome": "completed", "items": 2}})
            self.assertNotIn("secretpayload", trace_path.read_text(encoding="utf-8"))

    def test_observe_async_generators_isolated_across_concurrent_tasks(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            async def worker(label: str) -> AsyncGenerator[str, None]:
                for index in range(2):
                    async with span(f"span_{label}"):
                        # Yield control so the two workers interleave; leaked
                        # contextvars would cross-attach the spans.
                        await asyncio.sleep(0)
                    yield f"{label}{index}"

            async def collect(label: str) -> list[str]:
                return [value async for value in worker(label)]

            async def main() -> tuple[list[str], list[str]]:
                return await asyncio.gather(collect("a"), collect("b"))

            results = asyncio.run(main())
            self.assertEqual(sorted(results[0] + results[1]), ["a0", "a1", "b0", "b1"])

            events = read_events(workdir / ".bir" / "traces.jsonl")
            trace_events = [event for event in events if event["type"] == "trace"]
            span_events = [event for event in events if event["type"] == "span"]
            self.assertEqual(len(trace_events), 2)
            self.assertEqual(len(span_events), 4)

            traces_by_id = {event["id"]: event for event in trace_events}
            for span_event in span_events:
                self.assertIn(span_event["trace_id"], traces_by_id)
                self.assertEqual(span_event["parent_id"], span_event["trace_id"])
            self.assertEqual(len({event["trace_id"] for event in span_events}), 2)

    def test_sample_rate_zero_drops_async_generator_but_still_yields(self) -> None:
        with temporary_workdir() as workdir:
            configure(sample_rate=0.0)

            @observe()
            async def stream() -> AsyncGenerator[int, None]:
                async with span("child"):
                    score("helpfulness", 0.9)
                yield 1
                yield 2

            async def collect() -> list[int]:
                return [value async for value in stream()]

            self.assertEqual(asyncio.run(collect()), [1, 2])

            self.assertFalse((workdir / ".bir" / "traces.jsonl").exists())
            self.assertEqual(load_events(), [])

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

    def test_async_with_span_records_same_span_event_as_sync(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            async def answer() -> None:
                async with span("retrieve_context"):
                    await asyncio.sleep(0)

            asyncio.run(answer())

            events = read_events(workdir / ".bir" / "traces.jsonl")
            span_event = next(event for event in events if event["type"] == "span")
            trace_event = next(event for event in events if event["type"] == "trace")
            self.assertEqual(span_event["trace_id"], trace_event["trace_id"])
            self.assertEqual(span_event["parent_id"], trace_event["id"])
            self.assertEqual(span_event["name"], "retrieve_context")
            self.assertEqual(span_event["status"], "success")
            self.assertIsNone(span_event["error"])

    def test_async_with_span_isolated_across_concurrent_tasks(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            async def task(value: str) -> str:
                async with span(f"span_{value}"):
                    # Yield control so the two tasks interleave; if the parent_id
                    # contextvar leaked across tasks the spans would nest wrong.
                    await asyncio.sleep(0)
                return value

            async def main() -> tuple[str, str]:
                return await asyncio.gather(task("a"), task("b"))

            self.assertEqual(sorted(asyncio.run(main())), ["a", "b"])

            events = read_events(workdir / ".bir" / "traces.jsonl")
            trace_events = [event for event in events if event["type"] == "trace"]
            span_events = [event for event in events if event["type"] == "span"]
            self.assertEqual(len(trace_events), 2)
            self.assertEqual(len(span_events), 2)

            traces_by_id = {event["id"]: event for event in trace_events}
            for span_event in span_events:
                self.assertIn(span_event["trace_id"], traces_by_id)
                parent = traces_by_id[span_event["trace_id"]]
                self.assertEqual(span_event["parent_id"], parent["id"])
                self.assertEqual(span_event["status"], "success")
            # Each span belongs to its own task's trace, with no cross-task leakage.
            self.assertEqual(len({event["trace_id"] for event in span_events}), 2)

    def test_async_with_generation_records_same_generation_event_as_sync(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            async def answer() -> None:
                async with generation("local.llm", model="demo", capture_output=True) as gen:
                    await asyncio.sleep(0)
                    gen.set_output("ok")
                    gen.set_usage(input_tokens=1, output_tokens=2)

            asyncio.run(answer())

            events = read_events(workdir / ".bir" / "traces.jsonl")
            generation_event = next(event for event in events if event["type"] == "generation")
            trace_event = next(event for event in events if event["type"] == "trace")
            self.assertEqual(generation_event["trace_id"], trace_event["trace_id"])
            self.assertEqual(generation_event["parent_id"], trace_event["id"])
            self.assertEqual(generation_event["name"], "local.llm")
            self.assertEqual(generation_event["model"], "demo")
            self.assertEqual(generation_event["output"], "ok")
            self.assertEqual(
                generation_event["usage"],
                {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            )
            self.assertEqual(generation_event["status"], "success")
            self.assertIsNone(generation_event["error"])

    def test_async_with_generation_records_error_status(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            async def fail() -> None:
                async with generation("openai.chat"):
                    await asyncio.sleep(0)
                    raise RuntimeError("provider failed")

            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                asyncio.run(fail())

            events = read_events(workdir / ".bir" / "traces.jsonl")
            generation_event = next(event for event in events if event["type"] == "generation")
            trace_event = next(event for event in events if event["type"] == "trace")
            self.assertEqual(generation_event["status"], "error")
            self.assertEqual(generation_event["error"], "provider failed")
            self.assertEqual(trace_event["status"], "error")

    def test_async_with_tool_call_records_same_tool_call_event_as_sync(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            async def answer() -> None:
                async with tool_call("search_docs", capture_output=True) as tool:
                    await asyncio.sleep(0)
                    tool.set_output(["doc-1"])

            asyncio.run(answer())

            events = read_events(workdir / ".bir" / "traces.jsonl")
            tool_event = next(event for event in events if event["type"] == "tool_call")
            trace_event = next(event for event in events if event["type"] == "trace")
            self.assertEqual(tool_event["trace_id"], trace_event["trace_id"])
            self.assertEqual(tool_event["parent_id"], trace_event["id"])
            self.assertEqual(tool_event["name"], "search_docs")
            self.assertEqual(tool_event["output"], ["doc-1"])
            self.assertEqual(tool_event["status"], "success")
            self.assertIsNone(tool_event["error"])

    def test_async_with_tool_call_records_error_status(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            async def fail() -> None:
                async with tool_call("search_docs"):
                    await asyncio.sleep(0)
                    raise LookupError("search failed")

            with self.assertRaisesRegex(LookupError, "search failed"):
                asyncio.run(fail())

            events = read_events(workdir / ".bir" / "traces.jsonl")
            tool_event = next(event for event in events if event["type"] == "tool_call")
            trace_event = next(event for event in events if event["type"] == "trace")
            self.assertEqual(tool_event["status"], "error")
            self.assertEqual(tool_event["error"], "search failed")
            self.assertEqual(trace_event["status"], "error")

    def test_async_with_retrieval_records_same_tool_call_shape_as_sync(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            async def answer() -> None:
                async with retrieval("vector_search", query="hello", capture_output=True) as result:
                    await asyncio.sleep(0)
                    result.add_document(id="doc-1", rank=1, score=0.5)

            asyncio.run(answer())

            events = read_events(workdir / ".bir" / "traces.jsonl")
            retrieval_event = next(event for event in events if event["name"] == "vector_search")
            trace_event = next(event for event in events if event["type"] == "trace")
            self.assertEqual(retrieval_event["type"], "tool_call")
            self.assertEqual(retrieval_event["trace_id"], trace_event["trace_id"])
            self.assertEqual(retrieval_event["parent_id"], trace_event["id"])
            self.assertEqual(retrieval_event["metadata"], {"kind": "retrieval"})
            self.assertEqual(
                retrieval_event["output"],
                {"documents": [{"id": "doc-1", "rank": 1, "score": 0.5}]},
            )
            self.assertEqual(retrieval_event["status"], "success")
            self.assertIsNone(retrieval_event["error"])

    def test_async_with_retrieval_records_error_status(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            async def fail() -> None:
                async with retrieval("vector_search", query="hello"):
                    await asyncio.sleep(0)
                    raise LookupError("retrieval failed")

            with self.assertRaisesRegex(LookupError, "retrieval failed"):
                asyncio.run(fail())

            events = read_events(workdir / ".bir" / "traces.jsonl")
            retrieval_event = next(event for event in events if event["name"] == "vector_search")
            trace_event = next(event for event in events if event["type"] == "trace")
            self.assertEqual(retrieval_event["type"], "tool_call")
            self.assertEqual(retrieval_event["status"], "error")
            self.assertEqual(retrieval_event["error"], "retrieval failed")
            self.assertEqual(trace_event["status"], "error")

    def test_async_with_trace_records_same_trace_event_as_sync(self) -> None:
        with temporary_workdir() as workdir:

            async def workflow() -> None:
                async with trace("manual_workflow", metadata={"kind": "manual"}):
                    await asyncio.sleep(0)
                    async with span("retrieve_context"):
                        score("helpfulness", 0.9)

            asyncio.run(workflow())

            events = read_events(workdir / ".bir" / "traces.jsonl")
            trace_event = next(event for event in events if event["type"] == "trace")
            span_event = next(event for event in events if event["type"] == "span")
            score_event = next(event for event in events if event["type"] == "score")
            self.assertEqual(trace_event["name"], "manual_workflow")
            self.assertEqual(trace_event["id"], trace_event["trace_id"])
            self.assertIsNone(trace_event["parent_id"])
            self.assertEqual(trace_event["metadata"], {"kind": "manual"})
            self.assertEqual(trace_event["status"], "success")
            self.assertIsNone(trace_event["error"])
            self.assertEqual(span_event["trace_id"], trace_event["id"])
            self.assertEqual(span_event["parent_id"], trace_event["id"])
            self.assertEqual(score_event["parent_id"], span_event["id"])

    def test_async_with_trace_records_error_status(self) -> None:
        with temporary_workdir() as workdir:

            async def workflow() -> None:
                async with trace("manual_workflow"):
                    await asyncio.sleep(0)
                    raise RuntimeError("workflow failed")

            with self.assertRaisesRegex(RuntimeError, "workflow failed"):
                asyncio.run(workflow())

            events = read_events(workdir / ".bir" / "traces.jsonl")
            trace_event = next(event for event in events if event["type"] == "trace")
            self.assertEqual(trace_event["status"], "error")
            self.assertEqual(trace_event["error"], "workflow failed")

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

    def test_score_records_redacted_metadata(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer() -> None:
                score(
                    "faithfulness",
                    0.4,
                    metadata={"reason": "answer cites no context", "api_key": "sk-secret"},
                )

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            score_event = next(event for event in events if event["type"] == "score")
            self.assertEqual(score_event["value"], 0.4)
            self.assertEqual(
                score_event["metadata"],
                {"reason": "answer cites no context", "api_key": "[redacted]"},
            )

    def test_score_without_metadata_keeps_empty_metadata(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer() -> None:
                score("helpfulness", 0.9)

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            score_event = next(event for event in events if event["type"] == "score")
            self.assertEqual(score_event["metadata"], {})

    def test_score_rejects_non_mapping_metadata(self) -> None:
        with temporary_workdir():

            @observe()
            def answer() -> None:
                score("helpfulness", 0.9, metadata=["not", "a", "mapping"])  # type: ignore[arg-type]

            with self.assertRaisesRegex(TypeError, "score metadata must be a mapping"):
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
                    gen.set_cost(input_cost=0.000005, output_cost=0.000014)
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
            self.assertEqual(
                generation_event["cost"],
                {"input_cost": 0.000005, "output_cost": 0.000014, "total_cost": 0.000019},
            )
            self.assertEqual(generation_event["currency"], "USD")
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

    def test_generation_records_prompt_version_without_capturing_prompt_text_by_default(self) -> None:
        with temporary_workdir() as workdir:

            @observe(capture_inputs=True, capture_outputs=True)
            def answer(question: str) -> str:
                prompt_record = prompt(
                    "answer_question",
                    version="v1",
                    template="Answer {question} with api_key={api_key}",
                    variables={"question": question, "api_key": "sk-secret"},
                )
                with generation("local.llm", prompt=prompt_record) as gen:
                    gen.set_output("ok")
                return "ok"

            answer("hello")

            events = read_events(workdir / ".bir" / "traces.jsonl")
            generation_event = next(event for event in events if event["type"] == "generation")
            metadata = cast(dict[str, Any], generation_event["metadata"])
            self.assertIsInstance(metadata, dict)
            prompt_metadata = cast(dict[str, Any], metadata["prompt"])
            self.assertIsInstance(prompt_metadata, dict)
            self.assertEqual(prompt_metadata["name"], "answer_question")
            self.assertEqual(prompt_metadata["version"], "v1")
            self.assertEqual(
                prompt_metadata["template_sha256"],
                hashlib.sha256("Answer {question} with api_key={api_key}".encode("utf-8")).hexdigest(),
            )
            self.assertNotIn("template", prompt_metadata)
            self.assertNotIn("variables", prompt_metadata)
            self.assertNotIn("rendered", prompt_metadata)
            self.assertNotIn("sk-secret", json.dumps(generation_event))

    def test_prompt_can_capture_template_variables_and_rendered_text_when_opted_in(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer(question: str) -> str:
                prompt_record = prompt(
                    "answer_question",
                    version="v2",
                    template="Answer {question} with token={token}",
                    variables={"question": question, "token": "raw-token"},
                    metadata={"owner": "evals", "secret": "prompt-secret"},
                    capture_template=True,
                    capture_variables=True,
                    capture_rendered=True,
                )
                with generation("local.llm", prompt=prompt_record):
                    pass
                return "ok"

            answer("hello")

            events = read_events(workdir / ".bir" / "traces.jsonl")
            generation_event = next(event for event in events if event["type"] == "generation")
            metadata = cast(dict[str, Any], generation_event["metadata"])
            self.assertIsInstance(metadata, dict)
            prompt_metadata = cast(dict[str, Any], metadata["prompt"])
            self.assertIsInstance(prompt_metadata, dict)
            self.assertEqual(prompt_metadata["template"], "Answer {question} with token={token}")
            self.assertEqual(prompt_metadata["variables"], {"question": "hello", "token": "[redacted]"})
            self.assertEqual(prompt_metadata["rendered"], "Answer hello with token=[redacted]")
            self.assertEqual(prompt_metadata["metadata"], {"owner": "evals", "secret": "[redacted]"})

    def test_prompt_rejects_empty_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "prompt name"):
            prompt("")

    def test_sdk_rejects_empty_event_names(self) -> None:
        with self.assertRaisesRegex(ValueError, "observe name"):
            observe(name="")
        with self.assertRaisesRegex(ValueError, "span name"):
            span("")
        with self.assertRaisesRegex(ValueError, "generation name"):
            generation("")
        with self.assertRaisesRegex(ValueError, "tool_call name"):
            tool_call("")
        with self.assertRaisesRegex(ValueError, "retrieval name"):
            retrieval("", query="hello")
        with self.assertRaisesRegex(ValueError, "score name"):
            score("", 1.0)

    def test_generation_usage_rejects_non_finite_values(self) -> None:
        with temporary_workdir():

            @observe()
            def answer() -> None:
                with generation("local.llm") as gen:
                    gen.set_usage(input_tokens=float("inf"))

            with self.assertRaisesRegex(ValueError, "input_tokens must be finite"):
                answer()

    def test_generation_usage_rejects_negative_values(self) -> None:
        with temporary_workdir():

            @observe()
            def answer() -> None:
                with generation("local.llm") as gen:
                    gen.set_usage(input_tokens=-1)

            with self.assertRaisesRegex(ValueError, "input_tokens must be non-negative"):
                answer()

    def test_generation_usage_requires_at_least_one_token_field(self) -> None:
        with temporary_workdir():

            @observe()
            def answer() -> None:
                with generation("local.llm") as gen:
                    gen.set_usage()

            with self.assertRaisesRegex(ValueError, "usage requires at least one token field"):
                answer()

    def test_generation_cost_rejects_non_finite_values(self) -> None:
        with temporary_workdir():

            @observe()
            def answer() -> None:
                with generation("local.llm") as gen:
                    gen.set_cost(input_cost=float("inf"))

            with self.assertRaisesRegex(ValueError, "input_cost must be finite"):
                answer()

    def test_generation_cost_rejects_negative_values(self) -> None:
        with temporary_workdir():

            @observe()
            def answer() -> None:
                with generation("local.llm") as gen:
                    gen.set_cost(total_cost=-0.01)

            with self.assertRaisesRegex(ValueError, "total_cost must be non-negative"):
                answer()

    def test_generation_cost_requires_at_least_one_cost_field(self) -> None:
        with temporary_workdir():

            @observe()
            def answer() -> None:
                with generation("local.llm") as gen:
                    gen.set_cost()

            with self.assertRaisesRegex(ValueError, "cost requires at least one cost field"):
                answer()

    def test_generation_cost_rejects_invalid_currency(self) -> None:
        with temporary_workdir():

            @observe()
            def answer() -> None:
                with generation("local.llm") as gen:
                    gen.set_cost(total_cost=0.01, currency="")

            with self.assertRaisesRegex(ValueError, "currency must not be empty"):
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

    def test_retrieval_records_document_tool_call_shape(self) -> None:
        with temporary_workdir() as workdir:

            @observe(capture_inputs=True, capture_outputs=True)
            def answer(question: str) -> list[str]:
                with retrieval("vector_search", query=question, metadata={"provider": "local"}) as result:
                    result.add_document(
                        id="doc-1",
                        rank=1,
                        score=0.82,
                        source="docs",
                        text="authorization: Bearer doc-secret",
                        metadata={"token": "document-token"},
                    )
                    result.add_document(id="doc-2")
                return ["doc-1", "doc-2"]

            answer("hello")

            events = read_events(workdir / ".bir" / "traces.jsonl")
            retrieval_event = next(event for event in events if event["name"] == "vector_search")
            trace_event = next(event for event in events if event["type"] == "trace")
            self.assertEqual(retrieval_event["type"], "tool_call")
            self.assertEqual(retrieval_event["trace_id"], trace_event["trace_id"])
            self.assertEqual(retrieval_event["parent_id"], trace_event["id"])
            self.assertEqual(retrieval_event["metadata"], {"provider": "local", "kind": "retrieval"})
            self.assertEqual(retrieval_event["input"], {"query": "hello"})
            self.assertEqual(
                retrieval_event["output"],
                {
                    "documents": [
                        {
                            "id": "doc-1",
                            "rank": 1,
                            "score": 0.82,
                            "source": "docs",
                            "text": "authorization: Bearer [redacted]",
                            "metadata": {"token": "[redacted]"},
                        },
                        {"id": "doc-2"},
                    ]
                },
            )
            self.assertEqual(retrieval_event["status"], "success")

    def test_retrieval_capture_can_be_enabled_per_call(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer() -> None:
                with retrieval(
                    "vector_search",
                    query={"text": "hello"},
                    capture_input=True,
                    capture_output=True,
                ) as result:
                    result.set_documents([{"id": "doc-1", "text": "local context"}])

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            retrieval_event = next(event for event in events if event["name"] == "vector_search")
            self.assertEqual(retrieval_event["type"], "tool_call")
            self.assertEqual(retrieval_event["metadata"], {"kind": "retrieval"})
            self.assertEqual(retrieval_event["input"], {"query": {"text": "hello"}})
            self.assertEqual(retrieval_event["output"], {"documents": [{"id": "doc-1", "text": "local context"}]})

    def test_retrieval_uses_opt_in_capture_defaults(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer() -> None:
                with retrieval("vector_search", query="hello") as result:
                    result.add_document(id="doc-1")

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            retrieval_event = next(event for event in events if event["name"] == "vector_search")
            self.assertIsNone(retrieval_event["input"])
            self.assertIsNone(retrieval_event["output"])

    def test_retrieval_rejects_invalid_document_numeric_fields(self) -> None:
        with temporary_workdir():

            @observe()
            def negative_rank() -> None:
                with retrieval("vector_search", query="hello") as result:
                    result.add_document(id="doc-1", rank=-1)

            @observe()
            def bool_rank() -> None:
                with retrieval("vector_search", query="hello") as result:
                    result.add_document(id="doc-1", rank=True)  # type: ignore[arg-type]

            @observe()
            def negative_score() -> None:
                with retrieval("vector_search", query="hello") as result:
                    result.add_document(id="doc-1", score=-0.1)

            @observe()
            def set_documents_invalid_score() -> None:
                with retrieval("vector_search", query="hello") as result:
                    result.set_documents([{"id": "doc-1", "score": -0.1}])

            with self.assertRaisesRegex(ValueError, "retrieval document rank must be non-negative"):
                negative_rank()
            with self.assertRaisesRegex(TypeError, "retrieval document rank must be an int"):
                bool_rank()
            with self.assertRaisesRegex(ValueError, "retrieval document score must be non-negative"):
                negative_score()
            with self.assertRaisesRegex(ValueError, "retrieval document score must be non-negative"):
                set_documents_invalid_score()

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
        tool_event = next(event for event in events if event.type == "tool_call")
        score_event = next(event for event in events if event.type == "score")
        self.assertEqual(tool_event.metadata, {"kind": "retrieval"})
        self.assertEqual(
            tool_event.output,
            {
                "documents": [
                    {
                        "id": "doc-1",
                        "rank": 1,
                        "score": 0.82,
                        "source": "docs",
                        "text": "Bir records local traces with JSONL.",
                    }
                ]
            },
        )
        self.assertEqual(generation_event.parent_id, "trace-fixture-1")
        self.assertEqual(generation_event.model, "demo-model")
        self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 24, "total_tokens": 36})
        self.assertEqual(
            generation_event.cost,
            {"input_cost": 0.000012, "output_cost": 0.000048, "total_cost": 0.00006},
        )
        self.assertEqual(generation_event.currency, "USD")
        self.assertEqual(generation_event.raw["usage"], {"input_tokens": 12, "output_tokens": 24, "total_tokens": 36})
        self.assertEqual(
            generation_event.raw["cost"],
            {"input_cost": 0.000012, "output_cost": 0.000048, "total_cost": 0.00006},
        )
        self.assertEqual(generation_event.raw["currency"], "USD")
        self.assertEqual(score_event.parent_id, "generation-fixture-1")
        self.assertEqual(score_event.value, 0.82)
        self.assertEqual(score_event.raw["value"], 0.82)
        trace_event = next(event for event in events if event.type == "trace")
        self.assertEqual(
            trace_event.metadata,
            {"service": {"name": "rag-api", "environment": "production"}},
        )
        self.assertEqual(
            generation_event.metadata,
            {
                "provider": "local",
                "prompt": {
                    "name": "answer_question",
                    "version": "v1",
                    "template_sha256": hashlib.sha256(
                        "Answer the question: {question}".encode("utf-8")
                    ).hexdigest(),
                },
            },
        )

    def test_load_events_parses_server_shape_with_explicit_optional_nulls(self) -> None:
        # The server persists every event with model_dump(exclude_none=False), so
        # optional fields the SDK omits are written as explicit JSON nulls. The SDK
        # reader must keep parsing that canonical persisted shape so it cannot drift
        # away from the writer. See docs/IMPLEMENTATION_ROADMAP.md Stage 2.
        with temporary_workdir() as workdir:
            trace_path = workdir / "server-shape.jsonl"
            base = {
                "schema_version": "1.0",
                "trace_id": "trace-1",
                "parent_id": "trace-1",
                "start_time": "2026-01-01T00:00:00+00:00",
                "end_time": "2026-01-01T00:00:01+00:00",
                "status": "success",
                "metadata": {},
                "input": None,
                "output": None,
                "error": None,
                # exclude_none=False spells unset optional fields as explicit nulls.
                "model": None,
                "usage": None,
                "cost": None,
                "currency": None,
            }
            generation_event = dict(base, id="generation-1", name="local.llm", type="generation", value=None)
            score_event = dict(base, id="score-1", name="helpfulness", type="score", value=0.82)
            trace_path.write_text(
                json.dumps(generation_event) + "\n" + json.dumps(score_event) + "\n",
                encoding="utf-8",
            )

            events = load_events(trace_path)

            self.assertEqual([event.type for event in events], ["generation", "score"])
            generation_loaded = next(event for event in events if event.type == "generation")
            self.assertIsNone(generation_loaded.value)
            self.assertIsNone(generation_loaded.model)
            self.assertIsNone(generation_loaded.usage)
            self.assertIsNone(generation_loaded.cost)
            self.assertIsNone(generation_loaded.currency)
            score_loaded = next(event for event in events if event.type == "score")
            self.assertEqual(score_loaded.value, 0.82)
            self.assertIsNone(score_loaded.model)
            self.assertIsNone(score_loaded.usage)
            self.assertIsNone(score_loaded.cost)
            self.assertIsNone(score_loaded.currency)
            # The explicit nulls survive on the raw payload, so a re-send keeps them.
            self.assertIsNone(score_loaded.raw["usage"])
            self.assertIsNone(score_loaded.raw["model"])

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

            negative_usage_path = workdir / "negative-usage.jsonl"
            event_with_negative_usage = dict(
                valid_event,
                id="generation-1",
                type="generation",
                parent_id="trace-1",
                usage={"input_tokens": -1},
            )
            negative_usage_path.write_text(json.dumps(event_with_negative_usage) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "usage.input_tokens.*non-negative"):
                load_events(negative_usage_path)

            invalid_cost_path = workdir / "invalid-cost.jsonl"
            event_with_invalid_cost = dict(
                valid_event,
                id="generation-1",
                type="generation",
                parent_id="trace-1",
                cost={"input_cost": float("inf")},
            )
            invalid_cost_path.write_text(
                json.dumps(event_with_invalid_cost, allow_nan=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "cost.input_cost"):
                load_events(invalid_cost_path)

            bool_cost_path = workdir / "bool-cost.jsonl"
            event_with_bool_cost = dict(
                valid_event,
                id="generation-1",
                type="generation",
                parent_id="trace-1",
                cost={"input_cost": True},
            )
            bool_cost_path.write_text(json.dumps(event_with_bool_cost) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(TypeError, "cost.input_cost"):
                load_events(bool_cost_path)

            negative_cost_path = workdir / "negative-cost.jsonl"
            event_with_negative_cost = dict(
                valid_event,
                id="generation-1",
                type="generation",
                parent_id="trace-1",
                cost={"input_cost": -0.01},
            )
            negative_cost_path.write_text(json.dumps(event_with_negative_cost) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cost.input_cost.*non-negative"):
                load_events(negative_cost_path)

            invalid_currency_path = workdir / "invalid-currency.jsonl"
            event_with_invalid_currency = dict(
                valid_event,
                id="generation-1",
                type="generation",
                parent_id="trace-1",
                currency=123,
            )
            invalid_currency_path.write_text(json.dumps(event_with_invalid_currency) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "currency"):
                load_events(invalid_currency_path)

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

    def test_send_events_posts_local_events_in_one_batch(self) -> None:
        with temporary_workdir():

            @observe(capture_inputs=True)
            def answer(question: str) -> str:
                score("helpfulness", 0.9)
                return question.upper()

            answer("hello")
            posted_batches: list[list[dict[str, object]]] = []
            posted_urls: list[str] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                self.assertEqual(timeout, 10.0)
                posted_urls.append(request_url(request))
                batch = posted_request_batch(request)
                posted_batches.append(batch)
                return batch_response_accepting(batch)

            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                result = send_events("http://server.test")

            self.assertEqual(result.accepted, 2)
            self.assertEqual(len(result.event_ids), 2)
            self.assertEqual(result.attempted, 2)
            self.assertEqual(result.skipped, 0)
            self.assertEqual(posted_urls, ["http://server.test/v1/events/batch"])
            posted_events = posted_batches[0]
            posted_types = [event["type"] for event in posted_events]
            self.assertEqual(posted_types, ["trace", "score"])
            trace_event = next(event for event in posted_events if event["type"] == "trace")
            self.assertEqual(trace_event["input"], {"question": "hello"})

    def test_send_events_batches_complete_traces_root_first(self) -> None:
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
            posted_batches: list[list[dict[str, object]]] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                batch = posted_request_batch(request)
                posted_batches.append(batch)
                return batch_response_accepting(batch)

            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                result = send_events("http://server.test")

            self.assertEqual(result.accepted, 5)
            self.assertEqual(result.attempted, 5)
            self.assertEqual(result.skipped, 0)
            posted_events = posted_batches[0]
            self.assertEqual(
                [event["type"] for event in posted_events],
                ["trace", "span", "tool_call", "generation", "score"],
            )
            self.assertEqual(result.event_ids, [event["id"] for event in posted_events])

    def test_send_events_falls_back_to_per_event_posts_when_batch_missing(self) -> None:
        with temporary_workdir():

            @observe()
            def answer(question: str) -> str:
                score("helpfulness", 0.9)
                return question.upper()

            answer("hello")
            posted_urls: list[str] = []
            posted_events: list[dict[str, object]] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                posted_urls.append(request_url(request))
                if request_url(request).endswith("/v1/events/batch"):
                    raise http_error(request, 404, b'{"detail":"Not Found"}')
                posted_events.append(posted_request_body(request))
                return FakeHttpResponse()

            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                result = send_events("http://server.test")

            self.assertEqual(result.accepted, 2)
            self.assertEqual(
                posted_urls,
                [
                    "http://server.test/v1/events/batch",
                    "http://server.test/v1/events",
                    "http://server.test/v1/events",
                ],
            )
            self.assertEqual([event["type"] for event in posted_events], ["trace", "score"])
            self.assertEqual(result.event_ids, [event["id"] for event in posted_events])
            self.assertEqual(result.attempted, 2)
            self.assertEqual(result.skipped, 0)

    def test_send_events_makes_no_requests_when_there_are_no_events(self) -> None:
        with temporary_workdir():

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                raise AssertionError("send_events must not post when there are no events")

            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                result = send_events("http://server.test")

            self.assertEqual(result.accepted, 0)
            self.assertEqual(result.event_ids, [])
            self.assertEqual(result.attempted, 0)
            self.assertEqual(result.skipped, 0)

    def test_send_events_raises_when_server_rejects_batch(self) -> None:
        with temporary_workdir():

            @observe()
            def answer() -> str:
                return "ok"

            answer()
            posted_urls: list[str] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                posted_urls.append(request_url(request))
                raise http_error(request, 422, b'{"detail":"rejected"}')

            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                with self.assertRaisesRegex(RuntimeError, "HTTP 422"):
                    send_events("http://server.test")
            self.assertEqual(posted_urls, ["http://server.test/v1/events/batch"])

    def test_send_events_raises_on_invalid_batch_response(self) -> None:
        with temporary_workdir():

            @observe()
            def answer() -> str:
                return "ok"

            answer()

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                return FakeHttpResponse(b'{"accepted":1}')

            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                with self.assertRaisesRegex(RuntimeError, "invalid batch response"):
                    send_events("http://server.test")

    def test_send_events_uses_server_accepted_batch_result(self) -> None:
        with temporary_workdir():

            @observe()
            def answer() -> str:
                return "ok"

            answer()

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                return FakeHttpResponse(b'{"accepted":0,"event_ids":[]}')

            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                result = send_events("http://server.test")

            self.assertEqual(result.accepted, 0)
            self.assertEqual(result.event_ids, [])
            self.assertEqual(result.attempted, 1)
            self.assertEqual(result.skipped, 1)

    def test_send_events_fallback_uses_server_accepted_count(self) -> None:
        with temporary_workdir():

            @observe()
            def answer() -> str:
                return "ok"

            answer()

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                if request_url(request).endswith("/v1/events/batch"):
                    raise http_error(request, 404, b'{"detail":"Not Found"}')
                return FakeHttpResponse(b'{"accepted":0,"id":"already-seen"}')

            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                result = send_events("http://server.test")

            self.assertEqual(result.accepted, 0)
            self.assertEqual(result.event_ids, [])
            self.assertEqual(result.attempted, 1)
            self.assertEqual(result.skipped, 1)

    def test_send_events_retries_transient_failure_then_succeeds(self) -> None:
        with temporary_workdir():

            @observe()
            def answer() -> str:
                return "ok"

            answer()
            batch_attempts: list[object] = []
            sleeps: list[float] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                batch_attempts.append(request)
                if len(batch_attempts) == 1:
                    raise urllib.error.URLError("temporary network blip")
                return batch_response_accepting(posted_request_batch(request))

            with patch("bir._sdk.time.sleep", side_effect=lambda seconds: sleeps.append(seconds)):
                with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                    result = send_events("http://server.test")

            self.assertEqual(len(batch_attempts), 2)
            self.assertEqual(sleeps, [0.5])
            self.assertEqual(result.accepted, 1)
            self.assertEqual(result.attempted, 1)

    def test_send_events_retries_server_5xx_then_succeeds(self) -> None:
        with temporary_workdir():

            @observe()
            def answer() -> str:
                return "ok"

            answer()
            batch_attempts: list[object] = []
            sleeps: list[float] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                batch_attempts.append(request)
                if len(batch_attempts) == 1:
                    raise http_error(request, 503, b'{"detail":"Server Error"}')
                return batch_response_accepting(posted_request_batch(request))

            with patch("bir._sdk.time.sleep", side_effect=lambda seconds: sleeps.append(seconds)):
                with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                    result = send_events("http://server.test")

            self.assertEqual(len(batch_attempts), 2)
            self.assertEqual(len(sleeps), 1)
            self.assertEqual(result.accepted, 1)

    def test_send_events_raises_after_exhausting_retries_with_backoff(self) -> None:
        with temporary_workdir():

            @observe()
            def answer() -> str:
                return "ok"

            answer()
            batch_attempts: list[object] = []
            sleeps: list[float] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                batch_attempts.append(request)
                raise urllib.error.URLError("network down")

            with patch("bir._sdk.time.sleep", side_effect=lambda seconds: sleeps.append(seconds)):
                with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                    with self.assertRaisesRegex(RuntimeError, "could not send events"):
                        send_events("http://server.test", retries=2, backoff=0.5)

            # One initial attempt plus two retries, with exponential backoff between.
            self.assertEqual(len(batch_attempts), 3)
            self.assertEqual(sleeps, [0.5, 1.0])

    def test_send_events_does_not_retry_client_error(self) -> None:
        with temporary_workdir():

            @observe()
            def answer() -> str:
                return "ok"

            answer()
            batch_attempts: list[object] = []
            sleeps: list[float] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                batch_attempts.append(request)
                raise http_error(request, 422, b'{"detail":"rejected"}')

            with patch("bir._sdk.time.sleep", side_effect=lambda seconds: sleeps.append(seconds)):
                with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                    with self.assertRaisesRegex(RuntimeError, "HTTP 422"):
                        send_events("http://server.test")

            self.assertEqual(len(batch_attempts), 1)
            self.assertEqual(sleeps, [])

    def test_send_events_retries_transient_failure_on_per_event_path(self) -> None:
        with temporary_workdir():

            @observe()
            def answer() -> str:
                return "ok"

            answer()
            per_event_attempts: list[object] = []
            sleeps: list[float] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                if request_url(request).endswith("/v1/events/batch"):
                    raise http_error(request, 404, b'{"detail":"Not Found"}')
                per_event_attempts.append(request)
                if len(per_event_attempts) == 1:
                    raise urllib.error.URLError("temporary network blip")
                return FakeHttpResponse()

            with patch("bir._sdk.time.sleep", side_effect=lambda seconds: sleeps.append(seconds)):
                with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                    result = send_events("http://server.test")

            self.assertEqual(len(per_event_attempts), 2)
            self.assertEqual(sleeps, [0.5])
            self.assertEqual(result.accepted, 1)

    def test_send_events_mark_sent_skips_already_sent_events_on_resend(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer() -> str:
                score("helpfulness", 0.9)
                return "ok"

            answer()
            trace_path = workdir / ".bir" / "traces.jsonl"
            original_jsonl = trace_path.read_bytes()

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                return batch_response_accepting(posted_request_batch(request))

            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                first = send_events("http://server.test", mark_sent=True)

            self.assertEqual(first.accepted, 2)
            self.assertEqual(first.attempted, 2)

            sidecar = workdir / ".bir" / "traces.jsonl.sent"
            self.assertTrue(sidecar.exists())
            recorded = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(set(recorded["event_ids"]), set(first.event_ids))
            # Marking sent state must never rewrite the trace JSONL.
            self.assertEqual(trace_path.read_bytes(), original_jsonl)

            def must_not_post(request: object, timeout: float) -> FakeHttpResponse:
                raise AssertionError("a re-send must not post already-sent events")

            with patch("bir._sdk.urllib.request.urlopen", side_effect=must_not_post):
                second = send_events("http://server.test", mark_sent=True)

            self.assertEqual(second.accepted, 0)
            self.assertEqual(second.attempted, 0)
            self.assertEqual(second.event_ids, [])
            self.assertEqual(trace_path.read_bytes(), original_jsonl)

    def test_send_events_mark_sent_sends_only_unsent_events(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer(value: str) -> str:
                return value

            answer("first")
            posted_batches: list[list[dict[str, object]]] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                batch = posted_request_batch(request)
                posted_batches.append(batch)
                return batch_response_accepting(batch)

            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                first = send_events("http://server.test", mark_sent=True)
            self.assertEqual(first.attempted, 1)

            # A new trace is recorded; only it should be posted on the next send.
            answer("second")
            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                second = send_events("http://server.test", mark_sent=True)

            self.assertEqual(second.attempted, 1)
            self.assertEqual(second.accepted, 1)
            self.assertEqual(len(posted_batches), 2)
            self.assertEqual(len(posted_batches[1]), 1)
            sent_ids = json.loads((workdir / ".bir" / "traces.jsonl.sent").read_text(encoding="utf-8"))
            self.assertEqual(len(sent_ids["event_ids"]), 2)

    def test_send_events_does_not_write_sidecar_by_default(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer() -> str:
                return "ok"

            answer()

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                return batch_response_accepting(posted_request_batch(request))

            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                result = send_events("http://server.test")

            self.assertEqual(result.accepted, 1)
            self.assertFalse((workdir / ".bir" / "traces.jsonl.sent").exists())

    def test_send_events_uploads_only_active_file_by_default(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / ".bir" / "traces.jsonl"
            max_bytes = self._probe_trace_line_bytes(trace_path) * 2
            configure(max_bytes=max_bytes, backup_count=50)
            for index in range(12):
                with trace(f"trace-{index:03d}"):
                    pass

            # Rotation stranded older events in .1 .. siblings the default ignores.
            self.assertTrue((workdir / ".bir" / "traces.jsonl.1").exists())
            active_ids = {event.id for event in load_events()}
            all_ids = {event.id for event in load_events(include_rotated=True)}
            self.assertLess(len(active_ids), len(all_ids))

            posted_batches: list[list[dict[str, object]]] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                batch = posted_request_batch(request)
                posted_batches.append(batch)
                return batch_response_accepting(batch)

            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                result = send_events("http://server.test")

            posted_ids = {event["id"] for event in posted_batches[0]}
            self.assertEqual(posted_ids, active_ids)
            self.assertEqual(result.attempted, len(active_ids))
            self.assertEqual(result.accepted, len(active_ids))

    def test_send_events_include_rotated_uploads_rotated_files_oldest_first(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / ".bir" / "traces.jsonl"
            max_bytes = self._probe_trace_line_bytes(trace_path) * 2
            configure(max_bytes=max_bytes, backup_count=50)
            for index in range(12):
                with trace(f"trace-{index:03d}"):
                    pass

            self.assertTrue((workdir / ".bir" / "traces.jsonl.1").exists())
            self.assertLess(len(load_events()), 12)

            posted_batches: list[list[dict[str, object]]] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                batch = posted_request_batch(request)
                posted_batches.append(batch)
                return batch_response_accepting(batch)

            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                result = send_events("http://server.test", include_rotated=True)

            posted_names = [event["name"] for event in posted_batches[0]]
            self.assertEqual(posted_names, [f"trace-{index:03d}" for index in range(12)])
            self.assertEqual(result.attempted, 12)
            self.assertEqual(result.accepted, 12)
            self.assertEqual(result.skipped, 0)

    def test_send_events_include_rotated_deduplicates_overlapping_event_ids(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer() -> str:
                score("helpfulness", 0.9)
                return "ok"

            answer()
            trace_path = workdir / ".bir" / "traces.jsonl"
            # A rotated sibling that overlaps the active file (e.g. a copied
            # backup) must not upload the shared events twice.
            (workdir / ".bir" / "traces.jsonl.1").write_bytes(trace_path.read_bytes())
            active_ids = [event.id for event in load_events()]
            self.assertEqual(len(active_ids), 2)

            posted_batches: list[list[dict[str, object]]] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                batch = posted_request_batch(request)
                posted_batches.append(batch)
                return batch_response_accepting(batch)

            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                result = send_events("http://server.test", include_rotated=True)

            posted = posted_batches[0]
            posted_ids = [str(event["id"]) for event in posted]
            self.assertEqual(len(posted_ids), len(set(posted_ids)))
            self.assertEqual(sorted(posted_ids), sorted(active_ids))
            self.assertEqual([event["type"] for event in posted], ["trace", "score"])
            self.assertEqual(result.attempted, 2)
            self.assertEqual(result.accepted, 2)

    def test_send_events_include_rotated_orders_traces_root_first_and_keeps_orphans(self) -> None:
        with temporary_workdir() as workdir:
            with trace("complete"):
                with span("complete-span"):
                    pass
            with trace("orphan"):
                with span("orphan-span"):
                    pass

            trace_path = workdir / ".bir" / "traces.jsonl"
            lines = trace_path.read_text(encoding="utf-8").splitlines()
            payloads = [json.loads(line) for line in lines]
            by_trace: dict[str, dict[str, str]] = {}
            for line, payload in zip(lines, payloads):
                by_trace.setdefault(str(payload["trace_id"]), {})[str(payload["type"])] = line
            complete_tid = next(p["trace_id"] for p in payloads if p["type"] == "trace" and p["name"] == "complete")
            orphan_tid = next(p["trace_id"] for p in payloads if p["type"] == "trace" and p["name"] == "orphan")

            # The rotated sibling holds a whole trace; the active file keeps only
            # an orphaned span whose root is in no selected file.
            (workdir / ".bir" / "traces.jsonl.1").write_text(
                by_trace[complete_tid]["trace"] + "\n" + by_trace[complete_tid]["span"] + "\n",
                encoding="utf-8",
            )
            trace_path.write_text(by_trace[orphan_tid]["span"] + "\n", encoding="utf-8")

            posted_batches: list[list[dict[str, object]]] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                batch = posted_request_batch(request)
                posted_batches.append(batch)
                return batch_response_accepting(batch)

            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                result = send_events("http://server.test", include_rotated=True)

            posted = posted_batches[0]
            self.assertEqual([event["type"] for event in posted], ["trace", "span", "span"])
            self.assertEqual(
                [event["name"] for event in posted],
                ["complete", "complete-span", "orphan-span"],
            )
            self.assertEqual(result.attempted, 3)
            self.assertEqual(result.accepted, 3)

    def test_send_events_include_rotated_mark_sent_skips_recorded_ids_across_files(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer(value: str) -> str:
                return value

            answer("first")
            trace_path = workdir / ".bir" / "traces.jsonl"
            # Overlap the active file into a rotated sibling so the first trace
            # lives in both files at once.
            (workdir / ".bir" / "traces.jsonl.1").write_bytes(trace_path.read_bytes())

            posted_batches: list[list[dict[str, object]]] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                batch = posted_request_batch(request)
                posted_batches.append(batch)
                return batch_response_accepting(batch)

            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                first = send_events("http://server.test", include_rotated=True, mark_sent=True)

            self.assertEqual(first.attempted, 1)
            self.assertEqual(first.accepted, 1)
            sidecar = workdir / ".bir" / "traces.jsonl.sent"
            self.assertTrue(sidecar.exists())

            # A new trace is appended only to the active file.
            answer("second")
            with patch("bir._sdk.urllib.request.urlopen", side_effect=fake_urlopen):
                second = send_events("http://server.test", include_rotated=True, mark_sent=True)

            self.assertEqual(second.attempted, 1)
            self.assertEqual(second.accepted, 1)
            first_ids = {event["id"] for event in posted_batches[0]}
            second_ids = [event["id"] for event in posted_batches[1]]
            self.assertEqual(len(second_ids), 1)
            self.assertNotIn(second_ids[0], first_ids)
            # The sidecar now records both traces' IDs across the file set.
            recorded = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(len(recorded["event_ids"]), 2)

    def test_load_events_rejects_invalid_jsonl(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "bad.jsonl"
            trace_path.write_text("not-json\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Invalid JSON"):
                load_events(trace_path)

    def test_concurrent_trace_writes_produce_valid_jsonl(self) -> None:
        with temporary_workdir() as workdir:

            @observe(capture_inputs=True)
            def answer(index: int) -> str:
                score("thread_index", index)
                return str(index)

            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(answer, range(50)))

            self.assertEqual(results, [str(index) for index in range(50)])
            trace_path = workdir / ".bir" / "traces.jsonl"
            raw_lines = trace_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(raw_lines), 100)

            events = load_events(trace_path)
            self.assertEqual(len(events), 100)
            self.assertEqual(sum(1 for event in events if event.type == "trace"), 50)
            self.assertEqual(sum(1 for event in events if event.type == "score"), 50)

    def test_cross_process_trace_writes_preserve_every_event(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / ".bir" / "traces.jsonl"
            worker_code = """
import sys, time
from pathlib import Path
from bir import configure, trace
ready, start, trace_path, worker, count = sys.argv[1:]
configure(trace_path=trace_path)
Path(ready).touch()
while not Path(start).exists():
    time.sleep(0.001)
for index in range(int(count)):
    with trace(f"worker-{worker}-{index}"):
        pass
"""
            workers = 4
            count = 50
            self._run_synced_subprocesses(
                workdir,
                worker_code,
                [[str(trace_path), str(worker), str(count)] for worker in range(workers)],
            )

            raw_lines = trace_path.read_text(encoding="utf-8").splitlines()
            payloads = [json.loads(line) for line in raw_lines]
            expected_names = {f"worker-{worker}-{index}" for worker in range(workers) for index in range(count)}
            self.assertEqual(len(payloads), workers * count)
            self.assertEqual({payload["name"] for payload in payloads}, expected_names)
            self.assertEqual(len({payload["id"] for payload in payloads}), workers * count)

    def _probe_trace_line_bytes(self, trace_path: Path) -> int:
        """Write one representative trace line, measure its size, then remove it.

        Rotation tests size ``max_bytes`` as a multiple of a real line so they do
        not hardcode the serialized event length. The probe uses the same
        ``trace-XXX`` name shape as the events written afterward.
        """

        with trace("trace-probe"):
            pass
        line_bytes = trace_path.stat().st_size
        trace_path.unlink()
        return line_bytes

    def test_rotation_is_disabled_by_default(self) -> None:
        with temporary_workdir() as workdir:
            for index in range(20):
                with trace(f"trace-{index:03d}"):
                    pass

            # With no max_bytes configured the file grows without rotating.
            self.assertEqual(list((workdir / ".bir").glob("traces.jsonl.*")), [])
            self.assertEqual(len(load_events()), 20)
            # The opt-in read path is a harmless no-op when nothing has rotated.
            self.assertEqual(len(load_events(include_rotated=True)), 20)

    def test_rotation_retains_every_event_across_files_in_order(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / ".bir" / "traces.jsonl"
            max_bytes = self._probe_trace_line_bytes(trace_path) * 2

            # A generous backup_count keeps every rotated file so no event is dropped.
            configure(max_bytes=max_bytes, backup_count=50)
            for index in range(12):
                with trace(f"trace-{index:03d}"):
                    pass

            # Rotation happened, and the active file is kept under the cap.
            self.assertTrue((workdir / ".bir" / "traces.jsonl.1").exists())
            self.assertLessEqual(trace_path.stat().st_size, max_bytes)

            # Reading rotated files reconstructs the full chronological sequence.
            names = [event.name for event in load_events(include_rotated=True)]
            self.assertEqual(names, [f"trace-{index:03d}" for index in range(12)])

            # The default read sees only the active file: a strict, newest suffix.
            active_names = [event.name for event in load_events()]
            self.assertLess(len(active_names), len(names))
            self.assertEqual(active_names, names[len(names) - len(active_names):])
            self.assertEqual(load_traces(include_rotated=True)[0].name, "trace-000")

    def test_rotation_respects_backup_count_and_drops_oldest(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / ".bir" / "traces.jsonl"
            max_bytes = self._probe_trace_line_bytes(trace_path) * 2

            configure(max_bytes=max_bytes, backup_count=2)
            for index in range(15):
                with trace(f"trace-{index:03d}"):
                    pass

            # At most backup_count rotated files are retained; nothing beyond it.
            rotated = list((workdir / ".bir").glob("traces.jsonl.*"))
            self.assertLessEqual(len(rotated), 2)
            self.assertFalse((workdir / ".bir" / "traces.jsonl.3").exists())

            names = [event.name for event in load_events(include_rotated=True)]
            all_names = [f"trace-{index:03d}" for index in range(15)]
            # The oldest events were dropped, leaving the newest contiguous suffix.
            self.assertLess(len(names), len(all_names))
            self.assertEqual(names, sorted(names))
            self.assertEqual(names[-1], "trace-014")
            self.assertEqual(names, all_names[len(all_names) - len(names):])

    def test_each_rotated_file_is_valid_jsonl_on_its_own(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / ".bir" / "traces.jsonl"
            max_bytes = self._probe_trace_line_bytes(trace_path) * 2

            configure(max_bytes=max_bytes, backup_count=50)
            for index in range(12):
                with trace(f"trace-{index:03d}"):
                    pass

            rotated = list((workdir / ".bir").glob("traces.jsonl.*"))
            self.assertGreaterEqual(len(rotated), 1)
            # Each rotated file parses independently and holds at least one line.
            total = len(load_events(trace_path))
            for rotated_path in rotated:
                file_events = load_events(rotated_path)
                self.assertGreaterEqual(len(file_events), 1)
                total += len(file_events)
            self.assertEqual(total, 12)

    def test_rotation_with_zero_backups_keeps_only_active_file(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / ".bir" / "traces.jsonl"
            max_bytes = self._probe_trace_line_bytes(trace_path) * 2

            configure(max_bytes=max_bytes, backup_count=0)
            for index in range(10):
                with trace(f"trace-{index:03d}"):
                    pass

            # No backups are kept; the filled file is simply dropped on rotation.
            self.assertEqual(list((workdir / ".bir").glob("traces.jsonl.*")), [])
            self.assertLessEqual(trace_path.stat().st_size, max_bytes)
            names = [event.name for event in load_events()]
            self.assertLess(len(names), 10)
            self.assertEqual(names[-1], "trace-009")

    def test_configure_rejects_invalid_rotation_settings(self) -> None:
        with self.assertRaisesRegex(TypeError, "max_bytes"):
            configure(max_bytes=cast(Any, "big"))
        with self.assertRaisesRegex(TypeError, "max_bytes"):
            configure(max_bytes=cast(Any, True))
        with self.assertRaisesRegex(ValueError, "max_bytes"):
            configure(max_bytes=-1)
        with self.assertRaisesRegex(TypeError, "backup_count"):
            configure(backup_count=cast(Any, True))
        with self.assertRaisesRegex(ValueError, "backup_count"):
            configure(backup_count=-1)

    def test_concurrent_writes_with_rotation_produce_valid_jsonl(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / ".bir" / "traces.jsonl"
            max_bytes = self._probe_trace_line_bytes(trace_path) * 3

            # backup_count comfortably exceeds the number of files so the run keeps
            # every event while still rotating under contention.
            configure(max_bytes=max_bytes, backup_count=1000)

            @observe()
            def answer(index: int) -> str:
                return str(index)

            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(answer, range(200)))

            self.assertEqual(results, [str(index) for index in range(200)])
            self.assertGreaterEqual(len(list((workdir / ".bir").glob("traces.jsonl.*"))), 1)

            events = load_events(include_rotated=True)
            self.assertEqual(len(events), 200)
            self.assertTrue(all(event.type == "trace" for event in events))

    def test_cross_process_rotation_preserves_valid_jsonl(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / ".bir" / "traces.jsonl"
            worker_code = """
import sys, time
from pathlib import Path
from bir import configure, trace
ready, start, trace_path, worker, count, max_bytes, backup_count = sys.argv[1:]
configure(trace_path=trace_path, max_bytes=int(max_bytes), backup_count=int(backup_count))
Path(ready).touch()
while not Path(start).exists():
    time.sleep(0.001)
for index in range(int(count)):
    with trace(f"rotated-{worker}-{index}"):
        pass
"""
            workers = 4
            count = 30
            max_bytes = 700
            backup_count = 200
            self._run_synced_subprocesses(
                workdir,
                worker_code,
                [
                    [str(trace_path), str(worker), str(count), str(max_bytes), str(backup_count)]
                    for worker in range(workers)
                ],
            )

            numeric_backups = [
                path
                for path in trace_path.parent.glob(f"{trace_path.name}.*")
                if path.name.removeprefix(f"{trace_path.name}.").isdigit()
            ]
            self.assertGreater(len(numeric_backups), 0)
            self.assertLessEqual(len(numeric_backups), backup_count)
            all_paths = numeric_backups + [trace_path]
            payloads = [json.loads(line) for path in all_paths for line in path.read_text(encoding="utf-8").splitlines()]
            expected_names = {f"rotated-{worker}-{index}" for worker in range(workers) for index in range(count)}
            self.assertEqual(len(payloads), workers * count)
            self.assertEqual({payload["name"] for payload in payloads}, expected_names)

    def test_cross_process_sent_id_merges_preserve_union(self) -> None:
        with temporary_workdir() as workdir:
            sent_path = workdir / ".bir" / "traces.jsonl.sent"
            worker_code = """
import sys, time
from pathlib import Path
from bir._sdk import _record_sent_ids
ready, start, sent_path, worker, batches, batch_size = sys.argv[1:]
Path(ready).touch()
while not Path(start).exists():
    time.sleep(0.001)
for batch in range(int(batches)):
    ids = [f"sent-{worker}-{batch}-{index}" for index in range(int(batch_size))]
    _record_sent_ids(Path(sent_path), ids)
"""
            workers = 4
            batches = 10
            batch_size = 5
            self._run_synced_subprocesses(
                workdir,
                worker_code,
                [[str(sent_path), str(worker), str(batches), str(batch_size)] for worker in range(workers)],
            )

            payload = json.loads(sent_path.read_text(encoding="utf-8"))
            expected_ids = {
                f"sent-{worker}-{batch}-{index}"
                for worker in range(workers)
                for batch in range(batches)
                for index in range(batch_size)
            }
            self.assertEqual(set(payload["event_ids"]), expected_ids)
            self.assertEqual(list(sent_path.parent.glob(f".{sent_path.name}.*.tmp")), [])

    def test_sent_id_temp_is_cleaned_and_lock_released_after_replace_error(self) -> None:
        with temporary_workdir() as workdir:
            sent_path = workdir / ".bir" / "traces.jsonl.sent"
            with patch("bir._sdk.Path.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    _record_sent_ids(sent_path, ["first"])

            self.assertEqual(list(sent_path.parent.glob(f".{sent_path.name}.*.tmp")), [])
            _record_sent_ids(sent_path, ["second"])
            self.assertEqual(json.loads(sent_path.read_text(encoding="utf-8")), {"event_ids": ["second"]})

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

    def test_configure_attaches_service_metadata_to_trace_root_events(self) -> None:
        with temporary_workdir() as workdir:
            configure(service_name="rag-api", environment="production")

            @observe()
            def answer() -> str:
                with span("retrieve_context"):
                    pass
                with generation("local.llm", model="demo") as gen:
                    gen.set_output("ok")
                return "ok"

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            root = next(event for event in events if event["type"] == "trace")
            self.assertEqual(
                root["metadata"],
                {"service": {"name": "rag-api", "environment": "production"}},
            )
            for event in events:
                if event["type"] != "trace":
                    self.assertNotIn("service", cast(dict[str, Any], event["metadata"]))

    def test_configure_service_metadata_supports_partial_fields(self) -> None:
        with temporary_workdir() as workdir:
            configure(environment="staging")

            @observe()
            def answer() -> str:
                return "ok"

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            self.assertEqual(events[0]["metadata"], {"service": {"environment": "staging"}})

    def test_trace_events_omit_service_metadata_by_default(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer() -> str:
                return "ok"

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            self.assertEqual(events[0]["metadata"], {})

    def test_trace_context_keeps_explicit_service_metadata(self) -> None:
        with temporary_workdir() as workdir:
            configure(service_name="rag-api")

            with trace("manual", metadata={"service": {"name": "override"}}):
                pass

            events = read_events(workdir / ".bir" / "traces.jsonl")
            self.assertEqual(events[0]["metadata"], {"service": {"name": "override"}})

    def test_trace_context_manager_records_nested_events(self) -> None:
        with temporary_workdir() as workdir:
            with trace("manual_workflow", metadata={"kind": "manual"}):
                with span("retrieve_context"):
                    with generation("local.llm", model="demo") as gen:
                        gen.set_output("ok")
                score("helpfulness", 0.9)

            events = read_events(workdir / ".bir" / "traces.jsonl")
            trace_event = next(event for event in events if event["type"] == "trace")
            span_event = next(event for event in events if event["type"] == "span")
            generation_event = next(event for event in events if event["type"] == "generation")
            score_event = next(event for event in events if event["type"] == "score")

            self.assertEqual(trace_event["name"], "manual_workflow")
            self.assertEqual(trace_event["metadata"], {"kind": "manual"})
            self.assertEqual(span_event["trace_id"], trace_event["id"])
            self.assertEqual(span_event["parent_id"], trace_event["id"])
            self.assertEqual(generation_event["parent_id"], span_event["id"])
            self.assertEqual(score_event["parent_id"], trace_event["id"])

    def test_configure_rejects_invalid_service_metadata(self) -> None:
        with self.assertRaisesRegex(ValueError, "service_name"):
            configure(service_name="")
        with self.assertRaisesRegex(ValueError, "environment"):
            configure(environment="")
        with self.assertRaisesRegex(TypeError, "service_name"):
            configure(service_name=cast(Any, 123))
        with self.assertRaisesRegex(TypeError, "environment"):
            configure(environment=cast(Any, 123))

    def test_configure_attaches_source_to_trace_root_events(self) -> None:
        with temporary_workdir() as workdir:
            configure(source="checkout-api")

            @observe()
            def answer() -> str:
                with span("retrieve_context"):
                    pass
                return "ok"

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            root = next(event for event in events if event["type"] == "trace")
            self.assertEqual(root["metadata"], {"source": "checkout-api"})
            for event in events:
                if event["type"] != "trace":
                    self.assertNotIn("source", cast(dict[str, Any], event["metadata"]))

    def test_configure_combines_source_with_service_metadata(self) -> None:
        with temporary_workdir() as workdir:
            configure(service_name="rag-api", environment="production", source="checkout-api")

            @observe()
            def answer() -> str:
                return "ok"

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            self.assertEqual(
                events[0]["metadata"],
                {"service": {"name": "rag-api", "environment": "production"}, "source": "checkout-api"},
            )

    def test_trace_events_omit_source_by_default(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer() -> str:
                return "ok"

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            self.assertNotIn("source", cast(dict[str, Any], events[0]["metadata"]))

    def test_trace_context_keeps_explicit_source(self) -> None:
        with temporary_workdir() as workdir:
            configure(source="checkout-api")

            with trace("manual", metadata={"source": "override"}):
                pass

            events = read_events(workdir / ".bir" / "traces.jsonl")
            self.assertEqual(events[0]["metadata"], {"source": "override"})

    def test_configure_rejects_invalid_source(self) -> None:
        with self.assertRaisesRegex(ValueError, "source"):
            configure(source="")
        with self.assertRaisesRegex(TypeError, "source"):
            configure(source=cast(Any, 123))

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

    def test_capture_redacts_high_signal_secret_formats(self) -> None:
        secrets = {
            "jwt_value": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature_value",
            "aws_key": "AKIA1234567890ABCDEF",
            "aws_session_key": "ASIA1234567890ABCDEF",
            "google_key": "AIzaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "google_key_ending_in_dash": "AIza" + ("A" * 34) + "-",
            "slack_value": "xoxb-fake-redaction-test",
            "github_value": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
            "github_oauth": "gho_0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        }

        for name, secret in secrets.items():
            with self.subTest(secret=name):
                self.assertEqual(_redact_secret_text(secret), "[redacted]")

        self.assertEqual(_safe_capture({"payload": list(secrets.values())}), {"payload": ["[redacted]"] * 8})

    def test_capture_does_not_redact_secret_format_near_misses(self) -> None:
        ordinary_text = [
            "JWTs have three dot-separated segments.",
            "eyJheader.payload",
            "The example domain eyJ.example.com is not a JWT.",
            "Order AKIA1234567890ABCDE has a 15-character suffix.",
            "Temporary reference ASIA1234567890ABCDE is not an access key ID.",
            "Google API keys start with AIza, but this is documentation.",
            "Slack token families include xoxb and xoxp.",
            "GitHub token prefixes include ghp_ and gho_ but need a long value.",
            "The ordinary number 12345678901234567890 is not a credential.",
        ]

        for value in ordinary_text:
            with self.subTest(value=value):
                self.assertEqual(_redact_secret_text(value), value)
        self.assertEqual(_safe_capture({"notes": ordinary_text}), {"notes": ordinary_text})

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

    def test_sample_rate_zero_drops_entire_trace_workflow(self) -> None:
        with temporary_workdir() as workdir:
            configure(sample_rate=0.0)

            @observe(capture_inputs=True, capture_outputs=True)
            def answer(question: str) -> str:
                with span("retrieve_context"):
                    with tool_call("search_docs", input={"query": question}) as tool:
                        tool.set_output(["doc-1"])
                    with retrieval("vector_search", query=question) as result:
                        result.add_document(id="doc-1")
                with generation("local.llm", model="demo") as gen:
                    gen.set_output("ok")
                score("helpfulness", 0.9)
                return "ok"

            # The function still runs and returns; only the writes are skipped.
            self.assertEqual(answer("hello"), "ok")

            self.assertFalse((workdir / ".bir" / "traces.jsonl").exists())
            self.assertEqual(load_events(), [])

    def test_sample_rate_zero_drops_span_and_score_inside_trace_context(self) -> None:
        with temporary_workdir() as workdir:
            configure(sample_rate=0.0)

            with trace("manual_workflow"):
                with span("retrieve_context"):
                    score("helpfulness", 0.9)

            self.assertFalse((workdir / ".bir" / "traces.jsonl").exists())
            self.assertEqual(load_events(), [])

    def test_sample_rate_zero_still_runs_and_reraises_user_exception(self) -> None:
        with temporary_workdir() as workdir:
            configure(sample_rate=0.0)

            @observe()
            def fail() -> None:
                with span("explode"):
                    raise ValueError("boom")

            with self.assertRaisesRegex(ValueError, "boom"):
                fail()

            self.assertFalse((workdir / ".bir" / "traces.jsonl").exists())
            self.assertEqual(load_events(), [])

    def test_sample_rate_zero_drops_async_observe(self) -> None:
        with temporary_workdir() as workdir:
            configure(sample_rate=0.0)

            @observe()
            async def answer(question: str) -> str:
                await asyncio.sleep(0)
                async with span("retrieve_context"):
                    score("helpfulness", 0.9)
                return "ok"

            self.assertEqual(asyncio.run(answer("hello")), "ok")

            self.assertFalse((workdir / ".bir" / "traces.jsonl").exists())
            self.assertEqual(load_events(), [])

    def test_sample_rate_one_records_full_workflow(self) -> None:
        with temporary_workdir() as workdir:
            configure(sample_rate=1.0)

            @observe()
            def answer(question: str) -> str:
                with span("retrieve_context"):
                    with tool_call("search_docs") as tool:
                        tool.set_output(["doc-1"])
                with generation("local.llm", model="demo") as gen:
                    gen.set_output("ok")
                score("helpfulness", 0.9)
                return "ok"

            answer("hello")

            events = read_events(workdir / ".bir" / "traces.jsonl")
            self.assertEqual(
                sorted(str(event["type"]) for event in events),
                ["generation", "score", "span", "tool_call", "trace"],
            )

    def test_sample_rate_partial_decision_is_deterministic_when_seeded(self) -> None:
        with temporary_workdir():
            configure(sample_rate=0.5)

            @observe()
            def answer(index: int) -> int:
                return index

            # random.random() >= sample_rate drops the trace, so a fixed draw
            # sequence makes the per-trace decision fully deterministic.
            with patch("bir._sdk.random.random", side_effect=[0.9, 0.1, 0.5, 0.0]):
                for index in range(4):
                    self.assertEqual(answer(index), index)

            # 0.9 and 0.5 are dropped (>= 0.5); 0.1 and 0.0 are kept.
            traces = load_traces()
            self.assertEqual(len(traces), 2)

    def test_configure_rejects_invalid_sample_rate(self) -> None:
        with self.assertRaisesRegex(ValueError, "sample_rate"):
            configure(sample_rate=-0.1)
        with self.assertRaisesRegex(ValueError, "sample_rate"):
            configure(sample_rate=1.5)
        with self.assertRaisesRegex(ValueError, "sample_rate"):
            configure(sample_rate=float("nan"))
        with self.assertRaisesRegex(TypeError, "sample_rate"):
            configure(sample_rate=cast(Any, "high"))
        with self.assertRaisesRegex(TypeError, "sample_rate"):
            configure(sample_rate=cast(Any, True))

    def test_config_from_env_with_nothing_set_matches_hardcoded_defaults(self) -> None:
        with env_vars():
            self.assertEqual(_config_from_env(), _Config())

    def test_env_trace_path_sets_default_trace_path(self) -> None:
        with env_vars(BIR_TRACE_PATH="/tmp/custom/bir-events.jsonl"):
            config = _config_from_env()
        self.assertEqual(config.trace_path, Path("/tmp/custom/bir-events.jsonl"))

    def test_env_capture_inputs_sets_default(self) -> None:
        with env_vars(BIR_CAPTURE_INPUTS="true"):
            config = _config_from_env()
        self.assertTrue(config.capture_inputs)
        self.assertFalse(config.capture_outputs)

    def test_env_capture_outputs_sets_default(self) -> None:
        with env_vars(BIR_CAPTURE_OUTPUTS="1"):
            config = _config_from_env()
        self.assertTrue(config.capture_outputs)
        self.assertFalse(config.capture_inputs)

    def test_env_sample_rate_sets_default(self) -> None:
        with env_vars(BIR_SAMPLE_RATE="0.25"):
            config = _config_from_env()
        self.assertEqual(config.sample_rate, 0.25)

    def test_env_service_name_and_environment_set_defaults(self) -> None:
        with env_vars(BIR_SERVICE_NAME="  rag-api  ", BIR_ENVIRONMENT="production"):
            config = _config_from_env()
        # The value is stripped before it is stored.
        self.assertEqual(config.service_name, "rag-api")
        self.assertEqual(config.environment, "production")

    def test_env_blank_value_is_treated_as_unset(self) -> None:
        with env_vars(BIR_SERVICE_NAME="   ", BIR_SAMPLE_RATE=""):
            config = _config_from_env()
        self.assertIsNone(config.service_name)
        self.assertEqual(config.sample_rate, 1.0)

    def test_env_capture_defaults_off_when_unset(self) -> None:
        with env_vars(BIR_SERVICE_NAME="rag-api"):
            config = _config_from_env()
        self.assertFalse(config.capture_inputs)
        self.assertFalse(config.capture_outputs)

    def test_env_invalid_bool_raises_clear_error(self) -> None:
        with env_vars(BIR_CAPTURE_INPUTS="maybe"):
            with self.assertRaisesRegex(ValueError, "BIR_CAPTURE_INPUTS"):
                _config_from_env()

    def test_env_invalid_sample_rate_raises_clear_error(self) -> None:
        with env_vars(BIR_SAMPLE_RATE="high"):
            with self.assertRaisesRegex(ValueError, "BIR_SAMPLE_RATE"):
                _config_from_env()
        with env_vars(BIR_SAMPLE_RATE="1.5"):
            with self.assertRaisesRegex(ValueError, "sample_rate"):
                _config_from_env()

    def test_parse_env_bool_accepts_truthy_and_falsy_values(self) -> None:
        for value in ("1", "true", "TRUE", "Yes", "on", " true "):
            self.assertIs(_parse_env_bool(value, "BIR_CAPTURE_INPUTS"), True)
        for value in ("0", "false", "FALSE", "No", "off", " 0 "):
            self.assertIs(_parse_env_bool(value, "BIR_CAPTURE_INPUTS"), False)

    def test_parse_env_bool_rejects_ambiguous_values(self) -> None:
        for value in ("maybe", "2", "", "tru", "t"):
            with self.assertRaisesRegex(ValueError, "BIR_CAPTURE_INPUTS"):
                _parse_env_bool(value, "BIR_CAPTURE_INPUTS")

    def test_parse_env_sample_rate_parses_and_range_checks(self) -> None:
        self.assertEqual(_parse_env_sample_rate("0.0"), 0.0)
        self.assertEqual(_parse_env_sample_rate("1"), 1.0)
        self.assertEqual(_parse_env_sample_rate(" 0.5 "), 0.5)
        with self.assertRaisesRegex(ValueError, "BIR_SAMPLE_RATE"):
            _parse_env_sample_rate("high")
        for out_of_range in ("1.5", "-0.1", "nan", "inf"):
            with self.assertRaisesRegex(ValueError, "sample_rate"):
                _parse_env_sample_rate(out_of_range)

    def test_explicit_configure_arguments_override_env_defaults(self) -> None:
        import bir._sdk as sdk

        with env_vars(BIR_SAMPLE_RATE="0.25", BIR_SERVICE_NAME="env-svc"):
            # Simulate the import-time construction that read these env vars.
            sdk._config = _config_from_env()
            configure(sample_rate=0.9)

        self.assertEqual(sdk._config.sample_rate, 0.9)  # explicit argument wins
        self.assertEqual(sdk._config.service_name, "env-svc")  # env value preserved

    def test_env_service_metadata_appears_on_trace_root(self) -> None:
        import bir._sdk as sdk

        with temporary_workdir() as workdir, env_vars(
            BIR_SERVICE_NAME="env-svc", BIR_ENVIRONMENT="production"
        ):
            sdk._config = _config_from_env()

            @observe()
            def answer() -> str:
                return "ok"

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            root = next(event for event in events if event["type"] == "trace")
            self.assertEqual(
                root["metadata"],
                {"service": {"name": "env-svc", "environment": "production"}},
            )

    def test_env_capture_flags_enable_capture(self) -> None:
        import bir._sdk as sdk

        with temporary_workdir() as workdir, env_vars(
            BIR_CAPTURE_INPUTS="true", BIR_CAPTURE_OUTPUTS="yes"
        ):
            sdk._config = _config_from_env()

            @observe()
            def answer(question: str) -> str:
                return "ok:" + question

            answer("hello")

            events = read_events(workdir / ".bir" / "traces.jsonl")
            root = next(event for event in events if event["type"] == "trace")
            self.assertEqual(root["input"], {"question": "hello"})
            self.assertEqual(root["output"], "ok:hello")

    def test_explicit_configure_capture_overrides_env_capture(self) -> None:
        import bir._sdk as sdk

        with temporary_workdir() as workdir, env_vars(
            BIR_CAPTURE_OUTPUTS="true", BIR_SERVICE_NAME="env-svc"
        ):
            sdk._config = _config_from_env()
            configure(capture_outputs=False)

            @observe()
            def answer(question: str) -> str:
                return "secret-" + question

            answer("hello")

            events = read_events(workdir / ".bir" / "traces.jsonl")
            root = next(event for event in events if event["type"] == "trace")
            # Explicit configure(capture_outputs=False) wins over the env var.
            self.assertIsNone(root["output"])
            # The env-provided service name still survives the configure() call.
            self.assertEqual(root["metadata"], {"service": {"name": "env-svc"}})


class SetMetadataTests(unittest.TestCase):
    """``set_metadata`` on the span/generation/tool_call/retrieval/trace managers."""

    def setUp(self) -> None:
        _reset_config_for_tests()

    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_generation_set_metadata_merges_with_constructor_and_prompt(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer() -> None:
                prompt_record = prompt("answer_question", version="v1")
                with generation(
                    "local.llm",
                    metadata={"provider": "openai"},
                    prompt=prompt_record,
                ) as gen:
                    gen.set_metadata({"route": "fast", "api_key": "sk-secret"})
                    gen.set_output("ok")

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            generation_event = next(event for event in events if event["type"] == "generation")
            metadata = cast(dict[str, Any], generation_event["metadata"])
            # Constructor metadata, the mid-body metadata, and the prompt block all survive.
            self.assertEqual(metadata["provider"], "openai")
            self.assertEqual(metadata["route"], "fast")
            self.assertEqual(metadata["api_key"], "[redacted]")
            self.assertEqual(cast(dict[str, Any], metadata["prompt"])["name"], "answer_question")
            self.assertNotIn("sk-secret", json.dumps(generation_event))

    def test_span_set_metadata_persists_redacted_metadata(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer() -> None:
                with span("retrieve_context") as current_span:
                    current_span.set_metadata({"cache_hit": True, "token": "raw-span-token"})

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            span_event = next(event for event in events if event["type"] == "span")
            self.assertEqual(span_event["metadata"], {"cache_hit": True, "token": "[redacted]"})

    def test_span_without_set_metadata_keeps_empty_metadata(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer() -> None:
                with span("retrieve_context"):
                    pass

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            span_event = next(event for event in events if event["type"] == "span")
            self.assertEqual(span_event["metadata"], {})

    def test_tool_call_set_metadata_merges_into_metadata(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer() -> None:
                with tool_call("search", metadata={"provider": "local"}) as call:
                    call.set_metadata({"latency_ms": 12})
                    call.set_output("done")

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            tool_event = next(event for event in events if event["type"] == "tool_call")
            self.assertEqual(tool_event["metadata"], {"provider": "local", "latency_ms": 12})

    def test_retrieval_set_metadata_composes_with_kind(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer() -> None:
                with retrieval("vector_search", query="hi") as result:
                    result.set_metadata({"index": "faiss", "documents_found": 2})
                    result.add_document(id="doc-1")

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            retrieval_event = next(event for event in events if event["name"] == "vector_search")
            self.assertEqual(
                retrieval_event["metadata"],
                {"kind": "retrieval", "index": "faiss", "documents_found": 2},
            )

    def test_trace_context_set_metadata_merges_with_service_metadata(self) -> None:
        with temporary_workdir() as workdir:
            configure(service_name="rag-api")

            with trace("manual", metadata={"request_kind": "interactive"}) as current_trace:
                current_trace.set_metadata({"route": "/answer", "secret": "raw-secret"})

            events = read_events(workdir / ".bir" / "traces.jsonl")
            root = next(event for event in events if event["type"] == "trace")
            self.assertEqual(
                root["metadata"],
                {
                    "request_kind": "interactive",
                    "route": "/answer",
                    "secret": "[redacted]",
                    "service": {"name": "rag-api"},
                },
            )

    def test_set_metadata_repeated_calls_merge_with_later_keys_winning(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer() -> None:
                with span("work") as current_span:
                    current_span.set_metadata({"attempt": 1, "stable": "keep"})
                    current_span.set_metadata({"attempt": 2})

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            span_event = next(event for event in events if event["type"] == "span")
            self.assertEqual(span_event["metadata"], {"attempt": 2, "stable": "keep"})

    def test_async_with_set_metadata_persists_metadata(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            async def answer() -> None:
                async with generation("local.llm") as gen:
                    await asyncio.sleep(0)
                    gen.set_metadata({"streamed": True})
                    gen.set_output("ok")

            asyncio.run(answer())

            events = read_events(workdir / ".bir" / "traces.jsonl")
            generation_event = next(event for event in events if event["type"] == "generation")
            self.assertEqual(generation_event["metadata"], {"streamed": True})

    def test_async_with_span_set_metadata_persists_metadata(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            async def answer() -> None:
                async with span("retrieve_context") as current_span:
                    await asyncio.sleep(0)
                    current_span.set_metadata({"cache_hit": True})

            asyncio.run(answer())

            events = read_events(workdir / ".bir" / "traces.jsonl")
            span_event = next(event for event in events if event["type"] == "span")
            self.assertEqual(span_event["metadata"], {"cache_hit": True})

    def test_set_metadata_rejects_non_mapping_argument(self) -> None:
        with temporary_workdir():
            managers = (
                trace("manual"),
                span("work"),
                generation("local.llm"),
                tool_call("search"),
                retrieval("vector_search", query="hi"),
            )
            for manager in managers:
                with self.subTest(manager=type(manager).__name__):
                    with self.assertRaisesRegex(TypeError, "set_metadata"):
                        manager.set_metadata(cast(Any, ["not", "a", "mapping"]))


class CurrentIdAccessorTests(unittest.TestCase):
    """``get_current_trace_id``/``get_current_span_id`` read-only accessors."""

    def setUp(self) -> None:
        _reset_config_for_tests()

    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_returns_none_outside_any_trace(self) -> None:
        self.assertIsNone(get_current_trace_id())
        self.assertIsNone(get_current_span_id())

    def test_accessors_are_public_and_no_setter_exposed(self) -> None:
        self.assertIn("get_current_trace_id", bir.__all__)
        self.assertIn("get_current_span_id", bir.__all__)
        self.assertIs(bir.get_current_trace_id, get_current_trace_id)
        self.assertIs(bir.get_current_span_id, get_current_span_id)
        # Only read accessors are public: the ContextVars and any setter stay private.
        self.assertNotIn("_current_trace_id", bir.__all__)
        self.assertNotIn("_current_parent_id", bir.__all__)
        self.assertFalse(hasattr(bir, "_current_trace_id"))
        self.assertFalse(hasattr(bir, "_current_parent_id"))
        self.assertFalse(hasattr(bir, "set_current_trace_id"))
        self.assertFalse(hasattr(bir, "set_current_span_id"))

    def test_returns_root_ids_at_trace_level(self) -> None:
        with temporary_workdir() as workdir:
            captured: dict[str, str | None] = {}

            @observe()
            def answer() -> None:
                captured["trace_id"] = get_current_trace_id()
                captured["span_id"] = get_current_span_id()

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            root = events[0]
            # At the trace root with no open child, both accessors return the root id.
            self.assertEqual(captured["trace_id"], root["id"])
            self.assertEqual(captured["span_id"], root["id"])

    def test_span_accessor_matches_child_event_ids_in_jsonl(self) -> None:
        with temporary_workdir() as workdir:
            captured: dict[str, str | None] = {}

            @observe()
            def answer() -> None:
                with span("retrieve"):
                    captured["trace_id"] = get_current_trace_id()
                    captured["span_id"] = get_current_span_id()
                    # A child event created here takes the active ids as its
                    # trace_id/parent_id, so the accessors must equal what is written.
                    score("relevance", 1.0)

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            root = next(event for event in events if event["type"] == "trace")
            span_event = next(event for event in events if event["type"] == "span")
            score_event = next(event for event in events if event["type"] == "score")
            # Inside the span the trace accessor is the root and the span accessor
            # is the innermost open node.
            self.assertEqual(captured["trace_id"], root["id"])
            self.assertEqual(captured["span_id"], span_event["id"])
            # The score recorded at that point carries exactly the captured ids.
            self.assertEqual(score_event["trace_id"], captured["trace_id"])
            self.assertEqual(score_event["parent_id"], captured["span_id"])

    def test_innermost_node_id_tracks_nested_spans_and_generations(self) -> None:
        with temporary_workdir() as workdir:
            captured: dict[str, str | None] = {}

            @observe()
            def answer() -> None:
                with span("outer"):
                    captured["outer_span"] = get_current_span_id()
                    with generation("inner_gen"):
                        captured["inner_gen"] = get_current_span_id()
                        captured["trace_at_depth"] = get_current_trace_id()
                    # After the generation exits, the innermost id reverts to the span.
                    captured["outer_span_again"] = get_current_span_id()

            answer()

            events = read_events(workdir / ".bir" / "traces.jsonl")
            root = next(event for event in events if event["type"] == "trace")
            outer_span = next(event for event in events if event["type"] == "span")
            inner_gen = next(event for event in events if event["type"] == "generation")
            self.assertEqual(captured["outer_span"], outer_span["id"])
            self.assertEqual(captured["inner_gen"], inner_gen["id"])
            self.assertEqual(captured["outer_span_again"], outer_span["id"])
            self.assertEqual(captured["trace_at_depth"], root["id"])
            # The generation nested under the span records the span as its parent.
            self.assertEqual(inner_gen["parent_id"], outer_span["id"])

    def test_trace_context_manager_exposes_ids(self) -> None:
        with temporary_workdir():
            with trace("manual") as manual_trace:
                self.assertEqual(get_current_trace_id(), manual_trace.id)
                self.assertEqual(get_current_span_id(), manual_trace.id)
            # The ids clear once the trace context exits.
            self.assertIsNone(get_current_trace_id())
            self.assertIsNone(get_current_span_id())

    def test_async_observe_returns_active_ids(self) -> None:
        with temporary_workdir() as workdir:
            captured: dict[str, str | None] = {}

            @observe()
            async def answer() -> None:
                await asyncio.sleep(0)
                captured["trace_id"] = get_current_trace_id()
                captured["span_id"] = get_current_span_id()

            asyncio.run(answer())

            events = read_events(workdir / ".bir" / "traces.jsonl")
            root = events[0]
            self.assertEqual(captured["trace_id"], root["id"])
            self.assertEqual(captured["span_id"], root["id"])

    def test_concurrent_async_tasks_observe_isolated_ids(self) -> None:
        with temporary_workdir() as workdir:
            seen_trace: dict[str, str | None] = {}
            seen_span: dict[str, str | None] = {}

            @observe()
            async def task(value: str) -> str:
                # Yield control so the gathered tasks interleave; leaked contextvars
                # would make both read the same id.
                await asyncio.sleep(0)
                seen_trace[value] = get_current_trace_id()
                seen_span[value] = get_current_span_id()
                return value

            async def main() -> None:
                await asyncio.gather(task("a"), task("b"))

            asyncio.run(main())

            events = read_events(workdir / ".bir" / "traces.jsonl")
            written_trace_ids = {event["trace_id"] for event in events}
            # Each task saw its own distinct trace id, together covering both roots.
            self.assertNotEqual(seen_trace["a"], seen_trace["b"])
            self.assertEqual({seen_trace["a"], seen_trace["b"]}, written_trace_ids)
            # At each task's trace root the span id equals its own trace id.
            self.assertEqual(seen_span["a"], seen_trace["a"])
            self.assertEqual(seen_span["b"], seen_trace["b"])

    def test_concurrent_threads_observe_isolated_ids(self) -> None:
        with temporary_workdir() as workdir:
            seen: dict[str, str | None] = {}
            # Hold both threads inside their own trace at once so a leak across
            # threads would be observable.
            barrier = threading.Barrier(2)

            @observe()
            def work(value: str) -> str:
                barrier.wait()
                seen[value] = get_current_trace_id()
                return value

            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(work, ["a", "b"]))

            events = read_events(workdir / ".bir" / "traces.jsonl")
            written_trace_ids = {event["trace_id"] for event in events}
            self.assertNotEqual(seen["a"], seen["b"])
            self.assertEqual({seen["a"], seen["b"]}, written_trace_ids)

    def test_ids_reset_to_none_after_trace_exits(self) -> None:
        with temporary_workdir():

            @observe()
            def answer() -> None:
                pass

            answer()

            self.assertIsNone(get_current_trace_id())
            self.assertIsNone(get_current_span_id())


if __name__ == "__main__":
    unittest.main()
