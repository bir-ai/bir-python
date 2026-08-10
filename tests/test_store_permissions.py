"""The files Bir writes for itself are readable only by the user who ran it.

They hold captured inputs and outputs, and redaction is documented as
best-effort, so the mode they are created with is a decision rather than
whatever the umask happened to allow. It was world-readable, which on a shared CI
runner or a multi-user host is the wrong default for a store of payloads.

The mode is set as the file is created rather than applied afterwards, so there
is no moment when it exists and anyone can read it, and it therefore only applies
to files Bir creates: one that already exists keeps the mode it has, because a
user who widened it meant to. Two things are deliberately left alone — the
directory, so a sibling process can still list the store, and anything the user
asked Bir to export to a path they named.

POSIX only: Windows has no umask and no owner/group/other bits to assert on.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import bir
from bir._sdk import _reset_config_for_tests
from bir.evals import Dataset, DatasetExample, contains, run_experiment

PRIVATE = 0o600


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


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@unittest.skipIf(sys.platform == "win32", "POSIX permission bits")
class StorePermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_config_for_tests()

    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_the_trace_store_and_its_rotated_siblings_are_private(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            bir.configure(trace_path=trace_path, max_bytes=900, backup_count=2)
            for index in range(20):
                with bir.trace(name=f"trace-{index}"):
                    with bir.span(name="child"):
                        pass

            rotated = sorted(workdir.glob("traces.jsonl.[0-9]"))
            self.assertTrue(rotated, "the store did not rotate, so nothing was proved about siblings")
            for path in [trace_path, *rotated]:
                with self.subTest(path=path.name):
                    self.assertEqual(mode_of(path), PRIVATE)

    def test_experiment_result_and_summary_are_private(self) -> None:
        with temporary_workdir() as workdir:
            result_path = workdir / "e.jsonl"
            run_experiment(
                "e",
                dataset=Dataset([DatasetExample(id="q", input={"s": "x"})]),
                task=lambda s: s,
                evaluators=[contains("x")],
                path=result_path,
            )

            # The summary is staged and renamed, so its mode comes from the temp
            # file the rename carried over.
            for path in (result_path, result_path.with_suffix(".summary.json")):
                with self.subTest(path=path.name):
                    self.assertEqual(mode_of(path), PRIVATE)

    def test_a_pruned_store_is_still_private(self) -> None:
        # Prune rewrites the store through a temp file it renames into place, so
        # the mode has to be set on the temp file or pruning would widen it.
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            bir.configure(trace_path=trace_path)
            for index in range(5):
                with bir.trace(name=f"trace-{index}"):
                    pass

            from bir.cli import main

            self.assertEqual(main(["prune", "--path", str(trace_path), "--keep-last", "2", "--yes", "--json"]), 0)
            self.assertEqual(mode_of(trace_path), PRIVATE)

    def test_an_existing_file_keeps_the_mode_it_has(self) -> None:
        # Someone who widened a file meant to, and the mode is only ever applied
        # as a file is created.
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            trace_path.touch()
            trace_path.chmod(0o644)
            bir.configure(trace_path=trace_path)

            with bir.trace(name="t"):
                pass

            self.assertEqual(mode_of(trace_path), 0o644)

    def test_the_directory_is_left_at_the_umask(self) -> None:
        # Listing the store stays possible; only reading the files does not.
        with temporary_workdir() as workdir:
            bir.configure(trace_path=workdir / ".bir" / "traces.jsonl")
            with bir.trace(name="t"):
                pass

            self.assertNotEqual(mode_of(workdir / ".bir"), PRIVATE)

    def test_an_export_the_user_asked_for_is_left_alone(self) -> None:
        # A dataset written to a path the caller named is a deliberate handoff,
        # not Bir's own store.
        with temporary_workdir() as workdir:
            dataset_path = workdir / "dataset.jsonl"
            Dataset([DatasetExample(id="q", input={"s": "x"})]).to_jsonl(dataset_path)

            self.assertNotEqual(mode_of(dataset_path), PRIVATE)


if __name__ == "__main__":
    unittest.main()
