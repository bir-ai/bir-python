"""Reading an experiment store an interrupted write damaged.

An experiment is two files: the result JSONL and a ``*.summary.json`` beside it.
The listing reads every summary in the directory, so one it cannot parse used to
raise for the whole directory — and because ``experiment-show`` and
``experiment-report`` resolve their target through the listing, one damaged file
made every intact experiment beside it unreachable through the CLI.

This is the same failure the trace store had, and it gets the same answer: the
strict default a program building on the loaders relies on, and an opt-in CLI
read that shows what survived and says what it could not read. The writer is
fixed too — a summary is staged and renamed, so a killed or failed write leaves
the previous one readable rather than destroying it in place.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

import bir
from bir import cli
from bir._eval_models import ExperimentSummary
from bir._eval_persistence import _write_experiment_summary
from bir._sdk import _reset_config_for_tests
from bir.evals import Dataset, DatasetExample, exact_match, list_experiments, load_experiment_summary, run_experiment

EXPERIMENT_DIR = Path("exp")


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


def record_experiments(*names: str) -> dict[str, str]:
    """Run one small experiment per name, returning each name's experiment id."""

    bir.configure(trace_path="store/traces.jsonl")
    dataset = Dataset([DatasetExample(id=f"example-{index}", input="q", expected="ok") for index in range(3)])
    ids: dict[str, str] = {}
    for name in names:
        result = run_experiment(
            name,
            dataset=dataset,
            task=lambda question: "ok",
            evaluators=[exact_match()],
            path=str(EXPERIMENT_DIR / f"{name}.jsonl"),
        )
        ids[name] = result.id
    return ids


def truncate_summary(name: str) -> None:
    """Leave the shape an interrupted write leaves: a summary cut in half."""

    path = EXPERIMENT_DIR / f"{name}.summary.json"
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) // 2])


def summary_fixture() -> ExperimentSummary:
    return ExperimentSummary(
        schema_version="1.0",
        experiment_id="id-1",
        name="fixture",
        start_time="2026-01-01T00:00:00+00:00",
        end_time="2026-01-01T00:00:01+00:00",
        status="success",
        example_count=1,
        error_count=0,
        aggregate_scores={"exact_match": 1.0},
        result_path="fixture.jsonl",
    )


class StrictByDefaultTests(unittest.TestCase):
    """The loaders still refuse what they cannot read completely."""

    def setUp(self) -> None:
        _reset_config_for_tests()

    def test_list_experiments_refuses_a_damaged_summary(self) -> None:
        with temporary_workdir():
            record_experiments("alpha", "beta")
            truncate_summary("beta")

            # A program building on this gets every experiment or an error, never
            # a silently partial list — the same contract load_traces() has.
            with self.assertRaisesRegex(ValueError, "Invalid JSON in experiment summary"):
                list_experiments(str(EXPERIMENT_DIR))

    def test_load_experiment_summary_refuses_a_damaged_summary(self) -> None:
        with temporary_workdir():
            record_experiments("alpha")
            truncate_summary("alpha")

            with self.assertRaisesRegex(ValueError, "Invalid JSON in experiment summary"):
                load_experiment_summary(EXPERIMENT_DIR / "alpha.summary.json")

    def test_the_commands_are_strict_without_the_flag(self) -> None:
        with temporary_workdir():
            record_experiments("alpha", "beta")
            truncate_summary("beta")

            code, _, err = run_cli("experiments", "--dir", str(EXPERIMENT_DIR))

            self.assertEqual(code, 1)
            self.assertIn("Invalid JSON in experiment summary", err)


