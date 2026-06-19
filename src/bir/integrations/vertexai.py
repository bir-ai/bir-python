"""Thin Google Vertex AI generative-models integration for recording Bir generations.

The wrapper intentionally avoids importing ``vertexai`` (the
``google-cloud-aiplatform`` package) so the Bir SDK stays dependency-free.
Applications pass a Vertex ``GenerativeModel.generate_content`` callable; the
wrapper invokes it inside a single Bir ``generation`` event and reads token usage
from the response when it is present.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from bir import generation

from ._common import _response_output, _string_or_none, _usage_tokens, _value


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


def _request_input(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = dict(kwargs)
    if args:
        payload["args"] = list(args)
    return payload
