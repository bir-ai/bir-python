"""Thin OpenAI chat-completions integration for recording Bir generations.

The wrapper intentionally avoids importing the ``openai`` package so the Bir SDK
stays dependency-free. Applications pass an OpenAI ``chat.completions.create``
callable; the wrapper invokes it inside a single Bir ``generation`` event and
reads the model and token usage from the response when they are present.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from bir import generation

from ._common import _is_streamed_response, _response_output, _string_or_none, _usage_tokens, _value


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


def _chunk_delta_content(chunk: Any) -> str | None:
    choices = _value(chunk, "choices")
    if not isinstance(choices, list) or not choices:
        return None

    delta = _value(choices[0], "delta")
    content = _value(delta, "content")
    return _string_or_none(content)


def _request_input(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = dict(kwargs)
    if args:
        payload["args"] = list(args)
    return payload
