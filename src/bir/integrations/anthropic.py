"""Thin Anthropic Messages integration for recording Bir generations.

The wrapper intentionally avoids importing the ``anthropic`` package so the Bir
SDK stays dependency-free. Applications pass an Anthropic ``messages.create``
callable; the wrapper invokes it inside a single Bir ``generation`` event and
reads the model and token usage from the response when they are present.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from typing import Any

from bir import generation

from ._common import (
    _is_async_streamed_response,
    _is_streamed_response,
    _response_output,
    _string_or_none,
    _usage_tokens,
    _value,
)


def trace_messages(
    create: Callable[..., Any],
    /,
    *args: Any,
    bir_name: str = "anthropic.messages",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Call an Anthropic Messages ``create`` and record one Bir generation.

    ``create`` is normally ``client.messages.create``; positional and keyword
    arguments are forwarded to it unchanged and returned to the caller. The
    request is recorded as the generation input, then ``model`` and token
    ``usage`` are read from the response when present.

    Like ``bir.generation()``, this must run inside an active trace (for example a
    ``@observe()`` function or ``with bir.trace(...)``). The wrapper's own options
    are prefixed ``bir_`` so they never collide with Anthropic ``create`` keyword
    arguments such as ``metadata``.
    """

    metadata: dict[str, Any] = {"integration": "anthropic"}
    if bir_metadata:
        metadata.update(bir_metadata)

    if kwargs.get("stream") is True:
        return _stream_messages(
            create,
            args,
            kwargs,
            bir_name=bir_name,
            metadata=metadata,
            bir_capture_input=bir_capture_input,
            bir_capture_output=bir_capture_output,
        )

    with generation(
        bir_name,
        model=_string_or_none(kwargs.get("model")),
        input=_request_input(args, kwargs),
        metadata=metadata,
        capture_input=bir_capture_input,
        capture_output=bir_capture_output,
    ) as gen:
        response = create(*args, **kwargs)
        _record_response(gen, response)
        return response


def _stream_messages(
    create: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    *,
    bir_name: str,
    metadata: Mapping[str, Any],
    bir_capture_input: bool | None,
    bir_capture_output: bool | None,
) -> Iterable[Any]:
    with generation(
        bir_name,
        model=_string_or_none(kwargs.get("model")),
        input=_request_input(args, kwargs),
        metadata=metadata,
        capture_input=bir_capture_input,
        capture_output=bir_capture_output,
    ) as gen:
        stream = create(*args, **kwargs)
        if not _is_streamed_response(stream):
            _record_response(gen, stream)
            return

        output_parts: list[str] = []
        usage_tokens: dict[str, int | float] = {}

        try:
            for chunk in stream:
                model = _chunk_model(chunk)
                if model is not None:
                    gen.model = model

                text = _chunk_delta_text(chunk)
                if text is not None:
                    output_parts.append(text)

                _merge_stream_usage(usage_tokens, chunk)

                yield chunk
        finally:
            gen.set_output("".join(output_parts))
            _record_usage(gen, usage_tokens)


