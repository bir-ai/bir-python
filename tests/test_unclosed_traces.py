"""A trace whose root was never written, and what the reader says about it.

Bir writes a trace's root event when the trace closes, so a trace that never
closes leaves its child events on disk with no root. Every trace-shaped read
resolves a trace through that root, so those events belong to no trace and are
dropped: ``bir traces`` prints "No traces found" over a store that holds them,
``load_traces()`` returns an empty list, and nothing connects the two facts.

A root goes missing three ways -- the process died before the trace closed,
rotation dropped the file the root was written to, or a framework bridge never
got the terminal callback that would have written it -- and all three leave the
same shape on disk. These tests pin what the reader reports about that shape.
Silence is the defect: the events are recorded, so the reader has to say they are
there and unreachable rather than imply the store is empty.
"""

from __future__ import annotations

import contextvars
import io
import json
import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from uuid import uuid4

import bir
from bir import cli, load_traces
from bir._sdk import _reset_config_for_tests
from bir.integrations._lifecycle import _MAX_OPEN_RUNS, _open_implicit_root, _reclaim_open_root
from bir.integrations.langchain import BirCallbackHandler

TRACE_PATH = Path("traces.jsonl")


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


def run_cli(*argv: str) -> tuple[int, str, str]:
    """Run ``cli.main`` with captured stdout/stderr, returning (code, out, err)."""

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def write_events(*events: dict[str, Any]) -> None:
    """Write raw events, which is how a store with a missing root is built.

    The SDK cannot be made to write one through its public API -- that is the
    point of the root -- so the store is assembled directly, in the shape an
    interrupted trace leaves behind.
    """

    with TRACE_PATH.open("w", encoding="utf-8") as store:
        for event in events:
            store.write(json.dumps(event, sort_keys=True) + "\n")


def event(
    *,
    event_id: str,
    trace_id: str,
    parent_id: str | None,
    name: str,
    event_type: str,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "id": event_id,
        "trace_id": trace_id,
        "parent_id": parent_id,
        "name": name,
        "type": event_type,
        "start_time": "2026-08-06T00:00:00+00:00",
        "end_time": "2026-08-06T00:00:01+00:00",
        "status": "success",
        "metadata": {},
        "input": None,
        "output": None,
        "error": None,
    }


ROOTLESS_TRACE = "11111111-1111-4111-8111-111111111111"


def write_rootless_store() -> None:
    """One trace whose root is missing, holding two recorded events."""

    write_events(
        event(
            event_id="22222222-2222-4222-8222-222222222222",
            trace_id=ROOTLESS_TRACE,
            parent_id=ROOTLESS_TRACE,
            name="llm",
            event_type="generation",
        ),
        event(
            event_id="33333333-3333-4333-8333-333333333333",
            trace_id=ROOTLESS_TRACE,
            parent_id="22222222-2222-4222-8222-222222222222",
            name="db",
            event_type="span",
        ),
    )


