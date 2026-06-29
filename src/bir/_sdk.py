"""Core tracing primitives and local JSONL persistence for the Bir SDK."""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import math
import os
import random
import re
import time
import urllib.error
import urllib.request
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from types import TracebackType
from typing import IO, Any, Callable, Iterable, Mapping, TypeVar, cast
from uuid import uuid4

if os.name == "nt":
    import msvcrt
else:
    import fcntl

F = TypeVar("F", bound=Callable[..., Any])
T = TypeVar("T")

_DEFAULT_TRACE_PATH = Path(".bir/traces.jsonl")
# Sidecar suffix appended to the trace file name to record the IDs the server has
# already accepted, so an opt-in ``send_events(mark_sent=True)`` can cheaply skip
# them on a later send. SDK-local bookkeeping only; never part of the event schema.
_SENT_IDS_SUFFIX = ".sent"
_SCHEMA_VERSION = "1.0"
_MAX_CAPTURE_DEPTH = 6
_MAX_DEPTH_REACHED = "[max_depth]"
# Sentinel appended when an opt-in capture-size limit truncates a value: as a
# suffix on an over-long string and as an extra element/sentinel entry on an
# over-large list or mapping. Like the depth and redaction markers it keeps the
# captured value valid JSON while making the truncation visible.
_TRUNCATED = "…[truncated]"
_REDACTED = "[redacted]"
_EVENT_TYPES = {"trace", "span", "generation", "tool_call", "score"}
_EVENT_STATUSES = {"success", "error"}
_EVENT_SORT_PRIORITY = {
    "trace": 0,
    "span": 1,
    "generation": 1,
    "tool_call": 1,
    "score": 2,
}
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
# Upper bounds on the user-supplied additive redaction rules accepted by
# ``configure``. They reject unboundedly large configuration that would slow
# every capture or grow memory without limit. Built-in rules are never counted
# against these caps and can never be disabled, replaced, or reordered.
_MAX_ADDITIONAL_SECRET_KEYS = 100
_MAX_ADDITIONAL_REDACTION_PATTERNS = 100
_MAX_ADDITIONAL_SECRET_KEY_LENGTH = 200
_MAX_ADDITIONAL_REDACTION_PATTERN_LENGTH = 1000
# Upper bound on the opt-in ``sample_rules`` table accepted by ``configure``.
# Rules are checked only at trace-root creation, but a cap still avoids carrying
# an unbounded process-global configuration. Exact name matching keeps lookups
# predictable and leaves the global ``sample_rate`` as the default.
_MAX_SAMPLE_RULES = 1000
# Upper bound on the opt-in ``model_prices`` table accepted by ``configure``. It
# rejects an unboundedly large price table while leaving ample room for a
# user-curated list. Bir bundles no prices; this only caps what a caller supplies.
_MAX_MODEL_PRICES = 1000
# The only keys accepted inside a single model's rate mapping: the per-token
# ``input``/``output`` rates and an optional ``currency`` (default "USD").
_MODEL_PRICE_RATE_KEYS = frozenset({"input", "output", "currency"})
_current_trace_id: ContextVar[str | None] = ContextVar("bir_current_trace_id", default=None)
_current_parent_id: ContextVar[str | None] = ContextVar("bir_current_parent_id", default=None)
_current_capture_inputs: ContextVar[bool | None] = ContextVar("bir_current_capture_inputs", default=None)
_current_capture_outputs: ContextVar[bool | None] = ContextVar("bir_current_capture_outputs", default=None)
# Set once at trace-root creation so every descendant event of a sampled-out
# trace is skipped. False means "keep this trace"; the default keeps everything.
_current_trace_dropped: ContextVar[bool] = ContextVar("bir_current_trace_dropped", default=False)


@dataclass(frozen=True)
class _ModelPrice:
    """Validated per-token rates and currency for one ``model_prices`` entry.

    ``input``/``output`` are non-negative, finite per-token rates and either may
    be ``None`` when only one side is priced; ``currency`` defaults to ``"USD"``.
    Frozen so the whole price table stays hashable on the immutable ``_Config``.
    """

    input: int | float | None
    output: int | float | None
    currency: str


@dataclass(frozen=True)
class _Config:
    trace_path: Path = _DEFAULT_TRACE_PATH
    capture_inputs: bool = False
    capture_outputs: bool = False
    service_name: str | None = None
    environment: str | None = None
    # Optional trace-source tag recorded on trace roots under ``metadata.source``.
    # It mirrors the ``source`` field the product's Playground writes and that the
    # server/dashboard filter on by exact match, giving SDK callers a first-class
    # way to tag where a trace came from. ``None`` records nothing.
    source: str | None = None
    # Master on/off switch for all recording. When False, every primitive still
    # runs the user's body and still propagates exceptions, but nothing is ever
    # written — an explicit, intent-revealing kill switch (feature flag, incident
    # toggle, tests) enforced through the same "trace dropped" path as sampling
    # (see ``_should_drop_trace`` and ``_write_event``) so the contract matches
    # exactly. Defaults to True, so untouched behavior is byte-for-byte unchanged.
    enabled: bool = True
    sample_rate: float = 1.0
    # Optional exact trace-root-name sampling overrides. Empty by default so the
    # global ``sample_rate`` remains the only sampling input unless a caller opts
    # in. Stored as a validated, name-sorted tuple so the frozen config stays
    # hashable; a rule affects only trace roots with the same name, and children
    # inherit the root's already-rolled decision.
    sample_rules: tuple[tuple[str, float], ...] = ()
    # ``max_bytes is None`` keeps the historical behavior of a single trace file
    # that grows without bound. When set, the active file is rotated before a
    # write that would push it past the cap, keeping at most ``backup_count``
    # rotated siblings (``traces.jsonl.1`` .. ``traces.jsonl.N``).
    max_bytes: int | None = None
    backup_count: int = 3
    # Additive, user-supplied redaction rules layered on top of the built-in
    # rules; both default to empty so out-of-the-box behavior is unchanged. They
    # only ever widen coverage: ``additional_secret_keys`` are extra mapping-key
    # names redacted by exact, case-insensitive match (already normalized to the
    # built-in name form), and ``additional_redaction_patterns`` are extra
    # compiled regexes whose every match is replaced with the ``[redacted]``
    # marker. Built-in rules always run regardless of these and can never be
    # disabled, replaced, or reordered.
    additional_secret_keys: frozenset[str] = frozenset()
    additional_redaction_patterns: tuple[re.Pattern[str], ...] = ()
    # Opt-in, local-only price table that auto-fills a generation's cost from its
    # token usage. Empty by default so cost stays user-provided and no prices are
    # bundled. Stored as a validated, name-sorted tuple of ``(model, _ModelPrice)``
    # pairs so the frozen config stays hashable; an explicit ``set_cost`` on a
    # generation always wins over a rate-derived cost.
    model_prices: tuple[tuple[str, _ModelPrice], ...] = ()
    # Opt-in capture-size limits applied during ``_safe_capture`` after redaction.
    # Both default to ``None`` (unlimited), so captured output is byte-for-byte
    # unchanged unless a caller opts in. ``max_value_length`` caps the length of a
    # captured string (truncating the already-redacted text and appending the
    # ``_TRUNCATED`` marker); ``max_collection_items`` caps how many items of a
    # captured list/tuple/set or mapping are kept (the rest are replaced by a
    # single ``_TRUNCATED`` marker). They bound an individual huge payload, a
    # different concern from the ``max_bytes`` whole-file rotation cap.
    max_value_length: int | None = None
    max_collection_items: int | None = None


@dataclass(frozen=True)
class TraceEvent:
    """A single trace, span, generation, tool call, or score loaded from storage."""

    id: str
    trace_id: str
    parent_id: str | None
    name: str
    type: str
    start_time: str
    end_time: str
    status: str
    metadata: dict[str, Any]
    input: Any
    output: Any
    error: str | None
    raw: dict[str, Any]
    value: int | float | None = None
    model: str | None = None
    usage: dict[str, int | float] | None = None
    cost: dict[str, int | float] | None = None
    currency: str | None = None

    @property
    def duration_ms(self) -> float:
        """Return the event duration in milliseconds."""

        return _duration_ms(self.start_time, self.end_time)


@dataclass(frozen=True)
class LoadedTrace:
    """A trace root event with all events that share its trace ID."""

    id: str
    name: str
    start_time: str
    end_time: str
    status: str
    events: list[TraceEvent]
    root: TraceEvent

    @property
    def duration_ms(self) -> float:
        """Return the root trace duration in milliseconds."""

        return self.root.duration_ms


@dataclass(frozen=True)
class SendEventsResult:
    """Result returned after sending local events to a Bir server."""

    accepted: int
    event_ids: list[str]
    attempted: int = 0

    @property
    def skipped(self) -> int:
        """Return events the server did not newly accept, usually duplicates."""

        return max(self.attempted - self.accepted, 0)


@dataclass(frozen=True)
class PromptRecord:
    """Prompt metadata attached to a generation event."""

    name: str
    version: str | None
    template: str | None
    variables: dict[str, Any]
    rendered: str | None
    metadata: dict[str, Any]
    capture_template: bool
    capture_variables: bool
    capture_rendered: bool

    def to_metadata(self) -> dict[str, Any]:
        """Return the redacted metadata representation stored on a generation event."""

        payload: dict[str, Any] = {"name": self.name}
        if self.version is not None:
            payload["version"] = self.version
        if self.template is not None:
            payload["template_sha256"] = hashlib.sha256(self.template.encode("utf-8")).hexdigest()
            if self.capture_template:
                payload["template"] = _safe_capture(self.template)
        if self.capture_variables:
            payload["variables"] = _safe_capture(self.variables)
        if self.capture_rendered:
            payload["rendered"] = _safe_capture(self.render())
        if self.metadata:
            payload["metadata"] = _safe_capture(self.metadata)
        return payload

    def render(self) -> str | None:
        """Render the prompt template with variables when no explicit rendered value exists."""

        if self.rendered is not None:
            return self.rendered
        if self.template is None:
            return None
        if not self.variables:
            return self.template
        return self.template.format(**self.variables)


# ``_config`` holds the active configuration. It is initialized from the BIR_*
# environment variables at import time by ``_config_from_env`` near the bottom of
# this module (defined there so the validators it reuses already exist) and is
# then replaced wholesale by ``configure``.
_write_lock = Lock()
_sent_ids_lock = Lock()


