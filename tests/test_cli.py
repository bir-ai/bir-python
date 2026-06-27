"""Tests for the stdlib-only ``bir`` command-line interface.

These exercise ``bir.cli.main`` end to end against a temporary trace file and
experiment directory: human output, ``--json`` output, and error exit codes. The
network is stubbed for ``send`` and ``send-experiment`` so no test touches a real
server. Local traces and experiments are produced through the public SDK API so
the CLI reads exactly the on-disk format the SDK writes.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import bir
from bir import cli
from bir._sdk import LoadedTrace, TraceEvent, _reset_config_for_tests
from bir.cli import _aggregate_stats, _percentile
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


def write_rich_trace(trace_path: Path) -> str:
    """Record one trace with a nested span, a generation, and a score; return its id.

    The body produces every event type ``bir show`` renders specially: a span
    nested inside another span (to exercise depth), a generation carrying a model
    and token usage, and a score carrying a value.
    """

    bir.configure(trace_path=trace_path)

    @bir.observe()
    def answer(question: str) -> str:
        with bir.span("outer"):
            with bir.span("inner"):
                pass
        with bir.generation("local.llm", model="demo-model") as gen:
            gen.set_output("ok")
            gen.set_usage(input_tokens=12, output_tokens=24)
        bir.score("helpfulness", 0.9)
        return "ok"

    answer("hello")
    return bir.load_traces(trace_path)[0].id


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


def write_stats_traces(trace_path: Path) -> None:
    """Record two successful traces (each with usage and USD cost) and one error trace.

    Each successful ``ok`` call records a generation carrying 10/20 input/output
    tokens and 0.001/0.002 USD input/output cost, plus a score. The ``boom`` call
    raises so its trace root is recorded with an ``error`` status. The totals are
    therefore 20/40/60 tokens and 0.002/0.004/0.006 USD across three traces.
    """

    bir.configure(trace_path=trace_path)

    @bir.observe()
    def ok(question: str) -> str:
        with bir.generation("local.llm", model="demo-model") as gen:
            gen.set_output("ok")
            gen.set_usage(input_tokens=10, output_tokens=20)
            gen.set_cost(input_cost=0.001, output_cost=0.002, currency="USD")
        bir.score("helpfulness", 0.9)
        return "ok"

    @bir.observe()
    def boom(question: str) -> str:
        raise ValueError("nope")

    ok("a")
    ok("b")
    try:
        boom("c")
    except ValueError:
        pass


def write_multi_currency_trace(trace_path: Path) -> None:
    """Record one trace whose generations bill 0.01 USD and 0.02 EUR separately."""

    bir.configure(trace_path=trace_path)

    @bir.observe()
    def mixed(question: str) -> str:
        with bir.generation("usd.llm") as gen:
            gen.set_cost(total_cost=0.01, currency="USD")
        with bir.generation("eur.llm") as gen:
            gen.set_cost(total_cost=0.02, currency="EUR")
        return "ok"

    mixed("a")


def make_event(**overrides: Any) -> TraceEvent:
    """Build a TraceEvent with safe defaults, overriding only the fields a test sets."""

    fields: dict[str, Any] = dict(
        id="e",
        trace_id="t",
        parent_id=None,
        name="n",
        type="trace",
        start_time="2024-01-01T00:00:00",
        end_time="2024-01-01T00:00:00",
        status="success",
        metadata={},
        input=None,
        output=None,
        error=None,
        raw={},
    )
    fields.update(overrides)
    return TraceEvent(**fields)


def make_generation(**overrides: Any) -> TraceEvent:
    return make_event(type="generation", **overrides)


def make_trace(trace_id: str, duration_ms: float, *, status: str = "success") -> LoadedTrace:
    """Build a LoadedTrace whose root spans exactly ``duration_ms`` milliseconds."""

    start = "2024-01-01T00:00:00"
    end = (datetime.fromisoformat(start) + timedelta(milliseconds=duration_ms)).isoformat()
    root = make_event(id=trace_id, trace_id=trace_id, type="trace", status=status, start_time=start, end_time=end)
    return LoadedTrace(
        id=trace_id, name="n", start_time=start, end_time=end, status=status, events=[root], root=root
    )


def stats_table_map(out: str) -> dict[str, str]:
    """Parse a ``bir stats`` table into a ``{metric: value}`` mapping.

    Columns are separated by two or more spaces, so splitting on that gap keeps
    the cost value's single-spaced ``input=.. output=.. total=..`` text intact.
    """

    rows: dict[str, str] = {}
    for line in out.splitlines()[1:]:  # skip the METRIC/VALUE header
        metric, value = re.split(r"\s{2,}", line, maxsplit=1)
        rows[metric] = value
    return rows


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


class ShowCommandTests(CliBaseTest):
    def test_renders_event_tree_with_salient_extras(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            trace_id = write_rich_trace(trace_path)

            code, out, err = run_cli("show", trace_id, "--path", str(trace_path))

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            lines = out.splitlines()
            # The root trace heads the tree, with every other event indented beneath it.
            self.assertTrue(lines[0].startswith("trace answer [success] "))
            self.assertTrue(all(line.startswith("  ") for line in lines[1:]))
            # A generation surfaces its model and usage; a score surfaces its value.
            gen_line = next(line for line in lines if "generation local.llm" in line)
            self.assertIn("model=demo-model", gen_line)
            self.assertIn("input_tokens=12", gen_line)
            self.assertIn("output_tokens=24", gen_line)
            score_line = next(line for line in lines if "score helpfulness" in line)
            self.assertIn("value=0.9", score_line)

    def test_nested_events_indent_by_depth(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            trace_id = write_rich_trace(trace_path)

            code, out, _ = run_cli("show", trace_id, "--path", str(trace_path))

            self.assertEqual(code, 0)
            lines = out.splitlines()
            outer = next(line for line in lines if "span outer" in line)
            inner = next(line for line in lines if "span inner" in line)
            # The inner span nests one level deeper than the outer span.
            self.assertTrue(outer.startswith("  span outer"))
            self.assertTrue(inner.startswith("    span inner"))

    def test_json_output_is_a_deterministic_nested_tree(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            trace_id = write_rich_trace(trace_path)

            code, out, err = run_cli("show", trace_id, "--path", str(trace_path), "--json")

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            payload = json.loads(out)
            self.assertEqual(payload["event"]["id"], trace_id)
            self.assertEqual(payload["event"]["type"], "trace")
            self.assertIsNone(payload["event"]["parent_id"])
            # The root's direct children are the span, generation, and score; all
            # point back at the root.
            child_types = sorted(child["event"]["type"] for child in payload["children"])
            self.assertEqual(child_types, ["generation", "score", "span"])
            self.assertTrue(all(child["event"]["parent_id"] == trace_id for child in payload["children"]))
            # The salient extras ride along on the right node types.
            generation = next(c for c in payload["children"] if c["event"]["type"] == "generation")
            self.assertEqual(generation["event"]["model"], "demo-model")
            self.assertEqual(generation["event"]["usage"]["input_tokens"], 12)
            score = next(c for c in payload["children"] if c["event"]["type"] == "score")
            self.assertEqual(score["event"]["value"], 0.9)
            # The nested span is a grandchild, reached through the outer span.
            outer = next(c for c in payload["children"] if c["event"]["name"] == "outer")
            self.assertEqual([gc["event"]["name"] for gc in outer["children"]], ["inner"])

            # Rendering again yields byte-identical output.
            _code, out_again, _err = run_cli("show", trace_id, "--path", str(trace_path), "--json")
            self.assertEqual(out, out_again)

    def test_unknown_trace_id_exits_nonzero_with_clean_stdout(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_rich_trace(trace_path)

            code, out, err = run_cli("show", "does-not-exist", "--path", str(trace_path))

            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("bir:", err)
            self.assertIn("not found", err)

    def test_include_rotated_reads_rotated_trace(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_active_and_rotated_trace(trace_path)
            # The older "first" trace was rotated into the sibling file.
            rotated_id = bir.load_traces(trace_path, include_rotated=True)[0].id

            # The default read resolves only the active file, so the rotated id is absent.
            code, out, err = run_cli("show", rotated_id, "--path", str(trace_path))
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("not found", err)

            # --include-rotated resolves the same files as `bir traces`, surfacing it.
            code, out, err = run_cli("show", rotated_id, "--path", str(trace_path), "--include-rotated")
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertTrue(out.startswith("trace answer [success] "))


class StatsCommandTests(CliBaseTest):
    def test_table_reports_counts_tokens_cost_and_latency(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_stats_traces(trace_path)

            code, out, err = run_cli("stats", "--path", str(trace_path))

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertEqual(out.splitlines()[0].split(), ["METRIC", "VALUE"])
            rows = stats_table_map(out)
            self.assertEqual(rows["traces"], "3")
            self.assertEqual(rows["success"], "2")
            self.assertEqual(rows["error"], "1")
            self.assertEqual(rows["input_tokens"], "20")
            self.assertEqual(rows["output_tokens"], "40")
            self.assertEqual(rows["total_tokens"], "60")
            self.assertEqual(rows["cost[USD]"], "input=0.002000 output=0.004000 total=0.006000")
            self.assertEqual(rows["latency_count"], "3")
            self.assertTrue(rows["latency_mean"].endswith("ms"))
            self.assertTrue(rows["latency_p95"].endswith("ms"))

    def test_json_reports_figures_and_is_deterministic(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_stats_traces(trace_path)

            code, out, err = run_cli("stats", "--path", str(trace_path), "--json")

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            payload = json.loads(out)
            self.assertEqual(payload["traces"], {"total": 3, "success": 2, "error": 1})
            self.assertEqual(payload["tokens"], {"input": 20, "output": 40, "total": 60})
            self.assertEqual(payload["latency_ms"]["count"], 3)
            self.assertIsInstance(payload["latency_ms"]["mean"], float)
            self.assertIsInstance(payload["latency_ms"]["p95"], float)
            self.assertEqual(len(payload["cost"]), 1)
            usd = payload["cost"][0]
            self.assertEqual(usd["currency"], "USD")
            self.assertAlmostEqual(usd["input_cost"], 0.002)
            self.assertAlmostEqual(usd["output_cost"], 0.004)
            self.assertAlmostEqual(usd["total_cost"], 0.006)

            # Re-running over the same store yields byte-identical JSON.
            _code, out_again, _err = run_cli("stats", "--path", str(trace_path), "--json")
            self.assertEqual(out, out_again)

    def test_currencies_are_reported_separately(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_multi_currency_trace(trace_path)

            code, out, _ = run_cli("stats", "--path", str(trace_path), "--json")

            self.assertEqual(code, 0)
            payload = json.loads(out)
            # Two distinct currency lines, sorted by code and never summed together.
            self.assertEqual([entry["currency"] for entry in payload["cost"]], ["EUR", "USD"])
            by_currency = {entry["currency"]: entry for entry in payload["cost"]}
            self.assertAlmostEqual(by_currency["EUR"]["total_cost"], 0.02)
            self.assertAlmostEqual(by_currency["USD"]["total_cost"], 0.01)

            # The table form lists both currencies as their own rows.
            _code, table, _err = run_cli("stats", "--path", str(trace_path))
            rows = stats_table_map(table)
            self.assertIn("cost[EUR]", rows)
            self.assertIn("cost[USD]", rows)

    def test_empty_input_exits_zero_with_zeroed_output(self) -> None:
        with temporary_workdir() as workdir:
            missing = workdir / "absent.jsonl"

            code, out, err = run_cli("stats", "--path", str(missing), "--json")

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertEqual(
                json.loads(out),
                {
                    "cost": [],
                    "latency_ms": {"count": 0, "mean": None, "p95": None},
                    "tokens": {"input": 0, "output": 0, "total": 0},
                    "traces": {"total": 0, "success": 0, "error": 0},
                },
            )

            # The table form also exits 0, zeroing counts and dashing absent figures.
            code, table, _err = run_cli("stats", "--path", str(missing))
            self.assertEqual(code, 0)
            rows = stats_table_map(table)
            self.assertEqual(rows["traces"], "0")
            self.assertEqual(rows["total_tokens"], "0")
            self.assertEqual(rows["cost"], "-")
            self.assertEqual(rows["latency_mean"], "-")
            self.assertEqual(rows["latency_p95"], "-")

    def test_include_rotated_counts_rotated_traces(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_active_and_rotated_trace(trace_path)

            # The default read counts only the active file's single trace.
            code, out, _ = run_cli("stats", "--path", str(trace_path), "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["traces"]["total"], 1)

            # --include-rotated also counts the rotated sibling.
            code, out, _ = run_cli("stats", "--path", str(trace_path), "--include-rotated", "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["traces"]["total"], 2)


class AggregateStatsTests(unittest.TestCase):
    """Unit-level coverage of the aggregation helper with controlled inputs."""

    def test_sums_tokens_and_groups_cost_by_currency(self) -> None:
        events = [
            make_generation(
                usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
                cost={"input_cost": 0.001, "output_cost": 0.002, "total_cost": 0.003},
                currency="USD",
            ),
            make_generation(
                usage={"input_tokens": 4, "output_tokens": 6, "total_tokens": 10},
                cost={"input_cost": 0.004, "output_cost": 0.006, "total_cost": 0.010},
                currency="USD",
            ),
            make_generation(
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                cost={"input_cost": 0.01, "output_cost": 0.02, "total_cost": 0.03},
                currency="EUR",
            ),
            make_event(type="span"),  # not a generation: ignored
            make_generation(),  # generation without usage or cost: contributes nothing
        ]

        stats = _aggregate_stats([], events)

        self.assertEqual(stats["tokens"], {"input": 15, "output": 27, "total": 42})
        self.assertEqual([entry["currency"] for entry in stats["cost"]], ["EUR", "USD"])
        by_currency = {entry["currency"]: entry for entry in stats["cost"]}
        self.assertAlmostEqual(by_currency["USD"]["input_cost"], 0.005)
        self.assertAlmostEqual(by_currency["USD"]["output_cost"], 0.008)
        self.assertAlmostEqual(by_currency["USD"]["total_cost"], 0.013)
        self.assertAlmostEqual(by_currency["EUR"]["total_cost"], 0.03)

    def test_latency_mean_and_p95_over_trace_durations(self) -> None:
        traces = [make_trace(f"t{i}", duration) for i, duration in enumerate([100.0, 200.0, 300.0, 400.0])]

        stats = _aggregate_stats(traces, [])

        self.assertEqual(stats["traces"], {"total": 4, "success": 4, "error": 0})
        self.assertEqual(stats["latency_ms"]["count"], 4)
        self.assertAlmostEqual(stats["latency_ms"]["mean"], 250.0)
        # Nearest-rank p95 of four values selects the largest: ceil(0.95*4)=4 -> index 3.
        self.assertAlmostEqual(stats["latency_ms"]["p95"], 400.0)

    def test_counts_success_and_error_traces(self) -> None:
        traces = [
            make_trace("ok1", 10.0),
            make_trace("ok2", 20.0),
            make_trace("bad", 30.0, status="error"),
        ]

        stats = _aggregate_stats(traces, [])

        self.assertEqual(stats["traces"], {"total": 3, "success": 2, "error": 1})

    def test_empty_inputs_yield_zeroed_figures(self) -> None:
        stats = _aggregate_stats([], [])

        self.assertEqual(stats["traces"], {"total": 0, "success": 0, "error": 0})
        self.assertEqual(stats["tokens"], {"input": 0, "output": 0, "total": 0})
        self.assertEqual(stats["cost"], [])
        self.assertEqual(stats["latency_ms"], {"count": 0, "mean": None, "p95": None})


class PercentileTests(unittest.TestCase):
    def test_nearest_rank_selection(self) -> None:
        self.assertEqual(_percentile([10.0], 95), 10.0)
        self.assertEqual(_percentile([float(n) for n in range(1, 11)], 95), 10.0)
        self.assertEqual(_percentile([float(n) for n in range(1, 21)], 95), 19.0)
        self.assertEqual(_percentile([float(n) for n in range(1, 101)], 95), 95.0)
        self.assertEqual(_percentile([1.0, 2.0, 3.0, 4.0], 50), 2.0)


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


class ExperimentShowCommandTests(CliBaseTest):
    def test_shows_summary_and_per_example_results(self) -> None:
        with temporary_workdir() as workdir:
            experiment_id = run_faq_experiment(workdir)

            code, out, err = run_cli("experiment-show", experiment_id, "--dir", str(workdir))

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            # The header carries the name, id, and run status counts.
            self.assertIn(f"faq ({experiment_id})", out)
            self.assertIn("status=success  examples=2  errors=0", out)
            # The evaluator aggregates appear as their own table.
            self.assertIn("EVALUATOR", out)
            self.assertIn("exact_match", out)
            self.assertIn("0.50", out)
            # Each example surfaces its id, status, and per-evaluator scores.
            self.assertIn("EXAMPLE", out)
            q1 = next(line for line in out.splitlines() if line.startswith("q1"))
            self.assertIn("success", q1)
            self.assertIn("exact_match=1.00", q1)
            q2 = next(line for line in out.splitlines() if line.startswith("q2"))
            self.assertIn("exact_match=0.00", q2)

    def test_json_output_is_deterministic(self) -> None:
        with temporary_workdir() as workdir:
            experiment_id = run_faq_experiment(workdir)

            code, out, err = run_cli("experiment-show", experiment_id, "--dir", str(workdir), "--json")

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            payload = json.loads(out)
            self.assertEqual(payload["id"], experiment_id)
            self.assertEqual(payload["name"], "faq")
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["example_count"], 2)
            self.assertEqual(payload["error_count"], 0)
            self.assertEqual(payload["aggregate_scores"]["exact_match"], 0.5)
            example_ids = [result["example_id"] for result in payload["results"]]
            self.assertEqual(example_ids, ["q1", "q2"])
            first = payload["results"][0]
            self.assertEqual(first["status"], "success")
            self.assertEqual(first["scores"]["exact_match"], 1.0)
            self.assertIsNone(first["error"])

            # Re-rendering the same experiment yields byte-identical JSON.
            _code, out_again, _err = run_cli(
                "experiment-show", experiment_id, "--dir", str(workdir), "--json"
            )
            self.assertEqual(out, out_again)

    def test_unknown_id_exits_nonzero_with_clean_stdout(self) -> None:
        with temporary_workdir() as workdir:
            run_faq_experiment(workdir)

            code, out, err = run_cli("experiment-show", "does-not-exist", "--dir", str(workdir))

            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("bir:", err)
            self.assertIn("not found", err)

    def test_dir_resolves_same_location_as_experiments(self) -> None:
        with temporary_workdir() as workdir:
            nested = workdir / "runs"
            experiment_id = run_faq_experiment(nested)

            # Without --dir the default directory holds nothing, so the id is absent.
            code, out, _ = run_cli("experiment-show", experiment_id)
            self.assertEqual(code, 1)
            self.assertEqual(out, "")

            # --dir resolves the same directory `bir experiments` reads from.
            code, out, err = run_cli("experiment-show", experiment_id, "--dir", str(nested))
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertIn(f"faq ({experiment_id})", out)


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


def _recording_exporter(captured: dict[str, Any], *, spans: int = 0):
    """Return a fake ``export_traces_to_otlp`` that records its call and returns ``spans``."""

    def fake(traces: Any, *, endpoint: Any, service_name: Any, headers: Any, timeout: Any) -> int:
        captured["traces"] = list(traces)
        captured["endpoint"] = endpoint
        captured["service_name"] = service_name
        captured["headers"] = headers
        captured["timeout"] = timeout
        return spans

    return fake


class ExportOtelCommandTests(CliBaseTest):
    """``bir export-otel`` fronts the existing OTLP exporter without importing it eagerly."""

    def test_exports_loaded_traces_and_prints_summary(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)
            captured: dict[str, Any] = {}

            with patch("bir.integrations.otel.export_traces_to_otlp", _recording_exporter(captured, spans=6)):
                code, out, err = run_cli(
                    "export-otel",
                    "--path",
                    str(trace_path),
                    "--endpoint",
                    "http://collector.test:4318/v1/traces",
                )

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            # Both local traces are loaded and forwarded to the exporter.
            self.assertEqual(len(captured["traces"]), 2)
            self.assertTrue(all(isinstance(trace, LoadedTrace) for trace in captured["traces"]))
            # Defaults: service.name "bir", no headers, no timeout override.
            self.assertEqual(captured["endpoint"], "http://collector.test:4318/v1/traces")
            self.assertEqual(captured["service_name"], "bir")
            self.assertIsNone(captured["headers"])
            self.assertIsNone(captured["timeout"])
            # The summary reports both the trace count and the exporter's span count.
            self.assertIn("2 trace", out)
            self.assertIn("6 spans", out)
            self.assertIn("http://collector.test:4318/v1/traces", out)

    def test_forwards_headers_service_name_and_timeout(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)
            captured: dict[str, Any] = {}

            with patch("bir.integrations.otel.export_traces_to_otlp", _recording_exporter(captured, spans=6)):
                code, out, err = run_cli(
                    "export-otel",
                    "--path",
                    str(trace_path),
                    "--endpoint",
                    "http://collector.test/v1/traces",
                    "--header",
                    "x-api-key=secret",
                    "--header",
                    "x-team=ml=ops",
                    "--service-name",
                    "rag-api",
                    "--timeout",
                    "5",
                )

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            # Repeated --header folds into a dict; only the first '=' splits, so a
            # value may itself contain '='.
            self.assertEqual(captured["headers"], {"x-api-key": "secret", "x-team": "ml=ops"})
            self.assertEqual(captured["service_name"], "rag-api")
            self.assertEqual(captured["timeout"], 5.0)

    def test_include_rotated_selects_rotated_traces(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_active_and_rotated_trace(trace_path)

            # Default: only the active file's single trace is exported.
            captured: dict[str, Any] = {}
            with patch("bir.integrations.otel.export_traces_to_otlp", _recording_exporter(captured, spans=1)):
                code, _out, err = run_cli(
                    "export-otel", "--path", str(trace_path), "--endpoint", "http://collector.test/v1/traces"
                )
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertEqual(len(captured["traces"]), 1)

            # --include-rotated also exports the rotated sibling.
            captured_rotated: dict[str, Any] = {}
            with patch("bir.integrations.otel.export_traces_to_otlp", _recording_exporter(captured_rotated, spans=2)):
                code, _out, err = run_cli(
                    "export-otel",
                    "--path",
                    str(trace_path),
                    "--include-rotated",
                    "--endpoint",
                    "http://collector.test/v1/traces",
                )
            self.assertEqual(code, 0)
            self.assertEqual(len(captured_rotated["traces"]), 2)

    def test_endpoint_is_required(self) -> None:
        with temporary_workdir() as workdir:
            with self.assertRaises(SystemExit) as raised:
                run_cli("export-otel", "--path", str(workdir / "traces.jsonl"))
            self.assertEqual(raised.exception.code, 2)

    def test_rejects_malformed_header(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)
            # A header with no '=' and one with an empty key are both rejected
            # during argument parsing, before any export is attempted.
            for malformed in ("noequals", "=value"):
                with self.assertRaises(SystemExit) as raised:
                    run_cli(
                        "export-otel",
                        "--path",
                        str(trace_path),
                        "--endpoint",
                        "http://collector.test/v1/traces",
                        "--header",
                        malformed,
                    )
                self.assertEqual(raised.exception.code, 2)

    def test_missing_extra_reports_actionable_error(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)
            real_import = builtins.__import__

            def blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
                if name == "opentelemetry" or name.startswith("opentelemetry."):
                    raise ImportError(f"No module named {name!r}")
                return real_import(name, *args, **kwargs)

            # The otel extra is absent: the real exporter raises ImportError, which
            # the command turns into a clean, actionable message and a non-zero exit.
            with patch.object(builtins, "__import__", side_effect=blocked_import):
                code, out, err = run_cli(
                    "export-otel", "--path", str(trace_path), "--endpoint", "http://collector.test/v1/traces"
                )

            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("bir:", err)
            self.assertIn("otel", err)
            self.assertIn("pip install", err)


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


class ModuleEntryPointTests(CliBaseTest):
    """``python -m bir`` must mirror the ``bir`` console script exactly."""

    def test_main_module_dispatches_to_cli_main(self) -> None:
        # The module entry point re-exports the very function the console script
        # is wired to, so both invocation paths share one implementation.
        import bir.__main__ as module

        self.assertIs(module.main, cli.main)

    def _run_module(self, *argv: str) -> subprocess.CompletedProcess[str]:
        """Invoke ``python -m bir`` with ``src`` importable, capturing output."""

        env = dict(os.environ)
        src = str(Path(bir.__file__).resolve().parent.parent)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [src, env.get("PYTHONPATH", "")]))
        return subprocess.run(
            [sys.executable, "-m", "bir", *argv],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_version_matches_console_script(self) -> None:
        result = self._run_module("--version")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), f"bir {bir.__version__}")

    def test_no_subcommand_exit_code_matches_main(self) -> None:
        # cli.main returns 1 and prints usage when no subcommand is given; the
        # module path must surface the same exit code.
        result = self._run_module()
        self.assertEqual(result.returncode, 1)
        self.assertIn("usage: bir", result.stderr)

    def test_traces_behaves_like_console_script(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)

            result = self._run_module("traces", "--path", str(trace_path), "--json")
            _code, expected, _err = run_cli("traces", "--path", str(trace_path), "--json")

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, expected)


if __name__ == "__main__":
    unittest.main()
