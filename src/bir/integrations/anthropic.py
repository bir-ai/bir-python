"""Thin Anthropic Messages integration for recording Bir generations.

The wrapper intentionally avoids importing the ``anthropic`` package so the Bir
SDK stays dependency-free. Applications pass an Anthropic ``messages.create``
callable; the wrapper invokes it inside a single Bir ``generation`` event and
reads the model and token usage from the response when they are present.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from bir import generation


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


def _response_output(response: Any) -> Any:
    model_dump = getattr(response, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    as_dict = getattr(response, "dict", None)
    if callable(as_dict):
        return as_dict()
    if isinstance(response, Mapping):
        return dict(response)
    return response


def _request_input(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = dict(kwargs)
    if args:
        payload["args"] = list(args)
    return payload


def _usage_tokens(usage: Any, *keys: str) -> int | float | None:
    for key in keys:
        value = _value(usage, key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return value
    return None


def _value(source: Any, key: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(key)
    return getattr(source, key, None)


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None
