"""Thin Instructor integration for recording Bir generations.

The wrappers intentionally avoid importing the ``instructor`` or ``openai``
packages so the Bir SDK stays dependency-free. Applications pass an
Instructor-patched client's ``chat.completions.create`` callable; the matching
wrapper invokes it inside a single Bir ``generation`` event and reads the model
and token usage from the raw completion when present.

Instructor can return either the parsed Pydantic model directly (the default
``create`` path) or a ``(parsed_model, raw_completion)`` tuple (the
``create_with_completion`` path). Both shapes are handled: when a tuple is
returned the raw completion supplies model and usage; otherwise usage is read
from the response object itself following the OpenAI-shaped usage block.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from bir import generation

from ._common import _response_output, _string_or_none, _usage_tokens, _value


def trace_create(
    create: Callable[..., Any],
    /,
    *args: Any,
    bir_name: str = "instructor.create",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Call an Instructor-patched ``create`` and record one Bir generation.

    ``create`` is normally ``client.chat.completions.create`` on an
    Instructor-patched client. Positional and keyword arguments are forwarded
    unchanged and the provider result is returned to the caller.

    Instructor may return the parsed Pydantic model directly or a
    ``(parsed_model, raw_completion)`` tuple (``create_with_completion``
    pattern). Both shapes are handled: the tuple's raw completion supplies
    usage and model; the direct shape is read the same way.

    Like ``bir.generation()``, this must run inside an active trace (for example
    a ``@observe()`` function or ``with bir.trace(...)``). The wrapper's own
    options are prefixed ``bir_`` so they never collide with provider keyword
    arguments such as ``metadata``.
    """

    metadata: dict[str, Any] = {"integration": "instructor"}
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
        result = create(*args, **kwargs)
        _record_result(gen, result)
        return result


async def trace_create_async(
    create: Callable[..., Awaitable[Any]],
    /,
    *args: Any,
    bir_name: str = "instructor.create",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Await an async Instructor-patched ``create`` and record one generation.

    The asynchronous counterpart of :func:`trace_create` for async clients.
    ``create`` is normally ``client.chat.completions.create`` on an async
    Instructor-patched client, returning a coroutine. It is awaited inside a
    single Bir ``generation`` event and the awaited result is returned.

    Like the sync wrapper this must run inside an active trace; the ``bir_``
    prefixed options never collide with provider keyword arguments.
    """

    metadata: dict[str, Any] = {"integration": "instructor"}
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
        result = await create(*args, **kwargs)
        _record_result(gen, result)
        return result


def _record_result(gen: Any, result: Any) -> None:
    """Read model, output, and usage from a direct or tuple Instructor result."""

    if isinstance(result, tuple) and len(result) == 2:
        parsed, completion = result
        response_model = _string_or_none(_value(completion, "model"))
        if response_model is not None:
            gen.model = response_model
        gen.set_output(_response_output(parsed))
        _record_usage(gen, _value(completion, "usage"))
    else:
        response_model = _string_or_none(_value(result, "model"))
        if response_model is not None:
            gen.model = response_model
        gen.set_output(_response_output(result))
        _record_usage(gen, _value(result, "usage"))


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
