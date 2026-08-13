"""What the CLI does when whoever was reading its output stops.

``bir traces | head -5`` is an ordinary thing to type, and it used to end like a
failure: ``bir: [Errno 32] Broken pipe`` on stderr, then the interpreter's own
``Exception ignored while flushing sys.stdout``, and exit **120** -- a status the
CLI never chose, produced by the shutdown flush failing where nothing can catch
it. Under ``set -o pipefail`` that reads as a broken command.

The rule now is that a departed reader is not a failure: nothing on stderr, and
exit 141 (128 + ``SIGPIPE``), the status any other tool reports in the same
situation and one that still distinguishes a truncated read from a complete one.
Every *other* write failure is untouched and still exits 1 with its reason, which
is the half of this that is easy to break: the fix must not silence a redirect to
a disk that is full.

These need a real process and a real pipe. An in-process test writes into a
``StringIO``, which has no descriptor to close, no block buffering, and no
interpreter shutdown -- exactly the three things that produced the defect.

POSIX only: the codes here are the ``128 + signal`` convention, and Windows has
neither ``SIGPIPE`` nor the same teardown for a pipe whose reader is gone. CI
covers Windows for everything else the CLI does.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import bir

BROKEN_PIPE_EXIT_CODE = 141
EVENT = {
    "schema_version": "1.0",
    "id": "11111111-1111-4111-8111-111111111111",
    "trace_id": "11111111-1111-4111-8111-111111111111",
    "parent_id": None,
    "name": "recorded",
    "type": "trace",
    "start_time": "2026-08-14T00:00:00+00:00",
    "end_time": "2026-08-14T00:00:01+00:00",
    "status": "success",
    "error": None,
    "metadata": {},
    "input": None,
    "output": None,
}


def cli_environment() -> dict[str, str]:
    env = dict(os.environ)
    src = str(Path(bir.__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [src, env.get("PYTHONPATH", "")]))
    return env


def write_store(path: Path, traces: int) -> None:
    """Write a store big enough to outrun a pipe buffer when it needs to be."""

    with path.open("w", encoding="utf-8") as store:
        for index in range(traces):
            event = dict(EVENT)
            event["id"] = event["trace_id"] = f"{index:032x}"
            event["name"] = f"trace-{index}"
            store.write(json.dumps(event) + "\n")


@unittest.skipIf(sys.platform == "win32", "exit 141 is the POSIX 128 + SIGPIPE convention")
class ClosedPipeTests(unittest.TestCase):
    """A reader that leaves is a normal ending; anything else is still a failure."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.workdir = Path(self._directory.name)
        self.store = self.workdir / "traces.jsonl"
        self.env = cli_environment()

    def tearDown(self) -> None:
        self._directory.cleanup()

    def run_cli(self, *argv: str, stdout: int) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, "-m", "bir", *argv],
            stdout=stdout,
            stderr=subprocess.PIPE,
            env=self.env,
            timeout=60,
        )

    def test_a_reader_that_stops_after_two_lines_is_not_a_failure(self) -> None:
        # 5,000 traces so the rendered table is far larger than a pipe buffer and
        # the write actually fails mid-render rather than fitting in the buffer.
        write_store(self.store, 5000)
        for argv in (
            ("traces", "--path", str(self.store)),
            ("traces", "--path", str(self.store), "--json"),
        ):
            with self.subTest(command=" ".join(argv)):
                process = subprocess.Popen(
                    [sys.executable, "-m", "bir", *argv],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=self.env,
                )
                assert process.stdout is not None
                first_lines = [process.stdout.readline() for _ in range(2)]
                process.stdout.close()
                _, stderr = process.communicate(timeout=60)

                self.assertTrue(first_lines[0], "the command printed nothing before the pipe closed")
                self.assertEqual(process.returncode, BROKEN_PIPE_EXIT_CODE)
                self.assertEqual(stderr.decode(), "")

    def test_a_reader_that_is_already_gone_is_not_a_failure(self) -> None:
        # The other half of the same story, and the one the explicit flush in
        # `main` exists for: a command whose whole output fits in the buffer
        # fails at the *flush*, which used to happen after `main` returned.
        write_store(self.store, 3)
        for argv in (
            ("stats", "--path", str(self.store)),
            ("traces", "--path", str(self.store)),
            ("config",),
        ):
            with self.subTest(command=" ".join(argv)):
                read_fd, write_fd = os.pipe()
                os.close(read_fd)
                try:
                    result = self.run_cli(*argv, stdout=write_fd)
                finally:
                    os.close(write_fd)

                self.assertEqual(result.returncode, BROKEN_PIPE_EXIT_CODE)
                self.assertEqual(result.stderr.decode(), "")

    def test_a_write_failure_that_is_not_a_closed_pipe_still_fails(self) -> None:
        # The guard has to be narrow. A descriptor that cannot be written to is
        # not a reader that lost interest, and it must still be reported and
        # still exit non-zero -- with the CLI's own 1, not the 120 the shutdown
        # flush used to substitute.
        write_store(self.store, 3)
        unwritable = self.workdir / "unwritable.out"
        unwritable.write_text("", encoding="utf-8")
        read_only_fd = os.open(unwritable, os.O_RDONLY)
        try:
            result = self.run_cli("stats", "--path", str(self.store), stdout=read_only_fd)
        finally:
            os.close(read_only_fd)

        self.assertEqual(result.returncode, 1)
        self.assertIn("bir:", result.stderr.decode())

    def test_output_that_can_be_written_still_is(self) -> None:
        # `main` now flushes and, when a write has failed, points stdout at
        # /dev/null. Neither may cost a working command its output.
        write_store(self.store, 200)
        target = self.workdir / "traces.out"
        with target.open("wb") as out_file:
            result = self.run_cli("traces", "--path", str(self.store), stdout=out_file.fileno())

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr.decode(), "")
        # A header plus one row per trace.
        self.assertEqual(len(target.read_text(encoding="utf-8").splitlines()), 201)

    def test_a_follow_stops_when_its_reader_leaves(self) -> None:
        # `bir tail` is the streaming path: it exits by signal rather than by
        # finishing, so a reader that leaves is the only thing that ends it
        # besides Ctrl-C. It has to notice rather than follow a file nobody is
        # reading for the rest of the day.
        write_store(self.store, 3)
        stderr_path = self.workdir / "tail.err"
        read_fd, write_fd = os.pipe()
        with stderr_path.open("wb") as stderr_file:
            follower = subprocess.Popen(
                [sys.executable, "-m", "bir", "tail", "--path", str(self.store)],
                stdout=write_fd,
                stderr=stderr_file,
                env=self.env,
            )
        os.close(write_fd)
        try:
            # The banner is printed once it is following, so waiting for it is
            # what makes cutting the pipe land on the polling loop.
            self._wait_for_banner(follower, stderr_path)
            os.close(read_fd)  # the reader goes away
            with self.store.open("a", encoding="utf-8") as store:
                for index in range(50):
                    event = dict(EVENT)
                    event["id"] = event["trace_id"] = f"{index:032x}"
                    event["name"] = f"after-{index}"
                    store.write(json.dumps(event) + "\n")

            follower.communicate(timeout=60)
        except BaseException:
            follower.kill()
            follower.communicate()
            raise

        self.assertEqual(follower.returncode, BROKEN_PIPE_EXIT_CODE)
        # Its own banner is the only thing it says; the broken pipe adds nothing.
        self.assertEqual(
            stderr_path.read_text(encoding="utf-8").splitlines(),
            [f"Following {self.store} (press Ctrl-C to stop)"],
        )

    def _wait_for_banner(self, follower: subprocess.Popen[bytes], stderr_path: Path) -> None:
        """Block until the follower says it is following, or fail."""

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if follower.poll() is not None:
                self.fail("the follower exited before it started following")
            if "Following" in stderr_path.read_text(encoding="utf-8"):
                return
            time.sleep(0.05)
        self.fail("the follower never started")


if __name__ == "__main__":
    unittest.main()
