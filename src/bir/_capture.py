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
# Recorded in place of a value whose own code raised while it was being
# captured, and appended to a container whose walk failed part-way -- the same
# shape ``_TRUNCATED`` uses for the other reason a walk stops early. Capture runs
# code Bir does not own, and recording a value must never decide whether the
# traced call succeeds, so every failure ends at this marker.
_UNCAPTURABLE = "[uncapturable]"

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

# The two halves of a PEM private-key block, matched separately so the block can
# be located without a quantifier that spans them. See
# :func:`_redact_private_key_blocks`. Neither label class can contain ``-``, so a
# footer can never fall inside a header's variable part.
_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")

# How far a secret runs once something has labeled it. It ends at whitespace or
# at punctuation that closes the value rather than belonging to it, so a header
# quoted inside a sentence does not swallow the sentence.
_SECRET_VALUE = r"[^\s,;\)\]\}]+"

# RFC 7235 writes an Authorization header as a scheme followed by the credential.
# Only the credential is secret; the scheme says which authentication a call
# used, which is worth keeping in a trace. These schemes carry a single
# ``token68`` credential.
_AUTH_SCHEME_TOKEN = r"bearer|basic|token|api-?key|ntlm|negotiate|ssws"
# These carry a comma-separated auth-param list instead. The part worth hiding is
# not the first pair -- a Digest ``response`` and a SigV4 ``Signature`` come last
# -- so the whole list goes rather than its head.
_AUTH_SCHEME_PARAMS = r"digest|hawk|signature|aws4-hmac-sha256"
_AUTH_ANY_SCHEME = f"{_AUTH_SCHEME_TOKEN}|{_AUTH_SCHEME_PARAMS}"

# One pattern rather than two, so a long captured value is scanned for the header
# once. The scheme group in the second branch stays optional so a bare
# ``Authorization: <token>`` is still covered, and the trailing lookahead is what
# keeps that optionality honest: without it the engine backtracks to an empty
# scheme and matches the scheme word itself, which destroyed the scheme and left
# the credential behind it untouched. That is also what makes the rule idempotent
# -- re-redacting an already-redacted header used to consume its scheme.
# A credential carried in a URI's userinfo, the spelling a connection string
# uses. The password is the secret; the scheme, user, and host are what make the
# trace worth reading, so they stay. Requiring the ``:`` and the ``@`` is what
# keeps an ordinary ``host:port`` and a passwordless ``https://user@host`` out of
# it -- neither carries a password to hide.
#
# It starts at ``://`` rather than at the scheme, and that is a cost decision
# rather than a stylistic one. Written as ``[A-Za-z][A-Za-z0-9+.-]*://`` the rule
# is quadratic: every letter in the value starts an attempt, and each one runs
# the greedy class to the end of its run and backtracks over it looking for the
# ``://``. Measured on one 64,000-character alphanumeric run -- the shape a
# base64 body has -- that spelling took 4,745 ms and grew 60x for 8x the input,
# against 0.03 ms and 7x here. The scheme is left out of the match entirely; it
# sits in front of it and is never replaced, so the result is identical.
_URI_CREDENTIAL_RULE = re.compile(r"(://[^\s/:@]*:)[^\s/@]*(@)")

_AUTH_HEADER_RULE = re.compile(
    rf"(?i)\b(?P<label>authorization\s*[:=]\s*)(?:"
    rf"(?P<param_scheme>(?:{_AUTH_SCHEME_PARAMS})\s+)"
    rf"(?!\[redacted\]){_SECRET_VALUE}(?:\s*,\s*{_SECRET_VALUE})*"
    rf"|"
    rf"(?P<token_scheme>(?:{_AUTH_SCHEME_TOKEN})\s+)?"
    rf"(?!\[redacted\])(?!(?:{_AUTH_ANY_SCHEME})\s){_SECRET_VALUE}"
    rf")"
)


def _capture_call_input(
    signature: inspect.Signature,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    config: _Config,
) -> dict[str, Any]:
    """Name the decorated call's arguments for recording, without refusing the call.

    ``inspect.signature`` follows ``__wrapped__``, so ``@observe`` over a
    decorator that widens its wrapper's signature is handed the narrow one and
    cannot bind a call the function itself accepts. Naming the arguments is
    bookkeeping; it must not turn a working call into a ``TypeError``.
    """

    try:
        bound = signature.bind_partial(*args, **kwargs)
        bound.apply_defaults()
    except Exception:
        return {_UNCAPTURABLE: _UNCAPTURABLE}
    return {name: _safe_capture(value, config=config, key=name) for name, value in bound.arguments.items()}