class _InterProcessFileLock:
    """Exclusive advisory lock backed by a stable sibling lock file.

    Callers must also hold their operation's in-process ``Lock`` before entering
    this lock. The SDK never nests trace and sent-sidecar locks: if a future
    operation needs both, it must acquire the trace lock first and the sidecar
    lock second.
    """

    def __init__(self, target_path: Path) -> None:
        self._path = target_path.with_name(f".{target_path.name}.lock")
        self._file: IO[bytes] | None = None

    def __enter__(self) -> _InterProcessFileLock:
        lock_file = self._path.open("a+b")
        try:
            if os.name == "nt":
                # msvcrt byte-range locks may extend past EOF, so avoid writing
                # a sentinel first; writes before the lock race with writers.
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except BaseException:
            lock_file.close()
            raise
        self._file = lock_file
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        lock_file = self._file
        self._file = None
        if lock_file is None:
            return
        try:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def configure(
    *,
    trace_path: str | Path | None = None,
    capture_inputs: bool | None = None,
    capture_outputs: bool | None = None,
    service_name: str | None = None,
    environment: str | None = None,
    source: str | None = None,
    enabled: bool | None = None,
    sample_rate: float | None = None,
    sample_rules: Mapping[str, float] | None = None,
    max_bytes: int | None = None,
    backup_count: int | None = None,
    additional_secret_keys: Iterable[str] | None = None,
    additional_redaction_patterns: Iterable[str | re.Pattern[str]] | None = None,
    model_prices: Mapping[str, Mapping[str, Any]] | None = None,
    max_value_length: int | None = None,
    max_collection_items: int | None = None,
) -> None:
    """Configure local SDK behavior.

    ``service_name`` and ``environment`` are recorded on trace root events
    under ``metadata.service`` so traces can be filtered by deployment later.

    ``source`` tags every trace root with ``metadata.source`` so traces can be
    filtered by where they originated. It is the SDK-side counterpart to the
    ``source`` the Bir server/dashboard already filter on (the product's
    Playground records ``"playground"``); the server matches it by exact,
    trimmed value, so pick a stable label such as ``"checkout-api"``. Like the
    trace ``metadata`` argument, an explicit ``source`` in a ``trace(metadata=...)``
    block still wins over this configured default. Defaults to ``None`` (no source
    recorded).

    ``enabled`` is the master on/off switch for all recording. The default
    ``True`` keeps every primitive recording as configured. Set it to ``False``
    for an explicit, intent-revealing kill switch (a feature flag, an incident
    toggle, a test): ``@observe``, ``trace``/``span``/``generation``/
    ``tool_call``/``retrieval``, and ``score`` all still run the wrapped code and
    still propagate exceptions, but nothing is ever written, making Bir a true
    no-op without touching call sites. It is enforced through the same path as
    sampling, so a trace already in flight when recording is disabled stops
    writing immediately, and ``configure(enabled=True)`` restores full recording
    for traces started afterward. ``get_current_trace_id()`` /
    ``get_current_span_id()`` still return the live in-process ids inside a trace
    while disabled (matching a sampled-out trace), so log correlation keeps
    working even though nothing is persisted.

    ``sample_rate`` is the probability (``0.0`` to ``1.0``) that a trace is
    recorded. It is decided once per trace root; when a trace is sampled out the
    function still runs and still raises, but the trace and every event under it
    write nothing. The default ``1.0`` records every trace. ``sample_rate`` only
    applies while ``enabled`` is ``True``; ``enabled=False`` turns everything off
    regardless of the rate.

    ``sample_rules`` is an opt-in mapping of exact trace root name to a sampling
    rate for that root. A matching rule overrides the global ``sample_rate``; an
    unmatched root uses the global rate. Rules are validated once here and stored
    immutably, and the decision is still made once per trace root and inherited by
    every descendant event. Passing ``sample_rules`` replaces the prior rule table
    (an empty mapping clears it); omitting it leaves the current rules unchanged.

    ``max_bytes`` enables opt-in size-based rotation of the local trace file. It
    defaults to ``None`` (unlimited), which keeps the historical single-file
    behavior. When set to a non-negative integer, the active file is rotated
    before any write that would push it past the cap: ``traces.jsonl`` becomes
    ``traces.jsonl.1``, the previous ``.1`` becomes ``.2``, and so on, keeping at
    most ``backup_count`` rotated files and dropping the oldest. Rotation always
    happens on whole-line boundaries, so every file stays valid JSONL and a JSON
    object is never split across files (a single line larger than ``max_bytes``
    is still written whole). ``backup_count`` defaults to ``3``; ``0`` keeps no
    rotated files and simply drops the active file when it fills.

    Note that a single logical trace may be split across rotated files when
    rotation happens mid-trace, so reading with ``include_rotated=True`` can
    surface incomplete traces near a rotation boundary.

    ``additional_secret_keys`` and ``additional_redaction_patterns`` add to the
    built-in redaction rules; they can only ever widen what is redacted and can
    never disable, replace, reorder, or change the ``[redacted]`` marker of the
    built-in rules. ``additional_secret_keys`` is an iterable of extra
    mapping-key names: a captured mapping key is redacted when it matches one of
    them exactly and case-insensitively, treating ``-`` and ``_`` as equivalent
    (this is whole-name exact matching, unlike the built-in substring rules).
    ``additional_redaction_patterns`` is an iterable of regex strings and/or
    already-compiled ``re.Pattern`` objects; every match of each pattern in any
    captured string, repr fallback, prompt text, eval metadata, or error message
    is replaced with ``[redacted]``, running after all built-in text patterns.
    Both are validated and compiled once here, so an empty key, empty pattern,
    invalid regex, non-string entry, bytes pattern, or an over-large list raises
    ``ValueError``/``TypeError`` immediately. Passing either argument replaces the
    previously configured *additional* rules of that kind (passing an empty
    iterable clears them); omitting it leaves the current additional rules
    unchanged. The built-in rules always remain in force either way.

    ``model_prices`` is an opt-in, local-only price table that auto-fills a
    generation's cost from its token usage. It is a mapping of model name to a
    rates mapping holding a non-negative, finite ``input`` and/or ``output``
    per-token rate plus an optional ``currency`` (default ``"USD"``). Bir bundles
    no prices, so the rates — and keeping them current — are yours to supply. When
    a generation has usage and a model matching a configured entry but no
    explicitly set cost, its ``input_cost``/``output_cost``/``total_cost`` are
    derived from the matching rates at the configured currency exactly as a manual
    ``set_cost(...)`` would record them (input rate times input tokens, output rate
    times output tokens, total summed when both sides are priced). An explicit
    ``set_cost(...)`` always wins and is never overwritten, and a generation whose
    usage lacks the needed token split is left without a derived cost. The table is
    validated once here, so a non-mapping table, a non-string or empty model name,
    a non-mapping or empty rate entry, an unknown rate key, a boolean, negative, or
    non-finite rate, an invalid currency, or an over-large table raises
    ``ValueError``/``TypeError`` immediately. Passing ``model_prices`` replaces the
    previously configured table (an empty mapping clears it); omitting it leaves
    the current table unchanged. With no table configured, generation cost behavior
    is unchanged.

    ``max_value_length`` and ``max_collection_items`` are opt-in capture-size
    limits that bound a single captured value so one huge payload (a base64
    image, a megabyte of model output) cannot bloat the local store. Both
    default to ``None`` (unlimited), which keeps captured output byte-for-byte
    unchanged. When ``max_value_length`` is a non-negative integer, a captured
    string longer than it is truncated to that many characters with a visible
    ``…[truncated]`` marker appended; truncation always runs *after* redaction,
    so a secret is replaced before any cut and can never be split in a way that
    defeats the redactor. When ``max_collection_items`` is a non-negative
    integer, a captured list, tuple, set, or mapping larger than it keeps only
    the first that-many items and records a single ``…[truncated]`` marker for
    the remainder, leaving the output valid JSON. Both apply uniformly to every
    capture path (inputs, outputs, metadata, repr fallbacks, and dataset and
    experiment capture) and compose with the existing capture-depth cap. They
    only bound captured values, never event names, models, ids, or the schema. A
    non-integer, boolean, or negative limit raises ``TypeError``/``ValueError``
    here.

    Any field left unset falls back to the value supplied by the matching
    environment variable (``BIR_TRACE_PATH``, ``BIR_CAPTURE_INPUTS``,
    ``BIR_CAPTURE_OUTPUTS``, ``BIR_DISABLED``, ``BIR_SAMPLE_RATE``,
    ``BIR_SERVICE_NAME``, ``BIR_ENVIRONMENT``, ``BIR_SOURCE``,
    ``BIR_MAX_VALUE_LENGTH``, ``BIR_MAX_COLLECTION_ITEMS``), which is read once at
    import time, and otherwise to the hardcoded default. A truthy ``BIR_DISABLED``
    sets ``enabled=False`` (it is the inverse of the ``enabled`` field, so the
    common "turn it off in production" case is a single boolean variable).
    Explicit arguments to this function take precedence over the environment, so
    ``configure(enabled=True)`` re-enables recording even when ``BIR_DISABLED`` is
    set.
    """

    global _config

    updates: dict[str, Any] = {}
    if trace_path is not None:
        updates["trace_path"] = Path(trace_path)
    if capture_inputs is not None:
        updates["capture_inputs"] = capture_inputs
    if capture_outputs is not None:
        updates["capture_outputs"] = capture_outputs
    if service_name is not None:
        updates["service_name"] = _validate_event_name(service_name, "service_name")
    if environment is not None:
        updates["environment"] = _validate_event_name(environment, "environment")
    if source is not None:
        updates["source"] = _validate_event_name(source, "source")
    if enabled is not None:
        updates["enabled"] = _validate_bool(enabled, "enabled")
    if sample_rate is not None:
        updates["sample_rate"] = _validate_sample_rate(sample_rate)
    if sample_rules is not None:
        updates["sample_rules"] = _validate_sample_rules(sample_rules)
    if max_bytes is not None:
        updates["max_bytes"] = _validate_non_negative_int(max_bytes, "max_bytes")
    if backup_count is not None:
        updates["backup_count"] = _validate_non_negative_int(backup_count, "backup_count")
    if additional_secret_keys is not None:
        updates["additional_secret_keys"] = _validate_additional_secret_keys(additional_secret_keys)
    if additional_redaction_patterns is not None:
        updates["additional_redaction_patterns"] = _validate_additional_redaction_patterns(
            additional_redaction_patterns
        )
    if model_prices is not None:
        updates["model_prices"] = _validate_model_prices(model_prices)
    if max_value_length is not None:
        updates["max_value_length"] = _validate_non_negative_int(max_value_length, "max_value_length")
    if max_collection_items is not None:
        updates["max_collection_items"] = _validate_non_negative_int(max_collection_items, "max_collection_items")

    _config = replace(_config, **updates)


def load_events(path: str | Path | None = None, *, include_rotated: bool = False) -> list[TraceEvent]:
    """Load local JSONL trace events.

    By default only the active trace file is read. Pass ``include_rotated=True``
    to also read size-rotated siblings (``traces.jsonl.1`` ..) created by
    ``configure(max_bytes=...)``. Rotated files are read oldest-first so the
    returned events stay in the same chronological order they were written,
    matching how a single never-rotated file would read. Because rotation can
    occur mid-trace, a single logical trace may be split across files.
    """

    trace_path = Path(path) if path is not None else _config.trace_path
    if not include_rotated:
        return _load_events_from_file(trace_path)

    events: list[TraceEvent] = []
    for file_path in _trace_files_oldest_first(trace_path):
        events.extend(_load_events_from_file(file_path))
    return events


def _load_events_from_file(trace_path: Path) -> list[TraceEvent]:
    if not trace_path.exists():
        return []

    events: list[TraceEvent] = []
    with trace_path.open("r", encoding="utf-8") as trace_file:
        for line_number, line in enumerate(trace_file, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in trace file {trace_path} at line {line_number}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"Trace file {trace_path} line {line_number} must contain a JSON object")
            events.append(_trace_event_from_payload(payload, trace_path=trace_path, line_number=line_number))
    return events


def _trace_files_oldest_first(trace_path: Path) -> list[Path]:
    """Return rotated trace files then the active file, oldest event first.

    Rotated siblings are named ``<trace_path>.<n>`` where a higher ``n`` is
    older, so the original write order is reconstructed by reading the
    highest-numbered backup first, down to ``.1``, and the active file last.
    """

    rotated: list[tuple[int, Path]] = []
    prefix = f"{trace_path.name}."
    try:
        entries = list(trace_path.parent.iterdir())
    except FileNotFoundError:
        entries = []
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        suffix = entry.name[len(prefix):]
        if suffix.isdigit() and int(suffix) >= 1:
            rotated.append((int(suffix), entry))
    rotated.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in rotated] + [trace_path]


def load_traces(path: str | Path | None = None, *, include_rotated: bool = False) -> list[LoadedTrace]:
    """Load local traces grouped by trace_id.

    ``include_rotated`` is forwarded to :func:`load_events`; see its note about
    traces possibly being split across rotated files.
    """

    events = load_events(path, include_rotated=include_rotated)
    events_by_trace_id: dict[str, list[TraceEvent]] = {}
    for event in events:
        events_by_trace_id.setdefault(event.trace_id, []).append(event)

    traces: list[LoadedTrace] = []
    for trace_id, trace_events in events_by_trace_id.items():
        depths = _event_depths(trace_events)
        sorted_events = sorted(trace_events, key=lambda event: _event_sort_key(event, depths[event.id]))
        root = next((event for event in sorted_events if event.type == "trace" and event.id == trace_id), None)
        if root is None:
            continue
        traces.append(
            LoadedTrace(
                id=trace_id,
                name=root.name,
                start_time=root.start_time,
                end_time=root.end_time,
                status=root.status,
                events=sorted_events,
                root=root,
            )
        )
    return sorted(traces, key=lambda trace: trace.start_time)


def send_events(
    server_url: str = "http://127.0.0.1:8000",
    *,
    path: str | Path | None = None,
    timeout: float = 10.0,
    retries: int = 2,
    backoff: float = 0.5,
    mark_sent: bool = False,
    include_rotated: bool = False,
) -> SendEventsResult:
    """Send local JSONL trace events to a Bir ingestion server.

    Transient failures are retried with exponential backoff: a network error,
    timeout, or HTTP 5xx is retried up to ``retries`` times (default ``2``),
    sleeping ``backoff * 2**attempt`` seconds between tries (``backoff`` defaults to
    ``0.5``). A 4xx response is a permanent rejection and is raised immediately
    without retry, matching the un-retried behavior. A healthy send still makes a
    single attempt, so the default behavior is unchanged.

    ``mark_sent`` is opt-in bookkeeping for cheap re-sends. When ``True``, the IDs
    the server accepts are recorded in a sidecar file next to the trace file
    (``<trace_path>.sent``) and skipped on later sends, so ``attempted`` reflects
    only events not yet recorded as sent. The sidecar is SDK-local: it never
    modifies the trace JSONL or the event schema, and a missing or corrupt sidecar
    is treated as empty so it can never block a send. With the default
    ``mark_sent=False`` nothing is recorded and re-sending the whole file stays
    safe because the server is idempotent on event IDs.

    ``include_rotated`` is opt-in upload of size-rotated trace files. The default
    ``False`` uploads only the active trace file, matching the historical
    behavior. When ``True``, retained rotated siblings (``traces.jsonl.1`` ..)
    created by ``configure(max_bytes=...)`` are uploaded oldest-first followed by
    the active file, so rotation can no longer strand unsent events. Events are
    deduplicated by ID when a rotated file overlaps the active file, and the
    ``mark_sent`` sidecar still anchors to the active trace path so recorded IDs
    are skipped across the whole selected file set.
    """

    retries = _validate_non_negative_int(retries, "retries")
    backoff = float(_validate_non_negative_number(backoff, "backoff"))

    events = _events_for_sending(path, include_rotated=include_rotated)
    sent_ids_path = _sent_ids_path(path) if mark_sent else None
    if sent_ids_path is not None:
        already_sent = _load_sent_ids(sent_ids_path)
        if already_sent:
            events = [event for event in events if event.id not in already_sent]

    endpoint = _events_endpoint(server_url)
    if not events:
        return SendEventsResult(accepted=0, event_ids=[], attempted=0)

    result = _post_loaded_events(events, endpoint, timeout=timeout, retries=retries, backoff=backoff)

    if sent_ids_path is not None and result.event_ids:
        _record_sent_ids(sent_ids_path, result.event_ids)
    return result


