"""Private local trace storage primitives for the Bir SDK.

Dependency direction is deliberately one-way: this module uses only the Python
standard library and scalar validation helpers from :mod:`bir._config`.  The
public-facing :mod:`bir._sdk` module supplies its active configuration when it
calls these functions; storage never imports SDK state or capture/lifecycle
code.  Network upload orchestration remains outside this module.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from types import TracebackType
from typing import IO, Any
from uuid import uuid4

from ._config import (
    _private_opener,
    _validate_non_negative_number,
    _validate_number,
    _validate_positive_int,
)

if os.name == "nt":
    import msvcrt
else:
    import fcntl

_SENT_IDS_SUFFIX = ".sent"
_SCHEMA_VERSION = "1.0"
_EVENT_TYPES = {"trace", "span", "generation", "tool_call", "score"}
_EVENT_STATUSES = {"success", "error"}
_EVENT_SORT_PRIORITY = {
    "trace": 0,
    "span": 1,
    "generation": 1,
    "tool_call": 1,
    "score": 2,
}


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


def _set_public_sdk_module(cls: type[Any]) -> None:
    """Restore the public SDK identity of a storage-owned value class."""

    cls.__module__ = "bir._sdk"
    for member in cls.__dict__.values():
        function: Any
        if isinstance(member, (classmethod, staticmethod)):
            function = member.__func__
        elif isinstance(member, property):
            function = member.fget
        else:
            function = member
        if callable(function) and getattr(function, "__module__", None) == __name__:
            function.__module__ = "bir._sdk"


for _public_sdk_model in (TraceEvent, LoadedTrace):
    _set_public_sdk_module(_public_sdk_model)

del _public_sdk_model


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


def load_events(
    path: str | Path | None = None,
    *,
    include_rotated: bool = False,
    default_path: Path,
    on_invalid: Callable[[ValueError], None] | None = None,
) -> list[TraceEvent]:
    """Load local JSONL trace events.

    ``default_path`` is supplied by :mod:`bir._sdk`; keeping path resolution
    explicit prevents this module from depending on active SDK configuration.
    """

    return list(
        _iter_trace_events(
            path,
            include_rotated=include_rotated,
            default_path=default_path,
            on_invalid=on_invalid,
        )
    )


def _iter_trace_events(
    path: str | Path | None = None,
    *,
    include_rotated: bool = False,
    default_path: Path,
    on_invalid: Callable[[ValueError], None] | None = None,
    on_incomplete_tail: Callable[[str], None] | None = None,
) -> Iterator[TraceEvent]:
    """Yield validated local events in their original write order.

    The iterator keeps only one JSONL line and parsed event live at a time. It is
    the internal streaming primitive for store operations; public loaders still
    materialize their documented list return types.

    ``on_invalid`` decides what an unreadable line means. Left ``None``, the
    first one raises and nothing is read, which is the contract every caller has
    always had. Supplied, the line is handed to the callback and skipped, so a
    store damaged by an interrupted write is still readable and the caller can
    tell the user what it could not read. Only callers that merely display
    events pass it: skipping a line while sending or pruning would silently drop
    or duplicate recorded data.

    ``on_incomplete_tail`` is the narrower opening a writing caller may take. It
    fires only for a final line the file never terminated -- see
    :func:`_iter_trace_events_from_file` -- and is applied to ``trace_path``
    alone, never to a rotated sibling, which nothing appends to.
    """

    trace_path = Path(path) if path is not None else default_path
    trace_files = _trace_files_oldest_first(trace_path) if include_rotated else [trace_path]
    for file_path in trace_files:
        yield from _iter_trace_events_from_file(
            file_path,
            on_invalid=on_invalid,
            on_incomplete_tail=on_incomplete_tail if file_path == trace_path else None,
        )


def _iter_trace_events_from_file(
    trace_path: Path,
    *,
    on_invalid: Callable[[ValueError], None] | None = None,
    on_incomplete_tail: Callable[[str], None] | None = None,
) -> Iterator[TraceEvent]:
    """Yield validated events from one JSONL file.

    ``on_incomplete_tail`` separates the one unreadable line that is provably not
    a record from every other one. An event is appended as one whole line ending
    in a newline, so a file whose last line has no newline ends in a write that
    never finished: those bytes were never a complete event and no reader has
    ever been able to read them. Iterating a text file yields the trailing
    fragment as the only line without a terminator, so recognizing it costs
    nothing and cannot misfire on a line further up.

    Left ``None`` -- the contract every caller has always had -- such a line is
    just an unreadable line and follows ``on_invalid``. Supplied, it is handed to
    the callback instead and skipped, which is how ``prune`` can rewrite a store
    an interrupted write damaged without ever skipping a line that was written
    whole.
    """

    if not trace_path.exists():
        return

    with trace_path.open("r", encoding="utf-8") as trace_file:
        for line_number, line in enumerate(trace_file, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = _trace_event_from_line(stripped, trace_path=trace_path, line_number=line_number)
            except ValueError as exc:
                if on_incomplete_tail is not None and not line.endswith("\n"):
                    on_incomplete_tail(line)
                    continue
                if on_invalid is None:
                    raise
                on_invalid(exc)
                continue
            yield event


def _trace_event_from_line(line: str, *, trace_path: Path, line_number: int) -> TraceEvent:
    """Parse and validate one JSONL line, raising ``ValueError`` if it cannot be."""

    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in trace file {trace_path} at line {line_number}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Trace file {trace_path} line {line_number} must contain a JSON object")
    return _trace_event_from_payload(payload, trace_path=trace_path, line_number=line_number)


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
        suffix = entry.name[len(prefix) :]
        if suffix.isdigit() and int(suffix) >= 1:
            rotated.append((int(suffix), entry))
    rotated.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in rotated] + [trace_path]


def load_traces(
    path: str | Path | None = None,
    *,
    include_rotated: bool = False,
    default_path: Path,
    on_invalid: Callable[[ValueError], None] | None = None,
) -> list[LoadedTrace]:
    """Load local traces grouped by trace_id."""

    events = _iter_trace_events(
        path,
        include_rotated=include_rotated,
        default_path=default_path,
        on_invalid=on_invalid,
    )
    return _traces_from_events(events)


def _traces_from_events(events: Iterable[TraceEvent]) -> list[LoadedTrace]:
    """Group already parsed events without reading their JSONL store again."""

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


def _count_events_per_trace(events: Iterable[TraceEvent]) -> dict[str, int]:
    """Count each trace's events, for a later pass that groups them one at a time.

    Retains one integer per trace, which is the bound the streaming CLI read
    commands already accept.
    """

    counts: dict[str, int] = {}
    for event in events:
        counts[event.trace_id] = counts.get(event.trace_id, 0) + 1
    return counts


def _iter_traces_from_events(
    events: Iterable[TraceEvent],
    *,
    event_counts: Mapping[str, int],
) -> Iterator[LoadedTrace]:
    """Group a stream of events into traces, releasing each once it is complete.

    ``event_counts`` says how many events each trace has, from an earlier pass
    over the same store (:func:`_count_events_per_trace`). A trace is emitted as
    soon as that many of its events have arrived, so only traces still missing an
    event are held: memory is bounded by how many traces interleave rather than
    by the size of the store.

    Counting is what makes this safe. The obvious signal — a trace is done when
    its root arrives — holds for a store this SDK wrote, because the root event
    is written when the trace closes, but not for one where the root comes first;
    the shared ``tests/fixtures`` store is written that way, and the exporter
    accepts any JSONL path. A count does not care where the root sits.

    Each completed trace goes through :func:`_traces_from_events`, so its own
    events are ordered identically to the materializing loader, and a trace whose
    root is absent is dropped exactly as it is there. What differs is the order
    the traces arrive in — completion order rather than start time — which is the
    price of not holding them all in order to sort them.
    """

    pending: dict[str, list[TraceEvent]] = {}
    for event in events:
        collected = pending.setdefault(event.trace_id, [])
        collected.append(event)
        if len(collected) < event_counts.get(event.trace_id, 0):
            continue
        grouped = _traces_from_events(pending.pop(event.trace_id))
        if grouped:
            yield grouped[0]


def _events_for_sending(
    path: str | Path | None = None,
    *,
    include_rotated: bool = False,
    default_path: Path,
) -> list[TraceEvent]:
    """Order local events for upload: complete traces root-first, then orphans.

    Events are deduplicated by ID, so a rotated file that overlaps the active file
    still uploads each event once. Orphan events whose trace root is missing are
    kept rather than dropped.
    """

    events = _iter_trace_events(path, include_rotated=include_rotated, default_path=default_path)
    return _order_events_for_sending(events)


def _order_events_for_sending(events: Iterable[TraceEvent]) -> list[TraceEvent]:
    """Order parsed events for upload without owning how the store is read."""

    events = list(events)
    traces = _traces_from_events(events)
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


def _iter_event_batches(events: Iterable[TraceEvent], batch_size: int) -> Iterator[list[TraceEvent]]:
    """Yield ordered event batches containing at most ``batch_size`` items."""

    batch_size = _validate_positive_int(batch_size, "batch_size")

    batch: list[TraceEvent] = []
    for event in events:
        batch.append(event)
        if len(batch) == batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


class _UploadEventSpool:
    """Disk-backed deduplication and upload ordering for parsed events."""

    def __init__(self) -> None:
        self.database_path: Path | None = None
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self._connection: sqlite3.Connection | None = None
        self._active_cursors: set[sqlite3.Cursor] = set()

    def __enter__(self) -> _UploadEventSpool:
        if self._connection is not None:
            raise RuntimeError("upload event spool is already open")

        temporary_directory = tempfile.TemporaryDirectory(prefix="bir-upload-spool-")
        database_path = Path(temporary_directory.name) / "events.sqlite3"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(database_path)
            connection.execute("PRAGMA journal_mode = OFF")
            connection.execute("PRAGMA synchronous = OFF")
            connection.execute("PRAGMA temp_store = FILE")
            connection.execute("PRAGMA cache_size = -2048")
            connection.executescript(
                """
                CREATE TABLE events (
                    sequence INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    trace_id TEXT NOT NULL,
                    parent_id TEXT,
                    event_type TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    depth INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL
                );
                CREATE INDEX events_trace_id ON events(trace_id);
                """
            )
        except BaseException:
            try:
                if connection is not None:
                    connection.close()
            finally:
                temporary_directory.cleanup()
            raise

        assert connection is not None
        self.database_path = database_path
        self._temporary_directory = temporary_directory
        self._connection = connection
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        connection = self._connection
        temporary_directory = self._temporary_directory
        self._connection = None
        self._temporary_directory = None
        try:
            for cursor in tuple(self._active_cursors):
                self._close_cursor(cursor)
            if connection is not None:
                connection.close()
        finally:
            if temporary_directory is not None:
                temporary_directory.cleanup()

    def add_events(self, events: Iterable[TraceEvent]) -> None:
        """Stream events into the spool, retaining the first occurrence of each ID."""

        connection = self._require_connection()
        for sequence, event in enumerate(events):
            connection.execute(
                """
                INSERT OR IGNORE INTO events (
                    sequence, event_id, trace_id, parent_id, event_type,
                    start_time, end_time, priority, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    event.id,
                    event.trace_id,
                    event.parent_id,
                    event.type,
                    event.start_time,
                    event.end_time,
                    _EVENT_SORT_PRIORITY.get(event.type, 99),
                    json.dumps(event.raw, sort_keys=True, separators=(",", ":"), allow_nan=False),
                ),
            )
            if (sequence + 1) % 1000 == 0:
                connection.commit()
        connection.commit()
        self._populate_depths()

    def iter_ordered_events(self) -> Iterator[TraceEvent]:
        """Yield complete traces root-first, followed by rootless events."""

        connection = self._require_connection()
        complete_trace_rows = connection.execute(
            """
            SELECT event.payload, event.sequence
            FROM events AS event
            JOIN events AS root
              ON root.trace_id = event.trace_id
             AND root.event_id = event.trace_id
             AND root.event_type = 'trace'
            JOIN (
                SELECT trace_id, MIN(sequence) AS first_sequence
                FROM events
                GROUP BY trace_id
            ) AS first_seen ON first_seen.trace_id = event.trace_id
            ORDER BY
                root.start_time,
                first_seen.first_sequence,
                event.start_time,
                event.priority,
                event.depth,
                event.end_time,
                event.sequence
            """
        )
        self._active_cursors.add(complete_trace_rows)
        try:
            for payload_text, sequence in complete_trace_rows:
                yield self._event_from_payload_text(payload_text, sequence)
        finally:
            self._close_cursor(complete_trace_rows)

        orphan_rows = connection.execute(
            """
            SELECT event.payload, event.sequence
            FROM events AS event
            WHERE NOT EXISTS (
                SELECT 1
                FROM events AS root
                WHERE root.trace_id = event.trace_id
                  AND root.event_id = event.trace_id
                  AND root.event_type = 'trace'
            )
            ORDER BY event.sequence
            """
        )
        self._active_cursors.add(orphan_rows)
        try:
            for payload_text, sequence in orphan_rows:
                yield self._event_from_payload_text(payload_text, sequence)
        finally:
            self._close_cursor(orphan_rows)

    def _close_cursor(self, cursor: sqlite3.Cursor) -> None:
        """Close one tracked iterator cursor, tolerating repeated cleanup."""

        self._active_cursors.discard(cursor)
        try:
            cursor.close()
        except sqlite3.Error:
            # ``__exit__`` may close a cursor while its generator is suspended;
            # the generator's eventual ``finally`` then reaches this a second
            # time. The cursor is already closed, which is the desired state.
            pass

    def _populate_depths(self) -> None:
        connection = self._require_connection()
        last_sequence = -1
        while True:
            rows = connection.execute(
                """
                SELECT sequence, event_id, trace_id, parent_id
                FROM events
                WHERE sequence > ?
                ORDER BY sequence
                LIMIT 1000
                """,
                (last_sequence,),
            ).fetchall()
            if not rows:
                break
            for sequence, event_id, trace_id, parent_id in rows:
                depth = self._event_depth(event_id, trace_id, parent_id)
                connection.execute("UPDATE events SET depth = ? WHERE sequence = ?", (depth, sequence))
            connection.commit()
            last_sequence = rows[-1][0]

    def _event_depth(self, event_id: str, trace_id: str, parent_id: str | None) -> int:
        connection = self._require_connection()
        depth = 0
        seen = {event_id}
        while parent_id is not None and parent_id not in seen:
            parent = connection.execute(
                "SELECT parent_id FROM events WHERE event_id = ? AND trace_id = ?",
                (parent_id, trace_id),
            ).fetchone()
            if parent is None:
                break
            seen.add(parent_id)
            depth += 1
            parent_id = parent[0]
        return depth

    def _event_from_payload_text(self, payload_text: str, sequence: int) -> TraceEvent:
        payload = json.loads(payload_text)
        if not isinstance(payload, dict):
            raise RuntimeError("upload event spool contains a non-object payload")
        database_path = self.database_path
        if database_path is None:
            raise RuntimeError("upload event spool is not open")
        return _trace_event_from_payload(payload, trace_path=database_path, line_number=sequence + 1)

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("upload event spool is not open")
        return self._connection


