"""What a forked child inherits from a process that was recording.

A ``threading.Lock`` held when ``os.fork()`` is called is inherited locked, and
the thread that would release it does not exist in the child. Before the at-fork
hooks these tests cover, the first event a child recorded waited for a holder
that could never come: five of five children forked into four recording threads
were still blocked when killed after five seconds, and
``multiprocessing.get_context("fork").Pool(2)`` from a threaded parent returned no
worker at all. Nothing was printed, on any channel -- the child was not slow, it
was stopped.

Every child here is bounded and killed if it outlives its deadline, and every
child leaves through ``os._exit`` so it cannot run the test runner's teardown
inside a process that only exists to answer one question. A regression therefore
fails these tests rather than hanging the suite.

``os.fork`` does not exist on Windows, where the SDK takes the ``msvcrt`` locking
branch instead; the whole module is skipped there.
"""

from __future__ import annotations

import multiprocessing
import os
import tempfile
import threading
import time
import unittest
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import bir
from bir import _sdk, _storage
from bir._sdk import _reset_config_for_tests

CHILD_TIMEOUT = 10.0
CHILD_OK = 0
CHILD_FAILED = 3


@contextmanager
def temporary_store() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as directory:
        store = Path(directory) / "traces.jsonl"
        bir.configure(trace_path=str(store), enabled=True)
        try:
            yield store
        finally:
            _reset_config_for_tests()


@contextmanager
def recording_threads(count: int = 4) -> Iterator[None]:
    """Keep ``count`` threads appending for the duration of the block."""

    stop = threading.Event()

    def hammer() -> None:
        while not stop.is_set():
            with bir.trace("background"):
                pass

    workers = [threading.Thread(target=hammer, daemon=True) for _ in range(count)]
    for worker in workers:
        worker.start()
    # Long enough that a fork lands between an append's open and its close rather
    # than before any of them has started.
    time.sleep(0.2)
    try:
        yield
    finally:
        stop.set()
        for worker in workers:
            worker.join(5)


def run_in_child(body: Callable[[], bool], *, timeout: float = CHILD_TIMEOUT) -> str:
    """Fork, run ``body`` in the child, and report what became of it.

    Returns ``"ok"``, ``"failed"`` (the body returned False or raised), or
    ``"hung"``. The child never returns to the caller: it leaves through
    ``os._exit`` so the test runner's own finalization does not run twice.
    """

    with warnings.catch_warnings():
        # CPython warns that forking a multi-threaded process may deadlock the
        # child. That is the situation under test, deliberately: the point is
        # that the SDK survives it rather than that it is a good idea.
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()
    if pid == 0:
        try:
            outcome = CHILD_OK if body() else CHILD_FAILED
        except BaseException:
            outcome = CHILD_FAILED
        os._exit(outcome)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        finished, status = os.waitpid(pid, os.WNOHANG)
        if finished:
            return "ok" if os.waitstatus_to_exitcode(status) == CHILD_OK else "failed"
        time.sleep(0.01)
    os.kill(pid, 9)
    os.waitpid(pid, 0)
    return "hung"


def _record_one_trace() -> bool:
    with bir.trace("child"):
        pass
    return True


def _record_in_pool_worker(index: int) -> int:
    with bir.trace(f"pool-{index}"):
        pass
    return index


