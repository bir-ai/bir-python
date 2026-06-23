"""Thin Google Vertex AI generative-models integration for recording Bir generations.

The wrapper intentionally avoids importing ``vertexai`` (the
``google-cloud-aiplatform`` package) so the Bir SDK stays dependency-free.
Applications pass a Vertex ``GenerativeModel.generate_content`` callable; the
wrapper invokes it inside a single Bir ``generation`` event and reads token usage
from the response when it is present. Passing ``stream=True`` (Vertex returns an
iterator of response chunks) makes the wrapper yield those chunks unchanged and
record the accumulated text and final ``usage_metadata`` once the stream is
consumed.
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


def trace_generate_content(
    generate: Callable[..., Any],
    /,
    *args: Any,
    bir_name: str = "vertexai.generate_content",
    bir_model: str | None = None,
    bir_metadata: Mapping[str, Any] | None = None,
    bir_capture_input: bool | None = None,
    bir_capture_output: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Call a Vertex ``generate_content`` and record one Bir generation.

    ``generate`` is normally ``model.generate_content`` for a
    ``vertexai.generative_models.GenerativeModel``; positional and keyword
    arguments are forwarded to it unchanged and returned to the caller. The
    request is recorded as the generation input and token usage is read from
    ``response.usage_metadata``
    (``prompt_token_count``/``candidates_token_count``/``total_token_count``) when
    present.

    Vertex binds the model to the ``GenerativeModel`` instance rather than passing
    it to ``generate_content``, so the model is not in the forwarded arguments.
    Pass ``bir_model`` to record which model was used; when the response carries a
    resolved ``model_version`` it refines that value.

    With ``stream=True`` (Vertex returns an iterator of ``GenerationResponse``
    chunks) the wrapper returns a lazy iterable that yields those chunks unchanged
    and assembles the output from each chunk's ``text`` (falling back to the first
    candidate's text parts), refining the model from a chunk ``model_version`` and
    reading the final ``usage_metadata`` when the stream is exhausted, closed, or
    raises.

    Like ``bir.generation()``, this must run inside an active trace (for example a
    ``@observe()`` function or ``with bir.trace(...)``). The wrapper's own options
    are prefixed ``bir_`` so they never collide with ``generate_content`` keyword
    arguments such as ``generation_config``. It is exported from
    ``bir.integrations`` as ``trace_vertex_generate_content`` so it does not
    collide with the Google Gemini wrapper of the same name.
    """

    metadata: dict[str, Any] = {"integration": "vertexai"}
    if bir_metadata:
        metadata.update(bir_metadata)

    if kwargs.get("stream") is True:
        return _stream_generate_content(
            generate,
            args,
            kwargs,
            bir_name=bir_name,
            bir_model=bir_model,
            metadata=metadata,
            bir_capture_input=bir_capture_input,
            bir_capture_output=bir_capture_output,
        )

    with generation(
        bir_name,
        model=_string_or_none(bir_model),
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
    bir_model: str | None,
    metadata: Mapping[str, Any],
    bir_capture_input: bool | None,
    bir_capture_output: bool | None,
) -> Iterable[Any]:
    with generation(
        bir_name,
        model=_string_or_none(bir_model),
        input=_request_input(args, kwargs),
        metadata=metadata,
        capture_input=bir_capture_input,
        capture_output=bir_capture_output,
    ) as gen:
        stream = generate(*args, **kwargs)
        if not _is_streamed_response(stream):
            # The provider ignored ``stream=True`` and returned one response;
            # record it via the one-shot path.
            _record_response(gen, stream)
            return

        output_parts: list[str] = []
        last_usage: Any = None

        try:
            for chunk in stream:
                response_model = _string_or_none(_value(chunk, "model_version"))
                if response_model is not None:
                    gen.model = response_model

                text = _chunk_text(chunk)
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
    # Vertex responses carry the resolved model on ``model_version`` in recent SDK
    # versions; prefer it over the caller-provided ``bir_model`` when present.
    response_model = _string_or_none(_value(response, "model_version"))
    if response_model is not None:
        gen.model = response_model
    _record_usage(gen, _value(response, "usage_metadata"))


def _record_usage(gen: Any, usage: Any) -> None:
    if usage is None:
        return
    input_tokens = _usage_tokens(usage, "prompt_token_count")
    output_tokens = _usage_tokens(usage, "candidates_token_count")
    total_tokens = _usage_tokens(usage, "total_token_count")
    # Vertex returns ``total_token_count``; ``set_usage`` derives it as a fallback
    # when the response carries only the two halves.
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return
    gen.set_usage(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)


def _chunk_text(chunk: Any) -> str | None:
    """Return the incremental output text from a Vertex streaming chunk.

    Vertex ``GenerationResponse`` chunks expose a ``text`` convenience accessor
    that concatenates the candidate parts' text; it is preferred when it yields a
    non-empty string. On a real response that accessor raises when the chunk has
    no single text part (for example a terminal chunk carrying only a finish
    reason or usage), so a failure falls back to reading the first candidate's
    text parts directly.
    """

    try:
        text = _value(chunk, "text")
    except Exception:
        text = None
    resolved = _string_or_none(text)
    if resolved is not None:
        return resolved
    return _candidate_text(chunk)


def _candidate_text(chunk: Any) -> str | None:
    candidates = _value(chunk, "candidates")
    if not isinstance(candidates, (list, tuple)) or not candidates:
        return None
    content = _value(candidates[0], "content")
    parts = _value(content, "parts")
    if not isinstance(parts, (list, tuple)):
        return None
    pieces = [piece for piece in (_string_or_none(_value(part, "text")) for part in parts) if piece]
    if not pieces:
        return None
    return "".join(pieces)


def _request_input(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = dict(kwargs)
    if args:
        payload["args"] = list(args)
    return payload