def _append_event(
    event: dict[str, Any],
    *,
    trace_path: Path,
    max_bytes: int | None,
    backup_count: int,
) -> None:
    """Serialize and append one event while holding the store's writer locks."""

    payload = json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    with _write_lock:
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        with _InterProcessFileLock(trace_path):
            _rotate_trace_file_if_needed(
                trace_path,
                payload,
                max_bytes=max_bytes,
                backup_count=backup_count,
            )
            with open(trace_path, "a", encoding="utf-8", opener=_private_opener) as trace_file:
                trace_file.write(payload)


def _rotate_trace_file_if_needed(
    trace_path: Path,
    payload: str,
    *,
    max_bytes: int | None,
    backup_count: int,
) -> None:
    """Rotate the active trace file before a write that would exceed ``max_bytes``.

    Rotation is decided on a complete active file, so files only ever break on
    whole-line boundaries. The incoming line is never split. Must be called while
    holding both ``_write_lock`` and the trace path's process lock.
    """

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
    _rotate_trace_files(trace_path, backup_count)


def _rotate_trace_files(trace_path: Path, backup_count: int) -> None:
    """Shift ``traces.jsonl`` -> ``.1`` -> ``.2`` and drop the oldest."""

    if backup_count <= 0:
        trace_path.unlink(missing_ok=True)
        return
    for index in range(backup_count - 1, 0, -1):
        source = trace_path.with_name(f"{trace_path.name}.{index}")
        if source.exists():
            source.replace(trace_path.with_name(f"{trace_path.name}.{index + 1}"))
    trace_path.replace(trace_path.with_name(f"{trace_path.name}.1"))


