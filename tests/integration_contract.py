"""Shared conformance contract for Bir's provider call-wrapper integrations.

Every ``trace_*`` wrapper independently implements the same lifecycle: forward
the caller's arguments untouched, strip its own ``bir_`` options, record exactly
one ``generation`` event, and finalize that event when the provider returns,
raises, streams, is closed early, or is cancelled. The per-provider test modules
verify that lifecycle with provider-shaped fakes, but each one chose its own
subset of guarantees, so the assertions drifted: early close and whole-response
fallback are asserted for some wrappers and not others, and cancellation was
asserted nowhere.

This module holds the shared half of that verification. A wrapper family
declares its capabilities as a :class:`WrapperContract` and the declaration is
turned into a test case by :func:`build_contract_test_case`; the declarations
live in ``test_integration_contract.py``. Only lifecycle guarantees that every
wrapper must honor belong here. Provider-specific parsing — which usage keys a
provider uses, which stream event carries the model, how a terminal event
reports totals — stays in the per-provider test module beside the integration.

The fakes are deliberately dict-shaped. The wrappers read responses through
``bir.integrations._common._value``, which accepts mappings and attribute
objects alike, so mappings exercise the same readers while keeping each
declaration short enough to read as documentation of the provider's shape.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import tempfile
import unittest
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from bir import TraceEvent, configure, load_events, trace
from bir._sdk import _reset_config_for_tests

# A provider message carrying a credential. Wrappers must never widen an error
# or a captured payload past the SDK's redaction, so every failure case in the
# matrix raises with this text and expects the redacted form back.
SECRET_TEXT = "api_key=sk-contract-secret"
REDACTED_TEXT = "api_key=[redacted]"

# The keyword-only options every wrapper owns besides ``bir_name``; each must
# default to ``None`` so an unset option never overrides the configuration.
# ``bir_name`` is checked separately against the family's declared default.
SHARED_BIR_OPTIONS = ("bir_metadata", "bir_capture_input", "bir_capture_output")


@contextmanager
def temporary_workdir() -> Iterator[Path]:
    """Run a case in a throwaway directory so it writes its own trace store."""

    previous = Path.cwd()
    with tempfile.TemporaryDirectory() as directory:
        workdir = Path(directory)
        os.chdir(workdir)
        try:
            yield workdir
        finally:
            os.chdir(previous)


class FakeStream:
    """A sync iterator over pre-built chunks, like a provider ``Stream``.

    ``error`` raises after the chunks are exhausted, modeling a mid-stream
    failure. ``closed`` records whether the consumer closed this object, which
    the wrappers deliberately do not do — they finalize their own generation and
    leave the provider stream to its owner.
    """

    def __init__(self, chunks: Sequence[Any], *, error: BaseException | None = None) -> None:
        self._chunks = list(chunks)
        self._index = 0
        self._error = error
        self.closed = False

    def __iter__(self) -> FakeStream:
        return self

    def __next__(self) -> Any:
        if self._index < len(self._chunks):
            chunk = self._chunks[self._index]
            self._index += 1
            return chunk
        if self._error is not None:
            raise self._error
        raise StopIteration

    def close(self) -> None:
        self.closed = True


class FakeAsyncStream:
    """The async counterpart of :class:`FakeStream`.

    ``__aiter__`` is what the async wrappers detect as a real stream. With
    ``block`` set, the iterator parks forever once the chunks run out and sets
    :attr:`blocked` first, so a test can cancel the consuming task at a known
    point mid-stream.
    """

    def __init__(
        self,
        chunks: Sequence[Any],
        *,
        error: BaseException | None = None,
        block: bool = False,
    ) -> None:
        self._chunks = list(chunks)
        self._index = 0
        self._error = error
        self._block = block
        # Never set: the parked ``__anext__`` is released only by cancellation.
        self._released = asyncio.Event()
        self.blocked = asyncio.Event()
        self.closed = False

    def __aiter__(self) -> FakeAsyncStream:
        return self

    async def __anext__(self) -> Any:
        if self._index < len(self._chunks):
            chunk = self._chunks[self._index]
            self._index += 1
            return chunk
        if self._block:
            self.blocked.set()
            await self._released.wait()
        if self._error is not None:
            raise self._error
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True


@dataclass(frozen=True)
class ResponseShape:
    """A whole (non-streamed) provider response and what Bir must read from it."""

    build: Callable[[], Any]
    model: str | None
    usage: Mapping[str, int | float] | None


@dataclass(frozen=True)
class UnaryCapability:
    """The single-response call path of a wrapper family."""

    sync_wrapper: Callable[..., Any]
    async_wrapper: Callable[..., Any]
    response: ResponseShape


def _stream_itself(stream: Any) -> Any:
    """Return the chunk iterator unchanged, the shape most providers stream."""

    return stream


@dataclass(frozen=True)
class StreamCapability:
    """The streaming call path of a wrapper family.

    ``enable`` holds the request keywords that select streaming (``stream=True``
    for wrappers that share one entry point) and is empty for families with
    dedicated streaming wrappers. ``text_chunk`` builds a chunk carrying
    incremental output text in the provider's shape, and ``model`` is the model
    Bir must record for a stream of those chunks alone. ``whole_response``
    declares what the wrapper falls back to when the provider answers a
    streaming request with a single response object, and ``envelope`` wraps the
    chunk iterator in whatever the streaming call actually returns — Bedrock's
    ``converse_stream`` answers with a response carrying the event stream on a
    ``stream`` member rather than returning the iterator itself.
    """

    sync_wrapper: Callable[..., Any]
    async_wrapper: Callable[..., Any]
    text_chunk: Callable[[str], Any]
    model: str | None
    whole_response: ResponseShape
    enable: Mapping[str, Any] = field(default_factory=dict)
    envelope: Callable[[Any], Any] = _stream_itself


@dataclass(frozen=True)
class WrapperContract:
    """One wrapper family's declared conformance capabilities.

    A family is a sync/async wrapper pair sharing a default event name, so a
    module exposing several entry points (``ollama.chat`` and
    ``ollama.generate``) declares one contract per pair.
    """

    id: str
    module: str
    integration: str
    default_name: str
    provider_roots: tuple[str, ...]
    request: Mapping[str, Any]
    metadata: Mapping[str, Any]
    unary: UnaryCapability | None = None
    streaming: StreamCapability | None = None
    # Wrapper-owned keyword options beyond the shared four, such as vertexai's
    # ``bir_model``. Declared so the signature case can tell an intentional
    # option from an ungoverned keyword that would shadow a provider argument.
    extra_options: tuple[str, ...] = ()

    def wrappers(self) -> tuple[Callable[..., Any], ...]:
        """Return every public wrapper this contract governs, without repeats.

        Families that select streaming with a request keyword reuse one entry
        point for both paths, so the same function is declared twice.
        """

        declared: list[Callable[..., Any]] = []
        if self.unary is not None:
            declared.extend((self.unary.sync_wrapper, self.unary.async_wrapper))
        if self.streaming is not None:
            declared.extend((self.streaming.sync_wrapper, self.streaming.async_wrapper))

        unique: list[Callable[..., Any]] = []
        for wrapper in declared:
            if not any(wrapper is seen for seen in unique):
                unique.append(wrapper)
        return tuple(unique)


class ContractTestCase(unittest.TestCase):
    """Assertions shared by the generated per-contract cases."""

    contract: ClassVar[WrapperContract]

    def tearDown(self) -> None:
        _reset_config_for_tests()

    def generation_event(self) -> TraceEvent:
        """Return the single generation the wrapper must have recorded."""

        events = [event for event in load_events() if event.type == "generation"]
        self.assertEqual(len(events), 1, f"expected exactly one generation event, recorded {len(events)}")
        return events[0]

    def assert_request_recorded(self, event: TraceEvent, **extra: Any) -> None:
        """Assert the contract's default name and metadata, plus named fields."""

        contract = self.contract
        self.assertEqual(event.name, contract.default_name)
        self.assertEqual(event.metadata, dict(contract.metadata))
        for key, value in extra.items():
            self.assertEqual(getattr(event, key), value)

    def assert_error_redacted(self, event: TraceEvent) -> None:
        self.assertEqual(event.status, "error")
        self.assertIsNotNone(event.error)
        error = event.error or ""
        self.assertIn(REDACTED_TEXT, error)
        self.assertNotIn(SECRET_TEXT, error)


