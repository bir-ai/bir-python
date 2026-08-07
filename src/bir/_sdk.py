"""Core tracing lifecycle and public orchestration for the Bir SDK.

Dependency direction: this public compatibility surface owns mutable runtime
state and composes the lower-level configuration, capture, storage, and sending
modules. Those private modules never import this module or one another upward.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import logging
import os  # noqa: F401 - compatibility patch seam for storage internals
import random
import re
import sqlite3  # noqa: F401 - compatibility patch seam for storage internals
import tempfile  # noqa: F401 - compatibility patch seam for storage internals
import time  # noqa: F401 - compatibility patch seam for transport internals
import urllib.error  # noqa: F401 - compatibility patch seam for transport internals
import urllib.request  # noqa: F401 - compatibility patch seam for transport internals
from collections.abc import Iterator
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from types import TracebackType
from typing import Any, Callable, Iterable, Mapping, TypeVar, cast
from uuid import uuid4

from . import _capture as _capture_helpers
from . import _config as _config_helpers
from . import _sending as _sending_helpers
from . import _storage as _storage_helpers
from ._config import (
    _Config,
    _config_from_env,
    _is_finite_number,
    _ModelPrice,
    _retrieval_document_from_mapping,
    _validate_additional_redaction_patterns,
    _validate_additional_secret_keys,
    _validate_bool,
    _validate_currency,
    _validate_event_name,
    _validate_model_prices,
    _validate_non_negative_int,
    _validate_non_negative_number,
    _validate_number,
    _validate_positive_int,
    _validate_sample_rate,
    _validate_sample_rules,
)
from ._config import (
    _price_for_model as _configured_price_for_model,
)
from ._config import (
    _sample_rate_for_trace as _configured_sample_rate_for_trace,
)
from ._sending import SendEventsResult
from ._storage import LoadedTrace, TraceEvent

# Keep established private names available from ``bir._sdk`` while the focused
# modules own their implementations and constants.
_DEFAULT_TRACE_PATH = _config_helpers._DEFAULT_TRACE_PATH
_ENV_FALSE_VALUES = _config_helpers._ENV_FALSE_VALUES
_ENV_TRUE_VALUES = _config_helpers._ENV_TRUE_VALUES
_MAX_ADDITIONAL_REDACTION_PATTERNS = _config_helpers._MAX_ADDITIONAL_REDACTION_PATTERNS
_MAX_ADDITIONAL_REDACTION_PATTERN_LENGTH = _config_helpers._MAX_ADDITIONAL_REDACTION_PATTERN_LENGTH
_MAX_ADDITIONAL_SECRET_KEYS = _config_helpers._MAX_ADDITIONAL_SECRET_KEYS
_MAX_ADDITIONAL_SECRET_KEY_LENGTH = _config_helpers._MAX_ADDITIONAL_SECRET_KEY_LENGTH
_MAX_MODEL_PRICES = _config_helpers._MAX_MODEL_PRICES
_MAX_SAMPLE_RULES = _config_helpers._MAX_SAMPLE_RULES
_MODEL_PRICE_RATE_KEYS = _config_helpers._MODEL_PRICE_RATE_KEYS
_compile_additional_redaction_pattern = _config_helpers._compile_additional_redaction_pattern
_env_value = _config_helpers._env_value
_parse_env_bool = _config_helpers._parse_env_bool
_parse_env_int = _config_helpers._parse_env_int
_parse_env_sample_rate = _config_helpers._parse_env_sample_rate
_validate_model_price = _config_helpers._validate_model_price
_MAX_CAPTURE_DEPTH = _capture_helpers._MAX_CAPTURE_DEPTH
_MAX_DEPTH_REACHED = _capture_helpers._MAX_DEPTH_REACHED
_TRUNCATED = _capture_helpers._TRUNCATED
_REDACTED = _capture_helpers._REDACTED
_UNCAPTURABLE = _capture_helpers._UNCAPTURABLE
_SECRET_KEY_PARTS = _capture_helpers._SECRET_KEY_PARTS
_SECRET_KEY_NAMES = _capture_helpers._SECRET_KEY_NAMES
# Snapshotting a caller's ``metadata=`` mapping runs the mapping's own code, so
# it goes through the same no-raise contract as the rest of capture.
_safe_metadata = _capture_helpers._safe_metadata

F = TypeVar("F", bound=Callable[..., Any])

# Sidecar suffix appended to the trace file name to record the IDs the server has
# already accepted, so an opt-in ``send_events(mark_sent=True)`` can cheaply skip
# them on a later send. SDK-local bookkeeping only; never part of the event schema.
_SENT_IDS_SUFFIX = _storage_helpers._SENT_IDS_SUFFIX
_SCHEMA_VERSION = _storage_helpers._SCHEMA_VERSION
_EVENT_TYPES = _storage_helpers._EVENT_TYPES
_EVENT_STATUSES = _storage_helpers._EVENT_STATUSES
_EVENT_SORT_PRIORITY = _storage_helpers._EVENT_SORT_PRIORITY

# Storage aliases keep established private imports available without restoring
# a second implementation. Wrappers below are used only where active config is
# an input or a historical private signature must remain unchanged.
_write_lock = _storage_helpers._write_lock
_sent_ids_lock = _storage_helpers._sent_ids_lock
_InterProcessFileLock = _storage_helpers._InterProcessFileLock
_iter_trace_events_from_file = _storage_helpers._iter_trace_events_from_file
_trace_files_oldest_first = _storage_helpers._trace_files_oldest_first
_traces_from_events = _storage_helpers._traces_from_events
_iter_traces_from_events = _storage_helpers._iter_traces_from_events
_count_events_per_trace = _storage_helpers._count_events_per_trace
_iter_event_batches = _storage_helpers._iter_event_batches
_UploadEventSpool = _storage_helpers._UploadEventSpool
_rotate_trace_files = _storage_helpers._rotate_trace_files
_PruneResult = _storage_helpers._PruneResult
_PruneTraceSelection = _storage_helpers._PruneTraceSelection
_trace_starts_before = _storage_helpers._trace_starts_before
_prune_trace_id_key = _storage_helpers._prune_trace_id_key
_PruneTraceIndex = _storage_helpers._PruneTraceIndex
_select_removed_trace_ids = _storage_helpers._select_removed_trace_ids
_stream_filtered_trace_file = _storage_helpers._stream_filtered_trace_file
_stage_filtered_trace_file = _storage_helpers._stage_filtered_trace_file
_load_sent_ids = _storage_helpers._load_sent_ids
_record_sent_ids = _storage_helpers._record_sent_ids
_write_sent_ids = _storage_helpers._write_sent_ids
_compact_sent_ids = _storage_helpers._compact_sent_ids
_trace_event_from_payload = _storage_helpers._trace_event_from_payload
_expect_string = _storage_helpers._expect_string
_expect_optional_string = _storage_helpers._expect_optional_string
_expect_datetime_string = _storage_helpers._expect_datetime_string
_expect_mapping = _storage_helpers._expect_mapping
_validate_json_value = _storage_helpers._validate_json_value
_duration_ms = _storage_helpers._duration_ms
_event_sort_key = _storage_helpers._event_sort_key
_event_depths = _storage_helpers._event_depths

# Transport is dependency-bottom code; request grouping and checkpoint
# orchestration stay in this module so their long-standing patch seams remain.
_TransientSendError = _sending_helpers._TransientSendError
_events_endpoint = _sending_helpers._events_endpoint
_is_retryable_status = _sending_helpers._is_retryable_status
_read_http_error_body = _sending_helpers._read_http_error_body
_post_event_batch = _sending_helpers._post_event_batch
_batch_result_from_response = _sending_helpers._batch_result_from_response
_post_event = _sending_helpers._post_event
_accepted_count_from_response = _sending_helpers._accepted_count_from_response
_send_with_retry = _sending_helpers._send_with_retry
_current_trace_id: ContextVar[str | None] = ContextVar("bir_current_trace_id", default=None)
_current_parent_id: ContextVar[str | None] = ContextVar("bir_current_parent_id", default=None)
_current_capture_inputs: ContextVar[bool | None] = ContextVar("bir_current_capture_inputs", default=None)
_current_capture_outputs: ContextVar[bool | None] = ContextVar("bir_current_capture_outputs", default=None)
# Set once at trace-root creation so every descendant event of a sampled-out
# trace is skipped. False means "keep this trace"; the default keeps everything.
_current_trace_dropped: ContextVar[bool] = ContextVar("bir_current_trace_dropped", default=False)


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
            # Rendering can fail on a template/variables mismatch (e.g. literal
            # braces in the template). Tracing must never break the traced call,
            # so record why rendering failed instead of raising out of __exit__.
            try:
                rendered = self.render()
            except Exception as exc:
                payload["rendered_error"] = _safe_error(exc)
            else:
                payload["rendered"] = _safe_capture(rendered)
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


# ``bir._sdk._config`` holds the active configuration. It is initialized from
# the BIR_* environment variables near the bottom of this module and then
# replaced wholesale by ``configure``. The immutable value type and parsing
# helpers live in :mod:`bir._config`; runtime ownership of the active instance
# stays here.


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

    return _storage_helpers.load_events(
        path,
        include_rotated=include_rotated,
        default_path=_config.trace_path,
    )


def _iter_trace_events(
    path: str | Path | None = None,
    *,
    include_rotated: bool = False,
    on_invalid: Callable[[ValueError], None] | None = None,
) -> Iterator[TraceEvent]:
    """Yield validated local events in their original write order.

    The iterator keeps only one JSONL line and parsed event live at a time. It is
    the internal streaming primitive for store operations; public loaders still
    materialize their documented list return types. ``on_invalid`` skips
    unreadable lines instead of raising; see the storage iterator for why only
    display callers pass it.
    """

    return _storage_helpers._iter_trace_events(
        path,
        include_rotated=include_rotated,
        default_path=_config.trace_path,
        on_invalid=on_invalid,
    )


def load_traces(path: str | Path | None = None, *, include_rotated: bool = False) -> list[LoadedTrace]:
    """Load local traces grouped by trace_id.

    ``include_rotated`` is forwarded to :func:`load_events`; see its note about
    traces possibly being split across rotated files.
    """

    return _storage_helpers.load_traces(
        path,
        include_rotated=include_rotated,
        default_path=_config.trace_path,
    )


def _load_events_skipping_invalid(
    path: str | Path | None,
    *,
    include_rotated: bool,
    on_invalid: Callable[[ValueError], None],
) -> list[TraceEvent]:
    """Load events, handing each unreadable line to ``on_invalid`` and skipping it.

    The public loaders stay strict: refusing a store they cannot fully read is
    the contract a program building on them relies on. A person running the CLI
    against a store an interrupted write damaged needs the opposite — the events
    that are still intact, and a note about what could not be read — so the
    commands that only display events use this instead.
    """

    return _storage_helpers.load_events(
        path,
        include_rotated=include_rotated,
        default_path=_config.trace_path,
        on_invalid=on_invalid,
    )


def _load_traces_skipping_invalid(
    path: str | Path | None,
    *,
    include_rotated: bool,
    on_invalid: Callable[[ValueError], None],
) -> list[LoadedTrace]:
    """Group traces, handing each unreadable line to ``on_invalid`` and skipping it."""

    return _storage_helpers.load_traces(
        path,
        include_rotated=include_rotated,
        default_path=_config.trace_path,
        on_invalid=on_invalid,
    )


def send_events(
    server_url: str = "http://127.0.0.1:8000",
    *,
    path: str | Path | None = None,
    timeout: float = 10.0,
    retries: int = 2,
    backoff: float = 0.5,
    mark_sent: bool = False,
    include_rotated: bool = False,
    batch_size: int | None = None,
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

    Pruning bounds the sidecar. An ID naming an event the store no longer holds
    can never be matched by a later send, so ``bir prune`` drops it, and a
    deployment that prunes on a schedule keeps a sidecar proportional to the
    traces it retains rather than to everything it has ever sent. Without
    pruning, the sidecar grows with the number of IDs recorded.

    ``include_rotated`` is opt-in upload of size-rotated trace files. The default
    ``False`` uploads only the active trace file, matching the historical
    behavior. When ``True``, retained rotated siblings (``traces.jsonl.1`` ..)
    created by ``configure(max_bytes=...)`` are uploaded oldest-first followed by
    the active file, so rotation can no longer strand unsent events. Events are
    deduplicated by ID when a rotated file overlaps the active file, and the
    ``mark_sent`` sidecar still anchors to the active trace path so recorded IDs
    are skipped across the whole selected file set.

    ``batch_size`` opts into disk-backed bounded upload preparation. A positive
    value preserves the same ordering while sending at most that many events per
    request group. Successful groups are checkpointed immediately when
    ``mark_sent=True``, so a later failure can resume without re-sending them.
    The default ``None`` preserves the historical single-request path. The
    returned ``event_ids`` list, and the loaded sent-ID set when
    ``mark_sent=True``, still grow with the number of IDs by design.
    """

    timeout = float(_validate_non_negative_number(timeout, "timeout"))
    retries = _validate_non_negative_int(retries, "retries")
    backoff = float(_validate_non_negative_number(backoff, "backoff"))
    if batch_size is not None:
        batch_size = _validate_positive_int(batch_size, "batch_size")

    sent_ids_path = _sent_ids_path(path) if mark_sent else None
    if batch_size is not None:
        already_sent = _load_sent_ids(sent_ids_path) if sent_ids_path is not None else set()
        endpoint = _events_endpoint(server_url)
        with _UploadEventSpool() as spool:
            spool.add_events(_iter_trace_events(path, include_rotated=include_rotated))
            ordered_events: Iterable[TraceEvent] = spool.iter_ordered_events()
            if already_sent:
                ordered_events = (event for event in ordered_events if event.id not in already_sent)
            batches = _iter_event_batches(ordered_events, batch_size)
            return _post_loaded_event_batches(
                batches,
                endpoint,
                timeout=timeout,
                retries=retries,
                backoff=backoff,
                sent_ids_path=sent_ids_path,
            )

    events = _events_for_sending(path, include_rotated=include_rotated)
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