@dataclass(frozen=True)
class _PruneResult:
    """Outcome of a :func:`_prune_trace_store` call.

    ``incomplete_tail_bytes`` is the size of a final line the active file never
    terminated, which the rewrite drops. It is separate from ``removed_events``
    because it is not an event -- no selection filter named it and no reader
    could ever read it -- and it is already inside ``bytes_reclaimed``, which
    measures the file rather than the selection.
    """

    removed_traces: int
    kept_traces: int
    removed_events: int
    bytes_reclaimed: int
    dry_run: bool
    incomplete_tail_bytes: int = 0


@dataclass(frozen=True)
class _PruneTraceSelection:
    """Counts produced by disk-backed prune selection."""

    removed_traces: int
    kept_traces: int


def _trace_starts_before(start_time: str, cutoff: datetime) -> bool:
    """Return True when ``start_time`` precedes ``cutoff``, comparing in UTC."""

    start = datetime.fromisoformat(start_time)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    else:
        start = start.astimezone(timezone.utc)
    return start < cutoff


def _prune_trace_id_key(trace_id: str) -> bytes:
    """Encode a trace ID losslessly for SQLite equality membership."""

    return trace_id.encode("utf-8", errors="surrogatepass")


class _PruneTraceIndex:
    """Disk-backed trace summaries for bounded-memory prune selection.

    Every event contributes only its trace's first-seen sequence; complete trace
    roots additionally contribute the fields prune filters need. The selected IDs
    remain in SQLite while the source files are streamed, bounding Python memory.
    """

    def __init__(self) -> None:
        self.database_path: Path | None = None
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> _PruneTraceIndex:
        if self._connection is not None:
            raise RuntimeError("prune trace index is already open")

        temporary_directory = tempfile.TemporaryDirectory(prefix="bir-prune-index-")
        database_path = Path(temporary_directory.name) / "traces.sqlite3"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(database_path)
            connection.execute("PRAGMA journal_mode = OFF")
            connection.execute("PRAGMA synchronous = OFF")
            connection.execute("PRAGMA temp_store = FILE")
            connection.execute("PRAGMA cache_size = -2048")
            connection.executescript(
                """
                CREATE TABLE trace_summaries (
                    trace_id BLOB PRIMARY KEY,
                    first_sequence INTEGER NOT NULL,
                    root_sequence INTEGER,
                    start_time TEXT,
                    end_time TEXT,
                    status TEXT
                );
                CREATE INDEX complete_trace_order
                    ON trace_summaries(start_time DESC, first_sequence ASC)
                    WHERE root_sequence IS NOT NULL;
                CREATE TABLE removed_trace_ids (
                    trace_id BLOB PRIMARY KEY
                ) WITHOUT ROWID;
                """
            )
        except BaseException:
            try:
                if connection is not None:
                    connection.close()
            except BaseException:
                pass
            try:
                temporary_directory.cleanup()
            except BaseException:
                pass
            raise

        assert connection is not None
        self.database_path = database_path
        self._temporary_directory = temporary_directory
        self._connection = connection
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        connection = self._connection
        temporary_directory = self._temporary_directory
        self._connection = None
        self._temporary_directory = None
        cleanup_error: BaseException | None = None
        try:
            if connection is not None:
                connection.close()
        except BaseException as error:
            cleanup_error = error
        try:
            if temporary_directory is not None:
                temporary_directory.cleanup()
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = error

        if exc_type is None and cleanup_error is not None:
            raise cleanup_error

    def add_events(self, events: Iterable[TraceEvent]) -> None:
        """Stream events into one summary row per seen trace ID."""

        connection = self._require_connection()
        for sequence, event in enumerate(events):
            trace_id_key = _prune_trace_id_key(event.trace_id)
            connection.execute(
                """
                INSERT OR IGNORE INTO trace_summaries (trace_id, first_sequence)
                VALUES (?, ?)
                """,
                (trace_id_key, sequence),
            )
            if event.type == "trace" and event.id == event.trace_id:
                connection.execute(
                    """
                    UPDATE trace_summaries
                    SET root_sequence = ?, start_time = ?, end_time = ?, status = ?
                    WHERE trace_id = ? AND (
                        root_sequence IS NULL
                        OR start_time > ?
                        OR (start_time = ? AND end_time > ?)
                    )
                    """,
                    (
                        sequence,
                        event.start_time,
                        event.end_time,
                        event.status,
                        trace_id_key,
                        event.start_time,
                        event.start_time,
                        event.end_time,
                    ),
                )
            if (sequence + 1) % 1000 == 0:
                connection.commit()
        connection.commit()

    def count_traces(self) -> int:
        """Return the number of complete traces represented by the index."""

        row = (
            self._require_connection()
            .execute("SELECT COUNT(*) FROM trace_summaries WHERE root_sequence IS NOT NULL")
            .fetchone()
        )
        assert row is not None
        return int(row[0])

    def select_removed_traces(
        self,
        *,
        before: datetime | None,
        keep_last: int | None,
        status: str | None,
    ) -> _PruneTraceSelection:
        """Persist selected trace IDs and return their counts."""

        connection = self._require_connection()
        try:
            connection.execute("DELETE FROM removed_trace_ids")
            rows = connection.execute(
                """
                SELECT trace_id, start_time, status, recency_rank
                FROM (
                    SELECT
                        trace_id,
                        first_sequence,
                        start_time,
                        status,
                        ROW_NUMBER() OVER (
                            ORDER BY start_time DESC, first_sequence ASC
                        ) AS recency_rank
                    FROM trace_summaries
                    WHERE root_sequence IS NOT NULL
                )
                ORDER BY start_time ASC, first_sequence ASC
                """
            )
            try:
                has_selector = before is not None or keep_last is not None
                for trace_id, start_time, trace_status, recency_rank in rows:
                    if status is not None and trace_status != status:
                        continue
                    if (
                        not has_selector
                        or (before is not None and _trace_starts_before(str(start_time), before))
                        or (keep_last is not None and int(recency_rank) > keep_last)
                    ):
                        connection.execute(
                            "INSERT INTO removed_trace_ids (trace_id) VALUES (?)",
                            (trace_id,),
                        )
            except BaseException:
                try:
                    rows.close()
                except BaseException:
                    pass
                raise
            else:
                rows.close()

            removed_row = connection.execute("SELECT COUNT(*) FROM removed_trace_ids").fetchone()
            total_row = connection.execute(
                "SELECT COUNT(*) FROM trace_summaries WHERE root_sequence IS NOT NULL"
            ).fetchone()
            assert removed_row is not None
            assert total_row is not None
            removed_traces = int(removed_row[0])
            total_traces = int(total_row[0])
            connection.commit()
        except BaseException:
            try:
                connection.rollback()
            except BaseException:
                pass
            raise

        return _PruneTraceSelection(
            removed_traces=removed_traces,
            kept_traces=total_traces - removed_traces,
        )

    def is_removed(self, trace_id: str) -> bool:
        """Return whether ``trace_id`` belongs to the current selection."""

        row = (
            self._require_connection()
            .execute(
                "SELECT 1 FROM removed_trace_ids WHERE trace_id = ?",
                (_prune_trace_id_key(trace_id),),
            )
            .fetchone()
        )
        return row is not None

    def _require_connection(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:
            raise RuntimeError("prune trace index is not open")
        return connection


def _select_removed_trace_ids(
    traces: list[LoadedTrace],
    *,
    before: datetime | None,
    keep_last: int | None,
    status: str | None,
) -> set[str]:
    """Choose trace IDs to drop for the given prune filters."""

    beyond_keep_last: set[str] = set()
    if keep_last is not None:
        by_recent = sorted(traces, key=lambda trace: trace.start_time, reverse=True)
        beyond_keep_last = {trace.id for trace in by_recent[keep_last:]}

    has_selector = before is not None or keep_last is not None
    removed: set[str] = set()
    for trace in traces:
        if status is not None and trace.status != status:
            continue
        if not has_selector:
            removed.add(trace.id)
        elif before is not None and _trace_starts_before(trace.start_time, before):
            removed.add(trace.id)
        elif keep_last is not None and trace.id in beyond_keep_last:
            removed.add(trace.id)
    return removed


def _stream_filtered_trace_file(
    file_path: Path,
    is_removed: Callable[[str], bool],
    destination: IO[bytes] | None,
    *,
    drop_incomplete_tail: bool = False,
) -> tuple[int, int]:
    """Stream surviving normalized lines to ``destination`` and return counts.

    ``drop_incomplete_tail`` leaves out a final line the file never terminated,
    matching what :func:`_iter_trace_events_from_file` already refused to count
    as an event. It is passed only for the active file, and only after selection
    saw the same line, so the rewrite drops exactly the bytes the caller was
    told about.
    """

    removed_events = 0
    kept_bytes = 0
    with file_path.open("r", encoding="utf-8") as trace_file:
        for line in trace_file:
            if drop_incomplete_tail and not line.endswith("\n"):
                continue
            stripped = line.strip()
            if not stripped:
                continue
            trace_id = json.loads(stripped).get("trace_id")
            if isinstance(trace_id, str) and is_removed(trace_id):
                removed_events += 1
                continue
            normalized_line = (stripped + "\n").encode("utf-8")
            kept_bytes += len(normalized_line)
            if destination is not None:
                destination.write(normalized_line)
    return removed_events, kept_bytes


def _stage_filtered_trace_file(
    file_path: Path,
    is_removed: Callable[[str], bool],
    *,
    dry_run: bool,
    drop_incomplete_tail: bool = False,
) -> tuple[Path | None, int, int]:
    """Stage one filtered trace file without accumulating surviving lines.

    A file is rewritten when it loses at least one event, and also when it ends
    in a line no write finished: dropping that line is the only way a store an
    interrupted write damaged becomes readable again, and leaving it in place
    because this run happened to select no traces would make the repair depend on
    the selection filter.
    """

    temp_path = None
    try:
        if dry_run:
            removed_events, kept_bytes = _stream_filtered_trace_file(
                file_path,
                is_removed,
                None,
                drop_incomplete_tail=drop_incomplete_tail,
            )
        else:
            temp_path = file_path.with_name(f".{file_path.name}.{os.getpid()}.{uuid4().hex}.tmp")
            # Replaces the trace file, so it is created with the store's mode
            # rather than handing prune's output a wider one.
            with open(temp_path, "xb", opener=_private_opener) as staged_file:
                removed_events, kept_bytes = _stream_filtered_trace_file(
                    file_path,
                    is_removed,
                    staged_file,
                    drop_incomplete_tail=drop_incomplete_tail,
                )
        if removed_events == 0 and not drop_incomplete_tail:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            return None, 0, 0
        source_bytes = file_path.stat().st_size
    except BaseException:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except BaseException:
                pass
        raise

    return temp_path, removed_events, source_bytes - kept_bytes


def _prune_trace_store(
    path: str | Path | None = None,
    *,
    default_path: Path,
    include_rotated: bool = False,
    before: datetime | None = None,
    keep_last: int | None = None,
    status: str | None = None,
    dry_run: bool = False,
) -> _PruneResult:
    """Remove whole traces from the local store, serialized against appends.

    Selection and rewrite remain disk-backed and run under the same in-process
    and advisory locks as append. Every staging file is complete before any
    original is atomically replaced.

    A store the writer was interrupted part-way through -- a full disk, an OOM
    kill -- ends in a line that has no newline and is not a whole event. Prune
    used to refuse such a store outright, which left the one command that
    reclaims space unusable on the store a full disk produces. That single line
    is now dropped by the rewrite and reported through
    :attr:`_PruneResult.incomplete_tail_bytes`. Nothing else moved: a line that
    was written whole and cannot be parsed still raises, wherever it is, because
    only the missing terminator proves nothing was ever recorded there.
    """

    trace_path = Path(path) if path is not None else default_path
    candidate_files = _trace_files_oldest_first(trace_path) if include_rotated else [trace_path]
    if not any(file_path.exists() for file_path in candidate_files):
        return _PruneResult(0, 0, 0, 0, dry_run)

    with _write_lock:
        with _InterProcessFileLock(trace_path):
            incomplete_tail = ""

            def record_incomplete_tail(line: str) -> None:
                nonlocal incomplete_tail
                incomplete_tail = line

            with _PruneTraceIndex() as index:
                index.add_events(
                    _iter_trace_events(
                        trace_path,
                        include_rotated=include_rotated,
                        default_path=default_path,
                        on_incomplete_tail=record_incomplete_tail,
                    )
                )
                incomplete_tail_bytes = len(incomplete_tail.encode("utf-8"))
                selection = index.select_removed_traces(before=before, keep_last=keep_last, status=status)
                if selection.removed_traces == 0 and not incomplete_tail_bytes:
                    return _PruneResult(0, selection.kept_traces, 0, 0, dry_run)

                files = _trace_files_oldest_first(trace_path) if include_rotated else [trace_path]
                removed_events = 0
                bytes_reclaimed = 0
                staged: list[tuple[Path, Path]] = []
                try:
                    for file_path in files:
                        if not file_path.exists():
                            continue
                        drop_incomplete_tail = bool(incomplete_tail_bytes) and file_path == trace_path
                        temp_path, file_removed_events, reclaimed_bytes = _stage_filtered_trace_file(
                            file_path,
                            index.is_removed,
                            dry_run=dry_run,
                            drop_incomplete_tail=drop_incomplete_tail,
                        )
                        if file_removed_events == 0 and not drop_incomplete_tail:
                            continue
                        removed_events += file_removed_events
                        bytes_reclaimed += reclaimed_bytes
                        if dry_run:
                            continue
                        assert temp_path is not None
                        staged.append((temp_path, file_path))
                    for temp_path, target_path in staged:
                        temp_path.replace(target_path)
                except BaseException:
                    for temp_path, _ in staged:
                        try:
                            temp_path.unlink(missing_ok=True)
                        except BaseException:
                            pass
                    raise
                else:
                    for temp_path, _ in staged:
                        temp_path.unlink(missing_ok=True)

                if not dry_run and removed_events:
                    # The store just shrank, so the upload sidecar can too. Still
                    # inside the trace lock, so the sidecar lock is taken second.
                    _compact_sent_ids(trace_path)

                return _PruneResult(
                    removed_traces=selection.removed_traces,
                    kept_traces=selection.kept_traces,
                    removed_events=removed_events,
                    bytes_reclaimed=bytes_reclaimed,
                    dry_run=dry_run,
                    incomplete_tail_bytes=incomplete_tail_bytes,
                )


def _sent_ids_path(path: str | Path | None, *, default_path: Path) -> Path:
    """Return the sidecar path that records IDs the server already accepted."""

    trace_path = Path(path) if path is not None else default_path
    return trace_path.with_name(trace_path.name + _SENT_IDS_SUFFIX)


def _load_sent_ids(sent_ids_path: Path) -> set[str]:
    """Load sent IDs, treating missing, unreadable, or malformed state as empty."""

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


def _write_sent_ids(sent_ids_path: Path, event_ids: set[str]) -> None:
    """Atomically replace the sent-ID sidecar with ``event_ids``.

    Callers create the parent directory before taking the sidecar lock, since the
    lock file is a sibling and cannot be opened without it.
    """

    payload = json.dumps({"event_ids": sorted(event_ids)}, separators=(",", ":")) + "\n"
    temp_path = sent_ids_path.with_name(f".{sent_ids_path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        # Staged through a temp file that is replaced onto the sidecar, so the
        # mode has to be set here: the rename carries it to the destination.
        with open(temp_path, "w", encoding="utf-8", opener=_private_opener) as staged_file:
            staged_file.write(payload)
        temp_path.replace(sent_ids_path)
    finally:
        temp_path.unlink(missing_ok=True)


def _record_sent_ids(sent_ids_path: Path, event_ids: list[str]) -> None:
    """Atomically merge ``event_ids`` into the sent-ID sidecar."""

    sent_ids_path.parent.mkdir(parents=True, exist_ok=True)
    with _sent_ids_lock:
        with _InterProcessFileLock(sent_ids_path):
            merged = _load_sent_ids(sent_ids_path)
            merged.update(event_ids)
            _write_sent_ids(sent_ids_path, merged)


def _iter_stored_event_ids(file_path: Path) -> Iterator[str]:
    """Yield the ``id`` of every event in one JSONL file, without validating it.

    Compaction only has to decide whether an id is still present, so a line is
    read for that field alone. A line that cannot be parsed yields nothing:
    dropping an id costs one duplicate upload, which the server is idempotent
    against, while failing here would make bookkeeping able to break a prune.
    """

    with file_path.open("r", encoding="utf-8") as trace_file:
        for line in trace_file:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, Mapping):
                event_id = payload.get("id")
                if isinstance(event_id, str):
                    yield event_id


def _compact_sent_ids(trace_path: Path) -> None:
    """Drop sidecar entries for events the store no longer holds.

    ``mark_sent`` records every accepted event id and nothing removed one, so the
    sidecar grew with everything ever sent while ``prune`` — the operation whose
    whole job is bounding local state — left it alone. An id naming an event the
    store no longer holds can never be matched by a later send, so it is weight
    with no effect, and it accumulates for as long as the deployment runs.

    Every file for this trace path is scanned, not only the ones a prune
    rewrote: a prune without ``include_rotated`` leaves rotated siblings holding
    their events, and those ids have to survive.

    Called while the caller holds the trace lock, so the sidecar lock is taken
    second, which is the order :class:`_InterProcessFileLock` documents. Nothing
    here can fail a prune: the sidecar is advisory, and leaving it as large as it
    already was is exactly the previous behavior.
    """

    sent_ids_path = trace_path.with_name(trace_path.name + _SENT_IDS_SUFFIX)
    if not sent_ids_path.exists():
        # No sidecar, so nothing to compact — and taking the lock would leave a
        # lock file beside a store that never opted into ``mark_sent``.
        return
    try:
        with _sent_ids_lock:
            with _InterProcessFileLock(sent_ids_path):
                recorded = _load_sent_ids(sent_ids_path)
                if not recorded:
                    # Missing, empty, or unreadable: all mean "nothing sent", and
                    # writing over an unreadable one would claim more than is known.
                    return
                surviving = {
                    event_id
                    for file_path in _trace_files_oldest_first(trace_path)
                    if file_path.exists()
                    for event_id in _iter_stored_event_ids(file_path)
                    if event_id in recorded
                }
                if len(surviving) == len(recorded):
                    return
                _write_sent_ids(sent_ids_path, surviving)
    except OSError:
        # The store is already pruned and the result already earned; a sidecar
        # that could not be rewritten is one that stayed the size it was.
        return


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
        raise ValueError(
            f"Trace file {trace_path} line {line_number} field 'type' has unsupported value {event_type!r}"
        )
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
            raise ValueError(
                f"Trace file {trace_path} line {line_number} score event is missing required field 'value'"
            )
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
        raise ValueError(f"Trace file {trace_path} line {line_number} field {field!r} must be an ISO datetime") from exc
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


def _event_sort_key(event: TraceEvent, depth: int = 0) -> tuple[str, int, int, str]:
    return (
        event.start_time,
        _EVENT_SORT_PRIORITY.get(event.type, 99),
        depth,
        event.end_time,
    )


def _event_depths(events: list[TraceEvent]) -> dict[str, int]:
    """Map each event ID to its guarded ancestor depth within ``events``."""

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
