"""Thin LiteLLM integration for recording Bir generations.

The wrapper intentionally avoids importing the ``litellm`` package so the Bir
SDK stays dependency-free. Applications pass the ``litellm.completion``
callable; the wrapper invokes it inside a single Bir ``generation`` event and
reads the model and token usage from the OpenAI-shaped response when present.

LiteLLM routes one ``completion`` call to many providers, so the provider is
derived from the request ``model`` id (for example ``anthropic/claude-3-5-sonnet``)
and recorded as ``metadata["provider"]`` for the dashboard's provider breakdown.
LiteLLM normalizes streamed chunks to the OpenAI shape, so ``stream=True`` records
the accumulated text and final usage after the stream is consumed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from bir import generation

from ._common import (
    _chunk_delta_content,
    _is_streamed_response,
    _response_output,
    _string_or_none,
    _usage_tokens,
    _value,
)


def trace_completion(
    completion: Callable[..., Any],
    /,
    *args: Any,
    bir_name: str = "litellm.completion",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Call ``litellm.completion`` and record one Bir generation.

    ``completion`` is normally ``litellm.completion``; positional and keyword
    arguments are forwarded to it unchanged and returned to the caller. The
    request is recorded as the generation input, then ``model`` and token
    ``usage`` are read from the OpenAI-shaped response when present. The provider
    is derived from the request ``model`` id (the prefix before the first ``/``)
    and recorded as ``metadata["provider"]``.

    With ``stream=True`` the wrapper returns a lazy iterable that yields the
    provider's chunks unchanged and assembles the output from
    ``choices[0].delta.content``, finalizing the model, output, and usage when the
    stream is exhausted, closed, or raises.

    Like ``bir.generation()``, this must run inside an active trace (for example a
    ``@observe()`` function or ``with bir.trace(...)``). The wrapper's own options
    are prefixed ``bir_`` so they never collide with LiteLLM ``completion``
    keyword arguments such as ``metadata``.
    """

    metadata: dict[str, Any] = {"integration": "litellm"}
    provider = _provider_hint(kwargs.get("model"))
    if provider is not None:
        metadata["provider"] = provider
    if bir_metadata:
        metadata.update(bir_metadata)

    if kwargs.get("stream") is True:
        return _stream_completion(
            completion,
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
        response = completion(*args, **kwargs)
        _record_response(gen, response)
        return response


def _stream_completion(
    completion: Callable[..., Any],
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
        stream = completion(*args, **kwargs)
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


async def trace_completion_async(
    completion: Callable[..., Awaitable[Any]],
    /,
    *args: Any,
    bir_name: str = "litellm.completion",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Await ``litellm.acompletion`` and record one Bir generation.

    The asynchronous counterpart of :func:`trace_completion` for
    ``litellm.acompletion``. ``completion`` returns a coroutine; it is awaited
    inside a single Bir ``generation`` event, arguments are forwarded unchanged,
    and the awaited response is returned. ``model`` and token ``usage`` are read
    from the OpenAI-shaped response when present, and the provider is derived from
    the request ``model`` id prefix and recorded as ``metadata["provider"]``.

    Like the sync wrapper this must run inside an active trace (for example an
    async ``@observe()`` function or ``async with bir.trace(...)``); the ``bir_``
    prefixed options never collide with LiteLLM ``completion`` keyword arguments.
    """

    metadata: dict[str, Any] = {"integration": "litellm"}
    provider = _provider_hint(kwargs.get("model"))
    if provider is not None:
        metadata["provider"] = provider
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
        response = await completion(*args, **kwargs)
        _record_response(gen, response)
        return response


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


def _provider_hint(model: Any) -> str | None:
    name = _string_or_none(model)
    if name is None:
        return None
    # LiteLLM model ids are typically "<provider>/<model>"; the prefix before the
    # first "/" is the provider. Bare ids (no "/") carry no provider hint.
    prefix, separator, _remainder = name.partition("/")
    if separator and prefix:
        return prefix
    return None


def _request_input(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = dict(kwargs)
    if args:
        payload["args"] = list(args)
    return payload
