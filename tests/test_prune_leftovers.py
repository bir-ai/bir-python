"""What an interrupted ``bir prune`` abandons, and what the next one does with it.

Prune is crash-safe in the direction that matters: it streams survivors into a
staging sibling and only then replaces the original, so a run that is killed
leaves the store exactly as it was. What it also leaves is a near-copy of the
store next to it and the SQLite selection index it was using, and nothing ever
picked either up again -- so the command whose purpose is reclaiming space could
leave more behind than it freed, and running it again did not help. Ctrl-C on a
long prune is the ordinary way to reach that.

These tests pin the sweep that reclaims them: which names it recognizes, which
siblings it must not touch, when a leftover counts as abandoned rather than in
use, that a preview still writes nothing, and that a leftover the sweep cannot
read or remove is left alone rather than failing the prune that found it.
"""

from __future__ import annotations

import ctypes
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

import bir
from bir import cli
from bir._sdk import _reset_config_for_tests
from bir._storage import (
    _PRUNE_INDEX_PREFIX,
    _WINDOWS_ERROR_INVALID_PARAMETER,
    _WINDOWS_MAX_PID,
    _WINDOWS_QUERY_LIMITED_INFORMATION,
    _WINDOWS_STILL_ACTIVE,
    _entries_matching,
    _process_is_running,
    _prune_staging_path,
    _PruneTraceIndex,
    _windows_process_is_running,
)


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


@contextmanager
def temporary_tempdir(workdir: Path) -> Iterator[Path]:
    """Point ``tempfile`` at a private directory so the index sweep is observable."""

    tempdir = workdir / "tmp"
    tempdir.mkdir()
    with patch.object(tempfile, "tempdir", str(tempdir)):
        yield tempdir