class SignatureContractTests(ContractTestCase):
    """Every wrapper takes the provider callable positionally and owns ``bir_`` options."""

    def test_wrappers_declare_the_shared_option_surface(self) -> None:
        contract = self.contract
        allowed = {"bir_name", *SHARED_BIR_OPTIONS, *contract.extra_options}

        for wrapper in contract.wrappers():
            with self.subTest(wrapper=wrapper.__name__):
                parameters = list(inspect.signature(wrapper).parameters.values())
                kinds = [parameter.kind for parameter in parameters]

                # The provider callable is positional-only so a provider keyword
                # named like it can still be forwarded, and *args/**kwargs carry
                # every remaining argument through untouched.
                self.assertEqual(parameters[0].kind, inspect.Parameter.POSITIONAL_ONLY)
                self.assertIn(inspect.Parameter.VAR_POSITIONAL, kinds)
                self.assertIn(inspect.Parameter.VAR_KEYWORD, kinds)

                keyword_only = {
                    parameter.name: parameter
                    for parameter in parameters
                    if parameter.kind is inspect.Parameter.KEYWORD_ONLY
                }
                self.assertEqual(set(keyword_only), allowed)
                self.assertEqual(keyword_only["bir_name"].default, contract.default_name)
                for name in (*SHARED_BIR_OPTIONS, *contract.extra_options):
                    self.assertIsNone(keyword_only[name].default)


