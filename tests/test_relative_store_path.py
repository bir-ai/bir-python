"""Where a relative store path points once the working directory moves.

``trace_path`` may be relative, and the default ``.bir/traces.jsonl`` is: it
means "a ``.bir`` directory where this program runs". The operating system used
to resolve that on *every* append, against whatever the working directory was at
that moment, so a process that changed directory mid-run split its store in two
without saying anything -- half the events where it started, half where it moved
to, and each ``bir traces`` showing half a picture. Daemonizing
(``os.chdir("/")``), a test fixture, and any job that runs work in a scratch
directory all reach it.

The rule now is that the path is anchored once, the first time the configuration
needs it, and every writer and reader in the process agrees from then on.

The other half is what these tests exist to protect: a relative path still means
the directory the program *records from*, not the directory it was imported in.
Anchoring at construction was implemented first and rejected here -- it broke
1,031 of this suite's own cases, every one of them a variation on "chdir into a
scratch directory, then record", which is what a program's tests do and what a
CLI that moves to a project root does. So the cases below pin both directions:
the store stops following a chdir, and it still lands where recording started.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import bir
from bir._config import _Config
from bir._sdk import _reset_config_for_tests


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


def recorded_names(store: Path) -> list[str]:
    if not store.exists():
        return []
    return [json.loads(line)["name"] for line in store.read_text(encoding="utf-8").splitlines()]


class RelativeStorePathTests(unittest.TestCase):
    """One process records into one store, whatever it does to its directory."""

    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_the_default_store_does_not_follow_a_chdir(self) -> None:
        with temporary_workdir() as workdir:
            (workdir / "app").mkdir()
            (workdir / "elsewhere").mkdir()
            os.chdir(workdir / "app")
            bir.configure(enabled=True)
            with bir.trace("before-chdir"):
                pass

            os.chdir(workdir / "elsewhere")
            with bir.trace("after-chdir"):
                pass

            self.assertEqual(
                recorded_names(workdir / "app" / ".bir" / "traces.jsonl"),
                ["before-chdir", "after-chdir"],
            )
            self.assertEqual(recorded_names(workdir / "elsewhere" / ".bir" / "traces.jsonl"), [])

    def test_a_configured_relative_path_does_not_follow_a_chdir_either(self) -> None:
        # Not only the default: any relative path a caller configures is anchored
        # the same way, because the operating system would resolve it the same way.
        with temporary_workdir() as workdir:
            (workdir / "here").mkdir()
            (workdir / "there").mkdir()
            os.chdir(workdir / "here")
            bir.configure(trace_path="logs/traces.jsonl", enabled=True)
            with bir.trace("first"):
                pass

            os.chdir(workdir / "there")
            with bir.trace("second"):
                pass

            self.assertEqual(recorded_names(workdir / "here" / "logs" / "traces.jsonl"), ["first", "second"])
            self.assertFalse((workdir / "there" / "logs").exists())

    def test_the_reader_follows_the_writer(self) -> None:
        # `load_events()` with no path has to read what this process wrote, which
        # means the same anchor rather than a fresh resolution against a working
        # directory that has since moved.
        with temporary_workdir() as workdir:
            (workdir / "start").mkdir()
            (workdir / "moved").mkdir()
            os.chdir(workdir / "start")
            bir.configure(enabled=True)
            with bir.trace("recorded-here"):
                pass

            os.chdir(workdir / "moved")
            self.assertEqual([event.name for event in bir.load_events()], ["recorded-here"])
            self.assertEqual([trace.root.name for trace in bir.load_traces()], ["recorded-here"])

    def test_a_relative_path_still_means_where_recording_starts(self) -> None:
        # The half that the rejected implementation broke. The configuration here
        # is built before the chdir -- as it is at import, and as it is for every
        # test fixture that moves into a scratch directory -- and the store still
        # has to land where the program actually records from.
        with temporary_workdir() as workdir:
            config = _Config()
            (workdir / "scratch").mkdir()
            os.chdir(workdir / "scratch")

            # Compared against `Path.cwd()` rather than against `workdir`: the
            # relative half is joined to what the operating system reports as the
            # current directory, which is already canonical (on macOS a temporary
            # directory under /var is reached through a symlink).
            self.assertEqual(config.anchored_trace_path(), Path.cwd() / ".bir" / "traces.jsonl")
            self.assertEqual(config.anchored_trace_path().parent.parent.name, "scratch")

    def test_reconfiguring_re_anchors(self) -> None:
        # `configure()` builds a new configuration, so a program that deliberately
        # moves its store can still do so; what it cannot do is move by accident.
        with temporary_workdir() as workdir:
            (workdir / "one").mkdir()
            (workdir / "two").mkdir()
            os.chdir(workdir / "one")
            bir.configure(enabled=True)
            with bir.trace("in-one"):
                pass

            os.chdir(workdir / "two")
            bir.configure(enabled=True)
            with bir.trace("in-two"):
                pass

            self.assertEqual(recorded_names(workdir / "one" / ".bir" / "traces.jsonl"), ["in-one"])
            self.assertEqual(recorded_names(workdir / "two" / ".bir" / "traces.jsonl"), ["in-two"])

    def test_an_absolute_path_is_kept_exactly_as_configured(self) -> None:
        # No `resolve()`: a symlinked temporary directory must not turn into its
        # canonical form in messages, in `bir config`, or on disk.
        with temporary_workdir() as workdir:
            store = workdir / "explicit.jsonl"
            config = _Config(trace_path=store)

            self.assertEqual(config.anchored_trace_path(), store)

    def test_the_anchor_is_computed_once(self) -> None:
        with temporary_workdir():
            config = _Config()
            first = config.anchored_trace_path()
            os.chdir(Path.cwd().parent)

            self.assertIs(config.anchored_trace_path(), first)


if __name__ == "__main__":
    unittest.main()