def _post_loaded_event_batches(
    batches: Iterable[list[TraceEvent]],
    endpoint: str,
    *,
    timeout: float,
    retries: int,
    backoff: float,
    sent_ids_path: Path | None = None,
) -> SendEventsResult:
    """Post batches sequentially and checkpoint each successful result.

    ``_post_loaded_events`` retains ownership of retry and batch-endpoint fallback
    behavior for each group. Results are aggregated only after a group returns
    successfully. If a later group raises, iteration stops immediately; accepted
    IDs returned by earlier groups have already been recorded when
    ``sent_ids_path`` is set. Counts are aggregated as reported by the server,
    while checkpoints contain only explicit ``event_ids`` and never infer IDs
    from an accepted count.
    """

    accepted = 0
    attempted = 0
    event_ids: list[str] = []
    for batch in batches:
        result = _post_loaded_events(
            batch,
            endpoint,
            timeout=timeout,
            retries=retries,
            backoff=backoff,
        )
        accepted += result.accepted
        attempted += result.attempted
        event_ids.extend(result.event_ids)
        if sent_ids_path is not None and result.event_ids:
            _record_sent_ids(sent_ids_path, result.event_ids)

    return SendEventsResult(accepted=accepted, event_ids=event_ids, attempted=attempted)


def _events_for_sending(
    path: str | Path | None = None,
    *,
    include_rotated: bool = False,
) -> list[TraceEvent]:
    """Order local events for upload: complete traces root-first, then orphans."""

    # Keep reads routed through this module's streaming wrapper so integrations
    # that instrument the established private seam still observe one store pass.
    events = _iter_trace_events(path, include_rotated=include_rotated)
    return _storage_helpers._order_events_for_sending(events)


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
    observe_metadata = _safe_metadata(metadata, field="observe metadata") if metadata is not None else None

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
                input_payload = _capture_call_input(signature, args, kwargs) if state.capture_inputs else None
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
                input_payload = _capture_call_input(signature, args, kwargs) if state.capture_inputs else None
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

    The caller re-raises ``exc`` after this returns. A store that cannot be
    written cannot change that: :func:`_write_event` reports a failed write
    rather than raising, so the user's own exception is what surfaces either way.
    ``metadata`` is optional event metadata (used by the generator wrappers); it
    defaults to ``None`` so the plain sync/async wrappers keep writing no
    metadata.
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
    _write_event(event)


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
        variables=_safe_metadata(variables, field="prompt variables"),
        rendered=rendered,
        metadata=_safe_metadata(metadata, field="prompt metadata"),
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
            metadata=_safe_capture(_safe_metadata(metadata, field="score metadata")),
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
            metadata=_safe_capture(_safe_metadata(metadata, field="score metadata")),
            value=score_value,
        )
    )