class RootlessTraceReportingTests(unittest.TestCase):
    """The read commands say what they could not turn into a trace."""

    def setUp(self) -> None:
        _reset_config_for_tests()

    def test_traces_reports_what_it_could_not_list(self) -> None:
        with temporary_workdir():
            write_rootless_store()

            code, out, err = run_cli("traces", "--path", str(TRACE_PATH))

            self.assertEqual(code, 0)
            self.assertIn("2 events across 1 trace have no trace root and are not listed", err)
            self.assertIn(ROOTLESS_TRACE, err)
            # The listing itself is unchanged: a trace with no root is still not
            # a trace, and inventing a row for it would be worse than saying so.
            self.assertIn("No traces found", out)

    def test_stats_reports_it_too_and_keeps_json_on_stdout(self) -> None:
        with temporary_workdir():
            write_rootless_store()

            code, out, err = run_cli("stats", "--path", str(TRACE_PATH), "--json")

            self.assertEqual(code, 0)
            self.assertIn("have no trace root", err)
            # A script must still be able to parse stdout, which is why the
            # report goes to stderr.
            self.assertEqual(json.loads(out)["traces"]["total"], 0)

    def test_show_distinguishes_a_rootless_trace_from_an_absent_one(self) -> None:
        with temporary_workdir():
            write_rootless_store()

            found_code, _, found_err = run_cli("show", ROOTLESS_TRACE, "--path", str(TRACE_PATH))
            missing_code, _, missing_err = run_cli("show", "no-such-trace", "--path", str(TRACE_PATH))

            self.assertEqual(found_code, 1)
            self.assertIn("2 recorded events but no trace root", found_err)
            # Reporting the recorded-but-unreachable case as "not found" sends
            # someone looking for the wrong thing.
            self.assertEqual(missing_code, 1)
            self.assertIn("not found", missing_err)
            self.assertNotIn("not found", found_err)

    def test_a_healthy_store_reports_nothing(self) -> None:
        with temporary_workdir():
            bir.configure(trace_path=str(TRACE_PATH))

            @bir.observe(name="checkout")
            def checkout() -> None:
                with bir.span(name="db"):
                    pass

            checkout()

            code, out, err = run_cli("traces", "--path", str(TRACE_PATH))

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertIn("checkout", out)

    def test_the_report_counts_traces_not_events(self) -> None:
        with temporary_workdir():
            second = "44444444-4444-4444-8444-444444444444"
            write_events(
                event(
                    event_id="22222222-2222-4222-8222-222222222222",
                    trace_id=ROOTLESS_TRACE,
                    parent_id=ROOTLESS_TRACE,
                    name="llm",
                    event_type="generation",
                ),
                event(
                    event_id="55555555-5555-4555-8555-555555555555",
                    trace_id=second,
                    parent_id=second,
                    name="llm",
                    event_type="generation",
                ),
            )

            _, _, err = run_cli("traces", "--path", str(TRACE_PATH))

            self.assertIn("2 events across 2 traces have no trace root and are not listed", err)

    def test_a_rootless_trace_beside_a_healthy_one_does_not_hide_it(self) -> None:
        with temporary_workdir():
            bir.configure(trace_path=str(TRACE_PATH))

            @bir.observe(name="checkout")
            def checkout() -> None:
                pass

            checkout()
            # Append the rootless events to the store the SDK just wrote.
            with TRACE_PATH.open("a", encoding="utf-8") as store:
                store.write(
                    json.dumps(
                        event(
                            event_id="22222222-2222-4222-8222-222222222222",
                            trace_id=ROOTLESS_TRACE,
                            parent_id=ROOTLESS_TRACE,
                            name="llm",
                            event_type="generation",
                        ),
                        sort_keys=True,
                    )
                    + "\n"
                )

            code, out, err = run_cli("traces", "--path", str(TRACE_PATH))

            self.assertEqual(code, 0)
            self.assertIn("checkout", out)
            self.assertIn("1 event across 1 trace has no trace root and is not listed", err)
            # And the public loader still drops it, which is the behavior the
            # report exists to explain rather than change.
            self.assertEqual([loaded.name for loaded in load_traces(str(TRACE_PATH))], ["checkout"])


