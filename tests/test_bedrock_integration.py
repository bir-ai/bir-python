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
from bir.integrations.bedrock import (
    trace_converse,
    trace_converse_async,
    trace_converse_stream,
    trace_converse_stream_async,
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


class FakeUsage:
    """Attribute-style Converse ``usage`` block (exercises the getattr path)."""

    def __init__(
        self,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> None:
        self.inputTokens = input_tokens
        self.outputTokens = output_tokens
        self.totalTokens = total_tokens


class FakeConverseResponse:
    """Object-style Converse response exposing ``usage`` and a ``model_dump`` payload."""

    def __init__(
        self,
        *,
        usage: object | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        self.usage = usage
        self._payload = payload or {}

    def model_dump(self) -> dict[str, object]:
        return dict(self._payload)


def _converse_dict(*, usage: dict[str, object] | None) -> dict[str, object]:
    """A realistic boto3 Converse response: a plain dict with a nested usage block."""

    response: dict[str, object] = {
        "output": {"message": {"role": "assistant", "content": [{"text": "Bir traces locally."}]}},
        "stopReason": "end_turn",
        "metrics": {"latencyMs": 42},
    }
    if usage is not None:
        response["usage"] = usage
    return response


def _content_block_delta(text: str) -> dict[str, object]:
    # A Converse stream ``contentBlockDelta`` event: text at delta.text.
    return {"contentBlockDelta": {"delta": {"text": text}, "contentBlockIndex": 0}}


def _message_stop(stop_reason: str) -> dict[str, object]:
    # The ``messageStop`` event marking the end of the assistant turn.
    return {"messageStop": {"stopReason": stop_reason}}


def _stream_metadata(usage: object) -> dict[str, object]:
    # The terminal ``metadata`` event carrying token usage at metadata.usage.
    return {"metadata": {"usage": usage, "metrics": {"latencyMs": 42}}}


def _converse_stream_response(events: object) -> dict[str, object]:
    # boto3's ``converse_stream`` returns a dict whose ``stream`` member is an
    # EventStream iterable of typed events alongside response metadata.
    return {"stream": events, "ResponseMetadata": {"HTTPStatusCode": 200}}


class FakeAsyncStream:
    """An async iterator over pre-built Converse events, like an aioboto3 stream.

    ``__aiter__`` makes the object an async stream that the wrapper detects, and
    ``__anext__`` replays the events. With ``error`` set it raises that exception
    after the events are exhausted, modeling a mid-stream failure.
    """

    def __init__(self, events: Sequence[object], *, error: BaseException | None = None) -> None:
        self._events = list(events)
        self._index = 0
        self._error = error
        self.aclosed = False

    def __aiter__(self) -> FakeAsyncStream:
        return self

    async def __anext__(self) -> object:
        if self._index < len(self._events):
            event = self._events[self._index]
            self._index += 1
            return event
        if self._error is not None:
            raise self._error
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.aclosed = True


class BedrockIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_records_generation_with_model_from_request_and_usage_from_response(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            received: dict[str, object] = {}
            created: list[dict[str, object]] = []

            def fake_converse(**kwargs: object) -> dict[str, object]:
                received.update(kwargs)
                response = _converse_dict(usage={"inputTokens": 12, "outputTokens": 6, "totalTokens": 18})
                created.append(response)
                return response

            with trace("chat"):
                response = trace_converse(
                    fake_converse,
                    modelId="anthropic.claude-3-5-sonnet-20240620-v1:0",
                    messages=[{"role": "user", "content": [{"text": "What is Bir?"}]}],
                )
                # The wrapper forwards the request and returns the unchanged response.
                self.assertIs(response, created[0])

            self.assertEqual(received["modelId"], "anthropic.claude-3-5-sonnet-20240620-v1:0")
            self.assertEqual(
                received["messages"],
                [{"role": "user", "content": [{"text": "What is Bir?"}]}],
            )

            traces = load_traces()
            self.assertEqual(len(traces), 1)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "bedrock.converse")
            self.assertEqual(generation_event.status, "success")
            # Converse responses omit a model, so it comes from the request keyword.
            self.assertEqual(generation_event.model, "anthropic.claude-3-5-sonnet-20240620-v1:0")
            self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18})
            self.assertEqual(generation_event.metadata["integration"], "bedrock")
            self.assertEqual(generation_event.input["modelId"], "anthropic.claude-3-5-sonnet-20240620-v1:0")
            self.assertEqual(
                generation_event.output["output"]["message"]["content"][0]["text"],
                "Bir traces locally.",
            )

    def test_records_usage_from_object_response_and_computes_total(self) -> None:
        with temporary_workdir():
            # An attribute-style response without ``totalTokens``: the SDK derives
            # the total from the two halves.
            def fake_converse(**kwargs: object) -> FakeConverseResponse:
                return FakeConverseResponse(usage=FakeUsage(input_tokens=7, output_tokens=3))

            with trace("chat"):
                trace_converse(fake_converse, modelId="amazon.titan-text-premier-v1:0", messages=[])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.model, "amazon.titan-text-premier-v1:0")
            self.assertEqual(generation_event.usage, {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})

    def test_uses_request_model_and_omits_usage_when_response_is_sparse(self) -> None:
        with temporary_workdir():
            def fake_converse(**kwargs: object) -> dict[str, object]:
                return _converse_dict(usage=None)

            with trace("chat"):
                trace_converse(fake_converse, modelId="amazon.titan-text-premier-v1:0", messages=[])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.model, "amazon.titan-text-premier-v1:0")
            self.assertIsNone(generation_event.usage)

    def test_forwards_bedrock_arguments_and_applies_bir_options(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True)
            received: dict[str, object] = {}

            def fake_converse(**kwargs: object) -> dict[str, object]:
                received.update(kwargs)
                return _converse_dict(usage=None)

            with trace("chat"):
                trace_converse(
                    fake_converse,
                    modelId="amazon.titan-text-premier-v1:0",
                    messages=[],
                    inferenceConfig={"temperature": 0.0},
                    bir_name="chat.turn",
                    bir_metadata={"feature": "qa"},
                )

            # Bedrock's own ``inferenceConfig`` kwarg is forwarded to ``converse``, not consumed.
            self.assertEqual(received["inferenceConfig"], {"temperature": 0.0})

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "chat.turn")
            self.assertEqual(generation_event.metadata["integration"], "bedrock")
            self.assertEqual(generation_event.metadata["feature"], "qa")
            self.assertEqual(generation_event.input["inferenceConfig"], {"temperature": 0.0})

    def test_records_error_and_redacts_secret_when_converse_raises(self) -> None:
        with temporary_workdir():
            def fake_converse(**kwargs: object) -> dict[str, object]:
                raise RuntimeError("request failed api_key=sk-secret123")

            with trace("chat"):
                with self.assertRaises(RuntimeError):
                    trace_converse(fake_converse, modelId="amazon.titan-text-premier-v1:0", messages=[])

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "request failed api_key=[redacted]")

    def test_records_streamed_generation_after_events_are_consumed(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            events = [
                _content_block_delta("Bir "),
                _content_block_delta("streams"),
                _message_stop("end_turn"),
                _stream_metadata({"inputTokens": 12, "outputTokens": 6, "totalTokens": 18}),
            ]
            received: dict[str, object] = {}

            def fake_converse_stream(**kwargs: object) -> dict[str, object]:
                received.update(kwargs)
                return _converse_stream_response(events)

            consumed: list[object] = []
            with trace("chat"):
                stream = trace_converse_stream(
                    fake_converse_stream,
                    modelId="anthropic.claude-3-5-sonnet-20240620-v1:0",
                    messages=[{"role": "user", "content": [{"text": "Stream it"}]}],
                )
                consumed = list(stream)

            # The stream's events are yielded unchanged and in order.
            self.assertEqual(consumed, events)
            self.assertEqual(received["modelId"], "anthropic.claude-3-5-sonnet-20240620-v1:0")

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "bedrock.converse_stream")
            self.assertEqual(generation_event.status, "success")
            # Converse stream events carry no model, so the request modelId stands.
            self.assertEqual(generation_event.model, "anthropic.claude-3-5-sonnet-20240620-v1:0")
            self.assertEqual(generation_event.output, "Bir streams")
            self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18})
            self.assertEqual(generation_event.metadata["integration"], "bedrock")
            self.assertEqual(generation_event.metadata["stop_reason"], "end_turn")

    def test_streamed_usage_from_object_event_derives_total(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)
            # The terminal metadata event carries an attribute-style usage block
            # without ``totalTokens``; the SDK derives the total from the halves.
            events = [
                _content_block_delta("Bir"),
                _stream_metadata(FakeUsage(input_tokens=5, output_tokens=2)),
            ]

            def fake_converse_stream(**kwargs: object) -> dict[str, object]:
                return _converse_stream_response(events)

            with trace("chat"):
                stream = trace_converse_stream(
                    fake_converse_stream, modelId="amazon.titan-text-premier-v1:0", messages=[]
                )
                self.assertEqual(list(stream), events)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.output, "Bir")
            self.assertEqual(generation_event.usage, {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7})

    def test_stream_falls_back_when_response_is_not_a_stream(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)

            def fake_converse_stream(**kwargs: object) -> dict[str, object]:
                # A stub that ignored the streaming API and returned one response.
                return _converse_dict(usage={"inputTokens": 7, "outputTokens": 3, "totalTokens": 10})

            consumed: list[object] = []
            with trace("chat"):
                stream = trace_converse_stream(
                    fake_converse_stream, modelId="amazon.titan-text-premier-v1:0", messages=[]
                )
                consumed = list(stream)

            # The non-streamed response is recorded in one piece; nothing is yielded.
            self.assertEqual(consumed, [])
            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "amazon.titan-text-premier-v1:0")
            self.assertEqual(generation_event.usage, {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})
            self.assertEqual(
                generation_event.output["output"]["message"]["content"][0]["text"],
                "Bir traces locally.",
            )

    def test_records_stream_error_and_redacts_secret(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)

            def failing_stream() -> Iterator[dict[str, object]]:
                yield _content_block_delta("partial api_key=sk-secret123 ")
                raise RuntimeError("stream failed api_key=sk-secret123")

            def fake_converse_stream(**kwargs: object) -> dict[str, object]:
                return _converse_stream_response(failing_stream())

            with trace("chat"):
                stream = trace_converse_stream(
                    fake_converse_stream, modelId="amazon.titan-text-premier-v1:0", messages=[]
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

            def fake_converse(**kwargs: object) -> dict[str, object]:
                calls.append(kwargs)
                return _converse_dict(usage=None)

            with self.assertRaises(RuntimeError):
                trace_converse(fake_converse, modelId="amazon.titan-text-premier-v1:0", messages=[])

            # The generation guard fires before the request is ever issued.
            self.assertEqual(calls, [])
            self.assertEqual(load_events(), [])


class BedrockAsyncIntegrationTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_awaits_converse_and_records_model_and_usage(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            received: dict[str, object] = {}
            created: list[dict[str, object]] = []

            async def fake_converse(**kwargs: object) -> dict[str, object]:
                received.update(kwargs)
                response = _converse_dict(usage={"inputTokens": 12, "outputTokens": 6, "totalTokens": 18})
                created.append(response)
                return response

            async def driver() -> object:
                async with trace("chat"):
                    return await trace_converse_async(
                        fake_converse,
                        modelId="anthropic.claude-3-5-sonnet-20240620-v1:0",
                        messages=[{"role": "user", "content": [{"text": "What is Bir?"}]}],
                    )

            response = asyncio.run(driver())
            # The wrapper forwards the request and returns the unchanged response.
            self.assertIs(response, created[0])

            self.assertEqual(received["modelId"], "anthropic.claude-3-5-sonnet-20240620-v1:0")
            self.assertEqual(
                received["messages"],
                [{"role": "user", "content": [{"text": "What is Bir?"}]}],
            )

            traces = load_traces()
            self.assertEqual(len(traces), 1)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "bedrock.converse")
            self.assertEqual(generation_event.status, "success")
            # Converse responses omit a model, so it comes from the request keyword.
            self.assertEqual(generation_event.model, "anthropic.claude-3-5-sonnet-20240620-v1:0")
            self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18})
            self.assertEqual(generation_event.metadata["integration"], "bedrock")
            self.assertEqual(generation_event.input["modelId"], "anthropic.claude-3-5-sonnet-20240620-v1:0")
            self.assertEqual(
                generation_event.output["output"]["message"]["content"][0]["text"],
                "Bir traces locally.",
            )

    def test_uses_request_model_and_omits_usage_when_response_is_sparse(self) -> None:
        with temporary_workdir():
            async def fake_converse(**kwargs: object) -> dict[str, object]:
                return _converse_dict(usage=None)

            async def driver() -> None:
                async with trace("chat"):
                    await trace_converse_async(
                        fake_converse, modelId="amazon.titan-text-premier-v1:0", messages=[]
                    )

            asyncio.run(driver())

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.model, "amazon.titan-text-premier-v1:0")
            self.assertIsNone(generation_event.usage)

    def test_forwards_bedrock_arguments_and_consumes_bir_options(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True)
            received: dict[str, object] = {}

            async def fake_converse(**kwargs: object) -> dict[str, object]:
                received.update(kwargs)
                return _converse_dict(usage=None)

            async def driver() -> None:
                async with trace("chat"):
                    await trace_converse_async(
                        fake_converse,
                        modelId="amazon.titan-text-premier-v1:0",
                        messages=[],
                        inferenceConfig={"temperature": 0.0},
                        bir_name="chat.turn",
                        bir_metadata={"feature": "qa"},
                    )

            asyncio.run(driver())

            # Bedrock's own ``inferenceConfig`` kwarg is forwarded; the bir_ options are consumed.
            self.assertEqual(received["inferenceConfig"], {"temperature": 0.0})
            self.assertNotIn("bir_name", received)
            self.assertNotIn("bir_metadata", received)

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "chat.turn")
            self.assertEqual(generation_event.metadata["integration"], "bedrock")
            self.assertEqual(generation_event.metadata["feature"], "qa")
            self.assertEqual(generation_event.input["inferenceConfig"], {"temperature": 0.0})

    def test_does_not_capture_input_or_output_by_default(self) -> None:
        with temporary_workdir():
            # Capture is opt-in: without configure() the request and response are
            # recorded as model + usage only, never the payloads.
            async def fake_converse(**kwargs: object) -> dict[str, object]:
                return _converse_dict(usage={"inputTokens": 7, "outputTokens": 3, "totalTokens": 10})

            async def driver() -> None:
                async with trace("chat"):
                    await trace_converse_async(
                        fake_converse, modelId="amazon.titan-text-premier-v1:0", messages=[]
                    )

            asyncio.run(driver())

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertIsNone(generation_event.input)
            self.assertIsNone(generation_event.output)
            self.assertEqual(generation_event.usage, {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})

    def test_records_error_and_redacts_secret_when_converse_raises(self) -> None:
        with temporary_workdir():
            async def fake_converse(**kwargs: object) -> dict[str, object]:
                raise RuntimeError("request failed api_key=sk-secret123")

            async def driver() -> None:
                async with trace("chat"):
                    await trace_converse_async(
                        fake_converse, modelId="amazon.titan-text-premier-v1:0", messages=[]
                    )

            with self.assertRaises(RuntimeError):
                asyncio.run(driver())

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "request failed api_key=[redacted]")

    def test_records_streamed_generation_after_events_are_consumed(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            events = [
                _content_block_delta("Bir "),
                _content_block_delta("streams"),
                _message_stop("end_turn"),
                _stream_metadata({"inputTokens": 12, "outputTokens": 6, "totalTokens": 18}),
            ]
            received: dict[str, object] = {}

            async def fake_converse_stream(**kwargs: object) -> dict[str, object]:
                received.update(kwargs)
                return _converse_stream_response(FakeAsyncStream(events))

            async def driver() -> list[object]:
                collected: list[object] = []
                async with trace("chat"):
                    stream = await trace_converse_stream_async(
                        fake_converse_stream,
                        modelId="anthropic.claude-3-5-sonnet-20240620-v1:0",
                        messages=[{"role": "user", "content": [{"text": "Stream it"}]}],
                    )
                    collected = [event async for event in stream]
                return collected

            consumed = asyncio.run(driver())

            # The stream's events are yielded unchanged and in order.
            self.assertEqual(consumed, events)
            self.assertEqual(received["modelId"], "anthropic.claude-3-5-sonnet-20240620-v1:0")

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.name, "bedrock.converse_stream")
            self.assertEqual(generation_event.status, "success")
            # Converse stream events carry no model, so the request modelId stands.
            self.assertEqual(generation_event.model, "anthropic.claude-3-5-sonnet-20240620-v1:0")
            self.assertEqual(generation_event.output, "Bir streams")
            self.assertEqual(generation_event.usage, {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18})
            self.assertEqual(generation_event.metadata["integration"], "bedrock")
            self.assertEqual(generation_event.metadata["stop_reason"], "end_turn")

    def test_stream_falls_back_when_response_is_not_a_stream(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)

            async def fake_converse_stream(**kwargs: object) -> dict[str, object]:
                # A stub that ignored the streaming API and returned one response.
                return _converse_dict(usage={"inputTokens": 7, "outputTokens": 3, "totalTokens": 10})

            async def driver() -> list[object]:
                collected: list[object] = []
                async with trace("chat"):
                    stream = await trace_converse_stream_async(
                        fake_converse_stream, modelId="amazon.titan-text-premier-v1:0", messages=[]
                    )
                    collected = [event async for event in stream]
                return collected

            consumed = asyncio.run(driver())

            # The non-streamed response is recorded in one piece; nothing is yielded.
            self.assertEqual(consumed, [])
            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "success")
            self.assertEqual(generation_event.model, "amazon.titan-text-premier-v1:0")
            self.assertEqual(generation_event.usage, {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})
            self.assertEqual(
                generation_event.output["output"]["message"]["content"][0]["text"],
                "Bir traces locally.",
            )

    def test_records_partial_output_when_stream_closed_early(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)

            async def fake_converse_stream(**kwargs: object) -> dict[str, object]:
                return _converse_stream_response(
                    FakeAsyncStream([_content_block_delta("Bir "), _content_block_delta("streams")])
                )

            async def driver() -> None:
                async with trace("chat"):
                    stream = await trace_converse_stream_async(
                        fake_converse_stream, modelId="amazon.titan-text-premier-v1:0", messages=[]
                    )
                    iterator = stream.__aiter__()
                    await iterator.__anext__()
                    # Closing the wrapper finalizes the generation from the partial output.
                    await stream.aclose()

            asyncio.run(driver())

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.output, "Bir ")

    def test_records_stream_error_and_redacts_secret(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)

            async def fake_converse_stream(**kwargs: object) -> dict[str, object]:
                return _converse_stream_response(
                    FakeAsyncStream(
                        [_content_block_delta("partial api_key=sk-secret123 ")],
                        error=RuntimeError("stream failed api_key=sk-secret123"),
                    )
                )

            async def driver() -> None:
                async with trace("chat"):
                    stream = await trace_converse_stream_async(
                        fake_converse_stream, modelId="amazon.titan-text-premier-v1:0", messages=[]
                    )
                    async for _event in stream:
                        pass

            with self.assertRaises(RuntimeError):
                asyncio.run(driver())

            generation_event = next(event for event in load_events() if event.type == "generation")
            self.assertEqual(generation_event.status, "error")
            self.assertEqual(generation_event.error, "stream failed api_key=[redacted]")
            # Partial output collected before the failure is recorded and redacted.
            self.assertEqual(generation_event.output, "partial api_key=[redacted] ")

    def test_requires_active_trace(self) -> None:
        with temporary_workdir():
            calls: list[dict[str, object]] = []

            async def fake_converse(**kwargs: object) -> dict[str, object]:
                calls.append(kwargs)
                return _converse_dict(usage=None)

            async def driver() -> None:
                await trace_converse_async(
                    fake_converse, modelId="amazon.titan-text-premier-v1:0", messages=[]
                )

            with self.assertRaises(RuntimeError):
                asyncio.run(driver())

            # The generation guard fires before the request is ever issued.
            self.assertEqual(calls, [])
            self.assertEqual(load_events(), [])


if __name__ == "__main__":
    unittest.main()
