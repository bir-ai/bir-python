"""Thin AWS Bedrock Converse integration for recording Bir generations.

The wrapper intentionally avoids importing ``boto3``/``botocore`` so the Bir SDK
stays dependency-free. Applications pass a Bedrock Runtime ``converse`` callable;
the wrapper invokes it inside a single Bir ``generation`` event and reads the
model from the request and token usage from the response when they are present.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from bir import generation

from ._common import _response_output, _string_or_none, _usage_tokens, _value


def trace_converse(
    converse: Callable[..., Any],
    /,
    *args: Any,
    bir_name: str = "bedrock.converse",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Call a Bedrock Runtime ``converse`` and record one Bir generation.

    ``converse`` is normally ``client.converse`` on a ``bedrock-runtime`` boto3
    client; positional and keyword arguments are forwarded to it unchanged and
    returned to the caller. The request is recorded as the generation input, the
    generation model comes from the request ``modelId`` keyword argument (Converse
    responses carry no model), and token usage is read from the response ``usage``
    block (``inputTokens``/``outputTokens``/``totalTokens``) when present.

    Like ``bir.generation()``, this must run inside an active trace (for example a
    ``@observe()`` function or ``with bir.trace(...)``). The wrapper's own options
    are prefixed ``bir_`` so they never collide with Converse keyword arguments
    such as ``inferenceConfig`` or ``additionalModelRequestFields``.
    """

    metadata: dict[str, Any] = {"integration": "bedrock"}
    if bir_metadata:
        metadata.update(bir_metadata)

    with generation(
        bir_name,
        model=_string_or_none(kwargs.get("modelId")),
        input=_request_input(args, kwargs),
        metadata=metadata,
        capture_input=bir_capture_input,
        capture_output=bir_capture_output,
    ) as gen:
        response = converse(*args, **kwargs)
        _record_response(gen, response)
        return response


def _record_response(gen: Any, response: Any) -> None:
    gen.set_output(_response_output(response))
    # Converse responses carry no model, so the request ``modelId`` keyword passed
    # to ``generation()`` stands; only usage is read from the response.
    _record_usage(gen, _value(response, "usage"))


def _record_usage(gen: Any, usage: Any) -> None:
    if usage is None:
        return
    input_tokens = _usage_tokens(usage, "inputTokens")
    output_tokens = _usage_tokens(usage, "outputTokens")
    total_tokens = _usage_tokens(usage, "totalTokens")
    # Converse returns ``totalTokens``; ``set_usage`` derives it as a fallback when
    # the response carries only the two halves.
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return
    gen.set_usage(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)


def _request_input(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = dict(kwargs)
    if args:
        payload["args"] = list(args)
    return payload
