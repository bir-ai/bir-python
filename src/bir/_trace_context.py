"""Strict W3C Trace Context parsing and formatting.

This module is the validated primitive behind the decision recorded in
``docs/adr/0001-distributed-trace-context.md``. Nothing in the SDK calls it yet
and it exports no public name: extraction is the one place where Bir would take
an identifier from a caller it does not control and write it into a local trace
store, so the parser is built and tested before any recording path is allowed to
use it.

The rules come from the W3C Trace Context recommendation:

    traceparent: <version>-<trace-id>-<parent-id>-<trace-flags>

``version`` is two lowercase hex digits and ``ff`` is invalid. ``trace-id`` is 32
and ``parent-id`` 16 lowercase hex digits, neither of which may be all zeros.
``trace-flags`` is two lowercase hex digits whose lowest bit is the sampled flag.
Version ``00`` must carry exactly those four fields; a higher version may append
more, which a version-00 parser ignores rather than rejects, so a future sender
does not break an older receiver.

Every parse failure returns ``None``. A malformed header from a remote caller is
not an error condition worth raising into a request path — it means "no usable
remote context", and the caller starts its own trace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

# Length caps applied before any parsing: an unbounded header is a way to push
# arbitrary bytes toward a JSONL store, so the header is rejected on size before
# its contents are examined.
MAX_HEADER_LENGTH = 512

# Matched with ``fullmatch``, never ``match``: Python's ``$`` also matches just
# before a trailing newline, so ``^[0-9a-f]{32}$`` accepts an id with a newline
# glued to it — which is exactly the byte that would break a line in the JSONL
# store this id can reach.
_VERSION = re.compile(r"[0-9a-f]{2}")
_TRACE_ID = re.compile(r"[0-9a-f]{32}")
_SPAN_ID = re.compile(r"[0-9a-f]{16}")
_FLAGS = re.compile(r"[0-9a-f]{2}")

_INVALID_VERSION = "ff"
_ZERO_TRACE_ID = "0" * 32
_ZERO_SPAN_ID = "0" * 16

_SAMPLED_FLAG = 0b0000_0001


@dataclass(frozen=True)
class RemoteTraceContext:
    """A validated ``traceparent`` from a caller outside this process.

    ``sampled`` is what the caller asked for, recorded rather than obeyed: see
    the ADR's sampling section for why a remote flag does not decide whether
    this process records.
    """

    trace_id: str
    span_id: str
    sampled: bool

    def to_metadata(self) -> dict[str, object]:
        """Return the mapping a trace root would carry to record this parent."""

        return {"trace_id": self.trace_id, "span_id": self.span_id, "sampled": self.sampled}


def parse_traceparent(header: str | None) -> RemoteTraceContext | None:
    """Return the context a ``traceparent`` header carries, or ``None``.

    ``None`` covers every rejection: absent, oversized, wrong shape, wrong
    character set, a reserved version, or an all-zero id. The caller cannot tell
    which, by design — a remote caller learns nothing from this about what this
    process accepts.
    """

    if not isinstance(header, str):
        return None
    if not header or len(header) > MAX_HEADER_LENGTH:
        return None

    # No surrounding-whitespace tolerance: the field is machine-generated, and
    # accepting sloppy input is how a parser starts accepting what it should not.
    fields = header.split("-")
    if len(fields) < 4:
        return None

    version, trace_id, span_id, flags = fields[0], fields[1], fields[2], fields[3]
    if not _VERSION.fullmatch(version) or version == _INVALID_VERSION:
        return None
    # Version 00 is exactly four fields. A later version may append more, which
    # this parser ignores so a newer sender still interoperates.
    if version == "00" and len(fields) != 4:
        return None
    if not _TRACE_ID.fullmatch(trace_id) or trace_id == _ZERO_TRACE_ID:
        return None
    if not _SPAN_ID.fullmatch(span_id) or span_id == _ZERO_SPAN_ID:
        return None
    if not _FLAGS.fullmatch(flags):
        return None

    return RemoteTraceContext(
        trace_id=trace_id,
        span_id=span_id,
        sampled=bool(int(flags, 16) & _SAMPLED_FLAG),
    )


def format_traceparent(trace_id: str, span_id: str, *, sampled: bool) -> str | None:
    """Return the ``traceparent`` for Bir ids, or ``None`` if they cannot carry one.

    Bir ids are UUIDs. A UUID is 16 bytes, which is exactly a W3C trace-id, so a
    trace id maps across without loss. A W3C parent-id is 8 bytes, so a span id
    is narrowed to its first 8 — 64 random bits, which is what any W3C sender
    emits anyway, and the narrowing is outbound only: the id a downstream service
    echoes back is never used to look up a local event.

    Returns ``None`` for an id this function cannot represent, including the
    degenerate all-zero UUID, which W3C reserves as invalid.
    """

    trace_hex = _uuid_hex(trace_id)
    span_hex = _uuid_hex(span_id)
    if trace_hex is None or span_hex is None:
        return None

    narrowed_span = span_hex[:16]
    if trace_hex == _ZERO_TRACE_ID or narrowed_span == _ZERO_SPAN_ID:
        return None

    flags = f"{_SAMPLED_FLAG if sampled else 0:02x}"
    return f"00-{trace_hex}-{narrowed_span}-{flags}"


def _uuid_hex(value: str) -> str | None:
    """Return a Bir id as 32 lowercase hex digits, or ``None`` if it is not a UUID."""

    if not isinstance(value, str) or not value:
        return None
    try:
        return UUID(value).hex
    except (ValueError, AttributeError, TypeError):
        return None
