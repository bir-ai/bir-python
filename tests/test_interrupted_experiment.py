"""An experiment stopped without unwinding must keep the examples it finished.

``run_experiment`` and ``run_experiment_async`` hold one handle open for the
whole run so rows stay in dataset order. Under default buffering those rows sat
in memory until the handle closed, which meant a process stopped without
unwinding lost all of them -- and ``SIGTERM``, the way an orchestrator stops a
process (a pod eviction, ``docker stop``, a cancelled CI job), is exactly that:
Python's default handler exits without running any cleanup. A run over a model
API can be an hour of paid calls, so the rows are flushed per finished example
instead.

Each test here starts a real experiment in a child process whose last example
blocks forever, waits until the rows for the earlier examples are readable on
disk *while the child is still running*, then stops the child the way an
orchestrator would and re-reads what survived. The wait is the part that pins
the flush: before it, no row reached the disk until the run ended.

``Popen.terminate`` sends ``SIGTERM`` on POSIX and calls ``TerminateProcess`` on
Windows, which is stricter still -- neither gives the child a chance to flush.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from bir.evals import load_experiment

ROOT = Path(__file__).resolve().parents[1]

# Runs one experiment whose examples before ``blocking_index`` return at once and
# whose example at that index never returns, so the run is reliably still in
# progress with exactly ``blocking_index`` examples finished.
EXPERIMENT_SOURCE = """
import sys
import time

from bir.evals import Dataset, DatasetExample, json_valid, run_experiment, run_experiment_async

output_path, runner, blocking_index = sys.argv[1], sys.argv[2], int(sys.argv[3])
dataset = Dataset([DatasetExample(id=f"q{i}", input={"n": i}) for i in range(blocking_index + 1)])

if runner == "async":
    import asyncio

    async def task(n):
        if n >= blocking_index:
            await asyncio.sleep(3600)
        return n

    asyncio.run(
        run_experiment_async(
            "interrupted",
            dataset=dataset,
            task=task,
            evaluators=[json_valid()],
            path=output_path,
            max_concurrency=2,
        )
    )
else:
    def task(n):
        if n >= blocking_index:
            time.sleep(3600)
        return n

    run_experiment(
        "interrupted",
        dataset=dataset,
        task=task,
        evaluators=[json_valid()],
        path=output_path,
        max_workers=1 if runner == "serial" else 2,
    )
"""

FINISHED_EXAMPLES = 3


class InterruptedExperimentTests(unittest.TestCase):
    def test_serial_runner_keeps_finished_rows(self) -> None:
        self._assert_finished_rows_survive("serial")

    def test_threaded_runner_keeps_finished_rows(self) -> None:
        self._assert_finished_rows_survive("threaded")

    def test_async_runner_keeps_finished_rows(self) -> None:
        self._assert_finished_rows_survive("async")

    def _assert_finished_rows_survive(self, runner: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "interrupted.jsonl"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "src")
            process = subprocess.Popen(
                [sys.executable, "-c", EXPERIMENT_SOURCE, str(output_path), runner, str(FINISHED_EXAMPLES)],
                cwd=directory,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                self._wait_for_rows(process, output_path, FINISHED_EXAMPLES)
            finally:
                process.terminate()
            stdout, stderr = process.communicate(timeout=30)

            self.assertNotEqual(process.returncode, 0, f"run finished instead of being stopped: {stdout}")
            loaded = load_experiment(output_path)
            self.assertEqual(
                [result.example_id for result in loaded.results],
                [f"q{index}" for index in range(FINISHED_EXAMPLES)],
                f"stopped run lost finished rows\nstdout: {stdout}\nstderr: {stderr}",
            )
            self.assertTrue(all(result.status == "success" for result in loaded.results))
            # The run never reached its end, so nothing may claim that it did.
            self.assertFalse(output_path.with_suffix(".summary.json").exists())

    def _wait_for_rows(self, process: subprocess.Popen[str], output_path: Path, expected_rows: int) -> None:
        """Block until ``expected_rows`` complete lines are readable in a live run."""

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(f"experiment exited before blocking\nstdout: {stdout}\nstderr: {stderr}")
            if _complete_row_count(output_path) >= expected_rows:
                return
            time.sleep(0.01)
        self.fail(f"only {_complete_row_count(output_path)} of {expected_rows} rows reached the disk during the run")


def _complete_row_count(path: Path) -> int:
    """Count rows a reader could parse, ignoring any line still being written."""

    try:
        return path.read_text(encoding="utf-8").count("\n")
    except FileNotFoundError:
        return 0


if __name__ == "__main__":
    unittest.main()
