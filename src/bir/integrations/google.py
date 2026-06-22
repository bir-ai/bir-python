"""Thin Google Gemini integration for recording Bir generations.

The wrapper intentionally avoids importing the ``google-genai`` (or legacy
``google-generativeai``) package so the Bir SDK stays dependency-free.
Applications pass a Gemini ``generate_content`` callable; the wrapper invokes it
inside a single Bir ``generation`` event and reads token usage from the response
when it is present.
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


def trace_generate_content(
    generate: Callable[..., Any],
    /,
    *args: Any,
    bir_name: str = "google.generate_content",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Call a Gemini ``generate_content`` and record one Bir generation.

    ``generate`` is normally ``client.models.generate_content`` (or
    ``model.generate_content``); positional and keyword arguments are forwarded
    to it unchanged and returned to the caller. The request is recorded as the
    generation input, the model is taken from the request ``model`` keyword
    (Gemini responses carry no top-level model), and token usage is read from
    ``response.usage_metadata`` when present.

    Like ``bir.generation()``, this must run inside an active trace (for example a
    ``@observe()`` function or ``with bir.trace(...)``). The wrapper's own options
    are prefixed ``bir_`` so they never collide with Gemini ``generate_content``
    keyword arguments such as ``config``.
    """

    metadata: dict[str, Any] = {"integration": "google"}
    if bir_metadata:
        metadata.update(bir_metadata)

    if kwargs.get("stream") is True:
        return _stream_generate_content(
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


def _stream_generate_content(
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

        # Gemini chunks carry no top-level model, so the request ``model`` keyword
        # passed to ``generation()`` stands; only text and usage are accumulated.
        output_parts: list[str] = []
        last_usage: Any = None

        try:
            for chunk in stream:
                text = _string_or_none(_value(chunk, "text"))
                if text is not None:
                    output_parts.append(text)

                usage = _value(chunk, "usage_metadata")
                if usage is not None:
                    last_usage = usage

                yield chunk
        finally:
            gen.set_output("".join(output_parts))
            _record_usage(gen, last_usage)


async def trace_generate_content_async(
    generate: Callable[..., Awaitable[Any]],
    /,
    *args: Any,
    bir_name: str = "google.generate_content",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Await an async Gemini ``generate_content`` and record one generation.

    The asynchronous counterpart of :func:`trace_generate_content` for the
    ``google-genai`` async client (``client.aio.models.generate_content``).
    ``generate`` returns a coroutine; it is awaited inside a single Bir
    ``generation`` event, arguments are forwarded unchanged, and the awaited
    response is returned. The model is taken from the request ``model`` keyword
    and usage from ``response.usage_metadata``. With ``stream=True`` the coroutine
    resolves to an async iterator that yields the provider's chunks unchanged and
    accumulates text and usage, finalizing when the stream is exhausted, closed,
    or raises.

    Like the sync wrapper this must run inside an active trace (for example an
    async ``@observe()`` function or ``async with bir.trace(...)``); the ``bir_``
    prefixed options never collide with Gemini ``generate_content`` keyword
    arguments such as ``config``.
    """

    metadata: dict[str, Any] = {"integration": "google"}
    if bir_metadata:
        metadata.update(bir_metadata)

    if kwargs.get("stream") is True:
        return _stream_generate_content_async(
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


async def _stream_generate_content_async(
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

        # Gemini chunks carry no top-level model, so the request ``model`` keyword
        # passed to ``generation()`` stands; only text and usage are accumulated.
        output_parts: list[str] = []
        last_usage: Any = None

        try:
            async for chunk in stream:
                text = _string_or_none(_value(chunk, "text"))
                if text is not None:
                    output_parts.append(text)

                usage = _value(chunk, "usage_metadata")
                if usage is not None:
                    last_usage = usage

                yield chunk
        finally:
            gen.set_output("".join(output_parts))
            _record_usage(gen, last_usage)


def _record_response(gen: Any, response: Any) -> None:
    gen.set_output(_response_output(response))
    # Gemini responses carry no top-level model, so the request ``model`` keyword
    # passed to ``generation()`` stands; only usage is refined from the response.
    _record_usage(gen, _value(response, "usage_metadata"))


def _record_usage(gen: Any, usage: Any) -> None:
    if usage is None:
        return
    input_tokens = _usage_tokens(usage, "prompt_token_count")
    output_tokens = _usage_tokens(usage, "candidates_token_count")
    total_tokens = _usage_tokens(usage, "total_token_count")
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return
    gen.set_usage(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)


def _request_input(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = dict(kwargs)
    if args:
        payload["args"] = list(args)
    return payload
