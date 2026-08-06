"""Keeping the upload sidecar bounded by the store rather than by history.

``mark_sent`` records every event id a server accepted in a ``<trace_path>.sent``
sidecar and skips those ids on later sends. Nothing removed an id, so the file
grew with everything ever sent — while ``prune``, the operation whose whole job
is bounding local state, left it alone. A deployment that pruned daily still
accumulated a sidecar naming events that had not existed for months.

Prune now compacts it: an id for an event the store no longer holds can never be
matched by a later send, so it is dropped. These tests pin what survives that
(everything still in the store, across rotated files a prune did not touch), what
does not, and that compaction can never cost a send — it is advisory bookkeeping,
so a missing, unreadable, or unwritable sidecar leaves the prune's own result
alone.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import bir
from bir._sdk import _prune_trace_store, _record_sent_ids, _reset_config_for_tests, load_events
from bir._storage import _iter_stored_event_ids

TRACE_PATH = Path("traces.jsonl")
SENT_PATH = Path("traces.jsonl.sent")


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


def record_traces(count: int) -> None:
    """Record ``count`` traces, each carrying one span."""

    bir.configure(trace_path=str(TRACE_PATH))
    for index in range(count):
        with bir.trace(f"request-{index}"):
            with bir.span("step"):
                pass


def mark_everything_sent() -> set[str]:
    """Record every event currently in the store as already sent."""

    event_ids = [event.id for event in load_events(str(TRACE_PATH))]
    _record_sent_ids(SENT_PATH, event_ids)
    return set(event_ids)


def sidecar_ids() -> set[str]:
    return set(json.loads(SENT_PATH.read_text(encoding="utf-8"))["event_ids"])


class PruneCompactsTheSidecarTests(unittest.TestCase):
    """What prune leaves in the sidecar is what the store still holds."""

    def setUp(self) -> None:
        _reset_config_for_tests()

    def test_ids_for_removed_traces_are_dropped(self) -> None:
        with temporary_workdir():
            record_traces(5)
            recorded = mark_everything_sent()
            self.assertEqual(len(recorded), 10)

            _prune_trace_store(str(TRACE_PATH), keep_last=1, dry_run=False)

            surviving = {event.id for event in load_events(str(TRACE_PATH))}
            # Exactly the store, no more and no less: an id for an event that is
            # gone can never be matched again, and one still there must be.
            self.assertEqual(sidecar_ids(), surviving)
            self.assertEqual(len(surviving), 2)

    def test_ids_for_kept_traces_survive(self) -> None:
        with temporary_workdir():
            record_traces(4)
            kept = {event.id for event in load_events(str(TRACE_PATH))[-4:]}
            mark_everything_sent()

            _prune_trace_store(str(TRACE_PATH), keep_last=2, dry_run=False)

            self.assertTrue(kept.issubset(sidecar_ids()))

    def test_a_resend_after_compaction_still_skips_what_is_in_the_store(self) -> None:
        with temporary_workdir():
            record_traces(5)
            mark_everything_sent()
            _prune_trace_store(str(TRACE_PATH), keep_last=1, dry_run=False)

            # Compaction must not un-send anything: the events still in the store
            # were accepted, so a re-send has nothing left to attempt.
            with patch("bir._sdk._post_loaded_events") as post:
                result = bir.send_events("http://127.0.0.1:8000", path=str(TRACE_PATH), mark_sent=True)

            post.assert_not_called()
            self.assertEqual(result.attempted, 0)

    def test_a_repeated_record_send_prune_cycle_stays_bounded(self) -> None:
        with temporary_workdir():
            sizes = []
            for _ in range(4):
                record_traces(20)
                mark_everything_sent()
                _prune_trace_store(str(TRACE_PATH), keep_last=1, dry_run=False)
                sizes.append(len(sidecar_ids()))

            # The sidecar tracks the store, not the history: every cycle ends at
            # the two events of the one trace prune kept.
            self.assertEqual(sizes, [2, 2, 2, 2])

    def test_a_dry_run_leaves_the_sidecar_alone(self) -> None:
        with temporary_workdir():
            record_traces(5)
            recorded = mark_everything_sent()

            _prune_trace_store(str(TRACE_PATH), keep_last=1, dry_run=True)

            # A preview writes nothing, and that has to include the bookkeeping.
            self.assertEqual(sidecar_ids(), recorded)

    def test_a_prune_that_removes_nothing_leaves_the_sidecar_alone(self) -> None:
        with temporary_workdir():
            record_traces(3)
            recorded = mark_everything_sent()

            _prune_trace_store(str(TRACE_PATH), keep_last=10, dry_run=False)

            self.assertEqual(sidecar_ids(), recorded)

    def test_ids_in_rotated_files_a_prune_did_not_touch_survive(self) -> None:
        with temporary_workdir():
            # Rotate so events live in a sibling file, then prune only the active
            # one. The rotated file still holds its events, so its ids must stay.
            bir.configure(trace_path=str(TRACE_PATH), max_bytes=600, backup_count=3)
            for index in range(12):
                with bir.trace(f"request-{index}"):
                    with bir.span("step"):
                        pass

            rotated = Path(f"{TRACE_PATH.name}.1")
            self.assertTrue(rotated.exists(), "the store must have rotated for this case to mean anything")
            rotated_ids = {json.loads(line)["id"] for line in rotated.read_text(encoding="utf-8").splitlines() if line}
            _record_sent_ids(SENT_PATH, sorted(rotated_ids | {event.id for event in load_events(str(TRACE_PATH))}))

            _prune_trace_store(str(TRACE_PATH), keep_last=1, dry_run=False)

            self.assertTrue(rotated_ids.issubset(sidecar_ids()))


class CompactionIsAdvisoryTests(unittest.TestCase):
    """Bookkeeping can never cost the prune that triggered it."""

    def setUp(self) -> None:
        _reset_config_for_tests()

    def test_no_sidecar_means_nothing_to_do(self) -> None:
        with temporary_workdir() as workdir:
            record_traces(3)

            result = _prune_trace_store(str(TRACE_PATH), keep_last=1, dry_run=False)

            self.assertEqual(result.removed_traces, 2)
            # A store that never opted into mark_sent gains no sidecar, and no
            # lock file beside it either.
            self.assertEqual(
                sorted(path.name for path in workdir.iterdir() if ".sent" in path.name),
                [],
            )

    def test_an_unreadable_sidecar_is_left_as_it_is(self) -> None:
        with temporary_workdir():
            record_traces(3)
            SENT_PATH.write_text("{ this is not json", encoding="utf-8")

            result = _prune_trace_store(str(TRACE_PATH), keep_last=1, dry_run=False)

            self.assertEqual(result.removed_traces, 2)
            # Unreadable already means "nothing sent"; overwriting it would claim
            # to know something the SDK does not.
            self.assertEqual(SENT_PATH.read_text(encoding="utf-8"), "{ this is not json")

    def test_a_sidecar_that_cannot_be_written_does_not_fail_the_prune(self) -> None:
        with temporary_workdir():
            record_traces(5)
            recorded = mark_everything_sent()

            with patch("bir._storage._write_sent_ids", side_effect=OSError("read-only file system")):
                result = _prune_trace_store(str(TRACE_PATH), keep_last=1, dry_run=False)

            # The store is pruned and the result is earned; the sidecar simply
            # stayed the size it already was, which is the previous behavior.
            self.assertEqual(result.removed_traces, 4)
            self.assertEqual(len(load_events(str(TRACE_PATH))), 2)
            self.assertEqual(sidecar_ids(), recorded)

    def test_a_damaged_store_refuses_the_prune_and_the_sidecar_with_it(self) -> None:
        with temporary_workdir():
            record_traces(4)
            recorded = mark_everything_sent()
            with TRACE_PATH.open("a", encoding="utf-8") as store:
                store.write("{ half a line\n")

            # Prune reads the store strictly, because skipping a line there would
            # delete traces it could not account for. Compaction never runs, so
            # the sidecar keeps naming events that are all still present.
            with self.assertRaisesRegex(ValueError, "Invalid JSON in trace file"):
                _prune_trace_store(str(TRACE_PATH), keep_last=1, dry_run=False)

            self.assertEqual(sidecar_ids(), recorded)

    def test_compaction_reads_past_lines_it_cannot_use(self) -> None:
        # Compaction decides only whether an id is still present, so anything it
        # cannot read an id from is skipped rather than aborting the scan: a
        # blank line, a line that is not JSON, a JSON value that is not an
        # object, and an object with no string id.
        with temporary_workdir():
            record_traces(1)
            usable = {event.id for event in load_events(str(TRACE_PATH))}
            with TRACE_PATH.open("a", encoding="utf-8") as store:
                store.write("\n")
                store.write("{ half a line\n")
                store.write('["not", "an", "object"]\n')
                store.write('{"id": 17}\n')

            self.assertEqual(set(_iter_stored_event_ids(TRACE_PATH)), usable)

    def test_compaction_reads_ids_without_validating_the_event(self) -> None:
        # A line prune would refuse still contributes its id, because an id is
        # all compaction needs from it.
        with temporary_workdir():
            record_traces(2)
            lines = TRACE_PATH.read_text(encoding="utf-8").splitlines()
            kept_id = json.loads(lines[0])["id"]
            with TRACE_PATH.open("a", encoding="utf-8") as store:
                store.write("{ half a line\n")

            self.assertEqual(
                [event_id for event_id in _iter_stored_event_ids(TRACE_PATH)][0],
                kept_id,
            )
            self.assertEqual(len(list(_iter_stored_event_ids(TRACE_PATH))), len(lines))


if __name__ == "__main__":
    unittest.main()