def _set_event_parent(context: Any, parent_id: str | None) -> None:
    """Record ``parent_id`` as this event's parent instead of the ambient one.

    ``context`` is an un-entered ``span()``, ``generation()``, ``tool_call()``, or
    ``retrieval()`` context. Bir normally parents an event to whichever event is
    open around it, which is right for code that nests by execution. The
    framework bridges do not nest that way: a framework announces its runs
    through callbacks and names each run's parent itself, so two runs it
    executes in parallel arrive interleaved and the open-context stack no longer
    describes the tree. A bridge maps the framework's parent id back to the Bir
    event id it recorded for that run and passes it here.

    Only the recorded ``parent_id`` changes. The event still becomes the ambient
    parent while it is open, so a provider wrapper or ``@observe()`` function the
    application runs inside a framework callback keeps nesting under it. Passing
    ``None`` (a run whose parent the bridge never saw, or has already ended)
    leaves the ambient parent in place. The id is written to the event as-is, so
    it must be an event id this process created in the same trace: this is
    internal to the bridges, not a way to inject a parent from outside.
    """

    if parent_id is not None:
        context._parent_override = parent_id


# Every contextvar token a Bir context holds while it is entered. Dropping them
# turns the context's teardown into a plain write, which is what reclaiming an
# abandoned bridge run needs; see :func:`_abandon_bridge_run`.
_BRIDGE_CONTEXT_TOKENS = (
    "_trace_token",
    "_parent_token",
    "_capture_inputs_token",
    "_capture_outputs_token",
    "_dropped_token",
)


