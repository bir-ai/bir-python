"""Thin Mistral chat integration for recording Bir generations.

The wrapper intentionally avoids importing the ``mistralai`` package so the Bir
SDK stays dependency-free. Applications pass a Mistral ``client.chat.complete``
callable; the wrapper invokes it inside a single Bir ``generation`` event and
reads the model and token usage from the OpenAI-shaped response when present.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from bir import generation

from ._common import _response_output, _string_or_none, _usage_tokens, _value


def trace_chat(
    complete: Callable[..., Any],
    /,
    *args: Any,
    bir_name: str = "mistral.chat",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Call a Mistral chat ``complete`` and record one Bir generation.

    ``complete`` is normally ``client.chat.complete``; positional and keyword
    arguments are forwarded to it unchanged and returned to the caller. The
    request is recorded as the generation input, then ``model`` and token
    ``usage`` are read from the OpenAI-shaped response when present.

    Like ``bir.generation()``, this must run inside an active trace (for example a
    ``@observe()`` function or ``with bir.trace(...)``). The wrapper's own options
    are prefixed ``bir_`` so they never collide with Mistral ``complete`` keyword
    arguments such as ``metadata``.
    """

    metadata: dict[str, Any] = {"integration": "mistral"}
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
        response = complete(*args, **kwargs)
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


def _request_input(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = dict(kwargs)
    if args:
        payload["args"] = list(args)
    return payload