async def trace_messages_async(
    create: Callable[..., Awaitable[Any]],
    /,
    *args: Any,
    bir_name: str = "anthropic.messages",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Await an async Anthropic Messages ``create`` and record one generation.

    The asynchronous counterpart of :func:`trace_messages` for ``AsyncAnthropic``
    clients. ``create`` is normally ``client.messages.create`` returning a
    coroutine; it is awaited inside a single Bir ``generation`` event, arguments
    are forwarded unchanged, and the awaited response is returned. With
    ``stream=True`` the coroutine resolves to an async iterator that yields the
    provider's events unchanged and accumulates text and usage from the message
    events, finalizing when the stream is exhausted, closed, or raises.

    Like the sync wrapper this must run inside an active trace (for example an
    async ``@observe()`` function or ``async with bir.trace(...)``); the ``bir_``
    prefixed options never collide with Anthropic ``create`` keyword arguments.
    """

    metadata: dict[str, Any] = {"integration": "anthropic"}
    if bir_metadata:
        metadata.update(bir_metadata)

    if kwargs.get("stream") is True:
        return _stream_messages_async(
            create,
            args,
            kwargs,
            bir_name=bir_name,
            metadata=metadata,
            bir_capture_input=bir_capture_input,
            bir_capture_output=bir_capture_output,
        )

    async with generation(
        bir_name,
        model=_string_or_none(kwargs.get("model")),
        input=_request_input(args, kwargs),
        metadata=metadata,
        capture_input=bir_capture_input,
        capture_output=bir_capture_output,
    ) as gen:
        response = await create(*args, **kwargs)
        _record_response(gen, response)
        return response


async def _stream_messages_async(
    create: Callable[..., Awaitable[Any]],
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    *,
    bir_name: str,
    metadata: Mapping[str, Any],
    bir_capture_input: bool | None,
    bir_capture_output: bool | None,
) -> AsyncIterator[Any]:
    async with generation(
        bir_name,
        model=_string_or_none(kwargs.get("model")),
        input=_request_input(args, kwargs),
        metadata=metadata,
        capture_input=bir_capture_input,
        capture_output=bir_capture_output,
    ) as gen:
        stream = await create(*args, **kwargs)
        if not _is_async_streamed_response(stream):
            _record_response(gen, stream)
            return

        output_parts: list[str] = []
        usage_tokens: dict[str, int | float] = {}

        try:
            async for chunk in stream:
                model = _chunk_model(chunk)
                if model is not None:
                    gen.model = model

                text = _chunk_delta_text(chunk)
                if text is not None:
                    output_parts.append(text)

                _merge_stream_usage(usage_tokens, chunk)

                yield chunk
        finally:
            gen.set_output("".join(output_parts))
            _record_usage(gen, usage_tokens)


def _chunk_model(chunk: Any) -> str | None:
    # Anthropic carries the model on the ``message_start`` event's nested message.
    message = _value(chunk, "message")
    return _string_or_none(_value(message, "model"))


def _chunk_delta_text(chunk: Any) -> str | None:
    # ``content_block_delta`` events carry incremental output at ``delta.text``;
    # other events (``message_delta`` stop reasons, content-block starts) expose
    # no ``delta.text`` and are skipped.
    delta = _value(chunk, "delta")
    return _string_or_none(_value(delta, "text"))


def _merge_stream_usage(tokens: dict[str, int | float], chunk: Any) -> None:
    # Streamed usage is split across events: ``message_start`` carries
    # ``input_tokens`` on the nested ``message.usage``, while ``message_delta``
    # carries the final ``output_tokens`` on the top-level ``usage``. Take the
    # latest value seen for each so the cumulative counts win.
    message = _value(chunk, "message")
    for source in (_value(message, "usage"), _value(chunk, "usage")):
        if source is None:
            continue
        input_tokens = _usage_tokens(source, "input_tokens")
        if input_tokens is not None:
            tokens["input_tokens"] = input_tokens
        output_tokens = _usage_tokens(source, "output_tokens")
        if output_tokens is not None:
            tokens["output_tokens"] = output_tokens


def _record_response(gen: Any, response: Any) -> None:
    gen.set_output(_response_output(response))

    # The model is read at __exit__, so refine it from the response here while
    # keeping the request model as a fallback when the response omits one.
    response_model = _string_or_none(_value(response, "model"))
    if response_model is not None:
        gen.set_model(response_model)

    _record_usage(gen, _value(response, "usage"))


def _record_usage(gen: Any, usage: Any) -> None:
    if usage is None:
        return
    input_tokens = _usage_tokens(usage, "input_tokens")
    output_tokens = _usage_tokens(usage, "output_tokens")
    # Anthropic responses omit a total, so derive it when both halves are present.
    total_tokens = (
        input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else None
    )
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return
    gen.set_usage(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)


def _request_input(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = dict(kwargs)
    if args:
        payload["args"] = list(args)
    return payload
