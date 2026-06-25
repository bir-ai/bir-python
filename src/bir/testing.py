"""Test helpers for asserting on your Bir instrumentation.

The public :func:`capture_traces` context manager redirects trace writes to a
private temporary file for the duration of a ``with`` block and yields a
:class:`CapturedTraces` handle that reads the captured events and traces back in
memory through the SDK's own public loaders. It lets application authors assert
on the traces their instrumentation produces without pointing
``configure(trace_path=...)`` at a scratch file by hand or touching their real
``.bir/`` directory.

Like :func:`bir.configure`, this mutates process-global SDK configuration for the
duration of the block: on entry it swaps the active ``trace_path`` to the
temporary file, and on exit it restores the previous configuration in full
(including a user-set ``trace_path``), even when the body raises. It only
redirects *where* events are written — capture opt-in, sampling, and redaction are
left exactly as configured — so a captured event is identical to what a real
``.bir/traces.jsonl`` write would contain. Because it mutates global state it is
not safe to run concurrently from multiple threads, the same caveat that applies
to ``configure``.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from . import _sdk
from ._sdk import LoadedTrace, TraceEvent, load_events, load_traces

__all__ = ["CapturedTraces", "capture_traces"]


class CapturedTraces:
    """Read-back handle for the events recorded inside a ``capture_traces`` block.

    While the block is open, :meth:`events` and :meth:`traces` read live from the
    temporary trace file through the public :func:`bir.load_events` /
    :func:`bir.load_traces` loaders, so a test body can assert on progress as it
    goes. When the block exits, the final state is snapshotted in memory just
    before the temporary file is removed, so the same methods keep returning the
    captured data after the ``with`` block has closed.
    """

    def __init__(self, trace_path: Path) -> None:
        self._trace_path = trace_path
        self._closed = False
        self._events: list[TraceEvent] = []
        self._traces: list[LoadedTrace] = []

    @property
    def trace_path(self) -> Path:
        """Temporary file that events are captured to for the block's duration."""

        return self._trace_path

    def events(self) -> list[TraceEvent]:
        """Return the events captured in the block, in load order.

        Inside the block this re-reads the temporary file on each call; after the
        block it returns the snapshot taken just before cleanup. Reads include any
        size-rotated siblings, so capture stays complete even if the inherited
        configuration rotates the file mid-block.
        """

        if self._closed:
            return list(self._events)
        return load_events(self._trace_path, include_rotated=True)

    def traces(self) -> list[LoadedTrace]:
        """Return the captured events grouped into traces by ``trace_id``.

        Inside the block this re-reads the temporary file on each call; after the
        block it returns the snapshot taken just before cleanup.
        """

        if self._closed:
            return list(self._traces)
        return load_traces(self._trace_path, include_rotated=True)

    def _finalize(self) -> None:
        """Snapshot the captured events and traces before the temp file is gone."""

        self._events = load_events(self._trace_path, include_rotated=True)
        self._traces = load_traces(self._trace_path, include_rotated=True)
        self._closed = True


@contextmanager
def capture_traces() -> Iterator[CapturedTraces]:
    """Redirect trace writes to a temporary file and yield a read-back handle.

    Use this in tests to assert on the traces your own instrumentation produces
    without writing to (or reading from) your real ``.bir/`` directory::

        from bir.testing import capture_traces

        with capture_traces() as captured:
            answer_question("hello")

        events = captured.events()
        assert [event.type for event in events] == ["trace", "generation"]

    Only the active ``trace_path`` is swapped; every other configured setting
    (capture opt-in, sampling, redaction, the price table, and service metadata)
    is kept, so events are captured exactly as they would be in production — just
    into an isolated temporary file. The previous configuration, including a
    user-set ``trace_path``, is restored when the block exits, even if the body
    raises, and the temporary file and directory are removed.

    Like :func:`bir.configure`, this mutates process-global configuration for the
    duration of the block and is therefore not safe to run concurrently across
    threads. Nested ``capture_traces()`` blocks are fine: each restores the
    configuration that was active when it was entered.
    """

    previous_config = _sdk._config
    temp_dir = tempfile.TemporaryDirectory(prefix="bir-capture-")
    trace_path = Path(temp_dir.name) / _sdk._DEFAULT_TRACE_PATH.name
    # Preserve every other setting (capture flags, sampling, redaction, prices,
    # service metadata) and only redirect where events are written.
    _sdk._config = replace(previous_config, trace_path=trace_path)
    captured = CapturedTraces(trace_path)
    try:
        yield captured
    finally:
        # Restore the prior configuration first so the global is sane even if the
        # snapshot or cleanup below were to fail, then snapshot the captured
        # events (the temp file still exists) before removing the directory.
        _sdk._config = previous_config
        try:
            captured._finalize()
        finally:
            temp_dir.cleanup()