def _abandon_bridge_run(context: Any, *, reason: str) -> None:
    """Write a bridge run's event without the end callback that never arrived.

    A framework bridge enters a Bir context in one callback and exits it in
    another. When the second never comes, the event is never written and the run
    keeps owning the ambient trace context, so everything recorded afterwards
    joins a trace whose root does not exist. Closing the run here writes the
    event that would otherwise be lost.

    The run's contextvar tokens are dropped rather than reset, which leaves the
    context's own teardown to do nothing but write; the caller restores the
    surrounding values with :func:`_restore_context` instead. A run is reclaimed
    from whichever callback noticed it was gone, which may be running in a
    context that merely inherited a copy of the one the run was entered in, where
    resetting a token raises. Restoring by value works in either, which is why
    the generator wrappers already tear their own context down that way.

    ``reason`` is recorded as ``metadata.abandoned``, so a run no framework ever
    closed is distinguishable in the store from one that closed normally.
    """

    metadata = getattr(context, "metadata", None)
    if isinstance(metadata, dict):
        metadata.setdefault("abandoned", reason)
    for token_attribute in _BRIDGE_CONTEXT_TOKENS:
        if getattr(context, token_attribute, None) is not None:
            setattr(context, token_attribute, None)
    context.__exit__(None, None, None)


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
    target.update(_safe_metadata(metadata, field="set_metadata() metadata"))