def _safe_capture(
    value: Any,
    *,
    config: _Config,
    key: str | None = None,
    depth: int = 0,
) -> Any:
    """Capture ``value`` for recording, never raising into the traced call.

    Capturing runs code the captured object owns -- a mapping's ``items()``, a
    sequence's ``__iter__``, a subclass's ``__len__`` -- none of which is Bir's to
    trust. Recording a value must never decide whether the traced call succeeds
    or what it returns, so a failure anywhere below is recorded as
    :data:`_UNCAPTURABLE` instead of propagating. This is the guard
    :func:`_safe_repr` has always had around ``__repr__``, applied to the rest of
    the capture path. Container walks keep what they already read (see
    :func:`_capture_mapping`), so this whole-value marker is the last resort.
    """

    try:
        return _capture_value(value, config=config, key=key, depth=depth)
    except Exception:
        return _UNCAPTURABLE


def _safe_metadata(value: Any, *, field: str) -> dict[str, Any]:
    """Snapshot a caller-supplied metadata mapping without letting it raise.

    Every ``metadata=`` argument is copied before it is captured, and copying one
    runs its own ``keys()``/``__getitem__``. A non-mapping stays a caller mistake
    and still raises, matching the checks ``observe()`` and ``score()`` already
    make; a mapping that fails while being read is recorded as uncapturable,
    because recording metadata must not decide whether the traced call succeeds.
    Only the snapshot happens here -- the values are captured later by
    :func:`_safe_capture`, which redacts them.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"bir {field} must be a mapping")
    try:
        return dict(value)
    except Exception:
        return {_UNCAPTURABLE: _UNCAPTURABLE}


def _capture_value(
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
    """Capture a mapping, bounding entry count by ``max_collection_items``.

    Walking the mapping runs its own ``items()`` and the iterator that returns,
    which may do I/O or fail outright (a config client, a lazily-loading row
    proxy). A walk that fails keeps the entries it already read and marks the
    rest uncapturable, the same way the entry-count limit marks what it left out.
    """

    limit = config.max_collection_items
    captured: dict[str, Any] = {}
    truncated = False
    uncapturable = False
    try:
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
    except Exception:
        uncapturable = True
    if truncated:
        captured[_TRUNCATED] = _TRUNCATED
    if uncapturable:
        captured[_UNCAPTURABLE] = _UNCAPTURABLE
    return captured


def _capture_sequence(value: Iterable[Any], depth: int, *, config: _Config) -> list[Any]:
    """Capture a sequence, bounding item count by ``max_collection_items``.

    Iteration is the sequence's own code, so a walk that fails keeps the items it
    already read and marks the rest uncapturable, mirroring
    :func:`_capture_mapping`.
    """

    limit = config.max_collection_items
    captured: list[Any] = []
    truncated = False
    uncapturable = False
    try:
        for index, item in enumerate(value):
            if limit is not None and index >= limit:
                truncated = True
                break
            captured.append(_safe_capture(item, config=config, depth=depth + 1))
    except Exception:
        uncapturable = True
    if truncated:
        captured.append(_TRUNCATED)
    if uncapturable:
        captured.append(_UNCAPTURABLE)
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
    """Render an exception's message for recording, without raising.

    ``str(exc)`` runs the exception's own ``__str__``. Letting that raise would
    replace the exception the traced call is already propagating with one of
    Bir's, so a failure records the exception's type instead -- the same fallback
    :func:`_safe_repr` uses when ``__repr__`` fails.
    """

    try:
        message = str(exc)
    except Exception:
        return f"<unrepresentable {type(exc).__name__}>"
    return _redact_secret_text(message, config=config)


def _redact_secret_text(value: str, *, config: _Config) -> str:
    redacted = value
    redacted = _AUTH_HEADER_RULE.sub(_redact_auth_header_match, redacted)
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
    # Before the token shapes below, so a token used as the *user* half of a URI
    # (the ``<token>:x-oauth-basic@`` convention) is still matched by its own rule
    # once the password beside it has gone.
    redacted = _URI_CREDENTIAL_RULE.sub(_redact_uri_credential_match, redacted)
    redacted = re.sub(r"\b(sk-[A-Za-z0-9_-]{4,})\b", _REDACTED, redacted)
    redacted = re.sub(
        r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?![A-Za-z0-9_-])",
        _REDACTED,
        redacted,
    )
    redacted = re.sub(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", _REDACTED, redacted)
    redacted = re.sub(r"(?<![0-9A-Za-z_-])AIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])", _REDACTED, redacted)
    redacted = re.sub(r"\bxox[baprs]-[0-9A-Za-z-]+\b", _REDACTED, redacted)
    # Both GitHub token families in one pass: the classic ``ghp_``-style prefixes
    # and the fine-grained ``github_pat_`` tokens that replaced them. The
    # fine-grained form joins its two halves with an underscore, so it needs a
    # character class the classic branch must not have.
    redacted = re.sub(
        r"\b(?:(?:ghp|gho|ghs|ghu|ghr)_[0-9A-Za-z]{36,}|github_pat_[0-9A-Za-z_]{50,})\b",
        _REDACTED,
        redacted,
    )
    redacted = re.sub(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}\b", _REDACTED, redacted)
    redacted = re.sub(r"(?<![0-9A-Za-z+/])[0-9A-Za-z+/]{86}==(?![0-9A-Za-z+/=])", _REDACTED, redacted)
    redacted = _redact_private_key_blocks(redacted)
    redacted = re.sub(r"\b(?:\d[ -]?){12,18}\d\b", _redact_pan_match, redacted)
    # User-supplied patterns run last and only add redaction coverage.
    for pattern in config.additional_redaction_patterns:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted


def _redact_private_key_blocks(text: str) -> str:
    """Replace every ``-----BEGIN … PRIVATE KEY-----`` block with the marker.

    One regex spanning both markers is the obvious spelling and it is quadratic:
    a trailing ``.*?`` rescans the rest of the value for every BEGIN that never
    gets an END, so the cost is the product of the value's length and the number
    of unterminated headers. Capture runs inline in the traced call and the value
    can come from a caller, so a large one made mostly of bare headers stalled the
    call for tens of seconds.

    The two markers are located once each instead, and the blocks are paired by
    walking the two position lists together. That reproduces what the regex
    matched -- leftmost BEGIN, nearest END at or after it, resume after the
    block -- at a cost linear in the length of the value.
    """

    # Scanning for the footer is skipped entirely when there is no header, so
    # text without a private key costs the one scan it always cost.
    begins = [(match.start(), match.end()) for match in _PEM_BEGIN.finditer(text)]
    if not begins:
        return text
    ends = [(match.start(), match.end()) for match in _PEM_END.finditer(text)]
    if not ends:
        return text

    parts: list[str] = []
    copied = 0  # everything before this index has been written to ``parts``
    next_end = 0  # index of the first END not yet consumed by a block
    for begin_start, begin_end in begins:
        if begin_start < copied:
            # This header sits inside a block already replaced, exactly as the
            # regex skipped it by resuming past its own match.
            continue
        # A block's END may start no earlier than the header's own end, which is
        # where the regex's ``.*?`` began matching.
        while next_end < len(ends) and ends[next_end][0] < begin_end:
            next_end += 1
        if next_end == len(ends):
            # No END is left, so no later header can have one either. This is
            # the case the regex used to pay for once per remaining header.
            break
        parts.append(text[copied:begin_start])
        parts.append(_REDACTED)
        copied = ends[next_end][1]
        next_end += 1

    if not parts:
        return text
    parts.append(text[copied:])
    return "".join(parts)


def _redact_labeled_secret_match(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2) or ''}{_REDACTED}"


def _redact_auth_header_match(match: re.Match[str]) -> str:
    """Keep the header label and the auth scheme; replace the credential."""

    scheme = match.group("param_scheme") or match.group("token_scheme") or ""
    return f"{match.group('label')}{scheme}{_REDACTED}"


def _redact_bearer_secret_match(match: re.Match[str]) -> str:
    return f"{match.group(1)}{_REDACTED}"


def _redact_uri_credential_match(match: re.Match[str]) -> str:
    """Keep a URI's scheme, user, and host; replace the password between them."""

    return f"{match.group(1)}{_REDACTED}{match.group(2)}"


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