def _post_loaded_events(
    events: list[TraceEvent],
    endpoint: str,
    *,
    timeout: float,
    retries: int,
    backoff: float,
) -> SendEventsResult:
    """Post already-loaded events, batching first and falling back per-event.

    Both the batch and per-event posts go through :func:`_send_with_retry` so a
    transient failure on either path is retried before it surfaces.
    """

    batch_result = _send_with_retry(
        lambda: _post_event_batch(f"{endpoint}/batch", [event.raw for event in events], timeout=timeout),
        retries=retries,
        backoff=backoff,
    )
    if batch_result is not None:
        return batch_result

    accepted = 0
    event_ids: list[str] = []
    for event in events:
        event_accepted = _send_with_retry(
            lambda event=event: _post_event(endpoint, event.raw, timeout=timeout),
            retries=retries,
            backoff=backoff,
        )
        accepted += event_accepted
        if event_accepted:
            event_ids.append(event.id)

    return SendEventsResult(accepted=accepted, event_ids=event_ids, attempted=len(events))


def _send_with_retry(operation: Callable[[], T], *, retries: int, backoff: float) -> T:
    """Run ``operation`` and retry transient send failures with exponential backoff.

    A transient failure (network error, timeout, or HTTP 5xx) is raised by the
    callers as :class:`_TransientSendError` and retried up to ``retries`` times,
    sleeping ``backoff * 2**attempt`` seconds before each retry. Permanent failures
    (HTTP 4xx, raised as ``RuntimeError``) propagate immediately. When the retries
    are exhausted the failure is surfaced as ``RuntimeError`` so callers see the
    same exception type a single failed attempt raises.
    """

    attempt = 0
    while True:
        try:
            return operation()
        except _TransientSendError as exc:
            if attempt >= retries:
                raise RuntimeError(str(exc)) from exc.cause
            time.sleep(backoff * (2**attempt))
            attempt += 1


def _events_for_sending(path: str | Path | None = None, *, include_rotated: bool = False) -> list[TraceEvent]:
    """Order local events for upload: complete traces root-first, then orphans.

    Events are deduplicated by ID, so a rotated file that overlaps the active file
    (for example a copied backup) still uploads each event once. Orphan events
    whose trace root is missing are kept rather than dropped. With
    ``include_rotated=True`` the active file and its size-rotated siblings are read
    oldest-first, preserving write-order chronology across files.
    """

    events = load_events(path, include_rotated=include_rotated)
    traces = load_traces(path, include_rotated=include_rotated)
    ordered_events: list[TraceEvent] = []
    ordered_event_ids: set[str] = set()

    for trace in traces:
        for event in trace.events:
            if event.id in ordered_event_ids:
                continue
            ordered_events.append(event)
            ordered_event_ids.add(event.id)

    for event in events:
        if event.id in ordered_event_ids:
            continue
        ordered_events.append(event)
        ordered_event_ids.add(event.id)
    return ordered_events