class _TraceContext:
    def __init__(self, *, name: str, metadata: Mapping[str, Any] | None) -> None:
        _validate_event_name(name, "trace name")
        self.name = name
        self.metadata: dict[str, Any] = _safe_metadata(metadata, field="trace metadata")
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
        # Bind the configuration once. ``configure()`` rebinds the whole object,
        # so one read is consistent, but two reads can straddle two objects and
        # snapshot inputs from one configuration and outputs from the next.
        config = _config
        self._capture_inputs_token = _current_capture_inputs.set(config.capture_inputs)
        self._capture_outputs_token = _current_capture_outputs.set(config.capture_outputs)
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
        _write_event(event)
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
        self._parent_override: str | None = None
        self._parent_token: Token[str | None] | None = None

    def __enter__(self) -> _Span:
        trace_id = _current_trace_id.get()
        parent_id = _current_parent_id.get()
        if trace_id is None or parent_id is None:
            raise RuntimeError("bir.span() requires an active trace. Use it inside a @observe() function.")

        self.id = _new_id()
        self.trace_id = trace_id
        self.parent_id = self._parent_override or parent_id
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
        _write_event(event)
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
        self.metadata: dict[str, Any] = _safe_metadata(metadata, field="generation metadata")
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
        self._parent_override: str | None = None
        self._parent_token: Token[str | None] | None = None

    def __enter__(self) -> _Generation:
        trace_id = _current_trace_id.get()
        parent_id = _current_parent_id.get()
        if trace_id is None or parent_id is None:
            raise RuntimeError("bir.generation() requires an active trace. Use it inside a @observe() function.")

        self.id = _new_id()
        self.trace_id = trace_id
        self.parent_id = self._parent_override or parent_id
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
        _write_event(event)
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
            # Validated like a supplied total: two finite counts can still add up
            # to infinity, and an unchecked sum reaches the writer, where the
            # event fails to serialize and is dropped with the store blamed for it.
            usage["total_tokens"] = _validate_non_negative_number(
                usage["input_tokens"] + usage["output_tokens"], "total_tokens"
            )
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
            # Validated like a supplied total, for the same reason as the token
            # sum above: two finite costs can add up to infinity.
            cost["total_cost"] = _validate_non_negative_number(cost["input_cost"] + cost["output_cost"], "total_cost")
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
        input_cost = price.input * input_tokens if price.input is not None and input_tokens is not None else None
        output_cost = price.output * output_tokens if price.output is not None and output_tokens is not None else None
        if input_cost is None and output_cost is None:
            return
        # A finite price and a finite token count can still multiply, or add up,
        # to infinity, and ``set_cost`` rejects that -- correctly, for a caller who
        # passed it. Here nobody passed anything: the cost was derived from
        # configuration, and deriving it is bookkeeping about the call rather than
        # part of it. So an unrepresentable cost is left off the event instead of
        # raised at the caller, which is the same rule a failed write follows.
        derived = [value for value in (input_cost, output_cost) if value is not None]
        if not all(_is_finite_number(value) for value in derived) or not _is_finite_number(sum(derived)):
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
        self.metadata: dict[str, Any] = _safe_metadata(metadata, field="tool_call metadata")
        self.capture_input = capture_input
        self.capture_output = capture_output
        self.id: str | None = None
        self.trace_id: str | None = None
        self.parent_id: str | None = None
        self.start_time: str | None = None
        self.output: Any = None
        self._parent_override: str | None = None
        self._parent_token: Token[str | None] | None = None

    def __enter__(self) -> _ToolCall:
        trace_id = _current_trace_id.get()
        parent_id = _current_parent_id.get()
        if trace_id is None or parent_id is None:
            raise RuntimeError("bir.tool_call() requires an active trace. Use it inside a @observe() function.")

        self.id = _new_id()
        self.trace_id = trace_id
        self.parent_id = self._parent_override or parent_id
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
        _write_event(event)
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
        retrieval_metadata = _safe_metadata(metadata, field="retrieval metadata")
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
            document["metadata"] = _safe_metadata(metadata, field="retrieval document metadata")
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
        source = _config.source
        if source is not None:
            event_metadata.setdefault("source", source)
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
    config = _config
    payload: dict[str, str] = {}
    if config.service_name is not None:
        payload["name"] = config.service_name
    if config.environment is not None:
        payload["environment"] = config.environment
    return payload or None


