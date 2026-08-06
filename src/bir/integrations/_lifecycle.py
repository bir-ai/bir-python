"""Shared open-run bookkeeping for the framework event bridges.

A bridge turns a framework's start/end callback stream into Bir events, so it
enters a Bir context in one callback and exits it in another. Between the two
the run owns the ambient trace context, and that is deliberate: an application's
own ``@observe()`` work running while the run is open nests under it.

It is also the failure mode. A framework that never emits the terminal callback
leaves the context entered for good — the run's event is never written, and every
event recorded afterwards in that context joins a trace whose root does not
exist, which no trace-shaped reader can find. The callback stream never says "that
run is gone", so this module supplies the two recoveries that do not require
being told:

* **A new top-level run reclaims an older one.** A framework does not begin
  unrelated top-level work while an earlier top-level run is still executing in
  the same context, so a bridge opening a fresh root finishes a root it is still
  holding for an earlier run. The earlier run's events become a complete,
  findable trace instead of being stranded.
* **A handler's registry is bounded.** Past :data:`_MAX_OPEN_RUNS` open runs the
  oldest is finished and evicted, so a framework that stops emitting terminal
  callbacks cannot grow the handler without limit.

Both write the run's event marked ``metadata.abandoned``, so a run no framework
ever closed is distinguishable in the store from one that closed normally.

What neither recovers is a context that sees no further bridge callbacks at all:
nothing distinguishes "this run is still executing" from "this run is gone", and
guessing would break the nesting above. The reader reports that state instead —
see ``bir traces`` on a store with rootless events.

The module is private to ``bir.integrations``; nothing here is exported from the
package.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from bir._sdk import (
    _abandon_bridge_run,
    _current_trace_id,
    _restore_context,
    _snapshot_context,
    _trace_context,
)

# Upper bound on the runs one handler may hold open at once. A framework nests
# runs tens deep at most, so this is far above any real callback tree and only
# binds when terminal callbacks stop arriving. It is a bound, not a tuning knob.
_MAX_OPEN_RUNS = 1024

_ABANDONED_EVICTED = "evicted"
_ABANDONED_SUPERSEDED = "superseded"


class _ActiveRun:
    """One framework run a handler has entered and not yet closed.

    ``context`` is the Bir context recording the run. ``implicit_trace`` is the
    trace root the handler had to open because the run arrived with no active
    Bir trace, and is ``None`` when the run joined a trace that already existed.
    """

    __slots__ = ("kind", "context", "implicit_trace")

    def __init__(self, kind: str, context: Any, *, implicit_trace: Any | None = None) -> None:
        self.kind = kind
        self.context = context
        self.implicit_trace = implicit_trace

    def abandon(self, reason: str) -> None:
        """Write this run's events as if the terminal callback had arrived.

        The run's own context closes before the root it opened, matching the
        order the end callback would have used.
        """

        _abandon_bridge_run(self.context, reason=reason)
        if self.implicit_trace is not None:
            _abandon_bridge_run(self.implicit_trace, reason=reason)


class _OpenRoot:
    """A trace root a bridge opened and is still holding in this context."""

    __slots__ = ("trace_id", "context", "snapshot", "runs", "key")

    def __init__(self, trace_id: str, context: Any, snapshot: Any) -> None:
        self.trace_id = trace_id
        self.context = context
        self.snapshot = snapshot
        # Filled in when the run that owns this root is registered, so reclaiming
        # the root also drops the handler's entry for it.
        self.runs: _OpenRuns | None = None
        self.key: str | None = None


# The bridge-opened trace roots still entered in *this* context. It is a
# contextvar because that is the scope of the problem: contextvars are per
# thread and per task, so a run leaked on one worker thread is neither visible
# nor fixable from another, and a reclaim must happen where the leak is.
_open_roots: ContextVar[tuple[_OpenRoot, ...]] = ContextVar("bir_bridge_open_roots", default=())


class _OpenRuns:
    """A handler's open runs, bounded so a lost callback cannot grow it forever.

    Used where a handler would otherwise keep a plain ``dict``. The mapping
    operations the handlers use are the whole surface — assignment, ``get``,
    ``pop``, and ``len`` — so this deliberately is not a full ``Mapping``.
    Insertion order is preserved, so the oldest open run is the first evicted.
    """

    __slots__ = ("_runs", "_limit")

    def __init__(self, *, limit: int = _MAX_OPEN_RUNS) -> None:
        self._runs: dict[str, _ActiveRun] = {}
        self._limit = limit

    def __setitem__(self, key: str, run: _ActiveRun) -> None:
        self._runs.pop(key, None)
        self._runs[key] = run
        _link_open_root(self, key, run)
        while len(self._runs) > self._limit:
            oldest_key, oldest = next(iter(self._runs.items()))
            del self._runs[oldest_key]
            _forget_open_root(self, oldest_key)
            # The framework stopped closing runs long enough ago that this one is
            # not coming back. Writing it is better than holding it forever.
            oldest.abandon(_ABANDONED_EVICTED)

    def get(self, key: str, default: _ActiveRun | None = None) -> _ActiveRun | None:
        return self._runs.get(key, default)

    def pop(self, key: str, default: _ActiveRun | None = None) -> _ActiveRun | None:
        run = self._runs.pop(key, default)
        _forget_open_root(self, key)
        return run

    def __len__(self) -> int:
        return len(self._runs)


def _bound_run_stack(stack: list[_ActiveRun], *, limit: int = _MAX_OPEN_RUNS) -> None:
    """Keep an arrival-order run stack bounded, writing what it has to drop.

    A framework that reports no correlation id can only be paired by the order
    its events arrive, so its handler stacks open runs instead of keying them.
    The stack needs the same bound as a keyed registry, and for the same reason:
    a run whose end never arrives is never popped. The oldest goes first, since
    the newest is the one still plausibly running.
    """

    while len(stack) > limit:
        stack.pop(0).abandon(_ABANDONED_EVICTED)


def _link_open_root(runs: _OpenRuns, key: str, run: _ActiveRun) -> None:
    """Point the root this run owns back at the handler entry holding it."""

    root = run.implicit_trace if run.implicit_trace is not None else (run.context if run.kind == "trace" else None)
    if root is None:
        return
    for entry in _open_roots.get():
        if entry.context is root:
            entry.runs = runs
            entry.key = key
            return


def _forget_open_root(runs: _OpenRuns, key: str) -> None:
    """Drop the registry link for a run the handler closed itself."""

    for entry in _open_roots.get():
        if entry.runs is runs and entry.key == key:
            entry.runs = None
            entry.key = None


def _reclaim_open_root() -> None:
    """Finish a trace root a bridge is still holding for an earlier top-level run.

    Called by a handler that is about to open a top-level run of its own. Only
    the innermost recorded root is considered, and only while it is still the
    ambient trace: anything else means the state is not what was recorded — the
    root was closed normally, or an application trace was opened around it — and
    the safe answer is to leave it alone and forget the entry.
    """

    entries = _open_roots.get()
    if not entries:
        return

    innermost = entries[-1]
    _open_roots.set(entries[:-1])
    if _current_trace_id.get() != innermost.trace_id:
        # Closed normally, or something else owns the context now.
        return

    run = innermost.runs.pop(innermost.key) if innermost.runs is not None and innermost.key is not None else None
    if run is not None:
        run.abandon(_ABANDONED_SUPERSEDED)
    else:
        _abandon_bridge_run(innermost.context, reason=_ABANDONED_SUPERSEDED)
    _restore_context(innermost.snapshot)


def _open_implicit_root(*, name: str, metadata: dict[str, Any]) -> Any | None:
    """Open a trace root for a run that arrived with no active Bir trace.

    Returns ``None`` when a trace is already active, which is the common case:
    the framework's own root callback, or the application's ``@observe()``, has
    already opened one. The caller enters nothing in that case.
    """

    if _current_trace_id.get() is not None:
        return None

    snapshot = _snapshot_context()
    context = _trace_context(name=name, metadata=metadata)
    context.__enter__()
    _record_open_root(context, snapshot)
    return context


def _record_open_root(context: Any, snapshot: Any) -> None:
    """Remember a root a bridge entered, so a later top-level run can reclaim it.

    Called only after the context is entered, which is what assigns its id, so
    the id is the one a later reclaim compares against the ambient trace.
    """

    _open_roots.set((*_open_roots.get(), _OpenRoot(context.id, context, snapshot)))


def _enter_framework_root(context: Any) -> None:
    """Enter a root the framework itself announced, and record it for reclaiming.

    The snapshot is taken before entering so the values the root replaced can be
    restored by value if it is ever reclaimed. Callers reclaim first, with
    :func:`_reclaim_open_root`, because a handler may decide against opening a
    root at all and the reclaim has to happen either way.
    """

    snapshot = _snapshot_context()
    context.__enter__()
    _record_open_root(context, snapshot)
