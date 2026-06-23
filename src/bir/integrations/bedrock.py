"""Thin AWS Bedrock Converse integration for recording Bir generations.

The wrapper intentionally avoids importing ``boto3``/``botocore`` so the Bir SDK
stays dependency-free. Applications pass a Bedrock Runtime ``converse`` callable;
the wrapper invokes it inside a single Bir ``generation`` event and reads the
model from the request and token usage from the response when they are present.
The Converse stream API (``converse_stream``) returns a response whose ``stream``
member is an iterable of typed events, so :func:`trace_converse_stream` yields
those events unchanged and records the accumulated text and final usage once the
stream is consumed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from bir import generation

from ._common import (
    _is_streamed_response,
    _response_output,
    _string_or_none,
    _usage_tokens,
    _value,
)


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


def trace_converse_stream(
    converse_stream: Callable[..., Any],
    /,
    *args: Any,
    bir_name: str = "bedrock.converse_stream",
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Iterable[Any]:
    """Call a Bedrock Runtime ``converse_stream`` and record one Bir generation.

    ``converse_stream`` is normally ``client.converse_stream`` on a
    ``bedrock-runtime`` boto3 client; positional and keyword arguments are
    forwarded to it unchanged. Unlike :func:`trace_converse`, the wrapper returns
    a lazy iterable that yields the Converse stream events (the items of the
    response ``stream`` member) unchanged in order, so iterate it directly instead
    of reaching into ``response["stream"]``. The output is assembled from each
    ``contentBlockDelta.delta.text``, the stop reason is read from the
    ``messageStop`` event, and the final token usage is read from the terminal
    ``metadata`` event's ``usage`` block
    (``inputTokens``/``outputTokens``/``totalTokens``); these are finalized when
    the stream is exhausted, closed, or raises. The generation model comes from
    the request ``modelId`` keyword argument (Converse stream events carry no
    model). A response that is not an event stream (for example a stub that
    returned a one-shot Converse response) is recorded in one piece instead.

    Like ``bir.generation()``, this must run inside an active trace (for example a
    ``@observe()`` function or ``with bir.trace(...)``). The wrapper's own options
    are prefixed ``bir_`` so they never collide with Converse keyword arguments
    such as ``inferenceConfig`` or ``additionalModelRequestFields``.
    """

    metadata: dict[str, Any] = {"integration": "bedrock"}
    if bir_metadata:
        metadata.update(bir_metadata)

    return _stream_converse(
        converse_stream,
        args,
        kwargs,
        bir_name=bir_name,
        metadata=metadata,
        bir_capture_input=bir_capture_input,
        bir_capture_output=bir_capture_output,
    )


def _stream_converse(
    converse_stream: Callable[..., Any],
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
        model=_string_or_none(kwargs.get("modelId")),
        input=_request_input(args, kwargs),
        metadata=metadata,
        capture_input=bir_capture_input,
        capture_output=bir_capture_output,
    ) as gen:
        response = converse_stream(*args, **kwargs)
        stream = _value(response, "stream")
        if not _is_streamed_response(stream):
            # The call did not actually stream (a stub returned a whole Converse
            # response, or the ``stream`` member is absent); record it one-shot.
            _record_response(gen, response)
            return

        output_parts: list[str] = []
        last_usage: Any = None
        last_stop_reason: str | None = None

        try:
            for event in stream:
                text = _event_delta_text(event)
                if text is not None:
                    output_parts.append(text)

                stop_reason = _event_stop_reason(event)
                if stop_reason is not None:
                    last_stop_reason = stop_reason

                usage = _event_usage(event)
                if usage is not None:
                    last_usage = usage

                yield event
        finally:
            gen.set_output("".join(output_parts))
            if last_stop_reason is not None:
                gen.set_metadata({"stop_reason": last_stop_reason})
            _record_usage(gen, last_usage)


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


def _event_delta_text(event: Any) -> str | None:
    # Converse ``contentBlockDelta`` events carry incremental output text at
    # ``contentBlockDelta.delta.text``; the start/stop/metadata events expose no
    # such text.
    delta = _value(_value(event, "contentBlockDelta"), "delta")
    return _string_or_none(_value(delta, "text"))


def _event_stop_reason(event: Any) -> str | None:
    # The ``messageStop`` event marks the end of the assistant turn and carries
    # why generation stopped at ``messageStop.stopReason``.
    return _string_or_none(_value(_value(event, "messageStop"), "stopReason"))


def _event_usage(event: Any) -> Any:
    # The terminal ``metadata`` event carries token usage at ``metadata.usage``;
    # earlier events expose none, so the latest usage seen wins.
    return _value(_value(event, "metadata"), "usage")


def _request_input(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = dict(kwargs)
    if args:
        payload["args"] = list(args)
    return payload