class SkipInvalidTests(unittest.TestCase):
    """``--skip-invalid`` reads past a damaged summary and says what it skipped."""

    def setUp(self) -> None:
        _reset_config_for_tests()

    def test_experiments_lists_what_survived(self) -> None:
        with temporary_workdir():
            record_experiments("alpha", "beta", "gamma")
            truncate_summary("beta")

            code, out, err = run_cli("experiments", "--dir", str(EXPERIMENT_DIR), "--skip-invalid")

            self.assertEqual(code, 0)
            self.assertIn("skipped 1 unreadable experiment summary", err)
            self.assertIn("beta.summary.json", err)
            # The two intact experiments are reachable again; before, an
            # unrelated third file hid them both.
            self.assertIn("alpha", out)
            self.assertIn("gamma", out)

    def test_json_output_stays_parseable(self) -> None:
        with temporary_workdir():
            record_experiments("alpha", "beta")
            truncate_summary("beta")

            code, out, err = run_cli("experiments", "--dir", str(EXPERIMENT_DIR), "--skip-invalid", "--json")

            self.assertEqual(code, 0)
            self.assertIn("skipped", err)
            # The report is on stderr precisely so this parse works.
            self.assertEqual([entry["name"] for entry in json.loads(out)], ["alpha"])

    def test_experiment_show_reaches_an_intact_experiment(self) -> None:
        with temporary_workdir():
            ids = record_experiments("alpha", "beta")
            truncate_summary("beta")

            code, out, err = run_cli("experiment-show", ids["alpha"], "--dir", str(EXPERIMENT_DIR), "--skip-invalid")

            self.assertEqual(code, 0)
            self.assertIn("skipped 1 unreadable experiment summary", err)
            self.assertIn("alpha", out)

    def test_experiment_report_reaches_an_intact_experiment(self) -> None:
        with temporary_workdir():
            ids = record_experiments("alpha", "beta")
            truncate_summary("beta")

            code, out, err = run_cli(
                "experiment-report",
                ids["alpha"],
                "--dir",
                str(EXPERIMENT_DIR),
                "--skip-invalid",
                "--format",
                "markdown",
            )

            self.assertEqual(code, 0)
            self.assertIn("skipped 1 unreadable experiment summary", err)
            self.assertIn("Experiment Report: alpha", out)

    def test_a_summary_that_parses_but_does_not_validate_is_skipped_too(self) -> None:
        with temporary_workdir():
            record_experiments("alpha", "beta")
            # Valid JSON, invalid summary: the file is readable but says nothing
            # a listing can use, which must be skipped like an unparseable one.
            path = EXPERIMENT_DIR / "beta.summary.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            del payload["experiment_id"]
            path.write_text(json.dumps(payload), encoding="utf-8")

            code, out, err = run_cli("experiments", "--dir", str(EXPERIMENT_DIR), "--skip-invalid")

            self.assertEqual(code, 0)
            self.assertIn("skipped 1 unreadable experiment summary", err)
            self.assertIn("alpha", out)

    def test_an_intact_directory_reports_nothing(self) -> None:
        with temporary_workdir():
            record_experiments("alpha", "beta")

            code, out, err = run_cli("experiments", "--dir", str(EXPERIMENT_DIR), "--skip-invalid")

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertIn("alpha", out)
            self.assertIn("beta", out)

    def test_a_damaged_result_file_still_only_costs_its_own_experiment(self) -> None:
        with temporary_workdir():
            ids = record_experiments("alpha", "beta")
            result_path = EXPERIMENT_DIR / "beta.jsonl"
            raw = result_path.read_bytes()
            result_path.write_bytes(raw[: len(raw) - 40])

            listed_code, listed_out, _ = run_cli("experiments", "--dir", str(EXPERIMENT_DIR))
            shown_code, _, shown_err = run_cli("experiment-show", ids["beta"], "--dir", str(EXPERIMENT_DIR))

            # A result file is read only by the experiment that owns it, so this
            # was already correctly scoped; the summary path now matches it.
            self.assertEqual(listed_code, 0)
            self.assertIn("alpha", listed_out)
            self.assertEqual(shown_code, 1)
            self.assertIn("Invalid JSON in experiment", shown_err)


class SummaryWriteTests(unittest.TestCase):
    """A summary is staged and renamed, so a failed write keeps the old one."""

    def setUp(self) -> None:
        _reset_config_for_tests()

    # The summary is staged through a handle rather than ``Path.write_text`` so
    # its mode can be set as it is created; these fail the staged write itself,
    # which is the same interruption from the same place.
    @staticmethod
    @contextmanager
    def failing_write(*, after_half: bool) -> Iterator[None]:
        real_open = builtins.open

        class FailingHandle:
            """A real handle whose ``write`` fails, wrapped because
            ``io.TextIOWrapper`` cannot be patched."""

            def __init__(self, handle: Any) -> None:
                self._handle = handle

            def __enter__(self) -> FailingHandle:
                return self

            def __exit__(self, *exc_info: object) -> bool:
                self._handle.close()
                return False

            def write(self, data: str) -> int:
                if after_half:
                    self._handle.write(data[: len(data) // 2])
                raise OSError("disk full")

        def failing_open(*args: Any, **kwargs: Any) -> Any:
            return FailingHandle(real_open(*args, **kwargs))

        with patch.object(builtins, "open", failing_open):
            yield

    def test_a_write_that_fails_part_way_leaves_the_previous_summary(self) -> None:
        with temporary_workdir() as workdir:
            target = workdir / "x.summary.json"
            _write_experiment_summary(target, summary_fixture())
            before = target.read_bytes()

            with self.failing_write(after_half=True):
                with self.assertRaises(OSError):
                    _write_experiment_summary(target, summary_fixture())

            # Truncating in place would have destroyed a valid summary here, and
            # taken the whole directory's listing with it.
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(load_experiment_summary(target).name, "fixture")

    def test_a_failed_write_leaves_no_temporary_file_behind(self) -> None:
        with temporary_workdir() as workdir:
            target = workdir / "x.summary.json"

            with self.failing_write(after_half=False):
                with self.assertRaises(OSError):
                    _write_experiment_summary(target, summary_fixture())

            self.assertEqual(list(workdir.iterdir()), [])

    def test_a_rewritten_summary_replaces_the_previous_one(self) -> None:
        with temporary_workdir() as workdir:
            target = workdir / "x.summary.json"
            _write_experiment_summary(target, summary_fixture())

            updated = ExperimentSummary(
                schema_version="1.0",
                experiment_id="id-2",
                name="rewritten",
                start_time="2026-01-02T00:00:00+00:00",
                end_time="2026-01-02T00:00:01+00:00",
                status="error",
                example_count=2,
                error_count=1,
                aggregate_scores={},
                result_path="rewritten.jsonl",
            )
            _write_experiment_summary(target, updated)

            self.assertEqual(load_experiment_summary(target).name, "rewritten")
            self.assertEqual([path.name for path in workdir.iterdir()], ["x.summary.json"])


if __name__ == "__main__":
    unittest.main()