class AbandonedBridgeRunTests(unittest.TestCase):
    """The recovery a long-lived handler gets when a framework drops a callback.

    The bridge contract asserts this for every handler whose framework says when
    a run is top-level. These cases pin the shape end to end, through the handler
    a server would actually keep around, and pin what the recovery is worth: the
    abandoned run stops being stranded events and becomes a trace.
    """

    def setUp(self) -> None:
        _reset_config_for_tests()

    def abandon_then_serve_again(self) -> None:
        """One dropped callback, then the next request on the same worker."""

        handler = BirCallbackHandler()
        handler.on_llm_start({"name": "llm"}, ["hi"], run_id=uuid4(), parent_run_id=None)

        run_id = uuid4()
        handler.on_chain_start({"name": "chain"}, {"q": "x"}, run_id=run_id, parent_run_id=None)
        handler.on_chain_end({"a": "y"}, run_id=run_id)

    def test_the_abandoned_run_becomes_a_findable_trace(self) -> None:
        with temporary_workdir():
            bir.configure(trace_path=str(TRACE_PATH))

            contextvars.copy_context().run(self.abandon_then_serve_again)

            traces = {loaded.name: loaded for loaded in load_traces(str(TRACE_PATH))}
            # Both the run's generation and the root it had to open are written,
            # so the run reads back as a whole trace rather than as events no
            # trace-shaped reader can reach.
            self.assertEqual(sorted(traces), ["chain", "llm"])
            self.assertEqual(len(traces["llm"].events), 2)

    def test_the_reclaim_says_the_run_was_never_closed(self) -> None:
        with temporary_workdir():
            bir.configure(trace_path=str(TRACE_PATH))

            contextvars.copy_context().run(self.abandon_then_serve_again)

            abandoned = [
                recorded for recorded in bir.load_events(str(TRACE_PATH)) if recorded.metadata.get("abandoned")
            ]
            # A trace nobody closed must not read like one that finished
            # normally, or a later look at the store cannot tell the difference.
            self.assertEqual({recorded.name for recorded in abandoned}, {"llm"})
            self.assertEqual({recorded.metadata["abandoned"] for recorded in abandoned}, {"superseded"})

    def test_the_store_stops_reporting_unreachable_events(self) -> None:
        with temporary_workdir():
            bir.configure(trace_path=str(TRACE_PATH))

            contextvars.copy_context().run(self.abandon_then_serve_again)

            code, out, err = run_cli("traces", "--path", str(TRACE_PATH))

            self.assertEqual(code, 0)
            # Nothing is stranded any more, so the rootless report has nothing
            # to say and both traces are listed.
            self.assertEqual(err, "")
            self.assertIn("llm", out)
            self.assertIn("chain", out)

    def test_a_run_reclaimed_from_another_context_is_still_written(self) -> None:
        with temporary_workdir():
            bir.configure(trace_path=str(TRACE_PATH))
            handler = BirCallbackHandler()

            # Abandon the run in one context and reclaim it from a copy of that
            # context. The run's contextvar tokens belong to the original, where
            # resetting them from here would raise, which is why the reclaim
            # restores the surrounding values instead of resetting tokens.
            outer = contextvars.copy_context()
            outer.run(handler.on_llm_start, {"name": "llm"}, ["hi"], run_id=uuid4(), parent_run_id=None)

            def serve_again() -> None:
                run_id = uuid4()
                handler.on_chain_start({"name": "chain"}, {"q": "x"}, run_id=run_id, parent_run_id=None)
                handler.on_chain_end({"a": "y"}, run_id=run_id)

            contextvars.copy_context().run(lambda: outer.run(serve_again))

            self.assertEqual(
                sorted(loaded.name for loaded in load_traces(str(TRACE_PATH))),
                ["chain", "llm"],
            )

    def test_open_runs_stay_bounded_without_any_terminal_callback(self) -> None:
        with temporary_workdir():
            bir.configure(trace_path=str(TRACE_PATH))
            handler = BirCallbackHandler()

            def flood() -> None:
                for _ in range(_MAX_OPEN_RUNS * 2):
                    handler.on_llm_start({"name": "llm"}, ["hi"], run_id=uuid4(), parent_run_id=None)

            contextvars.copy_context().run(flood)

            self.assertEqual(len(handler._active_runs), _MAX_OPEN_RUNS)
            # What the handler let go of was written rather than dropped, so the
            # bound costs bookkeeping rather than recorded data.
            evicted = [recorded for recorded in bir.load_events(str(TRACE_PATH)) if recorded.metadata.get("abandoned")]
            self.assertTrue(evicted)
            self.assertEqual({recorded.metadata["abandoned"] for recorded in evicted}, {"evicted"})


class ReclaimEdgeCaseTests(unittest.TestCase):
    """The reclaim's safety nets, which no bridge reaches on a normal path."""

    def setUp(self) -> None:
        _reset_config_for_tests()

    def test_a_root_with_no_registered_run_is_still_written(self) -> None:
        with temporary_workdir():
            bir.configure(trace_path=str(TRACE_PATH))

            def sequence() -> None:
                # A root recorded before the handler managed to register the run
                # that owns it — an exception between the two calls. Nothing
                # links the root to a handler entry, so the reclaim has to close
                # the root on its own rather than leaving it entered.
                _open_implicit_root(name="orphaned-root", metadata={"integration": "test"})
                _reclaim_open_root()

            contextvars.copy_context().run(sequence)

            traces = load_traces(str(TRACE_PATH))
            self.assertEqual([loaded.name for loaded in traces], ["orphaned-root"])
            self.assertEqual(traces[0].root.metadata["abandoned"], "superseded")

    def test_a_reclaim_with_nothing_open_does_nothing(self) -> None:
        with temporary_workdir():
            bir.configure(trace_path=str(TRACE_PATH))

            _reclaim_open_root()

            self.assertFalse(TRACE_PATH.exists())

    def test_a_root_closed_normally_is_not_reclaimed_twice(self) -> None:
        with temporary_workdir():
            bir.configure(trace_path=str(TRACE_PATH))

            def sequence() -> None:
                context = _open_implicit_root(name="closed-root", metadata={"integration": "test"})
                assert context is not None
                context.__exit__(None, None, None)
                # The entry is stale now: the root it names is no longer the
                # ambient trace, so the reclaim drops it instead of writing the
                # event a second time.
                _reclaim_open_root()

            contextvars.copy_context().run(sequence)

            events = bir.load_events(str(TRACE_PATH))
            self.assertEqual(len(events), 1)
            self.assertNotIn("abandoned", events[0].metadata)


if __name__ == "__main__":
    unittest.main()
