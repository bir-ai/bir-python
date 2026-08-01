"""Safe value capture, truncation, and additive secret redaction.

Dependency direction: this module depends only on :mod:`bir._config` and the
Python standard library. Callers provide the active ``_Config`` explicitly, so
capture never imports SDK runtime state and cannot form a cycle with it.
"""

from __future__ import annotations

import inspect
import math
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ._config import _Config

_MAX_CAPTURE_DEPTH = 6
_MAX_DEPTH_REACHED = "[max_depth]"
# Sentinel appended when an opt-in capture-size limit truncates a value: as a
# suffix on an over-long string and as an extra element/sentinel entry on an
# over-large list or mapping. Like the depth and redaction markers it keeps the
# captured value valid JSON while making the truncation visible.
_TRUNCATED = "…[truncated]"
_REDACTED = "[redacted]"

_SECRET_KEY_PARTS = (
    "access_key",
    "api_key",
    "apikey",
    "authorization",
    "auth_header",
    "client_secret",
    "password",
    "private_key",
    "secret",
    "token",
)
_SECRET_KEY_NAMES = {
    "auth",
    "credential",
    "credentials",
    "creds",
}


def _capture_call_input(
    signature: inspect.Signature,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    config: _Config,
) -> dict[str, Any]:
    bound = signature.bind_partial(*args, **kwargs)
    bound.apply_defaults()
    return {name: _safe_capture(value, config=config, key=name) for name, value in bound.arguments.items()}


def _safe_capture(
    value: Any,
    *,
    config: _Config,
    key: str | None = None,
    depth: int = 0,
) -> Any:
    if key is not None and _is_secret_key(key, config=config):
        return _REDACTED
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return _truncate_captured_text(_redact_secret_text(value, config=config), config=config)
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, Path):
        return _truncate_captured_text(_redact_secret_text(str(value), config=config), config=config)
    if depth >= _MAX_CAPTURE_DEPTH:
        return _MAX_DEPTH_REACHED
    if isinstance(value, Mapping):
        return _capture_mapping(value, depth, config=config)
    if isinstance(value, (list, tuple, set, frozenset)):
        return _capture_sequence(value, depth, config=config)
    return _truncate_captured_text(_safe_repr(value, config=config), config=config)


def _truncate_captured_text(text: str, *, config: _Config) -> str:
    """Bound an already-redacted captured string to ``max_value_length``.

    Truncation runs only on text that redaction has already processed, so a
    secret is always replaced before any cut and can never be split in a way
    that defeats the redactor. With no ``max_value_length`` configured (the
    default) the text is returned unchanged, so capture stays byte-for-byte
    identical unless a caller opts in.
    """

    limit = config.max_value_length
    if limit is None or len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATED


def _capture_mapping(value: Mapping[Any, Any], depth: int, *, config: _Config) -> dict[str, Any]:
    """Capture a mapping, bounding entry count by ``max_collection_items``."""

    limit = config.max_collection_items
    captured: dict[str, Any] = {}
    truncated = False
    for index, (item_key, item_value) in enumerate(value.items()):
        if limit is not None and index >= limit:
            truncated = True
            break
        item_key_text = _safe_key(item_key)
        captured[item_key_text] = _safe_capture(
            item_value,
            config=config,
            key=item_key_text,
            depth=depth + 1,
        )
    if truncated:
        captured[_TRUNCATED] = _TRUNCATED
    return captured


def _capture_sequence(value: Iterable[Any], depth: int, *, config: _Config) -> list[Any]:
    """Capture a sequence, bounding item count by ``max_collection_items``."""

    limit = config.max_collection_items
    captured: list[Any] = []
    truncated = False
    for index, item in enumerate(value):
        if limit is not None and index >= limit:
            truncated = True
            break
        captured.append(_safe_capture(item, config=config, depth=depth + 1))
    if truncated:
        captured.append(_TRUNCATED)
    return captured


def _is_secret_key(key: str, *, config: _Config) -> bool:
    normalized = key.lower().replace("-", "_")
    # User-supplied keys are stored already normalized to this same form, so they
    # join the built-in set as exact whole-name matches and only add coverage.
    if normalized in _SECRET_KEY_NAMES or normalized in config.additional_secret_keys:
        return True
    return any(secret_part in normalized for secret_part in _SECRET_KEY_PARTS)


def _safe_key(value: Any) -> str:
    try:
        return str(value)
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"


def _safe_repr(value: Any, *, config: _Config) -> str:
    try:
        return _redact_secret_text(repr(value), config=config)
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"


def _safe_error(exc: BaseException, *, config: _Config) -> str:
    return _redact_secret_text(str(exc), config=config)


def _redact_secret_text(value: str, *, config: _Config) -> str:
    redacted = value
    redacted = re.sub(
        r"(?i)\b(authorization\s*[:=]\s*)(bearer\s+)?(?!\[redacted\])[^\s,;\)\]\}]+",
        _redact_labeled_secret_match,
        redacted,
    )
    redacted = re.sub(
        (
            r"(?i)\b(access[_-]?key|api[_-]?key|apikey|auth|client[_-]?secret|credential|credentials|password|"
            r"private[_-]?key|secret|token)(\s*[:=]\s*)(?!\[redacted\])(?!\{[A-Za-z_][A-Za-z0-9_]*\})"
            r"[^\s,;\)\]\}]+"
        ),
        _redact_labeled_secret_match,
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b(bearer\s+)(?!\[redacted\])[^\s,;\)\]\}]+",
        _redact_bearer_secret_match,
        redacted,
    )
    redacted = re.sub(r"\b(sk-[A-Za-z0-9_-]{4,})\b", _REDACTED, redacted)
    redacted = re.sub(
        r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?![A-Za-z0-9_-])",
        _REDACTED,
        redacted,
    )
    redacted = re.sub(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", _REDACTED, redacted)
    redacted = re.sub(r"(?<![0-9A-Za-z_-])AIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])", _REDACTED, redacted)
    redacted = re.sub(r"\bxox[baprs]-[0-9A-Za-z-]+\b", _REDACTED, redacted)
    redacted = re.sub(r"\b(?:ghp|gho|ghs|ghu|ghr)_[0-9A-Za-z]{36,}\b", _REDACTED, redacted)
    redacted = re.sub(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}\b", _REDACTED, redacted)
    redacted = re.sub(r"(?<![0-9A-Za-z+/])[0-9A-Za-z+/]{86}==(?![0-9A-Za-z+/=])", _REDACTED, redacted)
    redacted = re.sub(
        r"(?s)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        _REDACTED,
        redacted,
    )
    redacted = re.sub(r"\b(?:\d[ -]?){12,18}\d\b", _redact_pan_match, redacted)
    # User-supplied patterns run last and only add redaction coverage.
    for pattern in config.additional_redaction_patterns:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted


def _redact_labeled_secret_match(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2) or ''}{_REDACTED}"


def _redact_bearer_secret_match(match: re.Match[str]) -> str:
    return f"{match.group(1)}{_REDACTED}"


def _redact_pan_match(match: re.Match[str]) -> str:
    """Redact a candidate card number only when its digits pass Luhn."""

    candidate = match.group(0)
    digits = candidate.replace(" ", "").replace("-", "")
    if 13 <= len(digits) <= 19 and _luhn_checksum_valid(digits):
        return _REDACTED
    return candidate


def _luhn_checksum_valid(digits: str) -> bool:
    """Return whether a bare digit string passes the Luhn checksum."""

    total = 0
    for index, char in enumerate(reversed(digits)):
        value = ord(char) - 48
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0
