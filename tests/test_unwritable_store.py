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


if __name__ == "__main__":
    unittest.main()