def observe(
    name: str | None = None,
    *,
    capture_inputs: bool | None = None,
    capture_outputs: bool | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Callable[[F], F]:
    """Decorate a sync or async function and record one trace event for each call.

    Generator and async-generator functions are also supported and are traced for
    their full iteration lifetime rather than only their creation: the wrapper
    stays lazy (no body runs and nothing is written until the first iteration),
    the trace stays open across every ``next``/``send``/``throw`` (or
    ``asend``/``athrow``) so child spans and generations created in the body
    attach to it, and it is finalized when the generator is exhausted (recorded as
    a successful trace), raises (recorded as a redacted error and re-raised), or is
    closed/cancelled early (recorded as a successful trace whose
    ``metadata.generator.outcome`` is ``"closed"``). Yielded values are never
    buffered; with output capture enabled only a bounded yielded-item count is
    recorded under ``metadata.generator.items``.

    ``metadata`` is an optional static mapping recorded on the produced trace ROOT
    event, redacted with the same rules as captured input/output. It is the
    decorator-side counterpart to ``trace(metadata=...)`` for tagging an entry
    point (route, tenant, feature flag) without rewriting it as a manual
    ``with trace(...)`` block. It is attached only when the decorated call opens a
    new trace root; a nested ``@observe()`` call records a span and never carries
    this trace-level metadata. For observed generators it composes with the
    recorded ``metadata.generator.*`` outcome (the generator keys win on conflict).
    """

    if name is not None:
        _validate_event_name(name, "observe name")
    if metadata is not None and not isinstance(metadata, Mapping):
        raise TypeError("bir observe metadata must be a mapping")
    observe_metadata = dict(metadata) if metadata is not None else None

    def decorator(func: F) -> F:
        trace_name = name or func.__name__
        signature = inspect.signature(func)

        # Generator and async-generator functions return their iterator before any
        # body runs, so they are detected before the coroutine/sync branches and
        # wrapped so the trace spans the actual iteration instead of closing at
        # creation time. ``iscoroutinefunction`` is false for async generators, so
        # ordering these first is safe.
        if inspect.isasyncgenfunction(func):

            @functools.wraps(func)
            async def async_generator_wrapper(*args: Any, **kwargs: Any) -> Any:
                # Nothing here runs until the first ``__anext__``/``asend`` because
                # the wrapper is itself an async generator, keeping creation lazy.
                underlying = func(*args, **kwargs)
                consumer_ctx = _snapshot_context()
                state = _begin_observe(trace_name, capture_inputs, capture_outputs, observe_metadata)
                input_payload = (
                    _capture_call_input(signature, args, kwargs) if state.capture_inputs else None
                )
                gen_ctx = _snapshot_context()
                yielded = 0
                resume: tuple[str, Any] = ("asend", None)
                while True:
                    # Advance the body with the generator's own trace context so its
                    # child events attach to this trace and never leak to the consumer.
                    _restore_context(gen_ctx)
                    try:
                        action, payload = resume
                        if action == "asend":
                            value = await underlying.asend(payload)
                        else:
                            value = await underlying.athrow(payload)
                    except StopAsyncIteration:
                        _finalize_generator(state, trace_name, input_payload, yielded, "completed", consumer_ctx)
                        return
                    except Exception as exc:
                        _finalize_generator(state, trace_name, input_payload, yielded, "error", consumer_ctx, exc)
                        raise
                    except BaseException:
                        # Cancellation (``CancelledError`` is a ``BaseException``)
                        # already unwound the body; record a non-error terminal state
                        # and re-raise without swallowing it.
                        _finalize_generator(state, trace_name, input_payload, yielded, "closed", consumer_ctx)
                        raise
                    gen_ctx = _snapshot_context()
                    _restore_context(consumer_ctx)
                    yielded += 1
                    try:
                        sent = yield value
                    except GeneratorExit:
                        consumer_ctx = _snapshot_context()
                        _restore_context(gen_ctx)
                        try:
                            await underlying.aclose()
                        finally:
                            _finalize_generator(state, trace_name, input_payload, yielded, "closed", consumer_ctx)
                        raise
                    except BaseException as exc:
                        consumer_ctx = _snapshot_context()
                        resume = ("athrow", exc)
                    else:
                        consumer_ctx = _snapshot_context()
                        resume = ("asend", sent)

            return cast(F, async_generator_wrapper)

        if inspect.isgeneratorfunction(func):

            @functools.wraps(func)
            def generator_wrapper(*args: Any, **kwargs: Any) -> Any:
                # Nothing here runs until the first ``next``/``send`` because the
                # wrapper is itself a generator, keeping creation lazy.
                underlying = func(*args, **kwargs)
                consumer_ctx = _snapshot_context()
                state = _begin_observe(trace_name, capture_inputs, capture_outputs, observe_metadata)
                input_payload = (
                    _capture_call_input(signature, args, kwargs) if state.capture_inputs else None
                )
                gen_ctx = _snapshot_context()
                yielded = 0
                resume: tuple[str, Any] = ("next", None)
                while True:
                    # Advance the body with the generator's own trace context so its
                    # child events attach to this trace and never leak to the consumer.
                    _restore_context(gen_ctx)
                    try:
                        action, payload = resume
                        if action == "next":
                            value = next(underlying)
                        elif action == "send":
                            value = underlying.send(payload)
                        else:
                            value = underlying.throw(payload)
                    except StopIteration:
                        _finalize_generator(state, trace_name, input_payload, yielded, "completed", consumer_ctx)
                        return
                    except Exception as exc:
                        _finalize_generator(state, trace_name, input_payload, yielded, "error", consumer_ctx, exc)
                        raise
                    except BaseException:
                        # KeyboardInterrupt and friends already unwound the body;
                        # record a non-error terminal state and re-raise.
                        _finalize_generator(state, trace_name, input_payload, yielded, "closed", consumer_ctx)
                        raise
                    gen_ctx = _snapshot_context()
                    _restore_context(consumer_ctx)
                    yielded += 1
                    try:
                        sent = yield value
                    except GeneratorExit:
                        consumer_ctx = _snapshot_context()
                        _restore_context(gen_ctx)
                        try:
                            underlying.close()
                        finally:
                            _finalize_generator(state, trace_name, input_payload, yielded, "closed", consumer_ctx)
                        raise
                    except BaseException as exc:
                        consumer_ctx = _snapshot_context()
                        resume = ("throw", exc)
                    else:
                        consumer_ctx = _snapshot_context()
                        resume = ("send", sent)

            return cast(F, generator_wrapper)

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                state = _begin_observe(trace_name, capture_inputs, capture_outputs, observe_metadata)
                input_payload = None
                try:
                    if state.capture_inputs:
                        input_payload = _capture_call_input(signature, args, kwargs)
                    result = await func(*args, **kwargs)
                except Exception as exc:
                    _finish_observe_error(state, trace_name, exc, input_payload)
                    raise
                _finish_observe_success(state, trace_name, input_payload, result)
                return result

            return cast(F, async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            state = _begin_observe(trace_name, capture_inputs, capture_outputs, observe_metadata)
            input_payload = None
            try:
                if state.capture_inputs:
                    input_payload = _capture_call_input(signature, args, kwargs)
                result = func(*args, **kwargs)
            except Exception as exc:
                _finish_observe_error(state, trace_name, exc, input_payload)
                raise
            _finish_observe_success(state, trace_name, input_payload, result)
            return result

        return cast(F, wrapper)

    return decorator


@dataclass(frozen=True)
class _ObserveState:
    """Per-call state shared by the sync and async ``observe`` wrappers."""

    event_id: str
    trace_id: str
    parent_id: str | None
    event_type: str
    start_time: str
    capture_inputs: bool
    capture_outputs: bool
    dropped: bool
    # Static, redaction-pending metadata supplied to ``@observe(metadata=...)``.
    # Attached only when this observation is a trace root; ``None`` records nothing.
    metadata: dict[str, Any] | None
    trace_token: Token[str | None]
    parent_token: Token[str | None]
    capture_inputs_token: Token[bool | None]
    capture_outputs_token: Token[bool | None]
    dropped_token: Token[bool]


def _begin_observe(
    trace_name: str,
    capture_inputs: bool | None,
    capture_outputs: bool | None,
    metadata: dict[str, Any] | None = None,
) -> _ObserveState:
    """Open an observation: choose trace-vs-span ids and bind the call contextvars.

    Both the sync and async wrappers call this so the trace decision and
    contextvar bookkeeping live in one place. ContextVars are task-local and each
    asyncio task runs with a copied context, so concurrent observed coroutines
    stay isolated. A new trace root also rolls the sampling decision once here so
    that every nested span inherits it instead of re-rolling.

    ``trace_name`` is used only when this observation opens a trace root, where
    exact-name ``sample_rules`` may override the global rate. ``metadata`` is the
    static mapping from ``@observe(metadata=...)``. It is stored on the returned
    state unredacted and only redacted and written when the observation turns out
    to be a trace root; nested spans ignore it.
    """

    active_trace_id = _current_trace_id.get()
    active_parent_id = _current_parent_id.get()
    event_id = _new_id()
    if active_trace_id is not None and active_parent_id is not None:
        trace_id = active_trace_id
        parent_id = active_parent_id
        event_type = "span"
        # Inherit the root's decision so a span never re-rolls sampling.
        dropped = _current_trace_dropped.get()
    else:
        trace_id = event_id
        parent_id = None
        event_type = "trace"
        dropped = _should_drop_trace(trace_name)
    start_time = _now()
    capture_inputs_for_call = _should_capture(capture_inputs, "inputs")
    capture_outputs_for_call = _should_capture(capture_outputs, "outputs")
    return _ObserveState(
        event_id=event_id,
        trace_id=trace_id,
        parent_id=parent_id,
        event_type=event_type,
        start_time=start_time,
        capture_inputs=capture_inputs_for_call,
        capture_outputs=capture_outputs_for_call,
        dropped=dropped,
        metadata=metadata,
        trace_token=_current_trace_id.set(trace_id),
        parent_token=_current_parent_id.set(event_id),
        capture_inputs_token=_current_capture_inputs.set(capture_inputs_for_call),
        capture_outputs_token=_current_capture_outputs.set(capture_outputs_for_call),
        dropped_token=_current_trace_dropped.set(dropped),
    )


def _observe_event_metadata(
    state: _ObserveState,
    extra: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Compose the metadata written for an observation's own event.

    The static ``@observe(metadata=...)`` mapping is redacted with the same rules
    as captured input/output and attached only when this observation is a trace
    root, mirroring how only roots carry trace-level metadata (service/source).
    ``extra`` is the wrapper-supplied event metadata — the generator wrappers'
    ``metadata.generator.*`` outcome — and is applied last so those system keys
    win on conflict. Returns ``None`` when nothing applies so plain ``@observe()``
    calls stay byte-for-byte identical.
    """

    combined: dict[str, Any] = {}
    if state.event_type == "trace" and state.metadata is not None:
        combined.update(_safe_capture(dict(state.metadata)))
    if extra:
        combined.update(extra)
    return combined or None


def _finish_observe_success(
    state: _ObserveState,
    trace_name: str,
    input_payload: Any,
    result: Any,
    metadata: Mapping[str, Any] | None = None,
    reset_context: bool = True,
) -> None:
    """Reset the call contextvars and write the success event for an observation.

    ``metadata`` is optional event metadata (used by the generator wrappers to
    record the bounded terminal-state marker); it defaults to ``None`` so the
    plain sync/async wrappers keep writing no metadata. ``reset_context`` is
    ``False`` for the generator wrappers, which tear down their own context by
    value (a generator can be finalized — via GC or cancellation — in a different
    context than the one that created the tokens, where token reset would fail).
    """

    end_time = _now()
    if reset_context:
        _reset_context(
            state.trace_token,
            state.parent_token,
            state.capture_inputs_token,
            state.capture_outputs_token,
            state.dropped_token,
        )
    if state.dropped:
        return
    output_payload = _safe_capture(result) if state.capture_outputs else None
    _write_event(
        _event(
            event_id=state.event_id,
            trace_id=state.trace_id,
            parent_id=state.parent_id,
            name=trace_name,
            event_type=state.event_type,
            start_time=state.start_time,
            end_time=end_time,
            status="success",
            error=None,
            metadata=_observe_event_metadata(state, metadata),
            input=input_payload,
            output=output_payload,
        )
    )


def _finish_observe_error(
    state: _ObserveState,
    trace_name: str,
    exc: BaseException,
    input_payload: Any,
    metadata: Mapping[str, Any] | None = None,
    reset_context: bool = True,
) -> None:
    """Reset the call contextvars and write the error event for a failed observation.

    A storage failure re-raises the original ``exc`` chained to it so the user's
    exception is never silently swallowed by a write error. ``metadata`` is
    optional event metadata (used by the generator wrappers); it defaults to
    ``None`` so the plain sync/async wrappers keep writing no metadata.
    ``reset_context`` is ``False`` for the generator wrappers, which tear down
    their own context by value (see :func:`_finish_observe_success`).
    """

    end_time = _now()
    if reset_context:
        _reset_context(
            state.trace_token,
            state.parent_token,
            state.capture_inputs_token,
            state.capture_outputs_token,
            state.dropped_token,
        )
    if state.dropped:
        return
    event = _event(
        event_id=state.event_id,
        trace_id=state.trace_id,
        parent_id=state.parent_id,
        name=trace_name,
        event_type=state.event_type,
        start_time=state.start_time,
        end_time=end_time,
        status="error",
        error=_safe_error(exc),
        metadata=_observe_event_metadata(state, metadata),
        input=input_payload,
    )
    try:
        _write_event(event)
    except Exception as storage_error:
        raise exc from storage_error


def _finalize_generator(
    state: _ObserveState,
    trace_name: str,
    input_payload: Any,
    yielded: int,
    outcome: str,
    consumer_ctx: _ContextSnapshot,
    exc: BaseException | None = None,
) -> None:
    """Finalize an observed generator's trace once iteration ends.

    ``outcome`` is the terminal disposition recorded under
    ``metadata.generator.outcome``: ``"completed"`` for normal exhaustion,
    ``"error"`` for an exception raised by the body, and ``"closed"`` for an
    explicit ``close``/``aclose`` or consumer cancellation. The yielded-item
    ``items`` count is bounded metadata recorded only when output capture is
    enabled, so streamed content is never buffered or persisted by default. Both
    the completed and closed outcomes are persisted with the existing
    ``"success"`` status; only the error outcome uses ``"error"`` and re-raises
    through :func:`_finish_observe_error`.

    Context is restored to ``consumer_ctx`` by value rather than by resetting the
    tokens from ``_begin_observe``: a generator may be finalized in a different
    context than the one that started it (GC, ``shutdown_asyncgens``, or
    cross-task cancellation), where token reset raises ``ValueError``.
    """

    _restore_context(consumer_ctx)
    generator_metadata: dict[str, Any] = {"outcome": outcome}
    if state.capture_outputs:
        generator_metadata["items"] = yielded
    metadata = {"generator": generator_metadata}
    if exc is not None:
        _finish_observe_error(state, trace_name, exc, input_payload, metadata=metadata, reset_context=False)
    else:
        _finish_observe_success(state, trace_name, input_payload, None, metadata=metadata, reset_context=False)


def span(name: str) -> _Span:
    """Create a nested span inside the current trace."""

    _validate_event_name(name, "span name")
    return _Span(name)


def trace(name: str, *, metadata: Mapping[str, Any] | None = None) -> _TraceContext:
    """Create a trace root with a context manager."""

    return _trace_context(name=name, metadata=metadata)


def prompt(
    name: str,
    *,
    version: str | None = None,
    template: str | None = None,
    variables: Mapping[str, Any] | None = None,
    rendered: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    capture_template: bool = False,
    capture_variables: bool = False,
    capture_rendered: bool = False,
) -> PromptRecord:
    """Describe the prompt version used by a generation."""

    if not name:
        raise ValueError("bir prompt name must not be empty")
    if version is not None and not version:
        raise ValueError("bir prompt version must not be empty")
    if template is not None and not isinstance(template, str):
        raise TypeError("bir prompt template must be a string")
    if rendered is not None and not isinstance(rendered, str):
        raise TypeError("bir rendered prompt must be a string")

    return PromptRecord(
        name=name,
        version=version,
        template=template,
        variables=dict(variables or {}),
        rendered=rendered,
        metadata=dict(metadata or {}),
        capture_template=capture_template,
        capture_variables=capture_variables,
        capture_rendered=capture_rendered,
    )


def generation(
    name: str,
    *,
    model: str | None = None,
    input: Any = None,
    metadata: Mapping[str, Any] | None = None,
    prompt: PromptRecord | None = None,
    capture_input: bool | None = None,
    capture_output: bool | None = None,
) -> _Generation:
    """Create a generation event for an LLM call inside the current trace."""

    _validate_event_name(name, "generation name")
    return _Generation(
        name=name,
        model=model,
        input=input,
        metadata=metadata,
        prompt_record=prompt,
        capture_input=capture_input,
        capture_output=capture_output,
    )


def tool_call(
    name: str,
    *,
    input: Any = None,
    metadata: Mapping[str, Any] | None = None,
    capture_input: bool | None = None,
    capture_output: bool | None = None,
) -> _ToolCall:
    """Create a tool call event inside the current trace."""

    _validate_event_name(name, "tool_call name")
    return _ToolCall(
        name=name,
        input=input,
        metadata=metadata,
        capture_input=capture_input,
        capture_output=capture_output,
    )


def retrieval(
    name: str,
    *,
    query: Any,
    metadata: Mapping[str, Any] | None = None,
    capture_input: bool | None = None,
    capture_output: bool | None = None,
) -> _Retrieval:
    """Create a retrieval tool call using Bir's documented RAG event shape."""

    _validate_event_name(name, "retrieval name")
    return _Retrieval(
        name=name,
        query=query,
        metadata=metadata,
        capture_input=capture_input,
        capture_output=capture_output,
    )


def score(name: str, value: int | float, *, metadata: Mapping[str, Any] | None = None) -> None:
    """Attach a score event to the current trace.

    Optional ``metadata`` (for example an evaluator's reasoning or threshold) is
    redacted with the same rules as captured input/output and stored on the
    score event so it can be inspected in the dashboard later.
    """

    _validate_event_name(name, "score name")
    if metadata is not None and not isinstance(metadata, Mapping):
        raise TypeError("bir score metadata must be a mapping")
    trace_id = _current_trace_id.get()
    parent_id = _current_parent_id.get()
    if trace_id is None or parent_id is None:
        raise RuntimeError("bir.score() requires an active trace. Use it inside a @observe() function.")
    score_value = _validate_number(value, "score value")

    timestamp = _now()
    _write_event(
        _event(
            event_id=_new_id(),
            trace_id=trace_id,
            parent_id=parent_id,
            name=name,
            event_type="score",
            start_time=timestamp,
            end_time=timestamp,
            status="success",
            error=None,
            metadata=_safe_capture(dict(metadata or {})),
            value=score_value,
        )
    )


def get_current_trace_id() -> str | None:
    """Return the active trace's id, or ``None`` outside any trace.

    The value is the same id written to the ``trace_id`` field of every event
    recorded while this trace is active, so an application log stamped with it can
    later be correlated with the trace. Read from a task-local context, so each
    asyncio task and thread observes its own active trace and never another's.
    While recording is disabled (``configure(enabled=False)``) or the trace was
    sampled out, this still returns the live id inside the trace so log
    correlation keeps working even though nothing is persisted.
    """

    return _current_trace_id.get()


def get_current_span_id() -> str | None:
    """Return the innermost active span's id, or ``None`` outside any trace.

    Inside a nested ``span()``/``generation()``/``tool_call()`` this is the
    innermost open node's id; directly inside a trace with no open child it is the
    trace root's id. The value is the same id written to the ``parent_id`` field
    of any child event created at this point, so it names what an event recorded
    now would attach to. Read from a task-local context, so each asyncio task and
    thread observes its own ids and never another's. Like
    :func:`get_current_trace_id`, this still returns the live id while recording
    is disabled or the trace was sampled out, so log correlation is unaffected.
    """

    return _current_parent_id.get()


def _trace_context(
    *,
    name: str,
    metadata: Mapping[str, Any] | None = None,
) -> _TraceContext:
    return _TraceContext(name=name, metadata=metadata)


def _record_score_event(
    *,
    trace_id: str,
    parent_id: str,
    name: str,
    value: int | float,
    metadata: Mapping[str, Any] | None = None,
    timestamp: str | None = None,
) -> None:
    _validate_event_name(name, "score name")
    score_value = _validate_number(value, "score value")
    score_time = timestamp or _now()
    _write_event(
        _event(
            event_id=_new_id(),
            trace_id=trace_id,
            parent_id=parent_id,
            name=name,
            event_type="score",
            start_time=score_time,
            end_time=score_time,
            status="success",
            error=None,
            metadata=_safe_capture(dict(metadata or {})),
            value=score_value,
        )
    )


def _merge_metadata(target: dict[str, Any], metadata: Mapping[str, Any]) -> None:
    """Merge user-supplied metadata into an event's pending metadata dict.

    Shared by the ``set_metadata`` setter on every trace-work context manager so
    context discovered mid-body (a resolved route, a cache-hit flag, a request
    id) can be recorded before the event is written. The argument must be a
    ``Mapping`` — mirroring the ``score()`` metadata check — and is applied with a
    plain ``dict.update``, so later keys win, both within a single call and across
    repeated calls. The merged dict is redacted by ``_safe_capture`` at the
    owning context manager's ``__exit__`` exactly like constructor-supplied
    metadata, so this never weakens redaction.
    """

    if not isinstance(metadata, Mapping):
        raise TypeError("bir set_metadata() requires a mapping")
    target.update(metadata)


class _TraceContext:
    def __init__(self, *, name: str, metadata: Mapping[str, Any] | None) -> None:
        _validate_event_name(name, "trace name")
        self.name = name
        self.metadata: dict[str, Any] = dict(metadata or {})
        self.id: str | None = None
        self.start_time: str | None = None
        self._dropped = False
        self._trace_token: Token[str | None] | None = None
        self._parent_token: Token[str | None] | None = None
        self._capture_inputs_token: Token[bool | None] | None = None
        self._capture_outputs_token: Token[bool | None] | None = None
        self._dropped_token: Token[bool] | None = None

    def __enter__(self) -> _TraceContext:
        self.id = _new_id()
        self.start_time = _now()
        self._dropped = _should_drop_trace(self.name)
        self._trace_token = _current_trace_id.set(self.id)
        self._parent_token = _current_parent_id.set(self.id)
        self._capture_inputs_token = _current_capture_inputs.set(_config.capture_inputs)
        self._capture_outputs_token = _current_capture_outputs.set(_config.capture_outputs)
        self._dropped_token = _current_trace_dropped.set(self._dropped)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc_type, traceback
        self._reset()

        if self.id is None or self.start_time is None:
            raise RuntimeError("bir trace context exited before it was entered")

        if self._dropped:
            return False

        event = _event(
            event_id=self.id,
            trace_id=self.id,
            parent_id=None,
            name=self.name,
            event_type="trace",
            start_time=self.start_time,
            end_time=_now(),
            status="error" if exc is not None else "success",
            error=_safe_error(exc) if exc is not None else None,
            metadata=_safe_capture(dict(self.metadata or {})),
        )
        try:
            _write_event(event)
        except Exception as storage_error:
            if exc is not None:
                raise exc from storage_error
            raise
        return False

    async def __aenter__(self) -> _TraceContext:
        # Delegate to the sync enter so one object works as both ``with trace(...)``
        # and ``async with trace(...)``. The trace and parent_id contextvars are set
        # here with no intervening await, so each asyncio task keeps its own values and
        # concurrent traces stay isolated.
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return self.__exit__(exc_type, exc, traceback)

    def set_metadata(self, metadata: Mapping[str, Any]) -> None:
        _merge_metadata(self.metadata, metadata)

    def _reset(self) -> None:
        if self._dropped_token is not None:
            _current_trace_dropped.reset(self._dropped_token)
        if self._capture_outputs_token is not None:
            _current_capture_outputs.reset(self._capture_outputs_token)
        if self._capture_inputs_token is not None:
            _current_capture_inputs.reset(self._capture_inputs_token)
        if self._parent_token is not None:
            _current_parent_id.reset(self._parent_token)
        if self._trace_token is not None:
            _current_trace_id.reset(self._trace_token)


class _Span:
    def __init__(self, name: str) -> None:
        self.name = name
        self.metadata: dict[str, Any] = {}
        self.id: str | None = None
        self.trace_id: str | None = None
        self.parent_id: str | None = None
        self.start_time: str | None = None
        self._parent_token: Token[str | None] | None = None

    def __enter__(self) -> _Span:
        trace_id = _current_trace_id.get()
        parent_id = _current_parent_id.get()
        if trace_id is None or parent_id is None:
            raise RuntimeError("bir.span() requires an active trace. Use it inside a @observe() function.")

        self.id = _new_id()
        self.trace_id = trace_id
        self.parent_id = parent_id
        self.start_time = _now()
        self._parent_token = _current_parent_id.set(self.id)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if self._parent_token is not None:
            _current_parent_id.reset(self._parent_token)

        if self.id is None or self.trace_id is None or self.start_time is None:
            raise RuntimeError("bir.span() exited before it was entered")

        event = _event(
            event_id=self.id,
            trace_id=self.trace_id,
            parent_id=self.parent_id,
            name=self.name,
            event_type="span",
            start_time=self.start_time,
            end_time=_now(),
            status="error" if exc is not None else "success",
            error=_safe_error(exc) if exc is not None else None,
            metadata=_safe_capture(dict(self.metadata or {})),
        )
        try:
            _write_event(event)
        except Exception as storage_error:
            if exc is not None:
                raise exc from storage_error
            raise
        return False

    async def __aenter__(self) -> _Span:
        # Delegate to the sync enter so one object works as both ``with span(...)``
        # and ``async with span(...)``. The parent_id contextvar is set here with no
        # intervening await, so each asyncio task keeps its own value and concurrent
        # spans stay isolated.
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return self.__exit__(exc_type, exc, traceback)

    def set_metadata(self, metadata: Mapping[str, Any]) -> None:
        _merge_metadata(self.metadata, metadata)


class _Generation:
    def __init__(
        self,
        *,
        name: str,
        model: str | None,
        input: Any,
        metadata: Mapping[str, Any] | None,
        prompt_record: PromptRecord | None,
        capture_input: bool | None,
        capture_output: bool | None,
    ) -> None:
        self.name = name
        self.model = model
        self.input = input
        self.metadata: dict[str, Any] = dict(metadata or {})
        self.prompt_record = prompt_record
        self.capture_input = capture_input
        self.capture_output = capture_output
        self.id: str | None = None
        self.trace_id: str | None = None
        self.parent_id: str | None = None
        self.start_time: str | None = None
        self.output: Any = None
        self.usage: dict[str, int | float] | None = None
        self.cost: dict[str, int | float] | None = None
        self.currency: str | None = None
        self._parent_token: Token[str | None] | None = None

    def __enter__(self) -> _Generation:
        trace_id = _current_trace_id.get()
        parent_id = _current_parent_id.get()
        if trace_id is None or parent_id is None:
            raise RuntimeError("bir.generation() requires an active trace. Use it inside a @observe() function.")

        self.id = _new_id()
        self.trace_id = trace_id
        self.parent_id = parent_id
        self.start_time = _now()
        self._parent_token = _current_parent_id.set(self.id)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if self._parent_token is not None:
            _current_parent_id.reset(self._parent_token)

        if self.id is None or self.trace_id is None or self.start_time is None:
            raise RuntimeError("bir.generation() exited before it was entered")

        input_payload = _safe_capture(self.input) if _should_capture(self.capture_input, "inputs") else None
        output_payload = _safe_capture(self.output) if _should_capture(self.capture_output, "outputs") else None
        metadata_payload = dict(self.metadata or {})
        if self.prompt_record is not None:
            metadata_payload["prompt"] = self.prompt_record.to_metadata()
        # Derive cost from a configured price table only when the caller set none;
        # an explicit set_cost() already populated self.cost and is never touched.
        self._fill_cost_from_prices()
        event = _event(
            event_id=self.id,
            trace_id=self.trace_id,
            parent_id=self.parent_id,
            name=self.name,
            event_type="generation",
            start_time=self.start_time,
            end_time=_now(),
            status="error" if exc is not None else "success",
            error=_safe_error(exc) if exc is not None else None,
            metadata=_safe_capture(metadata_payload),
            input=input_payload,
            output=output_payload,
            model=self.model,
            usage=self.usage,
            cost=self.cost,
            currency=self.currency,
        )
        try:
            _write_event(event)
        except Exception as storage_error:
            if exc is not None:
                raise exc from storage_error
            raise
        return False

    async def __aenter__(self) -> _Generation:
        # Delegate to the sync enter so one object works as both ``with generation(...)``
        # and ``async with generation(...)``. The parent_id contextvar is set here with no
        # intervening await, so each asyncio task keeps its own value and concurrent
        # generations stay isolated.
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return self.__exit__(exc_type, exc, traceback)

    def set_metadata(self, metadata: Mapping[str, Any]) -> None:
        _merge_metadata(self.metadata, metadata)

    def set_model(self, model: str | None) -> None:
        """Set or refine the model recorded on this generation.

        The model is read at ``__exit__``, so this can record a model only known
        once the provider responds (a streaming refinement, a router-chosen
        model) without passing it to ``generation(model=...)`` up front. Like the
        other ``set_*`` setters, the latest call wins. A non-empty string is
        validated like an event name; ``None`` is accepted and leaves no model,
        clearing any value supplied to ``generation(model=...)`` or by an earlier
        ``set_model(...)``.
        """

        if model is not None:
            _validate_event_name(model, "model")
        self.model = model

    def set_output(self, output: Any) -> None:
        self.output = output

    def set_usage(
        self,
        *,
        input_tokens: int | float | None = None,
        output_tokens: int | float | None = None,
        total_tokens: int | float | None = None,
    ) -> None:
        if input_tokens is None and output_tokens is None and total_tokens is None:
            raise ValueError("bir usage requires at least one token field")

        usage: dict[str, int | float] = {}
        if input_tokens is not None:
            usage["input_tokens"] = _validate_non_negative_number(input_tokens, "input_tokens")
        if output_tokens is not None:
            usage["output_tokens"] = _validate_non_negative_number(output_tokens, "output_tokens")
        if total_tokens is not None:
            usage["total_tokens"] = _validate_non_negative_number(total_tokens, "total_tokens")
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        self.usage = usage

    def set_cost(
        self,
        *,
        input_cost: int | float | None = None,
        output_cost: int | float | None = None,
        total_cost: int | float | None = None,
        currency: str = "USD",
    ) -> None:
        if input_cost is None and output_cost is None and total_cost is None:
            raise ValueError("bir cost requires at least one cost field")

        cost: dict[str, int | float] = {}
        if input_cost is not None:
            cost["input_cost"] = _validate_non_negative_number(input_cost, "input_cost")
        if output_cost is not None:
            cost["output_cost"] = _validate_non_negative_number(output_cost, "output_cost")
        if total_cost is not None:
            cost["total_cost"] = _validate_non_negative_number(total_cost, "total_cost")
        if total_cost is None and input_cost is not None and output_cost is not None:
            cost["total_cost"] = cost["input_cost"] + cost["output_cost"]
        validated_currency = _validate_currency(currency)
        self.cost = cost
        self.currency = validated_currency

    def _fill_cost_from_prices(self) -> None:
        """Derive cost from a configured price table when the caller set none.

        Fires only when ``configure(model_prices=...)`` holds an entry whose name
        matches this generation's model, usage is present, and no explicit
        ``set_cost(...)`` already ran (``self.cost is None``). The per-token
        ``input``/``output`` rates are multiplied by the matching token counts and
        routed through ``set_cost`` so the same validation, currency handling, and
        total derivation apply. An explicit cost always wins because this is a
        no-op once ``self.cost`` is set, and a usage lacking the needed token split
        (so neither side can be priced) leaves the cost unset.
        """

        if self.cost is not None or self.usage is None or self.model is None:
            return
        price = _price_for_model(self.model)
        if price is None:
            return
        input_tokens = self.usage.get("input_tokens")
        output_tokens = self.usage.get("output_tokens")
        input_cost = (
            price.input * input_tokens if price.input is not None and input_tokens is not None else None
        )
        output_cost = (
            price.output * output_tokens if price.output is not None and output_tokens is not None else None
        )
        if input_cost is None and output_cost is None:
            return
        self.set_cost(input_cost=input_cost, output_cost=output_cost, currency=price.currency)


class _ToolCall:
    def __init__(
        self,
        *,
        name: str,
        input: Any,
        metadata: Mapping[str, Any] | None,
        capture_input: bool | None,
        capture_output: bool | None,
    ) -> None:
        self.name = name
        self.input = input
        self.metadata: dict[str, Any] = dict(metadata or {})
        self.capture_input = capture_input
        self.capture_output = capture_output
        self.id: str | None = None
        self.trace_id: str | None = None
        self.parent_id: str | None = None
        self.start_time: str | None = None
        self.output: Any = None
        self._parent_token: Token[str | None] | None = None

    def __enter__(self) -> _ToolCall:
        trace_id = _current_trace_id.get()
        parent_id = _current_parent_id.get()
        if trace_id is None or parent_id is None:
            raise RuntimeError("bir.tool_call() requires an active trace. Use it inside a @observe() function.")

        self.id = _new_id()
        self.trace_id = trace_id
        self.parent_id = parent_id
        self.start_time = _now()
        self._parent_token = _current_parent_id.set(self.id)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if self._parent_token is not None:
            _current_parent_id.reset(self._parent_token)

        if self.id is None or self.trace_id is None or self.start_time is None:
            raise RuntimeError("bir.tool_call() exited before it was entered")

        input_payload = _safe_capture(self.input) if _should_capture(self.capture_input, "inputs") else None
        output_payload = _safe_capture(self.output) if _should_capture(self.capture_output, "outputs") else None
        event = _event(
            event_id=self.id,
            trace_id=self.trace_id,
            parent_id=self.parent_id,
            name=self.name,
            event_type="tool_call",
            start_time=self.start_time,
            end_time=_now(),
            status="error" if exc is not None else "success",
            error=_safe_error(exc) if exc is not None else None,
            metadata=_safe_capture(dict(self.metadata or {})),
            input=input_payload,
            output=output_payload,
        )
        try:
            _write_event(event)
        except Exception as storage_error:
            if exc is not None:
                raise exc from storage_error
            raise
        return False

    async def __aenter__(self) -> _ToolCall:
        # Delegate to the sync enter so one object works as both ``with tool_call(...)``
        # and ``async with tool_call(...)``. The parent_id contextvar is set here with no
        # intervening await, so each asyncio task keeps its own value and concurrent
        # tool calls stay isolated.
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return self.__exit__(exc_type, exc, traceback)

    def set_metadata(self, metadata: Mapping[str, Any]) -> None:
        _merge_metadata(self.metadata, metadata)

    def set_output(self, output: Any) -> None:
        self.output = output


class _Retrieval(_ToolCall):
    def __init__(
        self,
        *,
        name: str,
        query: Any,
        metadata: Mapping[str, Any] | None,
        capture_input: bool | None,
        capture_output: bool | None,
    ) -> None:
        retrieval_metadata = dict(metadata or {})
        retrieval_metadata["kind"] = "retrieval"
        super().__init__(
            name=name,
            input={"query": query},
            metadata=retrieval_metadata,
            capture_input=capture_input,
            capture_output=capture_output,
        )
        self.output = {"documents": []}

    def __enter__(self) -> _Retrieval:
        super().__enter__()
        return self

    async def __aenter__(self) -> _Retrieval:
        # Override the inherited tool-call ``__aenter__`` only to keep the static
        # return type ``_Retrieval``; the sync delegation it wraps is unchanged.
        await super().__aenter__()
        return self

    def add_document(
        self,
        *,
        id: str | None = None,
        text: str | None = None,
        rank: int | None = None,
        score: int | float | None = None,
        source: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        document: dict[str, Any] = {}
        if id is not None:
            document["id"] = id
        if rank is not None:
            document["rank"] = _validate_non_negative_int(rank, "retrieval document rank")
        if score is not None:
            document["score"] = _validate_non_negative_number(score, "retrieval document score")
        if source is not None:
            document["source"] = source
        if text is not None:
            document["text"] = text
        if metadata is not None:
            document["metadata"] = dict(metadata)
        self.output["documents"].append(document)

    def set_documents(self, documents: Iterable[Mapping[str, Any]]) -> None:
        self.output = {"documents": [_retrieval_document_from_mapping(document) for document in documents]}


def _event(
    *,
    event_id: str,
    trace_id: str,
    parent_id: str | None,
    name: str,
    event_type: str,
    start_time: str,
    end_time: str,
    status: str,
    error: str | None,
    metadata: Mapping[str, Any] | None = None,
    input: Any = None,
    output: Any = None,
    value: int | float | None = None,
    model: str | None = None,
    usage: Mapping[str, int | float] | None = None,
    cost: Mapping[str, int | float] | None = None,
    currency: str | None = None,
) -> dict[str, Any]:
    event_metadata = dict(metadata or {})
    if event_type == "trace":
        service_metadata = _service_metadata()
        if service_metadata is not None:
            event_metadata.setdefault("service", service_metadata)
        if _config.source is not None:
            event_metadata.setdefault("source", _config.source)
    event: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "id": event_id,
        "trace_id": trace_id,
        "parent_id": parent_id,
        "name": name,
        "type": event_type,
        "start_time": start_time,
        "end_time": end_time,
        "status": status,
        "metadata": event_metadata,
        "input": input,
        "output": output,
        "error": error,
    }
    if value is not None:
        event["value"] = value
    if model is not None:
        event["model"] = model
    if usage is not None:
        event["usage"] = dict(usage)
    if cost is not None:
        event["cost"] = dict(cost)
    if currency is not None:
        event["currency"] = currency
    return event


def _service_metadata() -> dict[str, str] | None:
    payload: dict[str, str] = {}
    if _config.service_name is not None:
        payload["name"] = _config.service_name
    if _config.environment is not None:
        payload["environment"] = _config.environment
    return payload or None


def _write_event(event: dict[str, Any]) -> None:
    # The master ``enabled`` switch short-circuits every write, so disabling
    # recording mid-trace (an incident toggle) stops even a trace that was
    # already sampled in, and any direct writer (``score``) is covered too.
    if not _config.enabled:
        return
    # Child events (spans, generations, tool calls, scores) run while the root's
    # contextvar is still active, so dropping them is centralized here. Trace
    # roots reset their contextvars before writing, so they check their own
    # stored decision instead and never reach this guard while dropped.
    if _current_trace_dropped.get():
        return
    payload = json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    with _write_lock:
        trace_path = _config.trace_path
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        with _InterProcessFileLock(trace_path):
            _rotate_trace_file_if_needed(trace_path, payload)
            with trace_path.open("a", encoding="utf-8") as trace_file:
                trace_file.write(payload)


def _rotate_trace_file_if_needed(trace_path: Path, payload: str) -> None:
    """Rotate the active trace file before a write that would exceed ``max_bytes``.

    A no-op unless ``max_bytes`` is configured. Rotation is decided on the
    already-complete active file (every prior write ended on a newline), so files
    only ever break on whole-line boundaries. The incoming line is never split:
    when an empty active file is about to receive a line larger than the cap, the
    line is still written whole rather than rotated away. Must be called while
    holding both ``_write_lock`` and the trace path's process lock.
    """

    max_bytes = _config.max_bytes
    if max_bytes is None:
        return
    try:
        current_size = trace_path.stat().st_size
    except FileNotFoundError:
        return
    if current_size == 0:
        return
    if current_size + len(payload.encode("utf-8")) <= max_bytes:
        return
    _rotate_trace_files(trace_path, _config.backup_count)


def _rotate_trace_files(trace_path: Path, backup_count: int) -> None:
    """Shift ``traces.jsonl`` -> ``.1`` -> ``.2`` .., dropping the oldest.

    ``Path.replace`` overwrites its destination atomically, so shifting ``.k``
    onto ``.k+1`` discards the previous oldest backup and keeps at most
    ``backup_count`` rotated files. ``backup_count == 0`` keeps none and just
    drops the filled active file.
    """

    if backup_count <= 0:
        trace_path.unlink(missing_ok=True)
        return
    for index in range(backup_count - 1, 0, -1):
        source = trace_path.with_name(f"{trace_path.name}.{index}")
        if source.exists():
            source.replace(trace_path.with_name(f"{trace_path.name}.{index + 1}"))
    trace_path.replace(trace_path.with_name(f"{trace_path.name}.1"))


def _events_endpoint(server_url: str) -> str:
    normalized_url = server_url.rstrip("/")
    if not normalized_url:
        raise ValueError("bir server_url must not be empty")
    return f"{normalized_url}/v1/events"


class _TransientSendError(Exception):
    """Internal signal that a send attempt failed transiently and may be retried.

    Carries the original cause so :func:`_send_with_retry` can chain it when the
    retries are exhausted and the failure is re-raised as a ``RuntimeError``.
    """

    def __init__(self, message: str, *, cause: BaseException) -> None:
        super().__init__(message)
        self.cause = cause


def _is_retryable_status(status: int) -> bool:
    """Return True for HTTP 5xx, the only status codes worth retrying."""

    return 500 <= status < 600


def _post_event_batch(
    endpoint: str,
    events: list[dict[str, Any]],
    *,
    timeout: float,
) -> SendEventsResult | None:
    """Post all events in one request; return None when the server has no batch endpoint."""

    payload = json.dumps(events, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        body = exc.read().decode("utf-8", errors="replace")
        message = f"bir server rejected event batch with HTTP {exc.code}: {body}"
        if _is_retryable_status(exc.code):
            raise _TransientSendError(message, cause=exc) from exc
        raise RuntimeError(message) from exc
    except urllib.error.URLError as exc:
        raise _TransientSendError(f"bir could not send events to {endpoint}: {exc.reason}", cause=exc) from exc
    except TimeoutError as exc:
        # A socket read timeout surfaces as TimeoutError rather than URLError.
        raise _TransientSendError(f"bir could not send events to {endpoint}: {exc}", cause=exc) from exc

    if status < 200 or status >= 300:
        raise RuntimeError(f"bir server rejected event batch with HTTP {status}: {body}")
    return _batch_result_from_response(body, attempted=len(events))


def _batch_result_from_response(body: str, *, attempted: int) -> SendEventsResult:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"bir server returned an invalid batch response: {body}") from exc
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"bir server returned an invalid batch response: {body}")
    accepted = payload.get("accepted")
    event_ids = payload.get("event_ids")
    if isinstance(accepted, bool) or not isinstance(accepted, int):
        raise RuntimeError(f"bir server returned an invalid batch response: {body}")
    if not isinstance(event_ids, list) or not all(isinstance(event_id, str) for event_id in event_ids):
        raise RuntimeError(f"bir server returned an invalid batch response: {body}")
    return SendEventsResult(accepted=accepted, event_ids=list(event_ids), attempted=attempted)


def _post_event(endpoint: str, event: Mapping[str, Any], *, timeout: float) -> int:
    payload = json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        message = f"bir server rejected event with HTTP {exc.code}: {body}"
        if _is_retryable_status(exc.code):
            raise _TransientSendError(message, cause=exc) from exc
        raise RuntimeError(message) from exc
    except urllib.error.URLError as exc:
        raise _TransientSendError(f"bir could not send event to {endpoint}: {exc.reason}", cause=exc) from exc
    except TimeoutError as exc:
        # A socket read timeout surfaces as TimeoutError rather than URLError.
        raise _TransientSendError(f"bir could not send event to {endpoint}: {exc}", cause=exc) from exc

    if status < 200 or status >= 300:
        raise RuntimeError(f"bir server rejected event with HTTP {status}: {body}")
    return _accepted_count_from_response(body)


def _accepted_count_from_response(body: str) -> int:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return 1
    if not isinstance(payload, Mapping):
        return 1
    accepted = payload.get("accepted")
    if isinstance(accepted, int) and not isinstance(accepted, bool):
        return accepted
    return 1


def _sent_ids_path(path: str | Path | None) -> Path:
    """Return the sidecar path that records IDs the server has already accepted.

    The sidecar lives next to the trace file being sent (``<trace_path>.sent``).
    ``path`` is resolved the same way :func:`load_events` resolves it so a custom
    ``send_events(path=...)`` records against the matching file. The ``.sent``
    suffix is non-numeric, so size-based rotation (which only shifts ``.1`` ..
    ``.N`` siblings) never touches it.
    """

    trace_path = Path(path) if path is not None else _config.trace_path
    return trace_path.with_name(trace_path.name + _SENT_IDS_SUFFIX)


def _load_sent_ids(sent_ids_path: Path) -> set[str]:
    """Load the set of already-sent event IDs from the sidecar.

    Bookkeeping must never block a send, so a missing, unreadable, or malformed
    sidecar is treated as empty: the worst case is re-sending events the
    idempotent server already has, exactly as a send without ``mark_sent`` would.
    """

    try:
        raw = sent_ids_path.read_text(encoding="utf-8")
    except OSError:
        return set()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if not isinstance(payload, Mapping):
        return set()
    event_ids = payload.get("event_ids")
    if not isinstance(event_ids, list):
        return set()
    return {event_id for event_id in event_ids if isinstance(event_id, str)}


def _record_sent_ids(sent_ids_path: Path, event_ids: list[str]) -> None:
    """Merge ``event_ids`` into the sidecar of already-sent IDs.

    Writes the union of the previously recorded IDs and the newly accepted ones
    through a temp-file replace so a crash mid-write cannot corrupt the sidecar.
    Only ever touches ``<trace_path>.sent`` — the trace JSONL is never modified.
    """

    sent_ids_path.parent.mkdir(parents=True, exist_ok=True)
    with _sent_ids_lock:
        with _InterProcessFileLock(sent_ids_path):
            merged = _load_sent_ids(sent_ids_path)
            merged.update(event_ids)
            payload = json.dumps({"event_ids": sorted(merged)}, separators=(",", ":")) + "\n"
            temp_path = sent_ids_path.with_name(f".{sent_ids_path.name}.{os.getpid()}.{uuid4().hex}.tmp")
            try:
                temp_path.write_text(payload, encoding="utf-8")
                temp_path.replace(sent_ids_path)
            finally:
                temp_path.unlink(missing_ok=True)


def _trace_event_from_payload(payload: dict[Any, Any], *, trace_path: Path, line_number: int) -> TraceEvent:
    required_fields = (
        "schema_version",
        "id",
        "trace_id",
        "parent_id",
        "name",
        "type",
        "start_time",
        "end_time",
        "status",
        "metadata",
        "input",
        "output",
        "error",
    )
    for field in required_fields:
        if field not in payload:
            raise ValueError(f"Trace file {trace_path} line {line_number} is missing required field {field!r}")

    schema_version = _expect_string(payload["schema_version"], "schema_version", trace_path, line_number)
    if schema_version != _SCHEMA_VERSION:
        raise ValueError(
            f"Trace file {trace_path} line {line_number} has unsupported schema_version {schema_version!r}"
        )
    event_id = _expect_string(payload["id"], "id", trace_path, line_number)
    trace_id = _expect_string(payload["trace_id"], "trace_id", trace_path, line_number)
    parent_id = _expect_optional_string(payload["parent_id"], "parent_id", trace_path, line_number)
    name = _expect_string(payload["name"], "name", trace_path, line_number)
    event_type = _expect_string(payload["type"], "type", trace_path, line_number)
    if event_type not in _EVENT_TYPES:
        raise ValueError(f"Trace file {trace_path} line {line_number} field 'type' has unsupported value {event_type!r}")
    start_time = _expect_datetime_string(payload["start_time"], "start_time", trace_path, line_number)
    end_time = _expect_datetime_string(payload["end_time"], "end_time", trace_path, line_number)
    if datetime.fromisoformat(end_time) < datetime.fromisoformat(start_time):
        raise ValueError(f"Trace file {trace_path} line {line_number} has end_time before start_time")
    status = _expect_string(payload["status"], "status", trace_path, line_number)
    if status not in _EVENT_STATUSES:
        raise ValueError(f"Trace file {trace_path} line {line_number} field 'status' has unsupported value {status!r}")
    metadata = _expect_mapping(payload["metadata"], "metadata", trace_path, line_number)
    error = _expect_optional_string(payload["error"], "error", trace_path, line_number)
    if event_type == "trace" and event_id != trace_id:
        raise ValueError(f"Trace file {trace_path} line {line_number} trace event id must match trace_id")
    if event_type == "trace" and parent_id is not None:
        raise ValueError(f"Trace file {trace_path} line {line_number} trace event parent_id must be null")
    if event_type != "trace" and parent_id is None:
        raise ValueError(f"Trace file {trace_path} line {line_number} {event_type} event requires parent_id")
    event_value = None
    if event_type == "score":
        if "value" not in payload:
            raise ValueError(f"Trace file {trace_path} line {line_number} score event is missing required field 'value'")
        event_value = _validate_number(payload["value"], "score value")
    elif payload.get("value") is not None:
        event_value = _validate_number(payload["value"], "value")
    event_model = None
    if payload.get("model") is not None:
        event_model = _expect_string(payload["model"], "model", trace_path, line_number)
    event_usage = None
    if "usage" in payload:
        usage = payload["usage"]
        if usage is not None:
            if not isinstance(usage, Mapping):
                raise ValueError(f"Trace file {trace_path} line {line_number} field 'usage' must be an object")
            event_usage = {}
            for key, value in usage.items():
                usage_key = _expect_string(key, "usage key", trace_path, line_number)
                event_usage[usage_key] = _validate_non_negative_number(value, f"usage.{key}")
    event_cost = None
    if "cost" in payload:
        cost = payload["cost"]
        if cost is not None:
            if not isinstance(cost, Mapping):
                raise ValueError(f"Trace file {trace_path} line {line_number} field 'cost' must be an object")
            event_cost = {}
            for key, value in cost.items():
                cost_key = _expect_string(key, "cost key", trace_path, line_number)
                event_cost[cost_key] = _validate_non_negative_number(value, f"cost.{cost_key}")
    event_currency = None
    if payload.get("currency") is not None:
        event_currency = _expect_string(payload["currency"], "currency", trace_path, line_number)
    _validate_json_value(metadata, "metadata", trace_path, line_number)
    _validate_json_value(payload["input"], "input", trace_path, line_number)
    _validate_json_value(payload["output"], "output", trace_path, line_number)
    for key, value in payload.items():
        _expect_string(key, "event key", trace_path, line_number)
        if key not in required_fields and key not in {"value", "model", "usage", "cost", "currency"}:
            _validate_json_value(value, key, trace_path, line_number)
    raw = {str(key): value for key, value in payload.items()}

    return TraceEvent(
        id=event_id,
        trace_id=trace_id,
        parent_id=parent_id,
        name=name,
        type=event_type,
        start_time=start_time,
        end_time=end_time,
        status=status,
        metadata=metadata,
        input=payload["input"],
        output=payload["output"],
        error=error,
        raw=raw,
        value=event_value,
        model=event_model,
        usage=event_usage,
        cost=event_cost,
        currency=event_currency,
    )


def _expect_string(value: Any, field: str, trace_path: Path, line_number: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Trace file {trace_path} line {line_number} field {field!r} must be a string")
    return value


def _expect_optional_string(value: Any, field: str, trace_path: Path, line_number: int) -> str | None:
    if value is None:
        return None
    return _expect_string(value, field, trace_path, line_number)


def _expect_datetime_string(value: Any, field: str, trace_path: Path, line_number: int) -> str:
    timestamp = _expect_string(value, field, trace_path, line_number)
    try:
        datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise ValueError(
            f"Trace file {trace_path} line {line_number} field {field!r} must be an ISO datetime"
        ) from exc
    return timestamp


def _expect_mapping(value: Any, field: str, trace_path: Path, line_number: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Trace file {trace_path} line {line_number} field {field!r} must be an object")
    return {str(key): item for key, item in value.items()}


def _validate_json_value(value: Any, field: str, trace_path: Path, line_number: int) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        try:
            _validate_number(value, field)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Trace file {trace_path} line {line_number} field {field!r} must be finite") from exc
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{field}[{index}]", trace_path, line_number)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"Trace file {trace_path} line {line_number} field {field!r} keys must be strings")
            _validate_json_value(item, f"{field}.{key}", trace_path, line_number)
        return
    raise ValueError(f"Trace file {trace_path} line {line_number} field {field!r} must be JSON-compatible")


def _duration_ms(start_time: str, end_time: str) -> float:
    start = datetime.fromisoformat(start_time)
    end = datetime.fromisoformat(end_time)
    return (end - start).total_seconds() * 1000


def _should_capture(override: bool | None, target: str) -> bool:
    if override is not None:
        return override
    context_value = _current_capture_inputs.get() if target == "inputs" else _current_capture_outputs.get()
    if context_value is not None:
        return context_value
    if target == "inputs":
        return _config.capture_inputs
    return _config.capture_outputs


def _should_drop_trace(trace_name: str) -> bool:
    """Decide whether the trace starting now should be sampled out.

    The master ``enabled`` switch is checked first: when recording is disabled
    every trace root is dropped (and its descendants inherit the decision), so a
    single flag turns all recording off without re-rolling sampling. Otherwise
    ``sample_rate`` is the probability of keeping a trace. An exact-name
    ``sample_rules`` entry overrides the global rate for the matching trace root;
    otherwise the global rate applies. The deterministic edges (``1.0`` keeps
    everything, ``0.0`` drops everything) never touch the random generator. Only
    partial rates draw from ``random.random()``.
    """

    if not _config.enabled:
        return True
    sample_rate = _sample_rate_for_trace(trace_name)
    if sample_rate >= 1.0:
        return False
    if sample_rate <= 0.0:
        return True
    return random.random() >= sample_rate


def _capture_call_input(
    signature: inspect.Signature,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    bound = signature.bind_partial(*args, **kwargs)
    bound.apply_defaults()
    return {name: _safe_capture(value, key=name) for name, value in bound.arguments.items()}


def _safe_capture(value: Any, *, key: str | None = None, depth: int = 0) -> Any:
    if key is not None and _is_secret_key(key):
        return _REDACTED
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return _truncate_captured_text(_redact_secret_text(value))
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, Path):
        return _truncate_captured_text(_redact_secret_text(str(value)))
    if depth >= _MAX_CAPTURE_DEPTH:
        return _MAX_DEPTH_REACHED
    if isinstance(value, Mapping):
        return _capture_mapping(value, depth)
    if isinstance(value, (list, tuple, set, frozenset)):
        return _capture_sequence(value, depth)
    return _truncate_captured_text(_safe_repr(value))


def _truncate_captured_text(text: str) -> str:
    """Bound an already-redacted captured string to ``max_value_length``.

    Truncation runs only on text that redaction has already processed, so a
    secret is always replaced before any cut and can never be split in a way
    that defeats the redactor. With no ``max_value_length`` configured (the
    default) the text is returned unchanged, so capture stays byte-for-byte
    identical unless a caller opts in.
    """

    limit = _config.max_value_length
    if limit is None or len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATED


def _capture_mapping(value: Mapping[Any, Any], depth: int) -> dict[str, Any]:
    """Capture a mapping, bounding entry count by ``max_collection_items``.

    With no limit configured every entry is captured, matching the historical
    behavior. When a limit is set and the mapping is larger, only the first that
    many entries (in iteration order) are kept and a single ``_TRUNCATED``
    sentinel entry records that the remainder was dropped, keeping the result
    valid JSON.
    """

    limit = _config.max_collection_items
    captured: dict[str, Any] = {}
    truncated = False
    for index, (item_key, item_value) in enumerate(value.items()):
        if limit is not None and index >= limit:
            truncated = True
            break
        item_key_text = _safe_key(item_key)
        captured[item_key_text] = _safe_capture(item_value, key=item_key_text, depth=depth + 1)
    if truncated:
        captured[_TRUNCATED] = _TRUNCATED
    return captured


def _capture_sequence(value: Iterable[Any], depth: int) -> list[Any]:
    """Capture a list/tuple/set, bounding item count by ``max_collection_items``.

    With no limit configured every item is captured, matching the historical
    behavior. When a limit is set and the sequence is larger, only the first
    that many items (in iteration order) are kept and a single ``_TRUNCATED``
    marker element records that the remainder was dropped, keeping the result
    valid JSON.
    """

    limit = _config.max_collection_items
    captured: list[Any] = []
    truncated = False
    for index, item in enumerate(value):
        if limit is not None and index >= limit:
            truncated = True
            break
        captured.append(_safe_capture(item, depth=depth + 1))
    if truncated:
        captured.append(_TRUNCATED)
    return captured


def _is_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    # The user-supplied ``additional_secret_keys`` are stored already normalized to
    # this same form, so they join the built-in name set as exact whole-name
    # matches (never substrings) and can only add coverage, never remove it.
    if normalized in _SECRET_KEY_NAMES or normalized in _config.additional_secret_keys:
        return True
    return any(secret_part in normalized for secret_part in _SECRET_KEY_PARTS)


def _safe_key(value: Any) -> str:
    try:
        return str(value)
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"


def _safe_repr(value: Any) -> str:
    try:
        return _redact_secret_text(repr(value))
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"


def _safe_error(exc: BaseException) -> str:
    return _redact_secret_text(str(exc))


def _redact_secret_text(value: str) -> str:
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
    # Stripe secret/restricted keys (``sk_live_``/``sk_test_``/``rk_live_``/``rk_test_``).
    redacted = re.sub(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}\b", _REDACTED, redacted)
    # Azure storage-style account keys: a 512-bit key base64-encoded to 88 chars
    # ending in ``==``. Anchored to that exact length class to avoid over-redaction.
    redacted = re.sub(r"(?<![0-9A-Za-z+/])[0-9A-Za-z+/]{86}==(?![0-9A-Za-z+/=])", _REDACTED, redacted)
    # PEM private-key blocks (``-----BEGIN ... PRIVATE KEY-----`` .. ``-----END ...
    # PRIVATE KEY-----``), spanning lines via a non-greedy DOTALL-scoped match.
    redacted = re.sub(
        r"(?s)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        _REDACTED,
        redacted,
    )
    # Credit-card / PAN numbers: 13-19 digit sequences, optionally split into
    # groups by single spaces or hyphens, that pass the Luhn checksum. A pure
    # regex cannot verify Luhn, so the regex only finds candidate digit groups and
    # the Luhn gate in ``_redact_pan_match`` decides replacement -- a candidate
    # that fails Luhn (an ordinary long id, a phone number) is left untouched. The
    # digit-run boundaries (``\b`` plus the {12,18}+1 length) also leave runs of
    # 20+ digits intact, so this never over-redacts an arbitrary long integer.
    redacted = re.sub(r"\b(?:\d[ -]?){12,18}\d\b", _redact_pan_match, redacted)
    # User-supplied patterns run last, in configuration order, and only ever add
    # redaction on top of the built-in rules above; each replaces its whole match
    # with the marker. They cannot weaken or bypass any built-in rule.
    for pattern in _config.additional_redaction_patterns:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted


def _redact_labeled_secret_match(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2) or ''}{_REDACTED}"


def _redact_bearer_secret_match(match: re.Match[str]) -> str:
    return f"{match.group(1)}{_REDACTED}"


def _redact_pan_match(match: re.Match[str]) -> str:
    """Redact a candidate card number only when its digits pass the Luhn check.

    The candidate regex matches a 13-19 digit run (optionally single-space- or
    hyphen-separated); this gate strips the separators and replaces the whole run
    with the marker only when the bare digits satisfy the Luhn checksum, so an
    arbitrary long digit string that happens to match the shape is left intact.
    """

    candidate = match.group(0)
    digits = candidate.replace(" ", "").replace("-", "")
    if 13 <= len(digits) <= 19 and _luhn_checksum_valid(digits):
        return _REDACTED
    return candidate


def _luhn_checksum_valid(digits: str) -> bool:
    """Return whether a bare digit string passes the Luhn (mod-10) checksum.

    ``digits`` must contain only ASCII digits (the caller strips separators).
    Doubling starts from the second-rightmost digit, matching the standard
    payment-card Luhn definition.
    """

    total = 0
    for index, char in enumerate(reversed(digits)):
        value = ord(char) - 48
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _validate_additional_secret_keys(value: Any) -> frozenset[str]:
    """Validate and normalize the user-supplied extra secret-key names.

    Accepts any non-string iterable of non-empty strings and returns them
    normalized to the same form the built-in name rule uses (lower-cased with
    ``-`` treated as ``_``), so matching is exact and case-insensitive. A bare
    ``str``/``bytes`` is rejected so a single key is never silently iterated
    character by character. The entry count and per-key length are bounded so a
    pathologically large configuration fails fast with a clear error.
    """

    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise TypeError("bir additional_secret_keys must be an iterable of strings")
    keys = list(value)
    if len(keys) > _MAX_ADDITIONAL_SECRET_KEYS:
        raise ValueError(f"bir additional_secret_keys must not exceed {_MAX_ADDITIONAL_SECRET_KEYS} entries")
    normalized: set[str] = set()
    for key in keys:
        if not isinstance(key, str):
            raise TypeError("bir additional_secret_keys entries must be strings")
        if not key:
            raise ValueError("bir additional_secret_keys entries must not be empty")
        if len(key) > _MAX_ADDITIONAL_SECRET_KEY_LENGTH:
            raise ValueError(
                f"bir additional_secret_keys entries must not exceed "
                f"{_MAX_ADDITIONAL_SECRET_KEY_LENGTH} characters"
            )
        normalized.add(key.lower().replace("-", "_"))
    return frozenset(normalized)


def _validate_additional_redaction_patterns(value: Any) -> tuple[re.Pattern[str], ...]:
    """Validate and compile the user-supplied extra text-redaction patterns.

    Accepts any non-string iterable of regex strings and/or already-compiled
    ``re.Pattern`` objects, compiling and validating each exactly once here so a
    bad pattern fails at ``configure`` time rather than on a later capture. The
    entry count is bounded to reject unboundedly large configuration. The
    returned patterns run in order, after every built-in rule.
    """

    if isinstance(value, (str, bytes, re.Pattern)) or not isinstance(value, Iterable):
        raise TypeError(
            "bir additional_redaction_patterns must be an iterable of regex strings or compiled patterns"
        )
    raw_patterns = list(value)
    if len(raw_patterns) > _MAX_ADDITIONAL_REDACTION_PATTERNS:
        raise ValueError(
            f"bir additional_redaction_patterns must not exceed {_MAX_ADDITIONAL_REDACTION_PATTERNS} entries"
        )
    return tuple(_compile_additional_redaction_pattern(pattern) for pattern in raw_patterns)


def _compile_additional_redaction_pattern(pattern: Any) -> re.Pattern[str]:
    """Return a compiled ``str`` regex for one ``additional_redaction_patterns`` entry.

    An already-compiled ``re.Pattern`` is accepted as-is so the caller's flags are
    preserved, but a bytes pattern is rejected because it cannot apply to captured
    text. A string is length-checked and compiled with default flags; an empty
    string or invalid regex raises ``ValueError``.
    """

    if isinstance(pattern, re.Pattern):
        if isinstance(pattern.pattern, bytes):
            raise TypeError(
                "bir additional_redaction_patterns compiled patterns must be str patterns, not bytes"
            )
        return cast("re.Pattern[str]", pattern)
    if not isinstance(pattern, str):
        raise TypeError(
            "bir additional_redaction_patterns entries must be regex strings or compiled re.Pattern objects"
        )
    if not pattern:
        raise ValueError("bir additional_redaction_patterns entries must not be empty")
    if len(pattern) > _MAX_ADDITIONAL_REDACTION_PATTERN_LENGTH:
        raise ValueError(
            f"bir additional_redaction_patterns entries must not exceed "
            f"{_MAX_ADDITIONAL_REDACTION_PATTERN_LENGTH} characters"
        )
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"bir additional_redaction_patterns entry is not a valid regex: {exc}") from exc


def _validate_event_name(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"bir {field} must be a string")
    if not value:
        raise ValueError(f"bir {field} must not be empty")
    return value


def _validate_bool(value: Any, field: str) -> bool:
    # Reject ints (and everything else): ``enabled`` is a strict on/off switch,
    # so ``1``/``0`` or a truthy string must not silently pass as a bool.
    if not isinstance(value, bool):
        raise TypeError(f"bir {field} must be a bool")
    return value


def _validate_number(value: Any, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"bir {field} must be an int or float")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"bir {field} must be finite")
    return value


def _validate_non_negative_number(value: Any, field: str) -> int | float:
    numeric_value = _validate_number(value, field)
    if numeric_value < 0:
        raise ValueError(f"bir {field} must be non-negative")
    return numeric_value


def _validate_sample_rate(value: Any) -> float:
    numeric_value = _validate_number(value, "sample_rate")
    if numeric_value < 0.0 or numeric_value > 1.0:
        raise ValueError("bir sample_rate must be between 0.0 and 1.0")
    return float(numeric_value)


def _validate_sample_rules(value: Any) -> tuple[tuple[str, float], ...]:
    """Validate exact trace-root-name sampling overrides.

    Accepts a mapping of trace root name to sample rate. The returned tuple is
    sorted by name so the frozen config remains deterministic and hashable. An
    empty mapping is valid and clears the prior rule table.
    """

    if not isinstance(value, Mapping):
        raise TypeError("bir sample_rules must be a mapping of trace name to sample rate")
    if len(value) > _MAX_SAMPLE_RULES:
        raise ValueError(f"bir sample_rules must not exceed {_MAX_SAMPLE_RULES} entries")
    normalized: list[tuple[str, float]] = []
    for trace_name, sample_rate in value.items():
        name = _validate_event_name(trace_name, "sample_rules keys")
        try:
            rate = _validate_sample_rate(sample_rate)
        except TypeError as exc:
            raise TypeError(f"bir sample_rules[{name!r}] rate must be an int or float") from exc
        except ValueError as exc:
            message = str(exc).replace("sample_rate", f"sample_rules[{name!r}] rate")
            raise ValueError(message) from exc
        normalized.append((name, rate))
    normalized.sort(key=lambda item: item[0])
    return tuple(normalized)


def _sample_rate_for_trace(trace_name: str) -> float:
    for name, sample_rate in _config.sample_rules:
        if name == trace_name:
            return sample_rate
    return _config.sample_rate


def _validate_non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"bir {field} must be an int")
    if value < 0:
        raise ValueError(f"bir {field} must be non-negative")
    return value


