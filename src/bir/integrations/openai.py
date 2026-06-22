"""Thin OpenAI integration for recording Bir generations.

The wrappers intentionally avoid importing the ``openai`` package so the Bir SDK
stays dependency-free. Applications pass an OpenAI ``chat.completions.create`` or
``responses.create`` callable; the matching wrapper invokes it inside a single
Bir ``generation`` event and reads the model, output, and token usage from the
response when they are present. The two OpenAI surfaces return different response
and streaming-event shapes, so they get their own wrappers:
``trace_chat_completion`` for Chat Completions and ``trace_response`` for the
Responses API.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from typing import Any

from bir import generation

from ._common import (
    _chunk_delta_content,
    _is_async_streamed_response,
    _is_streamed_response,
    _response_output,
    _string_or_none,
    _usage_tokens,
    _value,
)


def trace_chat_completion(
    create: Callable[..., Any],
    /,
    *args: Any,
    bir_name: str = "openai.chat.completions",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Call an OpenAI chat-completions ``create`` and record one Bir generation.

    ``create`` is normally ``client.chat.completions.create``; positional and
    keyword arguments are forwarded to it unchanged and returned to the caller.
    The request is recorded as the generation input, then ``model`` and token
    ``usage`` are read from the response when present.

    Like ``bir.generation()``, this must run inside an active trace (for example a
    ``@observe()`` function or ``with bir.trace(...)``). The wrapper's own options
    are prefixed ``bir_`` so they never collide with OpenAI ``create`` keyword
    arguments such as ``metadata``.
    """

    metadata: dict[str, Any] = {"integration": "openai"}
    if bir_metadata:
        metadata.update(bir_metadata)

    if kwargs.get("stream") is True:
        return _stream_chat_completion(
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


def _stream_chat_completion(
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
        last_usage: Any = None

        try:
            for chunk in stream:
                response_model = _string_or_none(_value(chunk, "model"))
                if response_model is not None:
                    gen.model = response_model

                content = _chunk_delta_content(chunk)
                if content is not None:
                    output_parts.append(content)

                usage = _value(chunk, "usage")
                if usage is not None:
                    last_usage = usage

                yield chunk
        finally:
            gen.set_output("".join(output_parts))
            _record_usage(gen, last_usage)


async def trace_chat_completion_async(
    create: Callable[..., Awaitable[Any]],
    /,
    *args: Any,
    bir_name: str = "openai.chat.completions",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Await an async OpenAI chat-completions ``create`` and record one generation.

    The asynchronous counterpart of :func:`trace_chat_completion` for
    ``AsyncOpenAI`` clients. ``create`` is normally
    ``client.chat.completions.create`` returning a coroutine; it is awaited inside
    a single Bir ``generation`` event, arguments are forwarded unchanged, and the
    awaited response is returned. With ``stream=True`` the coroutine resolves to an
    async iterator that yields the provider's events unchanged and finalizes the
    model, output, and usage when the stream is exhausted, closed, or raises.

    Like the sync wrapper this must run inside an active trace (for example an
    async ``@observe()`` function or ``async with bir.trace(...)``); the ``bir_``
    prefixed options never collide with OpenAI ``create`` keyword arguments.
    """

    metadata: dict[str, Any] = {"integration": "openai"}
    if bir_metadata:
        metadata.update(bir_metadata)

    if kwargs.get("stream") is True:
        return _stream_chat_completion_async(
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


async def _stream_chat_completion_async(
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
        last_usage: Any = None

        try:
            async for chunk in stream:
                response_model = _string_or_none(_value(chunk, "model"))
                if response_model is not None:
                    gen.model = response_model

                content = _chunk_delta_content(chunk)
                if content is not None:
                    output_parts.append(content)

                usage = _value(chunk, "usage")
                if usage is not None:
                    last_usage = usage

                yield chunk
        finally:
            gen.set_output("".join(output_parts))
            _record_usage(gen, last_usage)


def _record_response(gen: Any, response: Any) -> None:
    gen.set_output(_response_output(response))

    # _Generation has no set_model, and the model attribute is read at __exit__,
    # so refine it from the response here while keeping the request model as a
    # fallback when the response omits one.
    response_model = _string_or_none(_value(response, "model"))
    if response_model is not None:
        gen.model = response_model

    _record_usage(gen, _value(response, "usage"))


def _record_usage(gen: Any, usage: Any) -> None:
    if usage is None:
        return
    input_tokens = _usage_tokens(usage, "prompt_tokens", "input_tokens")
    output_tokens = _usage_tokens(usage, "completion_tokens", "output_tokens")
    total_tokens = _usage_tokens(usage, "total_tokens")
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return
    gen.set_usage(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)


def _request_input(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = dict(kwargs)
    if args:
        payload["args"] = list(args)
    return payload


# --- Responses API -----------------------------------------------------------
#
# The Responses API (``client.responses.create``) returns a different response
# shape than Chat Completions: text is aggregated on ``output_text`` and usage
# uses ``input_tokens``/``output_tokens``/``total_tokens``. Its stream is a
# sequence of typed events rather than message-delta chunks, so it gets its own
# wrapper while reusing ``_record_usage`` and ``_request_input`` above.


def trace_response(
    create: Callable[..., Any],
    /,
    *args: Any,
    bir_name: str = "openai.responses",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Call an OpenAI Responses ``create`` and record one Bir generation.

    ``create`` is normally ``client.responses.create``; positional and keyword
    arguments are forwarded to it unchanged and returned to the caller. The
    request is recorded as the generation input, then ``model``, the aggregated
    ``output_text``, and token ``usage`` are read from the response when present.

    With ``stream=True`` the wrapper returns a lazy iterable that yields the
    provider's events unchanged and assembles the output from
    ``response.output_text.delta`` events, finalizing the generation when the
    stream is exhausted, closed, or raises.

    Like ``bir.generation()``, this must run inside an active trace (for example a
    ``@observe()`` function or ``with bir.trace(...)``). The wrapper's own options
    are prefixed ``bir_`` so they never collide with OpenAI ``create`` keyword
    arguments such as ``metadata``.
    """

    metadata: dict[str, Any] = {"integration": "openai"}
    if bir_metadata:
        metadata.update(bir_metadata)

    if kwargs.get("stream") is True:
        return _stream_response(
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
        _record_response_result(gen, response)
        return response


def _stream_response(
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
            _record_response_result(gen, stream)
            return

        output_parts: list[str] = []
        usage_tokens: dict[str, int | float] = {}

        try:
            for event in stream:
                model = _event_model(event)
                if model is not None:
                    gen.model = model

                text = _event_delta_text(event)
                if text is not None:
                    output_parts.append(text)

                _merge_response_usage(usage_tokens, event)

                yield event
        finally:
            gen.set_output("".join(output_parts))
            _record_usage(gen, usage_tokens)


def _record_response_result(gen: Any, response: Any) -> None:
    gen.set_output(_response_text_or_shape(response))

    # _Generation has no set_model, and the model attribute is read at __exit__,
    # so refine it from the response here while keeping the request model as a
    # fallback when the response omits one.
    response_model = _string_or_none(_value(response, "model"))
    if response_model is not None:
        gen.model = response_model

    _record_usage(gen, _value(response, "usage"))


def _response_text_or_shape(response: Any) -> Any:
    """Return the Responses ``output_text`` when present, else the full shape.

    The Responses API exposes an aggregated ``output_text`` convenience string.
    When it is absent or empty (for example a response carrying only tool calls),
    fall back to the JSON-safe full response shape so nothing is silently lost.
    """

    text = _string_or_none(_value(response, "output_text"))
    if text is not None:
        return text
    return _response_output(response)


def _event_model(event: Any) -> str | None:
    # Responses stream events carry the model on the nested ``response`` snapshot
    # (``response.created`` through ``response.completed``); intermediate delta
    # events expose no ``response`` and are skipped, so the latest seen wins.
    response = _value(event, "response")
    return _string_or_none(_value(response, "model"))


def _event_delta_text(event: Any) -> str | None:
    # Only ``response.output_text.delta`` events carry incremental output text at
    # ``delta``; the matching ``...done`` event repeats the whole string and other
    # delta events (refusals, function-call arguments) are deliberately skipped.
    if _value(event, "type") != "response.output_text.delta":
        return None
    return _string_or_none(_value(event, "delta"))


def _merge_response_usage(tokens: dict[str, int | float], event: Any) -> None:
    # Streamed usage arrives on the terminal events' nested ``response.usage``
    # (``response.completed``/``incomplete``/``failed``); intermediate events carry
    # ``usage: None``. Take the latest value seen for each field.
    usage = _value(_value(event, "response"), "usage")
    if usage is None:
        return
    input_tokens = _usage_tokens(usage, "input_tokens")
    if input_tokens is not None:
        tokens["input_tokens"] = input_tokens
    output_tokens = _usage_tokens(usage, "output_tokens")
    if output_tokens is not None:
        tokens["output_tokens"] = output_tokens
    total_tokens = _usage_tokens(usage, "total_tokens")
    if total_tokens is not None:
        tokens["total_tokens"] = total_tokens


async def trace_response_async(
    create: Callable[..., Awaitable[Any]],
    /,
    *args: Any,
    bir_name: str = "openai.responses",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Await an async OpenAI Responses ``create`` and record one generation.

    The asynchronous counterpart of :func:`trace_response` for ``AsyncOpenAI``
    clients. ``create`` is normally ``client.responses.create`` returning a
    coroutine; it is awaited inside a single Bir ``generation`` event, arguments
    are forwarded unchanged, and the awaited response is returned. With
    ``stream=True`` the coroutine resolves to an async iterator that yields the
    provider's events unchanged, assembles output from
    ``response.output_text.delta`` events, and finalizes the model and usage from
    the terminal ``response.completed`` event when the stream is exhausted,
    closed, or raises.

    Like the sync wrapper this must run inside an active trace (for example an
    async ``@observe()`` function or ``async with bir.trace(...)``); the ``bir_``
    prefixed options never collide with OpenAI ``create`` keyword arguments.
    """

    metadata: dict[str, Any] = {"integration": "openai"}
    if bir_metadata:
        metadata.update(bir_metadata)

    if kwargs.get("stream") is True:
        return _stream_response_async(
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
        _record_response_result(gen, response)
        return response


async def _stream_response_async(
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
            _record_response_result(gen, stream)
            return

        output_parts: list[str] = []
        usage_tokens: dict[str, int | float] = {}

        try:
            async for event in stream:
                model = _event_model(event)
                if model is not None:
                    gen.model = model

                text = _event_delta_text(event)
                if text is not None:
                    output_parts.append(text)

                _merge_response_usage(usage_tokens, event)

                yield event
        finally:
            gen.set_output("".join(output_parts))
            _record_usage(gen, usage_tokens)