def _hostile_responses() -> list[tuple[str, Any]]:
    """Response objects that raise from the places a wrapper reads them.

    Each is a shape a real provider produces on a bad day: ``model_dump`` raising
    ``PydanticSerializationError`` on a field it cannot serialize, an object that
    loads lazily and fails on attribute access, an older client exposing
    ``dict()``, and something whose iteration fails when a wrapper is deciding
    whether it is a stream.
    """

    class DumpRaises:
        model = "contract-model"
        usage = None

        def model_dump(self) -> Any:
            raise RuntimeError("model_dump exploded")

    class PropertyRaises:
        @property
        def model(self) -> Any:
            raise RuntimeError("model property exploded")

    class DictRaises:
        model = "contract-model"
        usage = None

        def dict(self) -> Any:
            raise RuntimeError("dict() exploded")

    class IterationRaises:
        model = "contract-model"
        usage = None

        def __iter__(self) -> Any:
            raise RuntimeError("iteration exploded")

    return [
        ("model_dump raises", DumpRaises()),
        ("property raises", PropertyRaises()),
        ("dict raises", DictRaises()),
        ("iteration raises", IterationRaises()),
    ]


class UnaryContractTests(ContractTestCase):
    """The single-response path: forwarding, recording, errors, and capture."""

    def unary(self) -> UnaryCapability:
        capability = self.contract.unary
        assert capability is not None  # guaranteed by build_contract_test_case
        return capability

    def call_sync(self, provider: Callable[..., Any], **options: Any) -> Any:
        return self.unary().sync_wrapper(provider, **dict(self.contract.request), **options)

    def call_async(self, provider: Callable[..., Any], **options: Any) -> Any:
        returned: list[Any] = []

        async def driver() -> None:
            async with trace("contract"):
                returned.append(await self.unary().async_wrapper(provider, **dict(self.contract.request), **options))

        asyncio.run(driver())
        return returned[0]

    def test_sync_call_returns_the_provider_result_and_records_one_generation(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            response = self.unary().response.build()
            calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

            def provider(*args: Any, **kwargs: Any) -> Any:
                calls.append((args, kwargs))
                return response

            with trace("contract"):
                # The wrapper returns the provider's own object untouched.
                self.assertIs(self.call_sync(provider), response)

            self.assertEqual(calls, [((), dict(self.contract.request))])
            event = self.generation_event()
            self.assertEqual(event.status, "success")
            self.assert_request_recorded(
                event,
                model=self.unary().response.model,
                usage=self.unary().response.usage,
                input=dict(self.contract.request),
            )

    def test_async_call_returns_the_provider_result_and_records_one_generation(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            response = self.unary().response.build()
            calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

            async def provider(*args: Any, **kwargs: Any) -> Any:
                calls.append((args, kwargs))
                return response

            returned = self.call_async(provider)

            self.assertIs(returned, response)
            self.assertEqual(calls, [((), dict(self.contract.request))])
            event = self.generation_event()
            self.assertEqual(event.status, "success")
            self.assert_request_recorded(
                event,
                model=self.unary().response.model,
                usage=self.unary().response.usage,
                input=dict(self.contract.request),
            )

    def test_sync_call_forwards_arguments_and_keeps_bir_options_out_of_the_request(self) -> None:
        with temporary_workdir():
            received: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

            def provider(*args: Any, **kwargs: Any) -> Any:
                received.append((args, kwargs))
                return self.unary().response.build()

            with trace("contract"):
                self.unary().sync_wrapper(
                    provider,
                    "positional",
                    **dict(self.contract.request),
                    bir_name="contract.custom",
                    bir_metadata={"team": "support"},
                )

            args, kwargs = received[0]
            self.assertEqual(args, ("positional",))
            self.assertEqual(kwargs, dict(self.contract.request))
            self.assertEqual([name for name in kwargs if name.startswith("bir_")], [])
            event = self.generation_event()
            self.assertEqual(event.name, "contract.custom")
            self.assertEqual(event.metadata, {**dict(self.contract.metadata), "team": "support"})

    def test_async_call_forwards_arguments_and_keeps_bir_options_out_of_the_request(self) -> None:
        with temporary_workdir():
            received: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

            async def provider(*args: Any, **kwargs: Any) -> Any:
                received.append((args, kwargs))
                return self.unary().response.build()

            async def driver() -> None:
                async with trace("contract"):
                    await self.unary().async_wrapper(
                        provider,
                        "positional",
                        **dict(self.contract.request),
                        bir_name="contract.custom",
                        bir_metadata={"team": "support"},
                    )

            asyncio.run(driver())

            args, kwargs = received[0]
            self.assertEqual(args, ("positional",))
            self.assertEqual(kwargs, dict(self.contract.request))
            self.assertEqual([name for name in kwargs if name.startswith("bir_")], [])
            event = self.generation_event()
            self.assertEqual(event.name, "contract.custom")
            self.assertEqual(event.metadata, {**dict(self.contract.metadata), "team": "support"})

    def test_sync_call_records_the_provider_error_with_a_redacted_message(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)

            def provider(*args: Any, **kwargs: Any) -> Any:
                raise RuntimeError(f"provider failed {SECRET_TEXT}")

            with self.assertRaises(RuntimeError), trace("contract"):
                self.call_sync(provider)

            self.assert_error_redacted(self.generation_event())

    def test_async_call_records_the_provider_error_with_a_redacted_message(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)

            async def provider(*args: Any, **kwargs: Any) -> Any:
                raise RuntimeError(f"provider failed {SECRET_TEXT}")

            with self.assertRaises(RuntimeError):
                self.call_async(provider)

            self.assert_error_redacted(self.generation_event())

    def test_sync_call_survives_a_response_whose_own_code_raises(self) -> None:
        """A response Bir cannot read must not fail the call that returned it.

        Reading a provider's object runs the provider's code -- a property that
        computes, a ``model_dump`` that serializes -- and the call has already
        succeeded by then. Every other recording failure is caught: a store that
        cannot be written is reported, and a value whose ``__repr__`` raises is
        caught inside capture. This is the same rule at the point a third
        party's object is first touched.
        """

        for label, response in _hostile_responses():
            with self.subTest(response=label), temporary_workdir():
                configure(capture_inputs=True, capture_outputs=True)

                def provider(*args: Any, **kwargs: Any) -> Any:
                    return response

                with trace("contract"):
                    self.assertIs(self.call_sync(provider), response)

                # The event is still written; only what could be read of the
                # response is missing from it.
                self.assertEqual(self.generation_event().status, "success")

    def test_async_call_survives_a_response_whose_own_code_raises(self) -> None:
        for label, response in _hostile_responses():
            with self.subTest(response=label), temporary_workdir():
                configure(capture_inputs=True, capture_outputs=True)

                async def provider(*args: Any, **kwargs: Any) -> Any:
                    return response

                self.assertIs(self.call_async(provider), response)
                self.assertEqual(self.generation_event().status, "success")

    def test_sync_call_requires_an_active_trace_and_leaves_the_provider_uncalled(self) -> None:
        with temporary_workdir():
            calls: list[dict[str, Any]] = []

            def provider(**kwargs: Any) -> Any:
                calls.append(kwargs)
                return self.unary().response.build()

            with self.assertRaises(RuntimeError):
                self.call_sync(provider)

            self.assertEqual(calls, [])
            self.assertEqual(load_events(), [])

    def test_async_call_requires_an_active_trace_and_leaves_the_provider_uncalled(self) -> None:
        with temporary_workdir():
            calls: list[dict[str, Any]] = []

            async def provider(**kwargs: Any) -> Any:
                calls.append(kwargs)
                return self.unary().response.build()

            async def driver() -> None:
                await self.unary().async_wrapper(provider, **dict(self.contract.request))

            with self.assertRaises(RuntimeError):
                asyncio.run(driver())

            self.assertEqual(calls, [])
            self.assertEqual(load_events(), [])

    def test_sync_call_captures_no_input_or_output_by_default(self) -> None:
        with temporary_workdir():

            def provider(**kwargs: Any) -> Any:
                return self.unary().response.build()

            with trace("contract"):
                self.call_sync(provider)

            event = self.generation_event()
            self.assertIsNone(event.input)
            self.assertIsNone(event.output)
            # Capture is opt-in, but the non-payload fields are always recorded.
            self.assertEqual(event.model, self.unary().response.model)
            self.assertEqual(event.usage, self.unary().response.usage)

    def test_sync_call_capture_overrides_win_over_the_configuration(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=False, capture_outputs=False)

            def provider(**kwargs: Any) -> Any:
                return self.unary().response.build()

            with trace("contract"):
                self.call_sync(provider, bir_capture_input=True, bir_capture_output=True)

            event = self.generation_event()
            self.assertEqual(event.input, dict(self.contract.request))
            self.assertIsNotNone(event.output)

    def test_sync_call_capture_overrides_can_disable_a_configured_capture(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)

            def provider(**kwargs: Any) -> Any:
                return self.unary().response.build()

            with trace("contract"):
                self.call_sync(provider, bir_capture_input=False, bir_capture_output=False)

            event = self.generation_event()
            self.assertIsNone(event.input)
            self.assertIsNone(event.output)

    def test_async_call_capture_overrides_win_over_the_configuration(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=False, capture_outputs=False)

            async def provider(**kwargs: Any) -> Any:
                return self.unary().response.build()

            self.call_async(provider, bir_capture_input=True, bir_capture_output=True)

            event = self.generation_event()
            self.assertEqual(event.input, dict(self.contract.request))
            self.assertIsNotNone(event.output)

    def test_async_call_capture_overrides_can_disable_a_configured_capture(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)

            async def provider(**kwargs: Any) -> Any:
                return self.unary().response.build()

            self.call_async(provider, bir_capture_input=False, bir_capture_output=False)

            event = self.generation_event()
            self.assertIsNone(event.input)
            self.assertIsNone(event.output)


class StreamContractTests(ContractTestCase):
    """The streaming path: laziness, accumulation, close, error, and cancellation."""

    def streaming(self) -> StreamCapability:
        capability = self.contract.streaming
        assert capability is not None  # guaranteed by build_contract_test_case
        return capability

    def request(self) -> dict[str, Any]:
        return {**dict(self.contract.request), **dict(self.streaming().enable)}

    def open_sync(self, provider: Callable[..., Any], **options: Any) -> Any:
        return self.streaming().sync_wrapper(provider, **self.request(), **options)

    def text_chunks(self, *texts: str) -> list[Any]:
        return [self.streaming().text_chunk(text) for text in texts]

    def streamed(self, stream: Any) -> Any:
        """Return what the provider's streaming call answers with."""

        return self.streaming().envelope(stream)

    def consume_sync(self, provider: Callable[..., Any]) -> list[Any]:
        """Open and fully consume a sync stream inside a trace."""

        consumed: list[Any] = []
        with trace("contract"):
            consumed.extend(self.open_sync(provider))
        return consumed

    def consume_async(self, provider: Callable[..., Any]) -> list[Any]:
        """Open and fully consume an async stream inside a trace."""

        consumed: list[Any] = []

        async def driver() -> None:
            async with trace("contract"):
                stream = await self.streaming().async_wrapper(provider, **self.request())
                async for chunk in stream:
                    consumed.append(chunk)

        asyncio.run(driver())
        return consumed

    def test_sync_stream_defers_the_provider_call_until_iteration(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)
            calls: list[dict[str, Any]] = []

            def provider(**kwargs: Any) -> Any:
                calls.append(kwargs)
                return self.streamed(FakeStream(self.text_chunks("Bir ", "streams")))

            with trace("contract"):
                stream = self.open_sync(provider)
                # Opening the stream must not call the provider or record
                # anything; the wrapper's generation opens on first iteration.
                self.assertEqual(calls, [])
                self.assertEqual([event for event in load_events() if event.type == "generation"], [])
                self.assertEqual(len(list(stream)), 2)

            self.assertEqual(len(calls), 1)
            self.assertEqual(self.generation_event().output, "Bir streams")

    def test_async_stream_defers_the_provider_call_until_iteration(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)
            calls: list[dict[str, Any]] = []
            observed: list[int] = []

            async def provider(**kwargs: Any) -> Any:
                calls.append(kwargs)
                return self.streamed(FakeAsyncStream(self.text_chunks("Bir ", "streams")))

            consumed: list[Any] = []

            async def driver() -> None:
                async with trace("contract"):
                    stream = await self.streaming().async_wrapper(provider, **self.request())
                    observed.append(len(calls))
                    async for chunk in stream:
                        consumed.append(chunk)

            asyncio.run(driver())

            # Awaiting the wrapper resolves to the stream object only; the
            # provider call happens once iteration starts.
            self.assertEqual(observed, [0])
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(consumed), 2)
            self.assertEqual(self.generation_event().output, "Bir streams")

    def test_sync_stream_yields_chunks_unchanged_and_records_the_accumulated_output(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)
            chunks = self.text_chunks("Bir ", "streams")

            def provider(**kwargs: Any) -> Any:
                return self.streamed(FakeStream(chunks))

            consumed = self.consume_sync(provider)

            self.assertEqual(consumed, chunks)
            event = self.generation_event()
            self.assertEqual(event.status, "success")
            self.assertEqual(event.output, "Bir streams")
            self.assert_request_recorded(event, model=self.streaming().model)

    def test_async_stream_yields_chunks_unchanged_and_records_the_accumulated_output(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)
            chunks = self.text_chunks("Bir ", "streams")

            async def provider(**kwargs: Any) -> Any:
                return self.streamed(FakeAsyncStream(chunks))

            consumed = self.consume_async(provider)

            self.assertEqual(consumed, chunks)
            event = self.generation_event()
            self.assertEqual(event.status, "success")
            self.assertEqual(event.output, "Bir streams")
            self.assert_request_recorded(event, model=self.streaming().model)

    def test_sync_stream_records_partial_output_when_closed_early(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)
            provider_stream = FakeStream(self.text_chunks("Bir ", "streams"))

            def provider(**kwargs: Any) -> Any:
                return self.streamed(provider_stream)

            with trace("contract"):
                stream = self.open_sync(provider)
                iterator = iter(stream)
                next(iterator)
                stream.close()

            event = self.generation_event()
            self.assertEqual(event.output, "Bir ")
            # Closing mid-stream aborts the call, so the generation is finalized
            # as an error rather than silently reported as a complete success.
            self.assertEqual(event.status, "error")
            # The provider's own stream belongs to its caller; the wrapper never
            # closes it on their behalf.
            self.assertFalse(provider_stream.closed)

    def test_async_stream_records_partial_output_when_closed_early(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)
            provider_stream = FakeAsyncStream(self.text_chunks("Bir ", "streams"))

            async def provider(**kwargs: Any) -> Any:
                return self.streamed(provider_stream)

            async def driver() -> None:
                async with trace("contract"):
                    stream = await self.streaming().async_wrapper(provider, **self.request())
                    iterator = stream.__aiter__()
                    await iterator.__anext__()
                    await stream.aclose()

            asyncio.run(driver())

            event = self.generation_event()
            self.assertEqual(event.output, "Bir ")
            self.assertEqual(event.status, "error")
            self.assertFalse(provider_stream.closed)

    def test_sync_stream_records_the_error_with_a_redacted_message_and_partial_output(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)

            def provider(**kwargs: Any) -> Any:
                return self.streamed(
                    FakeStream(
                        self.text_chunks(f"partial {SECRET_TEXT} "),
                        error=RuntimeError(f"stream failed {SECRET_TEXT}"),
                    )
                )

            with self.assertRaises(RuntimeError):
                self.consume_sync(provider)

            event = self.generation_event()
            self.assert_error_redacted(event)
            self.assertEqual(event.output, f"partial {REDACTED_TEXT} ")

    def test_async_stream_records_the_error_with_a_redacted_message_and_partial_output(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)

            async def provider(**kwargs: Any) -> Any:
                return self.streamed(
                    FakeAsyncStream(
                        self.text_chunks(f"partial {SECRET_TEXT} "),
                        error=RuntimeError(f"stream failed {SECRET_TEXT}"),
                    )
                )

            with self.assertRaises(RuntimeError):
                self.consume_async(provider)

            event = self.generation_event()
            self.assert_error_redacted(event)
            self.assertEqual(event.output, f"partial {REDACTED_TEXT} ")

    def test_async_stream_records_partial_output_when_the_consumer_is_cancelled(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)
            provider_stream = FakeAsyncStream(self.text_chunks("Bir "), block=True)

            async def provider(**kwargs: Any) -> Any:
                return self.streamed(provider_stream)

            async def consume() -> None:
                async with trace("contract"):
                    stream = await self.streaming().async_wrapper(provider, **self.request())
                    async for _chunk in stream:
                        pass

            async def driver() -> None:
                task = asyncio.ensure_future(consume())
                # Cancel only once the stream is parked past its first chunk, so
                # the wrapper is suspended mid-iteration with output pending.
                await provider_stream.blocked.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            asyncio.run(driver())

            # A cancelled call must still leave a finalized generation behind
            # rather than dropping the work that already happened.
            event = self.generation_event()
            self.assertEqual(event.output, "Bir ")
            self.assertEqual(event.status, "error")

    def test_sync_stream_records_the_whole_response_when_the_provider_ignores_streaming(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)
            response = self.streaming().whole_response.build()

            def provider(**kwargs: Any) -> Any:
                return response

            consumed = self.consume_sync(provider)

            # Nothing is yielded because there were no chunks, but the response
            # is recorded in one piece instead of being lost.
            self.assertEqual(consumed, [])
            event = self.generation_event()
            self.assertEqual(event.status, "success")
            self.assertEqual(event.model, self.streaming().whole_response.model)
            self.assertEqual(event.usage, self.streaming().whole_response.usage)
            self.assertIsNotNone(event.output)

    def test_async_stream_records_the_whole_response_when_the_provider_ignores_streaming(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)
            response = self.streaming().whole_response.build()

            async def provider(**kwargs: Any) -> Any:
                return response

            consumed = self.consume_async(provider)

            self.assertEqual(consumed, [])
            event = self.generation_event()
            self.assertEqual(event.status, "success")
            self.assertEqual(event.model, self.streaming().whole_response.model)
            self.assertEqual(event.usage, self.streaming().whole_response.usage)
            self.assertIsNotNone(event.output)

    def test_sync_stream_requires_an_active_trace_and_leaves_the_provider_uncalled(self) -> None:
        with temporary_workdir():
            calls: list[dict[str, Any]] = []

            def provider(**kwargs: Any) -> Any:
                calls.append(kwargs)
                return self.streamed(FakeStream(self.text_chunks("Bir ")))

            with self.assertRaises(RuntimeError):
                list(self.open_sync(provider))

            self.assertEqual(calls, [])
            self.assertEqual(load_events(), [])

    def test_async_stream_requires_an_active_trace_and_leaves_the_provider_uncalled(self) -> None:
        with temporary_workdir():
            calls: list[dict[str, Any]] = []

            async def provider(**kwargs: Any) -> Any:
                calls.append(kwargs)
                return self.streamed(FakeAsyncStream(self.text_chunks("Bir ")))

            async def driver() -> None:
                stream = await self.streaming().async_wrapper(provider, **self.request())
                async for _chunk in stream:
                    pass

            with self.assertRaises(RuntimeError):
                asyncio.run(driver())

            self.assertEqual(calls, [])
            self.assertEqual(load_events(), [])


def build_contract_test_case(contract: WrapperContract) -> type[ContractTestCase]:
    """Return the conformance test case for one wrapper family.

    The case composes only the mixins the family declared, so a wrapper without
    a streaming path is never asked to pass streaming cases, and a declaration
    that adds a path immediately inherits every case for it.
    """

    bases: list[type[ContractTestCase]] = [SignatureContractTests]
    if contract.unary is not None:
        bases.append(UnaryContractTests)
    if contract.streaming is not None:
        bases.append(StreamContractTests)

    class_name = "".join(part.title() for part in contract.id.replace(".", "_").split("_")) + "ContractTests"
    return type(class_name, tuple(bases), {"contract": contract})