# A recording write that fails is reported here rather than raised, so the state
# below is what keeps "report it" from meaning "once per event of every trace".
# One message when writing starts failing, one when it recovers, and the count of
# what was lost in between.
_write_failure_lock = Lock()
_write_failing = False
_events_lost_while_failing = 0

# The SDK's own operational log. A library reporting that it cannot do its job
# belongs in the application's logging, where an operator already looks and can
# route or silence it; this is unrelated to ``bir.logging``, which stamps trace
# ids onto the application's own records.
_logger = logging.getLogger("bir")


def _write_event(event: dict[str, Any]) -> None:
    """Append one event, never letting a failed write reach the traced call.

    Recording is bookkeeping about a call, not part of it, so a store that cannot
    be written must not decide whether the call succeeded — a read-only container
    filesystem, a full disk, or a ``.bir/`` owned by another user would otherwise
    turn every completed call into an exception at its caller.

    Silence would be the other way to get that wrong, so the failure is reported
    once (see :func:`_report_write_failure`) instead of per event. Explicit
    operations are unaffected: ``prune``, ``send``, and the loaders are invoked
    for their effect and still raise what they hit.
    """

    # The master switch and root sampling decision remain lifecycle concerns.
    config = _config
    if not config.enabled or _current_trace_dropped.get():
        return
    try:
        # One binding for the whole write, so a reconfiguration cannot pair a new
        # trace path with the previous rotation settings.
        _storage_helpers._append_event(
            event,
            trace_path=config.trace_path,
            max_bytes=config.max_bytes,
            backup_count=config.backup_count,
        )
    except Exception as error:
        # Deliberately broad: from the caller's side every way of failing to
        # record is the same thing, and a serialization bug here would otherwise
        # destroy a production call rather than a trace.
        _report_write_failure(config.trace_path, error)
        return
    if _write_failing:
        _report_write_recovered()


