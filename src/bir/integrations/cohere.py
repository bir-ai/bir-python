"""Thin Cohere chat integration for recording Bir generations.

The wrapper intentionally avoids importing the ``cohere`` package so the Bir SDK
stays dependency-free. Applications pass a Cohere v2 ``client.chat`` callable;
the wrapper invokes it inside a single Bir ``generation`` event and reads token
usage from the nested Cohere response shape when present.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from bir import generation

from ._common import _response_output, _string_or_none, _usage_tokens, _value


def trace_chat(
    chat: Callable[..., Any],
    /,
    *args: Any,
    bir_name: str = "cohere.chat",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Call a Cohere v2 ``chat`` callable and record one Bir generation.

    ``chat`` is normally ``client.chat``; positional and keyword arguments are
    forwarded to it unchanged and returned to the caller. The request is recorded
    as the generation input, the generation model comes from the request
    ``model`` keyword argument, and token usage is read from
    ``response.usage.tokens`` when present.

    Like ``bir.generation()``, this must run inside an active trace (for example a
    ``@observe()`` function or ``with bir.trace(...)``). The wrapper's own options
    are prefixed ``bir_`` so they never collide with Cohere ``chat`` keyword
    arguments such as ``metadata``.
    """

    metadata: dict[str, Any] = {"integration": "cohere"}
    if bir_metadata:
        metadata.update(bir_metadata)

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


async def trace_chat_async(
    chat: Callable[..., Awaitable[Any]],
    /,
    *args: Any,
    bir_name: str = "cohere.chat",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Await an async Cohere v2 ``chat`` callable and record one generation.

    The asynchronous counterpart of :func:`trace_chat` for ``AsyncClientV2``
    (``client.chat``). ``chat`` returns a coroutine; it is awaited inside a single
    Bir ``generation`` event, arguments are forwarded unchanged, and the awaited
    response is returned. The model comes from the request ``model`` keyword and
    token usage is read from ``response.usage.tokens`` when present.

    Like the sync wrapper this must run inside an active trace (for example an
    async ``@observe()`` function or ``async with bir.trace(...)``); the ``bir_``
    prefixed options never collide with Cohere ``chat`` keyword arguments.
    """

    metadata: dict[str, Any] = {"integration": "cohere"}
    if bir_metadata:
        metadata.update(bir_metadata)

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


def _record_response(gen: Any, response: Any) -> None:
    gen.set_output(_response_output(response))
    _record_usage(gen, _value(response, "usage"))


def _record_usage(gen: Any, usage: Any) -> None:
    if usage is None:
        return
    tokens = _value(usage, "tokens")
    if tokens is None:
        return
    input_tokens = _usage_tokens(tokens, "input_tokens")
    output_tokens = _usage_tokens(tokens, "output_tokens")
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
