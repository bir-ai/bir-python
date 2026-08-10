"""Shared response-parsing helpers for the dependency-free provider integrations.

The wrappers read the model, token usage, and a serializable output from
whatever object the provider call returns, and those values use the same shapes
across providers. These small, side-effect-free helpers back that reading. The
module is private to ``bir.integrations``; nothing here is exported from the
package.

Everything here reads an object the provider owns, which means it runs code Bir
did not write: a property that computes, a ``model_dump`` that serializes, a
mapping that loads lazily. None of that may decide whether the traced call
succeeded. The call has already returned by the time these run, and reading its
result to make a record is bookkeeping about the call rather than part of it, so
every read that can execute somebody else's code is guarded and falls back to
what is still known. This is the same rule the store write and
:func:`bir._capture._safe_capture` already follow, applied at the point where a
third party's object is first touched.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _value(source: Any, key: str) -> Any:
    try:
        if isinstance(source, Mapping):
            return source.get(key)
        return getattr(source, key, None)
    except Exception:
        # ``getattr`` absorbs a missing attribute; a property that raises is a
        # different thing, and it is the provider's code, not a missing value.
        return None


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _text(value: Any) -> str | None:
    """Return ``str(value)``, or ``None`` when the value's own ``__str__`` fails.

    Rendering a provider's enum member or a framework's event key runs code Bir
    did not write, exactly as reading an attribute does, so it gets the same
    treatment: something that cannot say what it is contributes nothing rather
    than failing the call being recorded.
    """

    try:
        return str(value)
    except Exception:
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
    """Render the provider's response as something recordable.

    Each conversion is the provider's own code. When one raises, the response
    object itself is returned rather than a marker: capture will fall back to its
    ``repr`` under its own guard, so a response that cannot serialize still
    records what it can instead of recording nothing.
    """

    model_dump = _value(response, "model_dump")
    if callable(model_dump):
        try:
            return model_dump()
        except Exception:
            return response
    as_dict = _value(response, "dict")
    if callable(as_dict):
        try:
            return as_dict()
        except Exception:
            return response
    if isinstance(response, Mapping):
        try:
            return dict(response)
        except Exception:
            return response
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
    if callable(_value(response, "model_dump")):
        return False
    try:
        iter(response)
    except TypeError:
        return False
    except Exception:
        # ``__iter__`` is the provider's code too. Something that raises for a
        # reason other than not being iterable is not a stream this can read, so
        # it is recorded in one piece rather than failing the call.
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

    # ``hasattr`` absorbs only ``AttributeError``, so a ``__aiter__`` defined as
    # a property that raises would otherwise reach the caller.
    try:
        return hasattr(response, "__aiter__")
    except Exception:
        return False
