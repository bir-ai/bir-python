"""Shared response-parsing helpers for the dependency-free provider integrations.

The wrappers read the model, token usage, and a serializable output from
whatever object the provider call returns, and those values use the same shapes
across providers. These small, side-effect-free helpers back that reading. The
module is private to ``bir.integrations``; nothing here is exported from the
package.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _value(source: Any, key: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(key)
    return getattr(source, key, None)


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _usage_tokens(usage: Any, *keys: str) -> int | float | None:
    for key in keys:
        value = _value(usage, key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return value
    return None


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


def _chunk_delta_content(chunk: Any) -> str | None:
    """Return the incremental text from an OpenAI-shaped streaming chunk.

    OpenAI Chat Completions, Mistral, and LiteLLM all emit chunks carrying output
    text at ``choices[0].delta.content``. Chunks without a content delta (the
    role-only opener, the usage-only final chunk, tool-call deltas) yield
    ``None`` so the caller can skip them while accumulating the response text.
    """

    choices = _value(chunk, "choices")
    if not isinstance(choices, list) or not choices:
        return None

    delta = _value(choices[0], "delta")
    content = _value(delta, "content")
    return _string_or_none(content)


def _is_streamed_response(response: Any) -> bool:
    """Return ``True`` when ``response`` looks like an iterable stream of chunks.

    A streaming provider call returns an iterator of chunk events, while a
    non-streaming call returns a single response object (typically a pydantic
    model exposing ``model_dump``, a mapping, or a string). Those whole-response
    shapes are rejected so a streaming wrapper can fall back to recording them in
    one piece when a provider ignores the streaming request.
    """

    if isinstance(response, (str, bytes, bytearray, Mapping)):
        return False
    model_dump = getattr(response, "model_dump", None)
    if callable(model_dump):
        return False
    try:
        iter(response)
    except TypeError:
        return False
    return True


def _is_async_streamed_response(response: Any) -> bool:
    """Return ``True`` when ``response`` is an async stream of chunk events.

    The async wrappers await the provider call and must then tell a real async
    stream (an ``AsyncStream`` exposing ``__aiter__``) from the single response
    object a provider returns when it ignores the streaming request. Only the
    async-iterator protocol is accepted; whole-response shapes (pydantic models,
    mappings, strings) expose no ``__aiter__`` and fall back to one-shot
    recording, mirroring :func:`_is_streamed_response` for the sync path.
    """

    return hasattr(response, "__aiter__")