@contextmanager
def refusing_stat_for(target: Path) -> Iterator[None]:
    """Make one path unreadable to ``stat``, as a permission or a race would."""

    real_stat = Path.stat

    def refuse(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if path == target:
            raise PermissionError("refused")
        return real_stat(path, follow_symlinks=follow_symlinks)

    with patch.object(Path, "stat", new=refuse):
        yield


def run_cli(*argv: str) -> tuple[int, str, str]:
    """Run ``cli.main`` with captured stdout/stderr, returning (code, out, err)."""

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def record_traces(trace_path: Path, count: int) -> None:
    """Record ``count`` whole traces into ``trace_path`` through the public API."""

    bir.configure(trace_path=str(trace_path), enabled=True)
    for index in range(count):
        with bir.trace(f"request-{index}"):
            with bir.generation("llm", model="gpt-4o-mini") as generation:
                generation.set_usage(input_tokens=11, output_tokens=4)


def abandoned_staging_copy(file_path: Path, pid: int, *, size: int = 2048) -> Path:
    """Leave the staging sibling prune itself writes, as a killed run would."""

    with patch("os.getpid", return_value=pid):
        staged = _prune_staging_path(file_path)
    staged.write_bytes(b"x" * size)
    return staged


def abandoned_index(tempdir: Path, pid: int, *, size: int = 4096, suffix: str = "ab12cd34") -> Path:
    """Leave the index directory prune builds its selection in."""

    index = tempdir / f"{_PRUNE_INDEX_PREFIX}{pid}-{suffix}"
    index.mkdir()
    (index / "traces.sqlite3").write_bytes(b"x" * size)
    return index


class DeadProcessTest(unittest.TestCase):
    """Base class owning one pid that is provably not running any more."""

    dead_pid: int

    @classmethod
    def setUpClass(cls) -> None:
        # A reaped child's pid names no process, which is the state the writer of
        # an abandoned leftover is in. Reuse is the only way this can be wrong and
        # needs the operating system to hand the same number out within one run.
        process = subprocess.Popen([sys.executable, "-c", ""])
        process.wait()
        cls.dead_pid = process.pid

    def setUp(self) -> None:
        _reset_config_for_tests()


class PruneReclaimsWhatAnInterruptedPruneLeftTests(DeadProcessTest):
    """A prune that follows an interrupted one reclaims what it abandoned."""

    def test_a_write_run_sweeps_an_abandoned_staging_copy_and_index(self) -> None:
        with temporary_workdir() as workdir, temporary_tempdir(workdir) as tempdir:
            trace_path = workdir / "traces.jsonl"
            record_traces(trace_path, 3)
            staged = abandoned_staging_copy(trace_path, self.dead_pid, size=2048)
            index = abandoned_index(tempdir, self.dead_pid, size=4096)

            code, out, err = run_cli("prune", "--path", str(trace_path), "--keep-last", "1", "--yes")

            self.assertEqual(code, 0)
            self.assertFalse(staged.exists())
            self.assertFalse(index.exists())
            self.assertIn("swept 2 leftover file(s) of 6144 bytes", err)
            self.assertIn("removed=2", out)
            self.assertEqual(len(bir.load_traces(str(trace_path))), 1)

    def test_a_run_that_selects_nothing_still_sweeps(self) -> None:
        with temporary_workdir() as workdir, temporary_tempdir(workdir) as tempdir:
            trace_path = workdir / "traces.jsonl"
            record_traces(trace_path, 2)
            staged = abandoned_staging_copy(trace_path, self.dead_pid)
            index = abandoned_index(tempdir, self.dead_pid)

            # Nothing matches --keep-last 100, and the leftovers are still the
            # previous run's to reclaim.
            code, out, err = run_cli("prune", "--path", str(trace_path), "--keep-last", "100", "--yes")

            self.assertEqual(code, 0)
            self.assertFalse(staged.exists())
            self.assertFalse(index.exists())
            self.assertIn("swept 2 leftover file(s)", err)
            self.assertIn("removed=0", out)
            self.assertEqual(len(bir.load_traces(str(trace_path))), 2)

    def test_a_rotated_staging_copy_is_swept_without_include_rotated(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            record_traces(trace_path, 2)
            rotated = trace_path.with_name(trace_path.name + ".1")
            rotated.write_bytes(trace_path.read_bytes())
            # An interrupted --include-rotated run stages every file it rewrites,
            # and the leftover is the same garbage whichever flags the next run has.
            staged = abandoned_staging_copy(rotated, self.dead_pid, size=512)

            code, _out, err = run_cli("prune", "--path", str(trace_path), "--keep-last", "1", "--yes")

            self.assertEqual(code, 0)
            self.assertFalse(staged.exists())
            self.assertIn("swept 1 leftover file(s) of 512 bytes", err)
            self.assertTrue(rotated.exists())

    def test_the_sweep_recognizes_the_index_directory_prune_itself_builds(self) -> None:
        with temporary_workdir() as workdir, temporary_tempdir(workdir) as tempdir:
            trace_path = workdir / "traces.jsonl"
            record_traces(trace_path, 2)
            # Take the real name from the real producer so the recognizer cannot
            # drift away from it, then leave a copy behind as a killed run would.
            with patch("os.getpid", return_value=self.dead_pid), _PruneTraceIndex() as index:
                database_path = index.database_path
                assert database_path is not None
                abandoned_name = database_path.parent.name
            abandoned = tempdir / abandoned_name
            abandoned.mkdir()
            (abandoned / "traces.sqlite3").write_bytes(b"x" * 100)

            code, _out, err = run_cli("prune", "--path", str(trace_path), "--keep-last", "1", "--yes")

            self.assertEqual(code, 0)
            self.assertFalse(abandoned.exists())
            self.assertIn("swept 1 leftover file(s) of 100 bytes", err)


class SweepReportingTests(DeadProcessTest):
    """The sweep is reported the way the other repair prune performs already is."""

    def test_a_preview_reports_what_it_would_sweep_and_removes_nothing(self) -> None:
        with temporary_workdir() as workdir, temporary_tempdir(workdir) as tempdir:
            trace_path = workdir / "traces.jsonl"
            record_traces(trace_path, 2)
            staged = abandoned_staging_copy(trace_path, self.dead_pid, size=64)
            index = abandoned_index(tempdir, self.dead_pid, size=32)

            code, out, err = run_cli("prune", "--path", str(trace_path), "--keep-last", "1")

            self.assertEqual(code, 0)
            self.assertIn("would sweep 2 leftover file(s) of 96 bytes", err)
            self.assertIn("(dry run; pass --yes to apply)", out)
            # Without --yes nothing is written, and removing a file is a write.
            self.assertTrue(staged.exists())
            self.assertTrue(index.exists())

    def test_json_reports_the_sweep_as_its_own_fields(self) -> None:
        with temporary_workdir() as workdir, temporary_tempdir(workdir) as tempdir:
            trace_path = workdir / "traces.jsonl"
            record_traces(trace_path, 2)
            abandoned_staging_copy(trace_path, self.dead_pid, size=1000)
            abandoned_index(tempdir, self.dead_pid, size=24)

            code, out, _err = run_cli("prune", "--path", str(trace_path), "--keep-last", "1", "--yes", "--json")

            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(payload["swept_leftovers"], 2)
            self.assertEqual(payload["swept_leftover_bytes"], 1024)
            # The store's own saving is what bytes_reclaimed measures; what the
            # previous run abandoned is counted separately rather than folded in.
            self.assertLess(payload["bytes_reclaimed"], 1024)

    def test_a_clean_store_reports_no_sweep(self) -> None:
        with temporary_workdir() as workdir, temporary_tempdir(workdir):
            trace_path = workdir / "traces.jsonl"
            record_traces(trace_path, 2)

            code, out, err = run_cli("prune", "--path", str(trace_path), "--keep-last", "1", "--yes", "--json")

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            payload = json.loads(out)
            self.assertEqual(payload["swept_leftovers"], 0)
            self.assertEqual(payload["swept_leftover_bytes"], 0)


class SweepLeavesWhatIsNotItsTests(DeadProcessTest):
    """Only prune's own abandoned work is swept, and only when it is abandoned."""

    def test_a_leftover_of_a_live_process_stays(self) -> None:
        with temporary_workdir() as workdir, temporary_tempdir(workdir) as tempdir:
            trace_path = workdir / "traces.jsonl"
            record_traces(trace_path, 2)
            staged = abandoned_staging_copy(trace_path, os.getpid())
            index = abandoned_index(tempdir, os.getpid())

            code, _out, err = run_cli("prune", "--path", str(trace_path), "--keep-last", "1", "--yes")

            self.assertEqual(code, 0)
            self.assertTrue(staged.exists())
            self.assertTrue(index.exists())
            self.assertNotIn("swept", err)

    def test_a_live_pid_that_must_have_been_reused_is_swept_by_age(self) -> None:
        with temporary_workdir() as workdir, temporary_tempdir(workdir) as tempdir:
            trace_path = workdir / "traces.jsonl"
            record_traces(trace_path, 2)
            staged = abandoned_staging_copy(trace_path, os.getpid(), size=16)
            index = abandoned_index(tempdir, os.getpid(), size=16)
            a_day_and_an_hour_ago = time.time() - (25 * 60 * 60)
            os.utime(staged, (a_day_and_an_hour_ago, a_day_and_an_hour_ago))
            os.utime(index, (a_day_and_an_hour_ago, a_day_and_an_hour_ago))

            code, _out, err = run_cli("prune", "--path", str(trace_path), "--keep-last", "1", "--yes")

            self.assertEqual(code, 0)
            self.assertFalse(staged.exists())
            self.assertFalse(index.exists())
            self.assertIn("swept 2 leftover file(s) of 32 bytes", err)

    def test_the_lock_the_sidecar_and_unrelated_siblings_are_left_alone(self) -> None:
        with temporary_workdir() as workdir, temporary_tempdir(workdir) as tempdir:
            trace_path = workdir / "traces.jsonl"
            record_traces(trace_path, 2)
            # Every one of these is a sibling a prune must never remove: the
            # advisory lock it is holding, the upload sidecar's own staged write,
            # the store's rotated file, and a file that is simply not prune's.
            sidecar_staged = abandoned_staging_copy(trace_path.with_name(trace_path.name + ".sent"), self.dead_pid)
            rotated = trace_path.with_name(trace_path.name + ".1")
            rotated.write_bytes(trace_path.read_bytes())
            unrelated = [
                workdir / ".traces.jsonl.tmp",
                workdir / f".traces.jsonl.{self.dead_pid}.notahexuuid.tmp",
                workdir / "traces.jsonl.backup",
                tempdir / f"{_PRUNE_INDEX_PREFIX}notapid-ab12cd34",
                # A number too long for the name prune writes, so the sweep
                # never asks whether that process is running at all.
                workdir / f".traces.jsonl.{10**12}.{'0' * 32}.tmp",
            ]
            for path in unrelated:
                path.write_bytes(b"keep me")
            # An index directory a release before this one wrote carries no pid,
            # so nothing about it says its writer is gone. It is left to the
            # system's own sweep of the temporary directory rather than removed
            # on an age guess. The staging sibling's name is unchanged, so the
            # copy beside the store -- the one nothing else ever reclaims -- is
            # swept whichever release abandoned it.
            legacy_index = tempdir / f"{_PRUNE_INDEX_PREFIX}q1w2e3r4"
            legacy_index.mkdir()
            (legacy_index / "traces.sqlite3").write_bytes(b"keep me")
            unrelated.append(legacy_index)

            code, _out, err = run_cli("prune", "--path", str(trace_path), "--keep-last", "1", "--yes")

            self.assertEqual(code, 0)
            self.assertNotIn("swept", err)
            self.assertTrue(sidecar_staged.exists())
            self.assertTrue(rotated.exists())
            self.assertTrue(trace_path.with_name(f".{trace_path.name}.lock").exists())
            for path in unrelated:
                self.assertTrue(path.exists(), path.name)

    def test_a_leftover_that_cannot_be_removed_does_not_fail_the_prune(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            record_traces(trace_path, 3)
            staged = abandoned_staging_copy(trace_path, self.dead_pid)
            real_unlink = Path.unlink

            def refuse_the_leftover(path: Path, missing_ok: bool = False) -> None:
                if path == staged:
                    raise PermissionError("refused")
                real_unlink(path, missing_ok=missing_ok)

            with patch.object(Path, "unlink", new=refuse_the_leftover):
                code, out, err = run_cli("prune", "--path", str(trace_path), "--keep-last", "1", "--yes")

            # Bookkeeping cannot break the prune it rode along with: the store is
            # pruned, and what could not be removed is not claimed as reclaimed.
            self.assertEqual(code, 0)
            self.assertIn("removed=2", out)
            self.assertNotIn("swept", err)
            self.assertTrue(staged.exists())
            self.assertEqual(len(bir.load_traces(str(trace_path))), 1)


class SweepSurvivesWhatItCannotReadTests(DeadProcessTest):
    """A leftover the sweep cannot read is left where it is, and reported as left."""

    def test_a_leftover_that_cannot_be_measured_is_not_swept(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            record_traces(trace_path, 3)
            staged = abandoned_staging_copy(trace_path, self.dead_pid)

            with refusing_stat_for(staged):
                code, out, err = run_cli("prune", "--path", str(trace_path), "--keep-last", "1", "--yes")

            self.assertEqual(code, 0)
            self.assertIn("removed=2", out)
            self.assertNotIn("swept", err)
            self.assertTrue(staged.exists())

    def test_a_live_leftover_whose_age_cannot_be_read_stays(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            record_traces(trace_path, 3)
            # A live pid sends the sweep to the age it cannot read, and an
            # unreadable age is not evidence that the writer is gone.
            staged = abandoned_staging_copy(trace_path, os.getpid())

            with refusing_stat_for(staged):
                code, _out, err = run_cli("prune", "--path", str(trace_path), "--keep-last", "1", "--yes")

            self.assertEqual(code, 0)
            self.assertNotIn("swept", err)
            self.assertTrue(staged.exists())

    def test_a_directory_that_cannot_be_listed_contributes_nothing(self) -> None:
        missing = Path("no-such-directory") / "either"

        self.assertEqual(list(_entries_matching(missing, re.compile(".*"))), [])


class ProcessLivenessTests(DeadProcessTest):
    """How the sweep decides the process named in a leftover is gone."""

    def test_this_process_is_running_and_a_reaped_child_is_not(self) -> None:
        self.assertTrue(_process_is_running(os.getpid()))
        self.assertFalse(_process_is_running(self.dead_pid))
        self.assertFalse(_process_is_running(0))

    def test_another_running_process_is_running(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertTrue(_process_is_running(child.pid))
        finally:
            child.kill()
            child.wait()

    @unittest.skipIf(os.name == "nt", "os.kill is not the probe on Windows")
    def test_a_process_this_user_may_not_signal_is_running(self) -> None:
        with patch("os.kill", side_effect=PermissionError("not yours")):
            self.assertTrue(_process_is_running(self.dead_pid))

    def test_a_number_too_large_to_be_a_pid_is_not_judged_gone(self) -> None:
        # Past what a pid can hold on either platform: POSIX refuses to convert
        # it and Windows refuses to pass it. A name neither probe can answer for
        # is not evidence of a finished process, so the file stays.
        self.assertTrue(_process_is_running(2**64))

    @unittest.skipIf(os.name == "nt", "the real probe answers here")
    def test_the_windows_probe_keeps_what_it_cannot_ask_about(self) -> None:
        # Off Windows there is no ``ctypes.WinDLL`` to ask, which stands in for
        # every way the probe can fail to get an answer. All of them mean the
        # same thing: not evidence that a file is abandoned.
        self.assertTrue(_windows_process_is_running(self.dead_pid))


class FakeKernel32Function:
    """A stand-in for one ``kernel32`` entry point that records its signature."""

    def __init__(self, behaviour: Callable[..., int]) -> None:
        self.behaviour = behaviour
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *arguments: Any) -> int:
        return self.behaviour(*arguments)


class FakeKernel32:
    """The three calls the probe makes, answering as Windows would."""

    def __init__(
        self,
        *,
        handle: int,
        exit_code: int = 0,
        exit_code_readable: bool = True,
        open_raises: BaseException | None = None,
    ) -> None:
        self.opened: list[tuple[int, int, int]] = []
        self.closed: list[int] = []

        def open_process(access: int, inherit: int, pid: int) -> int:
            if open_raises is not None:
                raise open_raises
            self.opened.append((access, inherit, pid))
            return handle

        def get_exit_code(process: int, destination: Any) -> int:
            if not exit_code_readable:
                return 0
            destination._obj.value = exit_code
            return 1

        def close_handle(process: int) -> int:
            self.closed.append(process)
            return 1

        self.OpenProcess = FakeKernel32Function(open_process)
        self.GetExitCodeProcess = FakeKernel32Function(get_exit_code)
        self.CloseHandle = FakeKernel32Function(close_handle)


class WindowsLivenessProbeTests(unittest.TestCase):
    """The Windows probe's whole decision table, driven against a fake library.

    Only two of these rows are reachable on the Windows CI leg -- a process that
    is running and a reaped one -- and none at all anywhere else, so what the
    probe answers for a process it may not open, a call that fails, and a number
    too large to pass as a pid is pinned here instead of left to a platform no
    test can reach.
    """

    def probe(self, *, pid: int = 4242, last_error: int = 0, **kernel32_arguments: Any) -> tuple[bool, FakeKernel32]:
        kernel32 = FakeKernel32(**kernel32_arguments)
        with (
            patch.object(ctypes, "WinDLL", create=True, return_value=kernel32),
            patch.object(ctypes, "get_last_error", create=True, return_value=last_error),
        ):
            return _windows_process_is_running(pid), kernel32

    def test_the_probe_answers_every_outcome_windows_can_return(self) -> None:
        error_access_denied = 5  # It exists and belongs to somebody else.
        cases: tuple[tuple[str, bool, dict[str, Any]], ...] = (
            ("a running process", True, {"handle": 1234, "exit_code": _WINDOWS_STILL_ACTIVE}),
            # A reaped child stays openable while anyone holds a handle to it,
            # which is why the exit code rather than the handle decides.
            ("finished, a handle still held", False, {"handle": 1234, "exit_code": 0}),
            ("no such process", False, {"handle": 0, "last_error": _WINDOWS_ERROR_INVALID_PARAMETER}),
            ("exists, this user may not open it", True, {"handle": 0, "last_error": error_access_denied}),
            ("the exit code could not be read", True, {"handle": 1234, "exit_code_readable": False}),
            ("the call itself raised", True, {"handle": 0, "open_raises": ctypes.ArgumentError("refused")}),
            ("kernel32 could not be loaded", True, {"handle": 0, "open_raises": OSError("no library")}),
        )
        for label, expected, arguments in cases:
            with self.subTest(label):
                running, kernel32 = self.probe(**arguments)

                self.assertIs(running, expected)
                if arguments["handle"]:
                    # Query-only access, the pid asked about, and no leaked handle.
                    self.assertEqual(kernel32.opened, [(_WINDOWS_QUERY_LIMITED_INFORMATION, 0, 4242)])
                    self.assertEqual(kernel32.closed, [1234])
                else:
                    self.assertEqual(kernel32.closed, [])

    def test_a_number_outside_the_pid_space_is_never_asked_about(self) -> None:
        # ctypes masks an out-of-range value into the C type rather than
        # refusing it, so asking would be asking about a different process:
        # 2**64 arrives as pid 0, and 5,000,000,000 as pid 705,032,704, which
        # can name something that really is running. Both are answered without
        # the library being called at all.
        for pid in (2**64, _WINDOWS_MAX_PID + 1, 5_000_000_000, 0, -1):
            with self.subTest(pid=pid):
                running, kernel32 = self.probe(pid=pid, handle=1234, exit_code=_WINDOWS_STILL_ACTIVE)

                self.assertIs(running, True)
                self.assertEqual(kernel32.opened, [])

    def test_the_largest_pid_windows_can_hold_is_still_asked_about(self) -> None:
        running, kernel32 = self.probe(pid=_WINDOWS_MAX_PID, handle=0, last_error=_WINDOWS_ERROR_INVALID_PARAMETER)

        self.assertIs(running, False)
        self.assertEqual(kernel32.opened, [(_WINDOWS_QUERY_LIMITED_INFORMATION, 0, _WINDOWS_MAX_PID)])


if __name__ == "__main__":
    unittest.main()
