"""Recording into a store that cannot be written.

Bir records a call by appending to a local JSONL file, and that append can fail
for reasons the application did nothing to cause: a read-only container
filesystem, a full disk, a ``.bir/`` owned by another user, an ephemeral volume
that went away. Recording is bookkeeping *about* a call rather than part of it,
so none of that may decide whether the call succeeded — a function that charged
an order must not raise ``PermissionError`` at its caller because a trace could
not be written.

Silence would be the other way to get it wrong. A store that is not being written
is worth knowing about, so a failure is reported on the SDK's own ``bir`` logger:
once when writing starts failing, once when it recovers, with a count of what was
lost in between. Reporting per event would be its own failure, since one outage
would then produce one message per event of every trace.

These tests pin both halves for every entry point that records, and pin that the
operations invoked *for* their effect — ``prune``, ``send``, the loaders — still
raise what they hit.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

import bir
from bir._sdk import _prune_trace_store, _reset_config_for_tests, load_events, load_traces


@contextmanager
def temporary_workdir() -> Iterator[Path]:
    previous = Path.cwd()
    with tempfile.TemporaryDirectory() as directory:
        workdir = Path(directory)
        os.chdir(workdir)
        try:
            yield workdir
        finally:
            os.chdir(previous)
            _reset_config_for_tests()


def make_store_unwritable() -> None:
    """Point the trace path at a directory, so every append fails to open it.

    This is the portable trigger: the concrete ``OSError`` subclass differs by
    platform (``IsADirectoryError`` on POSIX, ``PermissionError`` on Windows) but
    the append fails either way, and unlike a permission bit it behaves the same
    when the suite runs as root.
    """

    blocked = Path("blocked")
    blocked.mkdir(exist_ok=True)
    bir.configure(trace_path=str(blocked))


class TracedCallsSurviveTests(unittest.TestCase):
    """Whatever the store does, the call returns what it computed."""

    def setUp(self) -> None:
        _reset_config_for_tests()

    def test_a_decorated_function_returns_its_result(self) -> None:
        with temporary_workdir():
            make_store_unwritable()

            @bir.observe(name="checkout")
            def checkout(order: str) -> str:
                return f"charged {order}"

            with self.assertLogs("bir", level="ERROR"):
                self.assertEqual(checkout("A-1"), "charged A-1")

    def test_an_async_function_returns_its_result(self) -> None:
        with temporary_workdir():
            make_store_unwritable()

            @bir.observe(name="checkout")
            async def checkout() -> str:
                return "charged"

            with self.assertLogs("bir", level="ERROR"):
                self.assertEqual(asyncio.run(checkout()), "charged")

    def test_a_generator_function_yields_everything(self) -> None:
        with temporary_workdir():
            make_store_unwritable()

            @bir.observe(name="stream")
            def stream() -> Iterator[str]:
                yield "a"
                yield "b"

            with self.assertLogs("bir", level="ERROR"):
                self.assertEqual(list(stream()), ["a", "b"])

    def test_every_context_manager_completes(self) -> None:
        with temporary_workdir():
            make_store_unwritable()

            with self.assertLogs("bir", level="ERROR"):
                with bir.trace(name="request"):
                    with bir.span(name="step"):
                        pass
                    with bir.generation(name="llm", model="m"):
                        pass
                    with bir.tool_call(name="search"):
                        pass
                    with bir.retrieval(name="docs", query="q"):
                        pass

    def test_score_completes(self) -> None:
        with temporary_workdir():
            make_store_unwritable()

            with self.assertLogs("bir", level="ERROR"):
                with bir.trace(name="request"):
                    bir.score(name="quality", value=1.0)

    def test_the_bodys_own_exception_still_wins(self) -> None:
        with temporary_workdir():
            make_store_unwritable()

            @bir.observe(name="failing")
            def failing() -> str:
                raise ValueError("business rule violated")

            # The precedence that already existed has to survive: the caller sees
            # what its own code raised, never what Bir hit while recording it.
            with self.assertLogs("bir", level="ERROR"):
                with self.assertRaisesRegex(ValueError, "business rule violated"):
                    failing()


class FailureIsReportedOnceTests(unittest.TestCase):
    """One message per outage, not one per event."""

    def setUp(self) -> None:
        _reset_config_for_tests()

    def test_a_persistent_outage_reports_once(self) -> None:
        with temporary_workdir():
            make_store_unwritable()

            @bir.observe(name="request")
            def request() -> None:
                with bir.span(name="step"):
                    pass

            with self.assertLogs("bir", level="ERROR") as captured:
                for _ in range(20):
                    request()

            # 20 calls, two events each; one report.
            self.assertEqual(len(captured.output), 1)
            self.assertIn("could not write to the trace store", captured.output[0])
            self.assertIn("blocked", captured.output[0])

    def test_recovery_is_reported_with_what_was_lost(self) -> None:
        with temporary_workdir() as workdir:
            make_store_unwritable()

            @bir.observe(name="request")
            def request() -> None:
                pass

            with self.assertLogs("bir", level="ERROR"):
                request()
                request()

            # The store becomes writable again.
            bir.configure(trace_path=str(workdir / "traces.jsonl"))
            with self.assertLogs("bir", level="WARNING") as captured:
                request()

            self.assertIn("resumed writing", captured.output[0])
            self.assertIn("2 event(s) were dropped", captured.output[0])
            self.assertEqual([event.name for event in load_events("traces.jsonl")], ["request"])

    def test_a_second_outage_is_reported_again(self) -> None:
        with temporary_workdir() as workdir:

            @bir.observe(name="request")
            def request() -> None:
                pass

            for _ in range(2):
                make_store_unwritable()
                with self.assertLogs("bir", level="ERROR") as captured:
                    request()
                self.assertEqual(len(captured.output), 1)
                # Recovering resets the report, so the next outage is not silent.
                bir.configure(trace_path=str(workdir / "traces.jsonl"))
                with self.assertLogs("bir", level="WARNING"):
                    request()

    def test_a_healthy_store_logs_nothing(self) -> None:
        with temporary_workdir():
            bir.configure(trace_path="traces.jsonl")

            @bir.observe(name="request")
            def request() -> None:
                pass

            with self.assertNoLogs("bir"):
                request()

            self.assertEqual(len(load_traces("traces.jsonl")), 1)


class ExplicitOperationsStayStrictTests(unittest.TestCase):
    """Commands invoked for their effect must still report failure."""

    def setUp(self) -> None:
        _reset_config_for_tests()

    def test_prune_still_raises(self) -> None:
        with temporary_workdir():
            bir.configure(trace_path="traces.jsonl")
            with bir.trace(name="request"):
                pass
            # A damaged store is the reachable failure for prune, and it must
            # keep refusing rather than deleting what it could not account for.
            with Path("traces.jsonl").open("a", encoding="utf-8") as store:
                store.write("{ half a line\n")

            with self.assertRaisesRegex(ValueError, "Invalid JSON in trace file"):
                _prune_trace_store("traces.jsonl", keep_last=1, dry_run=False)

    def test_the_loaders_still_raise(self) -> None:
        with temporary_workdir():
            Path("traces.jsonl").write_text("{ half a line\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Invalid JSON in trace file"):
                load_events("traces.jsonl")
            with self.assertRaisesRegex(ValueError, "Invalid JSON in trace file"):
                load_traces("traces.jsonl")

    def test_send_still_raises(self) -> None:
        with temporary_workdir():
            Path("traces.jsonl").write_text("{ half a line\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Invalid JSON in trace file"):
                bir.send_events("http://127.0.0.1:8000", path="traces.jsonl")


class RecordedDataIsUnchangedTests(unittest.TestCase):
    """A writable store records exactly what it did before."""

    def setUp(self) -> None:
        _reset_config_for_tests()

    def test_a_successful_trace_is_written_whole(self) -> None:
        with temporary_workdir():
            bir.configure(trace_path="traces.jsonl", capture_outputs=True)

            @bir.observe(name="checkout")
            def checkout() -> str:
                with bir.generation(name="llm", model="m") as generation:
                    generation.set_usage(input_tokens=3, output_tokens=4)
                return "done"

            checkout()

            events = [json.loads(line) for line in Path("traces.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([event["type"] for event in events], ["generation", "trace"])
            self.assertEqual(events[1]["status"], "success")
            self.assertEqual(events[1]["output"], "done")

    def test_a_failing_trace_still_records_its_error(self) -> None:
        with temporary_workdir():
            bir.configure(trace_path="traces.jsonl")

            @bir.observe(name="failing")
            def failing() -> None:
                raise ValueError("business rule violated")

            with self.assertRaises(ValueError):
                failing()

            event = load_events("traces.jsonl")[0]
            self.assertEqual(event.status, "error")
            self.assertEqual(event.error, "business rule violated")


@contextmanager
def failing_part_way_through_a_write(prefix_bytes: int) -> Iterator[None]:
    """Let the next append write ``prefix_bytes`` and then fail, as a full disk does.

    ``open`` is patched rather than the filesystem because a partial write is
    what an interrupted append actually leaves, and no portable filesystem
    trigger produces exactly that many bytes on demand.
    """

    real_open = open
    exhausted = False

    class _PartialWriter:
        def __init__(self, handle: Any) -> None:
            self._handle = handle

        def write(self, text: str) -> int:
            nonlocal exhausted
            if exhausted:
                return self._handle.write(text)
            exhausted = True
            self._handle.write(text[:prefix_bytes])
            self._handle.flush()
            raise OSError(28, "No space left on device")

        def __getattr__(self, name: str) -> Any:
            return getattr(self._handle, name)

        def __enter__(self) -> _PartialWriter:
            self._handle.__enter__()
            return self

        def __exit__(self, *exception: Any) -> Any:
            return self._handle.__exit__(*exception)

    def opener(*args: Any, **kwargs: Any) -> Any:
        handle = real_open(*args, **kwargs)
        if exhausted or "a" not in str(args[1] if len(args) > 1 else kwargs.get("mode", "")):
            return handle
        return _PartialWriter(handle)

    with mock.patch("bir._storage.open", opener, create=True):
        yield


class AppendingOntoAnUnfinishedWriteTests(unittest.TestCase):
    """An append never lands on the bytes of a write that did not finish.

    An append writes at the byte after whatever is already there, so a store
    ending mid-line would fuse the fragment and the incoming event into one line
    that parses as neither — destroying an event that was written whole along
    with the one that was not, and leaving damage no reader and no writing
    command can get past. The unfinished bytes are removed first instead.
    """

    def setUp(self) -> None:
        _reset_config_for_tests()

    def test_the_event_after_an_interrupted_write_is_written_whole(self) -> None:
        with temporary_workdir():
            bir.configure(trace_path="traces.jsonl")
            with bir.trace("before"):
                pass

            with failing_part_way_through_a_write(40):
                with self.assertLogs("bir", level="ERROR"):
                    with bir.trace("interrupted"):
                        pass

            store = Path("traces.jsonl")
            self.assertFalse(store.read_bytes().endswith(b"\n"))

            with self.assertLogs("bir", level="WARNING") as logs:
                with bir.trace("after"):
                    pass

            self.assertIn("ending in a write that never finished", logs.output[0])
            # Nothing unreadable is left, so the strict loaders read the store
            # whole rather than refusing it.
            self.assertEqual([event.name for event in load_events("traces.jsonl")], ["before", "after"])

    def test_the_traced_call_is_unaffected_by_the_repair(self) -> None:
        with temporary_workdir():
            bir.configure(trace_path="traces.jsonl")
            with bir.trace("before"):
                pass
            with failing_part_way_through_a_write(40):
                with self.assertLogs("bir", level="ERROR"):
                    with bir.trace("interrupted"):
                        pass

            @bir.observe(name="checkout")
            def checkout(order: str) -> str:
                return f"charged {order}"

            with self.assertLogs("bir", level="WARNING"):
                self.assertEqual(checkout("A-1"), "charged A-1")

    def test_a_store_left_unfinished_by_an_earlier_run_is_repaired(self) -> None:
        with temporary_workdir():
            # No failure in this process: the fragment is simply already on disk,
            # which is what an OOM-killed run or another writer leaves behind.
            bir.configure(trace_path="traces.jsonl")
            with bir.trace("before"):
                pass
            store = Path("traces.jsonl")
            store.write_bytes(store.read_bytes() + b'{"id":"frag","type":"tr')

            with self.assertLogs("bir", level="WARNING") as logs:
                with bir.trace("after"):
                    pass

            self.assertIn("23 byte(s) were dropped", logs.output[0])
            self.assertEqual([event.name for event in load_events("traces.jsonl")], ["before", "after"])

    def test_a_file_that_is_nothing_but_an_unfinished_write_is_emptied(self) -> None:
        with temporary_workdir():
            bir.configure(trace_path="traces.jsonl")
            Path("traces.jsonl").write_bytes(b'{"id":"frag","type":"tr')

            with self.assertLogs("bir", level="WARNING"):
                with bir.trace("after"):
                    pass

            self.assertEqual([event.name for event in load_events("traces.jsonl")], ["after"])

    def test_the_repair_happens_before_rotation_carries_it_away(self) -> None:
        with temporary_workdir():
            # A rotated sibling is never appended to, so a fragment renamed into
            # one would stay unreadable for good.
            bir.configure(trace_path="traces.jsonl", max_bytes=200, backup_count=3)
            with bir.trace("before"):
                pass
            store = Path("traces.jsonl")
            store.write_bytes(store.read_bytes() + b'{"id":"frag","type":"tr')

            with self.assertLogs("bir", level="WARNING"):
                for index in range(4):
                    with bir.trace(f"after-{index}"):
                        pass

            rotated = sorted(Path().glob("traces.jsonl.*"))
            self.assertTrue(rotated)
            for path in [*rotated, store]:
                with self.subTest(file=path.name):
                    self.assertTrue(path.read_bytes().endswith(b"\n"))
            # Reading every file strictly is the assertion: had the fragment been
            # renamed into a sibling it would still be there, and nothing appends
            # to a sibling to repair it later. ("before" is gone because rotation
            # dropped it past backup_count, which is rotation working.)
            names = [event.name for event in load_events("traces.jsonl", include_rotated=True)]
            self.assertEqual(names, ["after-0", "after-1", "after-2", "after-3"])

    def test_a_healthy_store_is_never_touched_and_says_nothing(self) -> None:
        with temporary_workdir():
            bir.configure(trace_path="traces.jsonl")
            store = Path("traces.jsonl")
            sizes = []
            with self.assertNoLogs("bir"):
                for index in range(5):
                    with bir.trace(f"t{index}"):
                        pass
                    sizes.append(store.stat().st_size)

            # Every append only ever added, and the store reads back whole.
            self.assertEqual(sizes, sorted(sizes))
            self.assertEqual(len(load_events("traces.jsonl")), 5)


if __name__ == "__main__":
    unittest.main()
