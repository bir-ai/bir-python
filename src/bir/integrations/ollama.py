"""Thin Ollama integration for recording Bir generations.

The wrappers intentionally avoid importing the ``ollama`` package so the Bir SDK
stays dependency-free. Applications pass an Ollama ``chat`` or ``generate``
callable (the module-level ``ollama.chat``/``ollama.generate`` functions or a
client's ``.chat``/``.generate`` methods); the matching wrapper invokes it inside
a single Bir ``generation`` event and reads the model, output text, and token
usage from the Ollama response when present. Ollama exposes two surfaces with
different response and streaming shapes, so they get their own wrappers:
``trace_chat`` for ``ollama.chat`` and ``trace_generate`` for ``ollama.generate``.

Ollama returns the model at ``model``, the assistant text at ``message.content``
(chat) or ``response`` (generate), and token usage at the top-level
``prompt_eval_count`` (input) and ``eval_count`` (output); the total is derived.
With ``stream=True`` Ollama yields incremental chunks (a ``message.content`` delta
for chat, a ``response`` delta for generate) and a final chunk (``done``) carrying
the token counts, so the streaming wrappers record the accumulated text and final
usage after the stream is consumed. The async ``AsyncClient`` streams the same
chunks, so the async wrappers stream them too.
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


def trace_chat(
    chat: Callable[..., Any],
    /,
    *args: Any,
    bir_name: str = "ollama.chat",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Call an Ollama ``chat`` callable and record one Bir generation.

    ``chat`` is normally ``ollama.chat`` or ``client.chat``; positional and
    keyword arguments are forwarded to it unchanged and returned to the caller.
    The request is recorded as the generation input, then ``model``, the assistant
    text at ``message.content``, and token usage (``prompt_eval_count`` /
    ``eval_count``) are read from the response when present.

    With ``stream=True`` the wrapper returns a lazy iterable that yields the
    provider's chunks unchanged and assembles the output from each chunk's
    ``message.content`` delta, finalizing the model, output, and usage when the
    stream is exhausted, closed, or raises.

    Like ``bir.generation()``, this must run inside an active trace (for example a
    ``@observe()`` function or ``with bir.trace(...)``). The wrapper's own options
    are prefixed ``bir_`` so they never collide with Ollama ``chat`` keyword
    arguments such as ``options`` or ``format``.
    """

    metadata = _metadata(bir_metadata)

    if kwargs.get("stream") is True:
        return _stream_chat(
            chat,
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
        response = chat(*args, **kwargs)
        _record_response(gen, response)
        return response


def _stream_chat(
    chat: Callable[..., Any],
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
        stream = chat(*args, **kwargs)
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

                text = _chat_delta_text(chunk)
                if text is not None:
                    output_parts.append(text)

                usage = _chunk_usage(chunk)
                if usage is not None:
                    last_usage = usage

                yield chunk
        finally:
            gen.set_output("".join(output_parts))
            _record_usage(gen, last_usage)


async def trace_chat_async(
    chat: Callable[..., Awaitable[Any]],
    /,
    *args: Any,
    bir_name: str = "ollama.chat",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Await an async Ollama ``chat`` callable and record one Bir generation.

    The asynchronous counterpart of :func:`trace_chat` for the Ollama
    ``AsyncClient`` (``client.chat``). ``chat`` returns a coroutine; it is awaited
    inside a single Bir ``generation`` event, arguments are forwarded unchanged,
    and the awaited response is returned. ``model``, the assistant text at
    ``message.content``, and token usage are read from the response when present.

    With ``stream=True`` the coroutine resolves to an async iterator that yields
    the provider's chunks unchanged and assembles the output from each chunk's
    ``message.content`` delta, finalizing the model, output, and usage when the
    stream is exhausted, closed, or raises.

    Like the sync wrapper this must run inside an active trace (for example an
    async ``@observe()`` function or ``async with bir.trace(...)``); the ``bir_``
    prefixed options never collide with Ollama ``chat`` keyword arguments.
    """

    metadata = _metadata(bir_metadata)

    if kwargs.get("stream") is True:
        return _stream_chat_async(
            chat,
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
        response = await chat(*args, **kwargs)
        _record_response(gen, response)
        return response


async def _stream_chat_async(
    chat: Callable[..., Awaitable[Any]],
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
        stream = await chat(*args, **kwargs)
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

                text = _chat_delta_text(chunk)
                if text is not None:
                    output_parts.append(text)

                usage = _chunk_usage(chunk)
                if usage is not None:
                    last_usage = usage

                yield chunk
        finally:
            gen.set_output("".join(output_parts))
            _record_usage(gen, last_usage)


def trace_generate(
    generate: Callable[..., Any],
    /,
    *args: Any,
    bir_name: str = "ollama.generate",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Call an Ollama ``generate`` callable and record one Bir generation.

    ``generate`` is normally ``ollama.generate`` or ``client.generate``; positional
    and keyword arguments are forwarded to it unchanged and returned to the caller.
    The request is recorded as the generation input, then ``model``, the completion
    text at ``response``, and token usage (``prompt_eval_count`` / ``eval_count``)
    are read from the response when present.

    With ``stream=True`` the wrapper returns a lazy iterable that yields the
    provider's chunks unchanged and assembles the output from each chunk's
    ``response`` delta, finalizing the model, output, and usage when the stream is
    exhausted, closed, or raises.

    Like ``bir.generation()``, this must run inside an active trace (for example a
    ``@observe()`` function or ``with bir.trace(...)``). The wrapper's own options
    are prefixed ``bir_`` so they never collide with Ollama ``generate`` keyword
    arguments such as ``options`` or ``format``.
    """

    metadata = _metadata(bir_metadata)

    if kwargs.get("stream") is True:
        return _stream_generate(
            generate,
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
        response = generate(*args, **kwargs)
        _record_response(gen, response)
        return response


def _stream_generate(
    generate: Callable[..., Any],
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
        stream = generate(*args, **kwargs)
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

                text = _generate_delta_text(chunk)
                if text is not None:
                    output_parts.append(text)

                usage = _chunk_usage(chunk)
                if usage is not None:
                    last_usage = usage

                yield chunk
        finally:
            gen.set_output("".join(output_parts))
            _record_usage(gen, last_usage)


async def trace_generate_async(
    generate: Callable[..., Awaitable[Any]],
    /,
    *args: Any,
    bir_name: str = "ollama.generate",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Await an async Ollama ``generate`` callable and record one Bir generation.

    The asynchronous counterpart of :func:`trace_generate` for the Ollama
    ``AsyncClient`` (``client.generate``). ``generate`` returns a coroutine; it is
    awaited inside a single Bir ``generation`` event, arguments are forwarded
    unchanged, and the awaited response is returned. ``model``, the completion text
    at ``response``, and token usage are read from the response when present.

    With ``stream=True`` the coroutine resolves to an async iterator that yields
    the provider's chunks unchanged and assembles the output from each chunk's
    ``response`` delta, finalizing the model, output, and usage when the stream is
    exhausted, closed, or raises.

    Like the sync wrapper this must run inside an active trace (for example an
    async ``@observe()`` function or ``async with bir.trace(...)``); the ``bir_``
    prefixed options never collide with Ollama ``generate`` keyword arguments.
    """

    metadata = _metadata(bir_metadata)

    if kwargs.get("stream") is True:
        return _stream_generate_async(
            generate,
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
        response = await generate(*args, **kwargs)
        _record_response(gen, response)
        return response


async def _stream_generate_async(
    generate: Callable[..., Awaitable[Any]],
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
        stream = await generate(*args, **kwargs)
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

                text = _generate_delta_text(chunk)
                if text is not None:
                    output_parts.append(text)

                usage = _chunk_usage(chunk)
                if usage is not None:
                    last_usage = usage

                yield chunk
        finally:
            gen.set_output("".join(output_parts))
            _record_usage(gen, last_usage)


def _metadata(bir_metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    metadata: dict[str, Any] = {"integration": "ollama"}
    if bir_metadata:
        metadata.update(bir_metadata)
    return metadata


def _record_response(gen: Any, response: Any) -> None:
    gen.set_output(_response_output(response))

    # The model is read at __exit__, so refine it from the response here while
    # keeping the request model as a fallback when the response omits one.
    response_model = _string_or_none(_value(response, "model"))
    if response_model is not None:
        gen.set_model(response_model)

    _record_usage(gen, response)


def _record_usage(gen: Any, source: Any) -> None:
    # Ollama reports token counts at the top level of the response (and of the
    # terminal streamed chunk), not in a nested ``usage`` object.
    if source is None:
        return
    input_tokens = _usage_tokens(source, "prompt_eval_count")
    output_tokens = _usage_tokens(source, "eval_count")
    # Ollama omits a total, so derive it when both halves are present.
    total_tokens = (
        input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else None
    )
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return
    gen.set_usage(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)


def _chat_delta_text(chunk: Any) -> str | None:
    # Chat chunks carry incremental output at ``message.content``; the terminal
    # ``done`` chunk repeats an empty ``message.content`` and is skipped.
    message = _value(chunk, "message")
    return _string_or_none(_value(message, "content"))


def _generate_delta_text(chunk: Any) -> str | None:
    # Generate chunks carry incremental output at ``response``; the terminal
    # ``done`` chunk repeats an empty ``response`` and is skipped.
    return _string_or_none(_value(chunk, "response"))


def _chunk_usage(chunk: Any) -> Any:
    # Only Ollama's terminal streamed chunk (``done: True``) carries the token
    # counts (``prompt_eval_count``/``eval_count``); earlier chunks omit them, so
    # return the chunk itself as the usage source only when it carries a count.
    if _usage_tokens(chunk, "prompt_eval_count") is not None or _usage_tokens(chunk, "eval_count") is not None:
        return chunk
    return None


def _request_input(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = dict(kwargs)
    if args:
        payload["args"] = list(args)
    return payload
