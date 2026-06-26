"""Thin DSPy integration for recording Bir generations.

The wrapper intentionally avoids importing the ``dspy`` package so the Bir SDK
stays dependency-free. DSPy programs route every language-model call through a
``dspy.LM`` instance whose underlying request method (``LM.forward``, also
exposed as ``LM.request`` on older versions) returns the raw LiteLLM-style
response — an OpenAI-shaped object carrying ``model`` and a token ``usage``
block. Applications pass that bound method; the wrapper invokes it inside a
single Bir ``generation`` event and reads the model and token usage from the
returned response when present.

The request-time model is read from the bound ``LM`` instance (DSPy stores it on
``lm.model``) or an explicit ``model`` keyword, then refined from the response's
own ``model`` when the provider echoes one back. ``dspy`` is never imported.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from bir import generation

from ._common import _response_output, _string_or_none, _usage_tokens, _value


def trace_lm(
    lm_call: Callable[..., Any],
    /,
    *args: Any,
    bir_name: str = "dspy.lm",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Call a DSPy LM request and record one Bir generation.

    ``lm_call`` is normally a ``dspy.LM`` instance's ``forward`` method (for
    example ``lm.forward``); positional and keyword arguments are forwarded to it
    unchanged and the provider response is returned to the caller. The request is
    recorded as the generation input, then ``model`` and token ``usage`` are read
    from the LiteLLM-shaped response when present.

    The request model is taken from the bound ``LM`` instance (``lm.model``) or an
    explicit ``model`` keyword, and refined from the response's ``model`` when the
    provider echoes one back.

    Like ``bir.generation()``, this must run inside an active trace (for example a
    ``@observe()`` function or ``with bir.trace(...)``). The wrapper's own options
    are prefixed ``bir_`` so they never collide with DSPy LM keyword arguments.
    """

    metadata: dict[str, Any] = {"integration": "dspy"}
    if bir_metadata:
        metadata.update(bir_metadata)

    with generation(
        bir_name,
        model=_request_model(lm_call, kwargs),
        input=_request_input(args, kwargs),
        metadata=metadata,
        capture_input=bir_capture_input,
        capture_output=bir_capture_output,
    ) as gen:
        response = lm_call(*args, **kwargs)
        _record_response(gen, response)
        return response


async def trace_lm_async(
    lm_call: Callable[..., Awaitable[Any]],
    /,
    *args: Any,
    bir_name: str = "dspy.lm",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Await an async DSPy LM request and record one Bir generation.

    The asynchronous counterpart of :func:`trace_lm` for DSPy's async request
    method (``LM.aforward``). ``lm_call`` returns a coroutine; it is awaited
    inside a single Bir ``generation`` event, arguments are forwarded unchanged,
    and the awaited response is returned. ``model`` and token ``usage`` are read
    from the LiteLLM-shaped response the same way as the sync wrapper.

    Like the sync wrapper this must run inside an active trace; the ``bir_``
    prefixed options never collide with DSPy LM keyword arguments.
    """

    metadata: dict[str, Any] = {"integration": "dspy"}
    if bir_metadata:
        metadata.update(bir_metadata)

    async with generation(
        bir_name,
        model=_request_model(lm_call, kwargs),
        input=_request_input(args, kwargs),
        metadata=metadata,
        capture_input=bir_capture_input,
        capture_output=bir_capture_output,
    ) as gen:
        response = await lm_call(*args, **kwargs)
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


def _request_model(lm_call: Callable[..., Any], kwargs: Mapping[str, Any]) -> str | None:
    """Read the request model from an explicit kwarg or the bound LM instance.

    DSPy binds the model to the ``LM`` instance (``lm.model``) rather than passing
    it per call, so when ``lm_call`` is a bound method its ``__self__`` carries the
    model. An explicit ``model`` keyword still wins when provided.
    """

    model = _string_or_none(kwargs.get("model"))
    if model is not None:
        return model
    instance = getattr(lm_call, "__self__", None)
    return _string_or_none(_value(instance, "model"))


def _request_input(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = dict(kwargs)
    if args:
        payload["args"] = list(args)
    return payload
