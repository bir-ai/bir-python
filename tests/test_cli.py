"""Tests for the stdlib-only ``bir`` command-line interface.

These exercise ``bir.cli.main`` end to end against a temporary trace file and
experiment directory: human output, ``--json`` output, and error exit codes. The
network is stubbed for ``send`` and ``send-experiment`` so no test touches a real
server. Local traces and experiments are produced through the public SDK API so
the CLI reads exactly the on-disk format the SDK writes.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

import bir
from bir import cli
from bir._sdk import _reset_config_for_tests
from bir.evals import Dataset, DatasetExample, contains, custom_evaluator, exact_match, run_experiment


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


def run_cli(*argv: str) -> tuple[int, str, str]:
    """Run ``cli.main`` with captured stdout/stderr, returning (code, out, err)."""

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class FakeHttpResponse:
    status = 200

    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def write_two_traces(trace_path: Path) -> None:
    """Record two traces (each with a span and a score) into ``trace_path``."""

    bir.configure(trace_path=trace_path)

    @bir.observe()
    def answer(question: str) -> str:
        with bir.span("retrieve_context"):
            pass
        bir.score("helpfulness", 0.9)
        return "ok"

    answer("first")
    answer("second")


def write_active_and_rotated_trace(trace_path: Path) -> None:
    """Record one trace into a ``.1`` rotated sibling and one into the active file.

    Simulates a prior size-based rotation: the older trace lives in
    ``<trace_path>.1`` and the newer one in the active ``<trace_path>``, so a
    default read sees one trace and an ``include_rotated`` read sees both.
    """

    bir.configure(trace_path=trace_path)

    @bir.observe()
    def answer(value: str) -> str:
        return value

    answer("first")
    trace_path.rename(trace_path.with_name(trace_path.name + ".1"))
    answer("second")


def run_faq_experiment(directory: Path) -> str:
    """Run a small deterministic experiment under ``directory`` and return its id."""

    dataset = Dataset(
        [
            DatasetExample(id="q1", input="hi", expected="ok"),
            DatasetExample(id="q2", input="yo", expected="no"),
        ]
    )
    result = run_experiment(
        "faq",
        dataset=dataset,
        task=lambda _question: "ok",
        evaluators=[exact_match(), contains("o")],
        path=directory / "faq.jsonl",
    )
    return result.id


class CliBaseTest(unittest.TestCase):
    def setUp(self) -> None:
        _reset_config_for_tests()

    def tearDown(self) -> None:
        _reset_config_for_tests()


class TracesCommandTests(CliBaseTest):
    def test_lists_traces_newest_first(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)

            code, out, err = run_cli("traces", "--path", str(trace_path))

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            lines = out.splitlines()
            self.assertEqual(lines[0].split(), ["START", "STATUS", "DURATION", "EVENTS", "NAME"])
            # Two data rows, each a 3-event trace named "answer".
            self.assertEqual(len(lines), 3)
            self.assertTrue(all(line.endswith("answer") for line in lines[1:]))
            self.assertIn("3", lines[1])

    def test_json_output_is_valid_and_limited(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)

            code, out, _ = run_cli("traces", "--path", str(trace_path), "--limit", "1", "--json")

            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(len(payload), 1)
            entry = payload[0]
            self.assertEqual(
                set(entry),
                {"id", "name", "status", "start_time", "duration_ms", "event_count"},
            )
            self.assertEqual(entry["name"], "answer")
            self.assertEqual(entry["event_count"], 3)
            self.assertIsInstance(entry["duration_ms"], float)

    def test_missing_trace_file_reports_empty(self) -> None:
        with temporary_workdir() as workdir:
            missing = workdir / "absent.jsonl"

            code, out, err = run_cli("traces", "--path", str(missing))

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertIn("No traces found", out)

    def test_empty_json_is_an_empty_array(self) -> None:
        with temporary_workdir() as workdir:
            code, out, _ = run_cli("traces", "--path", str(workdir / "absent.jsonl"), "--json")

            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out), [])

    def test_corrupt_trace_file_exits_nonzero(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            trace_path.write_text("{not valid json}\n", encoding="utf-8")

            code, out, err = run_cli("traces", "--path", str(trace_path))

            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("bir:", err)

    def test_rejects_non_positive_limit(self) -> None:
        with temporary_workdir() as workdir:
            with self.assertRaises(SystemExit) as raised:
                run_cli("traces", "--path", str(workdir / "traces.jsonl"), "--limit", "0")
            self.assertEqual(raised.exception.code, 2)

    def test_include_rotated_reads_rotated_siblings(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_active_and_rotated_trace(trace_path)

            # The default read sees only the active file's single trace.
            code, out, err = run_cli("traces", "--path", str(trace_path), "--json")
            self.assertEqual(code, 0)
            self.assertEqual(len(json.loads(out)), 1)

            # include_rotated also reads the rotated sibling, surfacing both traces.
            code, out, err = run_cli("traces", "--path", str(trace_path), "--include-rotated", "--json")
            self.assertEqual(code, 0)
            self.assertEqual(len(json.loads(out)), 2)


class ExperimentsCommandTests(CliBaseTest):
    def test_lists_experiments(self) -> None:
        with temporary_workdir() as workdir:
            run_faq_experiment(workdir)

            code, out, err = run_cli("experiments", "--dir", str(workdir))

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            lines = out.splitlines()
            self.assertEqual(lines[0].split(), ["ID", "NAME", "STATUS", "EXAMPLES", "ERRORS", "SCORES"])
            self.assertIn("faq", lines[1])
            self.assertIn("success", lines[1])
            self.assertIn("exact_match=0.50", lines[1])

    def test_json_output(self) -> None:
        with temporary_workdir() as workdir:
            experiment_id = run_faq_experiment(workdir)

            code, out, _ = run_cli("experiments", "--dir", str(workdir), "--json")

            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(len(payload), 1)
            entry = payload[0]
            self.assertEqual(entry["id"], experiment_id)
            self.assertEqual(entry["name"], "faq")
            self.assertEqual(entry["status"], "success")
            self.assertEqual(entry["example_count"], 2)
            self.assertEqual(entry["error_count"], 0)
            self.assertEqual(entry["aggregate_scores"]["exact_match"], 0.5)

    def test_missing_directory_reports_empty(self) -> None:
        with temporary_workdir() as workdir:
            code, out, err = run_cli("experiments", "--dir", str(workdir / "absent"))

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertIn("No experiments found", out)


class EvalGateCommandTests(CliBaseTest):
    @staticmethod
    def _run_experiment(path: Path, score: float) -> None:
        run_experiment(
            path.stem,
            dataset=Dataset([DatasetExample(id="q1", input=score)]),
            task=lambda value: value,
            evaluators=[custom_evaluator("quality", lambda output, _expected: output)],
            path=path,
        )

    def test_exits_nonzero_and_prints_json_for_regression(self) -> None:
        with temporary_workdir() as workdir:
            baseline = workdir / "baseline.jsonl"
            candidate = workdir / "candidate.jsonl"
            self._run_experiment(baseline, 0.9)
            self._run_experiment(candidate, 0.7)

            code, out, err = run_cli("eval-gate", str(baseline), str(candidate), "--tolerance", "0.1")

            self.assertEqual(code, 1)
            self.assertEqual(err, "")
            payload = json.loads(out)
            self.assertTrue(payload["has_regressions"])
            self.assertEqual(payload["regressed"], ["quality"])

    def test_exits_zero_at_tolerance_boundary(self) -> None:
        with temporary_workdir() as workdir:
            baseline = workdir / "baseline.jsonl"
            candidate = workdir / "candidate.jsonl"
            self._run_experiment(baseline, 0.8)
            self._run_experiment(candidate, 0.7)

            code, out, err = run_cli("eval-gate", str(baseline), str(candidate), "--tolerance", "0.1")

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertFalse(json.loads(out)["has_regressions"])

    @staticmethod
    def _run_scores(path: Path, scores: dict[str, float]) -> None:
        run_experiment(
            path.stem,
            dataset=Dataset([DatasetExample(id="row", input={"scores": scores})]),
            task=lambda scores: scores,
            evaluators=[
                custom_evaluator(name, lambda output, _expected, key=name: output[key])
                for name in scores
            ],
            path=path,
        )

    def test_score_tolerance_flag_overrides_global(self) -> None:
        with temporary_workdir() as workdir:
            baseline = workdir / "baseline.jsonl"
            candidate = workdir / "candidate.jsonl"
            self._run_experiment(baseline, 0.9)
            self._run_experiment(candidate, 0.7)

            # The 0.2 drop regresses at the default tolerance, but a per-evaluator
            # override of 0.3 absorbs it and the gate passes.
            code, out, err = run_cli(
                "eval-gate", str(baseline), str(candidate), "--score-tolerance", "quality=0.3"
            )

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            payload = json.loads(out)
            self.assertFalse(payload["has_regressions"])
            self.assertEqual(payload["effective_tolerances"], {"quality": 0.3})
            self.assertEqual(payload["regression_reasons"], {})

    def test_repeated_identical_score_tolerance_is_allowed(self) -> None:
        with temporary_workdir() as workdir:
            baseline = workdir / "baseline.jsonl"
            candidate = workdir / "candidate.jsonl"
            self._run_experiment(baseline, 0.9)
            self._run_experiment(candidate, 0.7)

            code, out, err = run_cli(
                "eval-gate",
                str(baseline),
                str(candidate),
                "--score-tolerance",
                "quality=0.3",
                "--score-tolerance",
                "quality=0.3",
            )

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertEqual(json.loads(out)["effective_tolerances"], {"quality": 0.3})

    def test_missing_score_regress_exits_nonzero(self) -> None:
        with temporary_workdir() as workdir:
            baseline = workdir / "baseline.jsonl"
            candidate = workdir / "candidate.jsonl"
            self._run_scores(baseline, {"quality": 0.9, "coverage": 1.0})
            self._run_scores(candidate, {"quality": 0.9})

            # Default ignore policy: removing an evaluator does not fail the gate.
            code, out, _ = run_cli("eval-gate", str(baseline), str(candidate))
            self.assertEqual(code, 0)
            self.assertFalse(json.loads(out)["has_regressions"])

            # Strict regress policy treats the baseline-only evaluator as lost coverage.
            code, out, err = run_cli(
                "eval-gate", str(baseline), str(candidate), "--missing-score", "regress"
            )
            self.assertEqual(code, 1)
            self.assertEqual(err, "")
            payload = json.loads(out)
            self.assertTrue(payload["has_regressions"])
            self.assertEqual(payload["missing_score"], "regress")
            self.assertEqual(payload["baseline_only"], ["coverage"])
            self.assertEqual(payload["regression_reasons"], {"coverage": "baseline_only"})

    def test_rejects_malformed_score_tolerance(self) -> None:
        with temporary_workdir() as workdir:
            baseline = workdir / "baseline.jsonl"
            candidate = workdir / "candidate.jsonl"
            self._run_experiment(baseline, 0.9)
            self._run_experiment(candidate, 0.7)

            for malformed in ("quality", "quality=", "quality=abc", "quality=-0.1", "quality=inf", "=0.1"):
                with self.assertRaises(SystemExit) as raised:
                    run_cli("eval-gate", str(baseline), str(candidate), "--score-tolerance", malformed)
                self.assertEqual(raised.exception.code, 2)

    def test_rejects_conflicting_score_tolerance(self) -> None:
        with temporary_workdir() as workdir:
            baseline = workdir / "baseline.jsonl"
            candidate = workdir / "candidate.jsonl"
            self._run_experiment(baseline, 0.9)
            self._run_experiment(candidate, 0.7)

            code, out, err = run_cli(
                "eval-gate",
                str(baseline),
                str(candidate),
                "--score-tolerance",
                "quality=0.1",
                "--score-tolerance",
                "quality=0.2",
            )

            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("conflicting --score-tolerance values for 'quality'", err)

    def test_unknown_score_tolerance_name_exits_nonzero(self) -> None:
        with temporary_workdir() as workdir:
            baseline = workdir / "baseline.jsonl"
            candidate = workdir / "candidate.jsonl"
            self._run_experiment(baseline, 0.9)
            self._run_experiment(candidate, 0.7)

            code, out, err = run_cli(
                "eval-gate", str(baseline), str(candidate), "--score-tolerance", "qualtiy=0.3"
            )

            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("shared evaluators present in both experiments", err)


class SendCommandTests(CliBaseTest):
    def test_send_reports_accepted_attempted_skipped(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                data = getattr(request, "data")
                events = json.loads(data.decode("utf-8"))
                body = json.dumps({"accepted": len(events), "event_ids": [event["id"] for event in events]})
                return FakeHttpResponse(body.encode("utf-8"))

            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                code, out, err = run_cli("send", "--path", str(trace_path), "--server", "http://server.test")

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            # Two traces, each trace + span + score = 6 events.
            self.assertEqual(out.strip(), "accepted=6 attempted=6 skipped=0")

    def test_send_surfaces_network_errors(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)

            with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
                code, out, err = run_cli("send", "--path", str(trace_path), "--server", "http://server.test")

            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("bir:", err)

    def test_send_omits_rotated_files_by_default(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_active_and_rotated_trace(trace_path)

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                data = getattr(request, "data")
                events = json.loads(data.decode("utf-8"))
                body = json.dumps({"accepted": len(events), "event_ids": [event["id"] for event in events]})
                return FakeHttpResponse(body.encode("utf-8"))

            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                code, out, err = run_cli("send", "--path", str(trace_path), "--server", "http://server.test")

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            # Only the active file's single trace is uploaded by default.
            self.assertEqual(out.strip(), "accepted=1 attempted=1 skipped=0")

    def test_send_include_rotated_uploads_rotated_and_active(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_active_and_rotated_trace(trace_path)
            posted_batches: list[list[dict[str, Any]]] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                data = getattr(request, "data")
                events = json.loads(data.decode("utf-8"))
                posted_batches.append(events)
                body = json.dumps({"accepted": len(events), "event_ids": [event["id"] for event in events]})
                return FakeHttpResponse(body.encode("utf-8"))

            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                code, out, err = run_cli(
                    "send",
                    "--path",
                    str(trace_path),
                    "--include-rotated",
                    "--server",
                    "http://server.test",
                )

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            # The rotated trace plus the active trace are both uploaded.
            self.assertEqual(out.strip(), "accepted=2 attempted=2 skipped=0")
            # Oldest-first: the rotated trace precedes the active one in the batch.
            posted_starts = [event["start_time"] for event in posted_batches[0]]
            self.assertEqual(posted_starts, sorted(posted_starts))


class SendExperimentCommandTests(CliBaseTest):
    def test_send_experiment_reports_accepted_and_id(self) -> None:
        with temporary_workdir() as workdir:
            run_faq_experiment(workdir)
            experiment_file = workdir / "faq.jsonl"
            response = FakeHttpResponse(json.dumps({"accepted": 1, "id": "experiment-1"}).encode("utf-8"))

            with patch("urllib.request.urlopen", return_value=response):
                code, out, err = run_cli("send-experiment", str(experiment_file), "--server", "http://server.test")

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertEqual(out.strip(), "accepted=1 id=experiment-1")

    def test_send_experiment_missing_file_exits_nonzero(self) -> None:
        with temporary_workdir() as workdir:
            missing = workdir / "absent.jsonl"

            def fail(*_args: Any, **_kwargs: Any) -> None:
                raise AssertionError("send-experiment must not reach the network for a missing file")

            with patch("urllib.request.urlopen", side_effect=fail):
                code, out, err = run_cli("send-experiment", str(missing))

            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("bir:", err)

    def test_send_experiment_forwards_retries_and_backoff(self) -> None:
        with temporary_workdir() as workdir:
            run_faq_experiment(workdir)
            experiment_file = workdir / "faq.jsonl"
            attempts: list[object] = []
            sleeps: list[float] = []
            success = FakeHttpResponse(json.dumps({"accepted": 1, "id": "experiment-1"}).encode("utf-8"))

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                attempts.append(request)
                if len(attempts) <= 3:
                    raise urllib.error.URLError("temporary network blip")
                return success

            with patch("bir._sdk.time.sleep", side_effect=lambda seconds: sleeps.append(seconds)):
                with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    code, out, err = run_cli(
                        "send-experiment",
                        str(experiment_file),
                        "--server",
                        "http://server.test",
                        "--retries",
                        "3",
                        "--backoff",
                        "0.25",
                    )

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertEqual(out.strip(), "accepted=1 id=experiment-1")
            # retries=3 allows four attempts; backoff=0.25 sets the first delay,
            # confirming both CLI options reach send_experiment.
            self.assertEqual(len(attempts), 4)
            self.assertEqual(sleeps, [0.25, 0.5, 1.0])

    def test_send_experiment_rejects_negative_retries(self) -> None:
        with temporary_workdir() as workdir:
            run_faq_experiment(workdir)
            experiment_file = workdir / "faq.jsonl"

            def fail(*_args: Any, **_kwargs: Any) -> None:
                raise AssertionError("invalid --retries must be rejected before any request")

            with patch("urllib.request.urlopen", side_effect=fail):
                with self.assertRaises(SystemExit) as raised:
                    run_cli("send-experiment", str(experiment_file), "--retries", "-1")
            self.assertEqual(raised.exception.code, 2)

    def test_send_experiment_rejects_non_finite_backoff(self) -> None:
        with temporary_workdir() as workdir:
            run_faq_experiment(workdir)
            experiment_file = workdir / "faq.jsonl"

            def fail(*_args: Any, **_kwargs: Any) -> None:
                raise AssertionError("invalid --backoff must be rejected before any request")

            with patch("urllib.request.urlopen", side_effect=fail):
                with self.assertRaises(SystemExit) as raised:
                    run_cli("send-experiment", str(experiment_file), "--backoff", "inf")
            self.assertEqual(raised.exception.code, 2)


class TailCommandTests(CliBaseTest):
    def test_follow_trace_emits_only_new_events(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            # An "old" event already present before following begins.
            trace_path.write_text(
                json.dumps({"type": "trace", "name": "old", "status": "success", "start_time": "T0"}) + "\n",
                encoding="utf-8",
            )

            out = io.StringIO()
            appended = {"done": False}

            def should_stop() -> bool:
                if not appended["done"]:
                    with trace_path.open("a", encoding="utf-8") as trace_file:
                        trace_file.write(
                            json.dumps({"type": "score", "name": "live", "status": "success", "start_time": "T1", "value": 0.5})
                            + "\n"
                        )
                    appended["done"] = True
                    return False
                return True

            cli._follow_trace(trace_path, out=out, poll_interval=0, should_stop=should_stop)

            rendered = out.getvalue()
            self.assertNotIn("old", rendered)
            self.assertIn("live", rendered)
            self.assertIn("value=0.5", rendered)

    def test_tail_command_follows_until_interrupted(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            trace_path.write_text("", encoding="utf-8")
            calls = {"n": 0}

            def fake_sleep(_seconds: float) -> None:
                calls["n"] += 1
                if calls["n"] == 1:
                    with trace_path.open("a", encoding="utf-8") as trace_file:
                        trace_file.write(
                            json.dumps({"type": "trace", "name": "live", "status": "success", "start_time": "T1"}) + "\n"
                        )
                    return
                raise KeyboardInterrupt

            with patch("bir.cli.time.sleep", side_effect=fake_sleep):
                code, out, err = run_cli("tail", "--path", str(trace_path))

            self.assertEqual(code, 0)
            self.assertIn("Following", err)
            self.assertIn("live", out)


class TopLevelTests(CliBaseTest):
    def test_help_exits_zero(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            run_cli("--help")
        self.assertEqual(raised.exception.code, 0)

    def test_version_prints_sdk_version(self) -> None:
        out = io.StringIO()
        with self.assertRaises(SystemExit) as raised, redirect_stdout(out):
            cli.main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn(bir.__version__, out.getvalue())

    def test_no_subcommand_prints_help_and_returns_one(self) -> None:
        code, _out, err = run_cli()
        self.assertEqual(code, 1)
        self.assertIn("usage: bir", err)


if __name__ == "__main__":
    unittest.main()