def _report_write_failure(trace_path: Path, error: BaseException) -> None:
    """Say once that recording has stopped working, and count what is being lost.

    The report is emitted only on the transition into failure. That bounds a
    persistent outage to one message, and it also bounds recursion: an
    application whose logging handler is itself traced would otherwise log, fail
    to record, and log again without end.
    """

    global _write_failing, _events_lost_while_failing

    with _write_failure_lock:
        _events_lost_while_failing += 1
        if _write_failing:
            return
        _write_failing = True
    # Logged outside the lock so a handler that traces cannot deadlock on it.
    _logger.error(
        "bir could not write to the trace store at %s: %s. Recording is paused and events are being "
        "dropped; the traced calls themselves are unaffected. This is reported once, and again when "
        "writing recovers.",
        trace_path,
        error,
    )


def _report_write_recovered() -> None:
    """Say that recording works again, and how much was lost while it did not."""

    global _write_failing, _events_lost_while_failing

    with _write_failure_lock:
        if not _write_failing:
            return
        lost = _events_lost_while_failing
        _write_failing = False
        _events_lost_while_failing = 0
    _logger.warning("bir resumed writing to the trace store; %d event(s) were dropped in the meantime.", lost)


def _rotate_trace_file_if_needed(trace_path: Path, payload: str) -> None:
    """Rotate the active trace file before a write that would exceed its cap."""

    config = _config
    _storage_helpers._rotate_trace_file_if_needed(
        trace_path,
        payload,
        max_bytes=config.max_bytes,
        backup_count=config.backup_count,
    )