def _retrieval_document_from_mapping(document: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(document)
    if "rank" in normalized and normalized["rank"] is not None:
        normalized["rank"] = _validate_non_negative_int(normalized["rank"], "retrieval document rank")
    if "score" in normalized and normalized["score"] is not None:
        normalized["score"] = _validate_non_negative_number(normalized["score"], "retrieval document score")
    return normalized


def _validate_currency(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("bir currency must be a string")
    if not value:
        raise ValueError("bir currency must not be empty")
    return value


def _validate_model_prices(value: Any) -> tuple[tuple[str, _ModelPrice], ...]:
    """Validate and normalize the opt-in ``model_prices`` table.

    Accepts a mapping of model name to a rates mapping, validating each entry once
    here so a bad table fails at ``configure`` time rather than on a later
    generation. Returns a name-sorted tuple of ``(model, _ModelPrice)`` pairs so
    the result is immutable and hashable on ``_Config``. An empty mapping is valid
    and clears any previously configured table.
    """

    if not isinstance(value, Mapping):
        raise TypeError("bir model_prices must be a mapping of model name to rates")
    if len(value) > _MAX_MODEL_PRICES:
        raise ValueError(f"bir model_prices must not exceed {_MAX_MODEL_PRICES} entries")
    normalized: list[tuple[str, _ModelPrice]] = []
    for model, rates in value.items():
        if not isinstance(model, str):
            raise TypeError("bir model_prices keys must be model-name strings")
        if not model:
            raise ValueError("bir model_prices keys must not be empty")
        normalized.append((model, _validate_model_price(rates, model)))
    normalized.sort(key=lambda item: item[0])
    return tuple(normalized)


def _validate_model_price(value: Any, model: str) -> _ModelPrice:
    """Validate one model's rate mapping into a frozen ``_ModelPrice``.

    Requires a mapping that sets at least one of the per-token ``input``/``output``
    rates, each validated as a non-negative, finite number (rejecting booleans),
    rejects any unknown rate key, and accepts an optional ``currency`` (default
    ``"USD"``) validated like ``set_cost``'s currency.
    """

    if not isinstance(value, Mapping):
        raise TypeError(f"bir model_prices[{model!r}] must be a mapping of rates")
    unknown = [key for key in value if key not in _MODEL_PRICE_RATE_KEYS]
    if unknown:
        listed = ", ".join(sorted(repr(key) for key in unknown))
        raise ValueError(f"bir model_prices[{model!r}] has unknown rate keys: {listed}")
    input_rate = value.get("input")
    output_rate = value.get("output")
    if input_rate is None and output_rate is None:
        raise ValueError(f"bir model_prices[{model!r}] must set at least one of 'input' or 'output'")
    validated_input = (
        _validate_non_negative_number(input_rate, f"model_prices[{model!r}].input")
        if input_rate is not None
        else None
    )
    validated_output = (
        _validate_non_negative_number(output_rate, f"model_prices[{model!r}].output")
        if output_rate is not None
        else None
    )
    currency = value.get("currency", "USD")
    return _ModelPrice(input=validated_input, output=validated_output, currency=_validate_currency(currency))


def _price_for_model(model: str) -> _ModelPrice | None:
    """Return the configured price entry whose model name matches, or ``None``.

    Reads the immutable, validated table on the active config. The table is empty
    by default, so with no ``configure(model_prices=...)`` this is a cheap miss
    and generation cost behavior is unchanged.
    """

    for name, price in _config.model_prices:
        if name == model:
            return price
    return None


def _event_sort_key(event: TraceEvent, depth: int = 0) -> tuple[str, int, int, str]:
    # ``depth`` (ancestor count) breaks ``start_time``/priority ties so an enclosing
    # parent sorts before a nested child even when their timestamps are identical.
    # Callers ordering siblings of a single parent can omit it: siblings share a
    # depth, so it never changes their order. Equal keys intentionally fall back to
    # Python's stable sort, preserving JSONL read order instead of exposing UUID
    # ordering when a coarse clock gives back-to-back events the same timestamp.
    return (
        event.start_time,
        _EVENT_SORT_PRIORITY.get(event.type, 99),
        depth,
        event.end_time,
    )


def _event_depths(events: list[TraceEvent]) -> dict[str, int]:
    """Map each event id to its depth (ancestor count) within ``events``.

    The trace root has depth 0 and every other event one more than its parent.
    Depth breaks ``start_time``/priority ties so an enclosing parent sorts before a
    nested child even when their timestamps are identical — which happens when the
    clock resolution is coarser than back-to-back event creation (notably on
    Windows), where a span and the tool call nested inside it can share a
    ``start_time``. The walk guards against a missing, self-referential, or cyclic
    ``parent_id`` so a malformed file cannot loop forever.
    """

    by_id = {event.id: event for event in events}
    depths: dict[str, int] = {}
    for event in events:
        depth = 0
        seen = {event.id}
        parent_id = event.parent_id
        while parent_id is not None and parent_id in by_id and parent_id not in seen:
            seen.add(parent_id)
            depth += 1
            parent_id = by_id[parent_id].parent_id
        depths[event.id] = depth
    return depths


def _reset_context(
    trace_token: Token[str | None],
    parent_token: Token[str | None],
    capture_inputs_token: Token[bool | None],
    capture_outputs_token: Token[bool | None],
    dropped_token: Token[bool],
) -> None:
    _current_trace_dropped.reset(dropped_token)
    _current_capture_outputs.reset(capture_outputs_token)
    _current_capture_inputs.reset(capture_inputs_token)
    _current_parent_id.reset(parent_token)
    _current_trace_id.reset(trace_token)


# Snapshot of every SDK contextvar value, used by the generator wrappers to swap
# the trace context in only while the underlying generator body is advancing.
# Tuple order matches ``_restore_context``: trace id, parent id, capture-inputs,
# capture-outputs, dropped.
_ContextSnapshot = tuple[str | None, str | None, bool | None, bool | None, bool]


def _snapshot_context() -> _ContextSnapshot:
    """Capture the current values of every SDK contextvar."""

    return (
        _current_trace_id.get(),
        _current_parent_id.get(),
        _current_capture_inputs.get(),
        _current_capture_outputs.get(),
        _current_trace_dropped.get(),
    )


def _restore_context(snapshot: _ContextSnapshot) -> None:
    """Restore the SDK contextvars to a previously captured snapshot.

    This sets absolute values rather than resetting tokens, so it composes with
    the token-based ``_begin_observe``/``_reset_context`` pair: the tokens created
    by ``_begin_observe`` still reset to their original pre-observe values at
    finalization regardless of the intermediate swaps performed here. Only the
    SDK's own contextvars are touched, so a generator body's effect on unrelated
    contextvars is left exactly as plain Python iteration would leave it.
    """

    _current_trace_id.set(snapshot[0])
    _current_parent_id.set(snapshot[1])
    _current_capture_inputs.set(snapshot[2])
    _current_capture_outputs.set(snapshot[3])
    _current_trace_dropped.set(snapshot[4])


def _new_id() -> str:
    return str(uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Environment-variable configuration
#
# The BIR_* variables let deployments configure the SDK without code changes.
# They supply the starting defaults for ``_config`` at import time; explicit
# ``configure(...)`` arguments still win. Capture stays opt-in: it is enabled
# only when an env var or a ``configure`` call asks for it.
# ---------------------------------------------------------------------------

_ENV_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_ENV_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _env_value(name: str) -> str | None:
    """Return the stripped value of ``name``, or ``None`` when unset or blank.

    A blank (whitespace-only) value is treated as unset so a deployment template
    that always defines ``BIR_*`` but sometimes leaves it empty falls back to the
    hardcoded default instead of failing at import.
    """

    raw = os.environ.get(name)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def _parse_env_bool(value: str, name: str) -> bool:
    """Parse a boolean-like environment value, rejecting ambiguous input."""

    normalized = value.strip().lower()
    if normalized in _ENV_TRUE_VALUES:
        return True
    if normalized in _ENV_FALSE_VALUES:
        return False
    allowed = ", ".join(sorted(_ENV_TRUE_VALUES | _ENV_FALSE_VALUES))
    raise ValueError(f"bir {name} must be a boolean-like value (one of: {allowed}), got {value!r}")


def _parse_env_sample_rate(value: str) -> float:
    """Parse a float sample rate from the environment and range-check it."""

    try:
        numeric = float(value.strip())
    except ValueError as exc:
        raise ValueError(f"bir BIR_SAMPLE_RATE must be a number between 0.0 and 1.0, got {value!r}") from exc
    return _validate_sample_rate(numeric)


def _parse_env_int(value: str, name: str) -> int:
    """Parse a non-negative integer capture-size limit from the environment.

    Reuses ``_validate_non_negative_int`` for the range check so the env path
    rejects the same values ``configure`` does, raising a clear ``ValueError``
    that names the variable for both non-integer text and a negative value.
    """

    try:
        numeric = int(value.strip())
    except ValueError as exc:
        raise ValueError(f"bir {name} must be a non-negative integer, got {value!r}") from exc
    return _validate_non_negative_int(numeric, name)


def _config_from_env() -> _Config:
    """Build the starting config from the ``BIR_*`` environment variables.

    Every field falls back to its hardcoded default when the matching variable is
    unset or blank, so with no environment set this returns a pristine
    ``_Config()``. Invalid values raise a clear error, and capture stays disabled
    unless an env var explicitly enables it.
    """

    defaults = _Config()
    trace_path = _env_value("BIR_TRACE_PATH")
    capture_inputs = _env_value("BIR_CAPTURE_INPUTS")
    capture_outputs = _env_value("BIR_CAPTURE_OUTPUTS")
    service_name = _env_value("BIR_SERVICE_NAME")
    environment = _env_value("BIR_ENVIRONMENT")
    source = _env_value("BIR_SOURCE")
    disabled = _env_value("BIR_DISABLED")
    sample_rate = _env_value("BIR_SAMPLE_RATE")
    max_value_length = _env_value("BIR_MAX_VALUE_LENGTH")
    max_collection_items = _env_value("BIR_MAX_COLLECTION_ITEMS")
    return _Config(
        trace_path=Path(trace_path) if trace_path is not None else defaults.trace_path,
        capture_inputs=(
            _parse_env_bool(capture_inputs, "BIR_CAPTURE_INPUTS")
            if capture_inputs is not None
            else defaults.capture_inputs
        ),
        capture_outputs=(
            _parse_env_bool(capture_outputs, "BIR_CAPTURE_OUTPUTS")
            if capture_outputs is not None
            else defaults.capture_outputs
        ),
        service_name=(
            _validate_event_name(service_name, "BIR_SERVICE_NAME")
            if service_name is not None
            else defaults.service_name
        ),
        environment=(
            _validate_event_name(environment, "BIR_ENVIRONMENT")
            if environment is not None
            else defaults.environment
        ),
        source=(
            _validate_event_name(source, "BIR_SOURCE")
            if source is not None
            else defaults.source
        ),
        # ``BIR_DISABLED`` is the inverse of the ``enabled`` field: a truthy value
        # disables all recording. Parsed with the shared boolean parser so it
        # rejects the same ambiguous values as the other BIR_* booleans.
        enabled=(
            not _parse_env_bool(disabled, "BIR_DISABLED")
            if disabled is not None
            else defaults.enabled
        ),
        sample_rate=(
            _parse_env_sample_rate(sample_rate) if sample_rate is not None else defaults.sample_rate
        ),
        max_value_length=(
            _parse_env_int(max_value_length, "BIR_MAX_VALUE_LENGTH")
            if max_value_length is not None
            else defaults.max_value_length
        ),
        max_collection_items=(
            _parse_env_int(max_collection_items, "BIR_MAX_COLLECTION_ITEMS")
            if max_collection_items is not None
            else defaults.max_collection_items
        ),
    )


# Apply env-derived defaults at import, now that the validators above exist.
# ``configure(...)`` still overrides these, and tests reset to a pristine
# ``_Config()`` via ``_reset_config_for_tests`` so ambient env never leaks in.
_config = _config_from_env()


def _reset_config_for_tests() -> None:
    """Reset the active config to hardcoded defaults, ignoring ambient env.

    Tests rely on a clean baseline, so this deliberately constructs a pristine
    ``_Config()`` rather than re-reading the ``BIR_*`` variables; otherwise a
    developer's real environment (or another test's monkeypatched env) could leak
    into an unrelated test.
    """

    global _config
    _config = _Config()