@unittest.skipUnless(hasattr(os, "fork"), "os.fork() exists only on POSIX platforms")
class ForkSafetyTests(unittest.TestCase):
    """A child forked out of a recording process has to be able to record."""

    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_the_lock_reinit_hook_the_fix_depends_on_exists(self) -> None:
        # `_at_fork_reinit` is a private CPython method, and it is what lets the
        # locks be reset in place rather than rebound -- `bir._sdk` re-exports
        # these objects, so a rebind would leave those names on the inherited,
        # permanently locked ones. The standard library's `logging` resets its own
        # locks the same way. If an interpreter ever drops it, this fails here
        # instead of hanging somebody's forked worker.
        for lock in (_storage._write_lock, _storage._sent_ids_lock, _sdk._write_failure_lock):
            self.assertTrue(
                hasattr(lock, "_at_fork_reinit"),
                "threading.Lock lost _at_fork_reinit; bir._storage._reinitialize_locks_after_fork needs another way "
                "to reset its locks in place",
            )

    def test_a_child_records_when_the_write_lock_was_held_at_fork(self) -> None:
        # The deterministic form of the bug: the lock is held by a thread that
        # will not exist in the child, so the child inherits it locked forever.
        with temporary_store():
            holding = threading.Event()
            release = threading.Event()

            def hold() -> None:
                with _storage._write_lock:
                    holding.set()
                    release.wait(30)

            holder = threading.Thread(target=hold, daemon=True)
            holder.start()
            try:
                self.assertTrue(holding.wait(5), "the holder thread never took the write lock")
                self.assertEqual(run_in_child(_record_one_trace), "ok")
            finally:
                release.set()
                holder.join(5)

    def test_children_record_when_forked_while_threads_are_appending(self) -> None:
        # The shape an application actually reaches it through: nobody holds the
        # lock on purpose, several threads are just recording.
        with temporary_store() as store, recording_threads():
            outcomes = [run_in_child(_record_one_trace) for _ in range(3)]
            self.assertEqual(outcomes, ["ok", "ok", "ok"])
            self.assertTrue(store.exists())

    def test_a_fork_start_method_pool_from_a_threaded_parent_returns(self) -> None:
        # The same thing without a hand-written fork. `fork` is asked for
        # explicitly because it is not the default everywhere, and it is what
        # `multiprocessing` uses on Linux through 3.13.
        with temporary_store(), recording_threads(), warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # as in run_in_child
            pool = multiprocessing.get_context("fork").Pool(2)
            try:
                pending = pool.map_async(_record_in_pool_worker, range(2))
                self.assertEqual(pending.get(timeout=CHILD_TIMEOUT), [0, 1])
            finally:
                pool.terminate()
                pool.join()

    def test_a_child_starts_its_own_write_failure_reporting(self) -> None:
        # The counters describe the parent's recording. A child that inherited
        # them would announce a recovery, and a loss count, for events another
        # process failed to write.
        with temporary_store():
            with _sdk._write_failure_lock:
                _sdk._write_failing = True
                _sdk._events_lost_while_failing = 7

            def check() -> bool:
                return _sdk._write_failing is False and _sdk._events_lost_while_failing == 0

            try:
                self.assertEqual(run_in_child(check), "ok")
            finally:
                with _sdk._write_failure_lock:
                    _sdk._write_failing = False
                    _sdk._events_lost_while_failing = 0

    def test_a_child_does_not_claim_the_parents_last_append(self) -> None:
        # `_verified_tail` is how an append skips reading the file's final byte:
        # it records that *this* process left the store at exactly that size. The
        # child performed no append, and the parent may write again before the
        # child does, so the child has to re-read rather than trust it.
        with temporary_store():
            with bir.trace("parent"):
                pass
            self.assertIsNotNone(_storage._verified_tail)

            def check() -> bool:
                return _storage._verified_tail is None

            self.assertEqual(run_in_child(check), "ok")

    def test_the_store_is_readable_after_parent_and_children_record(self) -> None:
        with temporary_store() as store:
            with bir.trace("parent-before"):
                pass
            outcomes = [run_in_child(_record_one_trace) for _ in range(4)]
            with bir.trace("parent-after"):
                pass

            self.assertEqual(outcomes, ["ok"] * 4)
            names = sorted(event.name for event in bir.load_events(str(store)))
            self.assertEqual(names, ["child"] * 4 + ["parent-after", "parent-before"])


if __name__ == "__main__":
    unittest.main()