def _prune_trace_store(
    path: str | Path | None = None,
    *,
    include_rotated: bool = False,
    before: datetime | None = None,
    keep_last: int | None = None,
    status: str | None = None,
    dry_run: bool = False,
) -> _PruneResult:
    """Remove whole traces from the local store, serialized against appends."""

    return _storage_helpers._prune_trace_store(
        path,
        default_path=_config.trace_path,
        include_rotated=include_rotated,
        before=before,
        keep_last=keep_last,
        status=status,
        dry_run=dry_run,
    )


def _sent_ids_path(path: str | Path | None) -> Path:
    """Return the sidecar path that records IDs the server has already accepted."""

    return _storage_helpers._sent_ids_path(path, default_path=_config.trace_path)


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
    return _capture_helpers._capture_call_input(signature, args, kwargs, config=_config)


def _safe_capture(value: Any, *, key: str | None = None, depth: int = 0) -> Any:
    return _capture_helpers._safe_capture(value, config=_config, key=key, depth=depth)


def _truncate_captured_text(text: str) -> str:
    return _capture_helpers._truncate_captured_text(text, config=_config)


def _capture_mapping(value: Mapping[Any, Any], depth: int) -> dict[str, Any]:
    return _capture_helpers._capture_mapping(value, depth, config=_config)


def _capture_sequence(value: Iterable[Any], depth: int) -> list[Any]:
    return _capture_helpers._capture_sequence(value, depth, config=_config)


def _is_secret_key(key: str) -> bool:
    return _capture_helpers._is_secret_key(key, config=_config)


def _safe_key(value: Any) -> str:
    return _capture_helpers._safe_key(value)


def _safe_repr(value: Any) -> str:
    return _capture_helpers._safe_repr(value, config=_config)


def _safe_error(exc: BaseException) -> str:
    return _capture_helpers._safe_error(exc, config=_config)


def _redact_secret_text(value: str) -> str:
    return _capture_helpers._redact_secret_text(value, config=_config)


def _redact_labeled_secret_match(match: re.Match[str]) -> str:
    return _capture_helpers._redact_labeled_secret_match(match)


def _redact_bearer_secret_match(match: re.Match[str]) -> str:
    return _capture_helpers._redact_bearer_secret_match(match)


def _redact_pan_match(match: re.Match[str]) -> str:
    return _capture_helpers._redact_pan_match(match)


def _luhn_checksum_valid(digits: str) -> bool:
    return _capture_helpers._luhn_checksum_valid(digits)


def _sample_rate_for_trace(trace_name: str) -> float:
    return _configured_sample_rate_for_trace(trace_name, _config)


def _price_for_model(model: str) -> _ModelPrice | None:
    return _configured_price_for_model(model, _config)


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


# Apply env-derived defaults at import. ``configure(...)`` still overrides these,
# and tests reset to a pristine ``_Config()`` via ``_reset_config_for_tests`` so
# ambient env never leaks in.
_config = _config_from_env()


def _reset_config_for_tests() -> None:
    """Reset the active config to hardcoded defaults, ignoring ambient env.

    Tests rely on a clean baseline, so this deliberately constructs a pristine
    ``_Config()`` rather than re-reading the ``BIR_*`` variables; otherwise a
    developer's real environment (or another test's monkeypatched env) could leak
    into an unrelated test.
    """

    global _config, _write_failing, _events_lost_while_failing
    _config = _Config()
    # A test that made a write fail must not leave the next one reporting a
    # recovery it did not cause.
    with _write_failure_lock:
        _write_failing = False
        _events_lost_while_failing = 0
