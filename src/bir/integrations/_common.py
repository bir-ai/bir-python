"""Shared response-parsing helpers for the OpenAI, Anthropic, and Google integrations.

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
