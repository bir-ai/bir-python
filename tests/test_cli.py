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
import itertools
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import bir
from bir import cli, configure, load_events, load_traces
from bir._sdk import LoadedTrace, TraceEvent, _reset_config_for_tests
from bir.cli import _aggregate_stats, _percentile, _TraceSummary, _UsageTotals
from bir.evals import (
    Dataset,
    DatasetExample,
    contains,
    custom_evaluator,
    exact_match,
    load_experiment,
    run_experiment,
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

    def read(self, amt: int | None = None) -> bytes:
        # http.client.HTTPResponse.read takes an optional byte count, and
        # the transport passes one to bound a success response.
        return self.body if amt is None else self.body[:amt]


@contextmanager
def deterministic_event_times() -> Iterator[None]:
    """Stamp recorded events with strictly increasing timestamps.

    The CLI fixtures record several trace roots back to back. On a coarse-
    resolution clock (notably Windows) those roots can share an identical
    ``start_time``, which makes start-time ordering and the inclusive ``--since`` /
    ``--until`` / ``--keep-last`` boundaries ambiguous and the tests flaky. Pinning
    ``_now`` to a monotonic micro-step counter gives every event a distinct,
    recording-ordered timestamp on every platform while keeping durations tiny.
    """

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    counter = itertools.count()

    def _fake_now() -> str:
        return (base + timedelta(microseconds=next(counter))).isoformat()

    with patch("bir._sdk._now", _fake_now):
        yield


def write_two_traces(trace_path: Path) -> None:
    """Record two traces (each with a span and a score) into ``trace_path``."""

    bir.configure(trace_path=trace_path)

    @bir.observe()
    def answer(question: str) -> str:
        with bir.span("retrieve_context"):
            pass
        bir.score("helpfulness", 0.9)
        return "ok"

    with deterministic_event_times():
        answer("first")
        answer("second")


def write_filterable_traces(trace_path: Path) -> None:
    """Record three traces with distinct names and a mix of statuses for filters.

    In recording order: a successful ``checkout``, a successful ``search``, and a
    failing ``checkout_retry`` (recorded with an ``error`` status). ``checkout`` and
    ``checkout_retry`` share the ``checkout`` substring while ``search`` does not, so
    the same fixture exercises ``--name``, ``--status``, and their combination.
    """

    bir.configure(trace_path=trace_path)

    @bir.observe(name="checkout")
    def checkout(value: str) -> str:
        return value

    @bir.observe(name="search")
    def search(value: str) -> str:
        return value

    @bir.observe(name="checkout_retry")
    def checkout_retry(value: str) -> str:
        raise ValueError("boom")

    with deterministic_event_times():
        checkout("a")
        search("b")
        try:
            checkout_retry("c")
        except ValueError:
            pass


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

    with deterministic_event_times():
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

    with deterministic_event_times():
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
    return LoadedTrace(id=trace_id, name="n", start_time=start, end_time=end, status=status, events=[root], root=root)


def make_summary(trace_id: str, duration_ms: float, *, status: str = "success") -> _TraceSummary:
    """Build the streaming summary ``bir stats`` aggregates over."""

    start = "2024-01-01T00:00:00"
    end = (datetime.fromisoformat(start) + timedelta(milliseconds=duration_ms)).isoformat()
    return _TraceSummary(
        id=trace_id,
        name="n",
        start_time=start,
        end_time=end,
        status=status,
        event_count=1,
        usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        costs={},
    )


def totals_from(events: list[TraceEvent]) -> _UsageTotals:
    """Accumulate token and cost totals the way the streaming pass does."""

    totals = _UsageTotals()
    for event in events:
        totals.add(event)
    return totals


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

    def test_name_filters_by_case_sensitive_substring(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_filterable_traces(trace_path)

            # Substring matches both "checkout" and "checkout_retry" but not "search".
            code, out, err = run_cli("traces", "--path", str(trace_path), "--name", "checkout", "--json")
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            names = sorted(entry["name"] for entry in json.loads(out))
            self.assertEqual(names, ["checkout", "checkout_retry"])

            # The table view honors the same filter.
            code, out, _ = run_cli("traces", "--path", str(trace_path), "--name", "search")
            self.assertEqual(code, 0)
            data_rows = out.splitlines()[1:]
            self.assertEqual(len(data_rows), 1)
            self.assertTrue(data_rows[0].endswith("search"))

            # Matching is case-sensitive, so a differently-cased query matches nothing.
            code, out, _ = run_cli("traces", "--path", str(trace_path), "--name", "CHECKOUT", "--json")
            self.assertEqual(json.loads(out), [])

    def test_status_filters_exact_status(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_filterable_traces(trace_path)

            code, out, _ = run_cli("traces", "--path", str(trace_path), "--status", "error", "--json")
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual([entry["name"] for entry in payload], ["checkout_retry"])
            self.assertEqual(payload[0]["status"], "error")

            code, out, _ = run_cli("traces", "--path", str(trace_path), "--status", "success", "--json")
            self.assertEqual({entry["name"] for entry in json.loads(out)}, {"checkout", "search"})

    def test_rejects_unknown_status(self) -> None:
        with temporary_workdir() as workdir:
            with self.assertRaises(SystemExit) as raised:
                run_cli("traces", "--path", str(workdir / "traces.jsonl"), "--status", "pending")
            self.assertEqual(raised.exception.code, 2)

    def test_since_and_until_filter_by_start_time(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_filterable_traces(trace_path)
            loaded = bir.load_traces(trace_path)  # oldest first

            # A lower bound past every start time empties the listing; an upper bound
            # past every start time keeps the whole listing.
            code, out, _ = run_cli("traces", "--path", str(trace_path), "--since", "2999-01-01", "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out), [])
            code, out, _ = run_cli("traces", "--path", str(trace_path), "--until", "2999-01-01", "--json")
            self.assertEqual(len(json.loads(out)), len(loaded))

            # An inclusive lower bound at a recorded start time keeps that trace and newer.
            bound = loaded[1].start_time
            code, out, _ = run_cli("traces", "--path", str(trace_path), "--since", bound, "--json")
            ids = [entry["id"] for entry in json.loads(out)]
            expected = [
                trace.id
                for trace in sorted(loaded, key=lambda trace: trace.start_time, reverse=True)
                if trace.start_time >= bound
            ]
            self.assertEqual(ids, expected)

            # --since and --until together form an inclusive window.
            low, high = loaded[0].start_time, loaded[1].start_time
            code, out, _ = run_cli("traces", "--path", str(trace_path), "--since", low, "--until", high, "--json")
            ids = [entry["id"] for entry in json.loads(out)]
            expected = [
                trace.id
                for trace in sorted(loaded, key=lambda trace: trace.start_time, reverse=True)
                if low <= trace.start_time <= high
            ]
            self.assertEqual(ids, expected)

    def test_filters_combine_with_and(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_filterable_traces(trace_path)

            # "checkout" matches two traces but only one of them is successful.
            code, out, _ = run_cli(
                "traces", "--path", str(trace_path), "--name", "checkout", "--status", "success", "--json"
            )
            self.assertEqual(code, 0)
            self.assertEqual([entry["name"] for entry in json.loads(out)], ["checkout"])

    def test_limit_is_applied_after_filtering(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_filterable_traces(trace_path)

            # "search" is the middle (not newest) trace; a limit-first implementation
            # would keep only the newest "checkout_retry" and then filter it away,
            # yielding nothing. Filtering before limiting must still surface "search".
            code, out, _ = run_cli("traces", "--path", str(trace_path), "--name", "search", "--limit", "1", "--json")
            self.assertEqual(code, 0)
            self.assertEqual([entry["name"] for entry in json.loads(out)], ["search"])

    def test_empty_filter_result_in_table_and_json(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_filterable_traces(trace_path)

            code, out, _ = run_cli("traces", "--path", str(trace_path), "--name", "absent")
            self.assertEqual(code, 0)
            self.assertIn("No traces found", out)

            code, out, _ = run_cli("traces", "--path", str(trace_path), "--name", "absent", "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out), [])

    def test_no_filters_lists_every_trace(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_filterable_traces(trace_path)

            code, out, _ = run_cli("traces", "--path", str(trace_path), "--json")
            self.assertEqual(code, 0)
            self.assertEqual(len(json.loads(out)), 3)

    def test_rejects_malformed_since(self) -> None:
        with temporary_workdir() as workdir:
            with self.assertRaises(SystemExit) as raised:
                run_cli("traces", "--path", str(workdir / "traces.jsonl"), "--since", "not-a-date")
            self.assertEqual(raised.exception.code, 2)


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


class StatsFilterTests(CliBaseTest):
    """The stats filters share ``bir traces`` semantics and bound the input set."""

    def test_status_filter_narrows_aggregation(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_stats_traces(trace_path)

            # Only the single error trace survives; it carries no generation, so
            # tokens and cost zero out while the error count stays 1.
            code, out, err = run_cli("stats", "--path", str(trace_path), "--status", "error", "--json")
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            payload = json.loads(out)
            self.assertEqual(payload["traces"], {"total": 1, "success": 0, "error": 1})
            self.assertEqual(payload["tokens"], {"input": 0, "output": 0, "total": 0})
            self.assertEqual(payload["cost"], [])
            self.assertEqual(payload["latency_ms"]["count"], 1)

            # The two successful traces keep every generation's tokens and cost,
            # and the table view honors the same filter.
            code, table, _ = run_cli("stats", "--path", str(trace_path), "--status", "success")
            self.assertEqual(code, 0)
            rows = stats_table_map(table)
            self.assertEqual(rows["traces"], "2")
            self.assertEqual(rows["success"], "2")
            self.assertEqual(rows["error"], "0")
            self.assertEqual(rows["total_tokens"], "60")
            self.assertEqual(rows["cost[USD]"], "input=0.002000 output=0.004000 total=0.006000")

    def test_name_filter_is_case_sensitive_substring(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_stats_traces(trace_path)

            # "ok" matches the two successful traces; the error trace is named "boom".
            code, out, _ = run_cli("stats", "--path", str(trace_path), "--name", "ok", "--json")
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(payload["traces"]["total"], 2)
            self.assertEqual(payload["tokens"]["total"], 60)

            # Matching is case-sensitive, so "OK" selects nothing and zeroes out.
            code, out, _ = run_cli("stats", "--path", str(trace_path), "--name", "OK", "--json")
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(payload["traces"]["total"], 0)
            self.assertEqual(payload["tokens"]["total"], 0)

    def test_since_and_until_bound_start_time(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_stats_traces(trace_path)
            loaded = sorted(bir.load_traces(trace_path), key=lambda trace: trace.start_time)

            # An inclusive lower bound at the last (error) trace's start keeps only it.
            code, out, _ = run_cli("stats", "--path", str(trace_path), "--since", loaded[-1].start_time, "--json")
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(payload["traces"]["total"], 1)
            self.assertEqual(payload["tokens"]["total"], 0)

            # An upper bound at the second trace's start keeps the two successful traces.
            code, out, _ = run_cli("stats", "--path", str(trace_path), "--until", loaded[1].start_time, "--json")
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(payload["traces"]["total"], 2)
            self.assertEqual(payload["tokens"]["total"], 60)

    def test_filters_combine_with_and(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_filterable_traces(trace_path)

            # "checkout" matches two traces but only one is successful, so the AND
            # of both filters aggregates exactly that one trace.
            code, out, _ = run_cli(
                "stats",
                "--path",
                str(trace_path),
                "--name",
                "checkout",
                "--status",
                "success",
                "--json",
            )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["traces"], {"total": 1, "success": 1, "error": 0})

    def test_empty_filter_result_exits_zero_with_zeroed_output(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_stats_traces(trace_path)

            code, out, err = run_cli("stats", "--path", str(trace_path), "--name", "absent", "--json")
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

    def test_rejects_malformed_since(self) -> None:
        with temporary_workdir() as workdir:
            with self.assertRaises(SystemExit) as raised:
                run_cli("stats", "--path", str(workdir / "traces.jsonl"), "--since", "not-a-date")
            self.assertEqual(raised.exception.code, 2)

    def test_match_all_filter_is_byte_identical_to_no_filter(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_stats_traces(trace_path)

            _code, baseline, _err = run_cli("stats", "--path", str(trace_path), "--json")
            # A bound past every start time keeps every trace, so the filtered
            # figures stay byte-identical to the unfiltered run.
            code, out, _ = run_cli("stats", "--path", str(trace_path), "--until", "2999-01-01", "--json")
            self.assertEqual(code, 0)
            self.assertEqual(out, baseline)


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

        stats = _aggregate_stats([], totals_from(events))

        self.assertEqual(stats["tokens"], {"input": 15, "output": 27, "total": 42})
        self.assertEqual([entry["currency"] for entry in stats["cost"]], ["EUR", "USD"])
        by_currency = {entry["currency"]: entry for entry in stats["cost"]}
        self.assertAlmostEqual(by_currency["USD"]["input_cost"], 0.005)
        self.assertAlmostEqual(by_currency["USD"]["output_cost"], 0.008)
        self.assertAlmostEqual(by_currency["USD"]["total_cost"], 0.013)
        self.assertAlmostEqual(by_currency["EUR"]["total_cost"], 0.03)

    def test_latency_mean_and_p95_over_trace_durations(self) -> None:
        traces = [make_summary(f"t{i}", duration) for i, duration in enumerate([100.0, 200.0, 300.0, 400.0])]

        stats = _aggregate_stats(traces, _UsageTotals())

        self.assertEqual(stats["traces"], {"total": 4, "success": 4, "error": 0})
        self.assertEqual(stats["latency_ms"]["count"], 4)
        self.assertAlmostEqual(stats["latency_ms"]["mean"], 250.0)
        # Nearest-rank p95 of four values selects the largest: ceil(0.95*4)=4 -> index 3.
        self.assertAlmostEqual(stats["latency_ms"]["p95"], 400.0)

    def test_counts_success_and_error_traces(self) -> None:
        traces = [
            make_summary("ok1", 10.0),
            make_summary("ok2", 20.0),
            make_summary("bad", 30.0, status="error"),
        ]

        stats = _aggregate_stats(traces, _UsageTotals())

        self.assertEqual(stats["traces"], {"total": 3, "success": 2, "error": 1})

    def test_empty_inputs_yield_zeroed_figures(self) -> None:
        stats = _aggregate_stats([], _UsageTotals())

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
            _code, out_again, _err = run_cli("experiment-show", experiment_id, "--dir", str(workdir), "--json")
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


class ExperimentReportCommandTests(CliBaseTest):
    def test_html_report_to_stdout_contains_all_sections(self) -> None:
        with temporary_workdir() as workdir:
            experiment_id = run_faq_experiment(workdir)

            code, out, err = run_cli("experiment-report", experiment_id, "--dir", str(workdir))

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            # A self-contained HTML document with no external assets.
            self.assertTrue(out.startswith("<!DOCTYPE html>"))
            self.assertIn("<style>", out)
            self.assertNotIn("<link", out)
            # Summary, evaluator aggregates, and per-example rows are all present.
            self.assertIn("Experiment Report: faq", out)
            self.assertIn(experiment_id, out)
            self.assertIn("<th>Evaluator</th>", out)
            self.assertIn("<td>exact_match</td>", out)
            self.assertIn("<td>q1</td>", out)
            self.assertIn("exact_match=1.00", out)

    def test_markdown_format_to_stdout(self) -> None:
        with temporary_workdir() as workdir:
            experiment_id = run_faq_experiment(workdir)

            code, out, err = run_cli("experiment-report", experiment_id, "--dir", str(workdir), "--format", "markdown")

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertIn("# Experiment Report: faq", out)
            self.assertIn("| Evaluator | Mean |", out)
            self.assertIn("| Example | Status | Scores | Error |", out)

    def test_output_writes_file_and_keeps_stdout_clean(self) -> None:
        with temporary_workdir() as workdir:
            experiment_id = run_faq_experiment(workdir)
            report_path = workdir / "out" / "report.html"

            code, out, err = run_cli(
                "experiment-report",
                experiment_id,
                "--dir",
                str(workdir),
                "--output",
                str(report_path),
            )

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertTrue(report_path.exists())
            contents = report_path.read_text(encoding="utf-8")
            self.assertTrue(contents.startswith("<!DOCTYPE html>"))
            self.assertIn("Experiment Report: faq", contents)
            # The report itself is written to the file, not stdout.
            self.assertNotIn("<!DOCTYPE html>", out)
            self.assertIn(str(report_path), out)

    def test_unknown_id_exits_nonzero_with_clean_stdout(self) -> None:
        with temporary_workdir() as workdir:
            run_faq_experiment(workdir)

            code, out, err = run_cli("experiment-report", "does-not-exist", "--dir", str(workdir))

            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("bir:", err)
            self.assertIn("not found", err)

    def test_a_write_that_fails_leaves_the_previous_report_untouched(self) -> None:
        with temporary_workdir() as workdir:
            experiment_id = run_faq_experiment(workdir)
            report_path = workdir / "report.html"
            code, _out, _err = run_cli(
                "experiment-report", experiment_id, "--dir", str(workdir), "--output", str(report_path)
            )
            self.assertEqual(code, 0)
            before = report_path.read_bytes()

            # A plain write truncates before a byte reaches the disk, so it is
            # the staging that has to fail for this to prove anything.
            for stage, target in (("write", "pathlib.Path.write_text"), ("replace", "pathlib.Path.replace")):
                with self.subTest(fails=stage):
                    with patch(target, side_effect=OSError(28, "No space left on device")):
                        code, out, err = run_cli(
                            "experiment-report", experiment_id, "--dir", str(workdir), "--output", str(report_path)
                        )

                    self.assertEqual(code, 1)
                    self.assertEqual(out, "")
                    self.assertIn("No space left on device", err)
                    self.assertEqual(report_path.read_bytes(), before)
                    self.assertEqual(list(workdir.glob(".*.tmp")), [])

    # Windows has no owner/group/other bits: os.chmod there sets only the
    # read-only flag, so a file asked for 0o600 reports 0o666 and the mode a
    # rename carries cannot be observed at all. Same guard and same reason as
    # tests/test_store_permissions.py.
    @unittest.skipIf(sys.platform == "win32", "POSIX permission bits")
    def test_a_new_report_keeps_the_umasks_mode_not_the_stores(self) -> None:
        with temporary_workdir() as workdir:
            experiment_id = run_faq_experiment(workdir)
            report_path = workdir / "report.html"

            code, _out, _err = run_cli(
                "experiment-report", experiment_id, "--dir", str(workdir), "--output", str(report_path)
            )

            self.assertEqual(code, 0)
            # A report is a deliberate handoff, not Bir's own bookkeeping, so it
            # must not arrive with the store's owner-only mode.
            self.assertNotEqual(stat.S_IMODE(report_path.stat().st_mode), 0o600)
            self.assertTrue(stat.S_IMODE(report_path.stat().st_mode) & stat.S_IRGRP)

    @unittest.skipIf(sys.platform == "win32", "POSIX permission bits")
    def test_re_rendering_keeps_the_mode_the_existing_report_had(self) -> None:
        with temporary_workdir() as workdir:
            experiment_id = run_faq_experiment(workdir)
            report_path = workdir / "report.html"
            run_cli("experiment-report", experiment_id, "--dir", str(workdir), "--output", str(report_path))
            os.chmod(report_path, 0o600)

            code, _out, _err = run_cli(
                "experiment-report", experiment_id, "--dir", str(workdir), "--output", str(report_path)
            )

            # A rename carries the staged file's mode, so a narrowed report would
            # silently widen again without this.
            self.assertEqual(code, 0)
            self.assertEqual(stat.S_IMODE(report_path.stat().st_mode), 0o600)

    def test_an_example_id_from_a_filesystem_walk_still_renders(self) -> None:
        with temporary_workdir() as workdir:
            # What os.fsdecode returns for a filename that is not valid UTF-8,
            # which is what a document-ingestion dataset's ids are.
            walked = os.fsdecode(b"doc-\xff.pdf")
            result = run_experiment(
                "walk",
                dataset=Dataset([DatasetExample(id=walked, input="hi", expected="ok")]),
                task=lambda _question: "ok",
                evaluators=[exact_match()],
                path=workdir / "walk.jsonl",
            )
            report_path = workdir / "report.html"

            for report_format in ("html", "markdown"):
                with self.subTest(format=report_format):
                    code, _out, err = run_cli(
                        "experiment-report",
                        result.id,
                        "--dir",
                        str(workdir),
                        "--format",
                        report_format,
                        "--output",
                        str(report_path),
                    )

                    self.assertEqual(code, 0)
                    self.assertEqual(err, "")
                    # Escaped rather than dropped, so the odd id stays visible.
                    self.assertIn("doc-\\udcff.pdf", report_path.read_text(encoding="utf-8"))


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
            evaluators=[custom_evaluator(name, lambda output, _expected, key=name: output[key]) for name in scores],
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
            code, out, err = run_cli("eval-gate", str(baseline), str(candidate), "--score-tolerance", "quality=0.3")

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
            code, out, err = run_cli("eval-gate", str(baseline), str(candidate), "--missing-score", "regress")
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

            code, out, err = run_cli("eval-gate", str(baseline), str(candidate), "--score-tolerance", "qualtiy=0.3")

            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("shared evaluators present in both experiments", err)

    @staticmethod
    def _run_per_example(path: Path, rows: dict[str, dict[str, float]]) -> None:
        evaluator_names = sorted({name for scores in rows.values() for name in scores})
        run_experiment(
            path.stem,
            dataset=Dataset(
                [DatasetExample(id=example_id, input={"scores": scores}) for example_id, scores in rows.items()]
            ),
            task=lambda scores: scores,
            evaluators=[
                custom_evaluator(name, lambda output, _expected, key=name: output[key]) for name in evaluator_names
            ],
            path=path,
        )

    def test_per_example_flag_includes_example_deltas(self) -> None:
        with temporary_workdir() as workdir:
            baseline = workdir / "baseline.jsonl"
            candidate = workdir / "candidate.jsonl"
            # Equal aggregate means; example "a" dropped and "b" improved.
            self._run_per_example(baseline, {"a": {"quality": 1.0}, "b": {"quality": 0.0}})
            self._run_per_example(candidate, {"a": {"quality": 0.0}, "b": {"quality": 1.0}})

            # Without the flag the output is the unchanged aggregate-only diff.
            code, out, err = run_cli("eval-gate", str(baseline), str(candidate))
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertNotIn("example_deltas", json.loads(out))

            # The flag adds per-example detail without changing the gate decision.
            code, out, err = run_cli("eval-gate", str(baseline), str(candidate), "--per-example")
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            payload = json.loads(out)
            self.assertFalse(payload["has_regressions"])
            self.assertEqual(payload["example_deltas"], {"quality": {"a": -1.0, "b": 1.0}})

    @staticmethod
    def _run_with_failures(path: Path, *, examples: int, failing: int, wrong: int = 0) -> None:
        """Run ``examples`` examples: the last ``failing`` raise, ``wrong`` miss."""

        def task(index: int) -> str:
            if index >= examples - failing:
                raise RuntimeError("model backend unavailable")
            if index >= examples - failing - wrong:
                return "not the expected answer"
            return "ok"

        run_experiment(
            path.stem,
            dataset=Dataset([DatasetExample(id=f"e{index}", input=index, expected="ok") for index in range(examples)]),
            task=task,
            evaluators=[exact_match()],
            path=path,
            raise_on_error=False,
        )

    def test_a_candidate_whose_examples_failed_exits_nonzero(self) -> None:
        with temporary_workdir() as workdir:
            baseline = workdir / "baseline.jsonl"
            candidate = workdir / "candidate.jsonl"
            # The baseline answers half its examples badly; the candidate raises on
            # those same ten, so its mean over what it still scored is higher.
            self._run_with_failures(baseline, examples=20, failing=0, wrong=10)
            self._run_with_failures(candidate, examples=20, failing=10)

            code, out, err = run_cli("eval-gate", str(baseline), str(candidate))

            self.assertEqual(code, 1)
            self.assertEqual(err, "")
            payload = json.loads(out)
            self.assertTrue(payload["has_regressions"])
            self.assertTrue(payload["failed_example_regression"])
            self.assertEqual(payload["failed_examples"], "regress")
            # The aggregate delta says the opposite, which is the whole point.
            self.assertEqual(payload["improved"], ["exact_match"])
            self.assertEqual(payload["regressed"], [])

    def test_failed_examples_ignore_exits_zero(self) -> None:
        with temporary_workdir() as workdir:
            baseline = workdir / "baseline.jsonl"
            candidate = workdir / "candidate.jsonl"
            self._run_with_failures(baseline, examples=20, failing=0, wrong=10)
            self._run_with_failures(candidate, examples=20, failing=10)

            code, out, err = run_cli("eval-gate", str(baseline), str(candidate), "--failed-examples", "ignore")

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            payload = json.loads(out)
            self.assertFalse(payload["has_regressions"])
            # Still measured and still reported; only the decision changed.
            self.assertTrue(payload["failed_example_regression"])
            self.assertEqual(payload["failed_examples"], "ignore")

    def test_the_payload_carries_both_runs_example_and_error_counts(self) -> None:
        with temporary_workdir() as workdir:
            baseline = workdir / "baseline.jsonl"
            candidate = workdir / "candidate.jsonl"
            self._run_with_failures(baseline, examples=8, failing=1)
            self._run_with_failures(candidate, examples=8, failing=1)

            code, out, err = run_cli("eval-gate", str(baseline), str(candidate))

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            payload = json.loads(out)
            self.assertEqual(payload["baseline_example_count"], 8)
            self.assertEqual(payload["baseline_error_count"], 1)
            self.assertEqual(payload["candidate_example_count"], 8)
            self.assertEqual(payload["candidate_error_count"], 1)
            self.assertFalse(payload["failed_example_regression"])

    def test_rejects_an_unknown_failed_examples_policy(self) -> None:
        with temporary_workdir() as workdir:
            baseline = workdir / "baseline.jsonl"
            candidate = workdir / "candidate.jsonl"
            self._run_experiment(baseline, 0.9)
            self._run_experiment(candidate, 0.9)

            with self.assertRaises(SystemExit) as raised:
                run_cli("eval-gate", str(baseline), str(candidate), "--failed-examples", "fail")

            self.assertEqual(raised.exception.code, 2)


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

            with patch("bir._sending._opener.open", side_effect=fake_urlopen):
                code, out, err = run_cli("send", "--path", str(trace_path), "--server", "http://server.test")

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            # Two traces, each trace + span + score = 6 events.
            self.assertEqual(out.strip(), "accepted=6 attempted=6 skipped=0")

    def test_send_surfaces_network_errors(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)

            with patch("bir._sending._opener.open", side_effect=urllib.error.URLError("connection refused")):
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

            with patch("bir._sending._opener.open", side_effect=fake_urlopen):
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

            with patch("bir._sending._opener.open", side_effect=fake_urlopen):
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

    def test_send_mark_sent_skips_on_second_send(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                data = getattr(request, "data")
                events = json.loads(data.decode("utf-8"))
                body = json.dumps({"accepted": len(events), "event_ids": [event["id"] for event in events]})
                return FakeHttpResponse(body.encode("utf-8"))

            with patch("bir._sending._opener.open", side_effect=fake_urlopen):
                first_code, first_out, first_err = run_cli(
                    "send", "--path", str(trace_path), "--server", "http://server.test", "--mark-sent"
                )
                second_code, second_out, second_err = run_cli(
                    "send", "--path", str(trace_path), "--server", "http://server.test", "--mark-sent"
                )

            self.assertEqual(first_code, 0)
            self.assertEqual(first_err, "")
            # Two traces, each trace + span + score = 6 events on the first send.
            self.assertEqual(first_out.strip(), "accepted=6 attempted=6 skipped=0")
            # The accepted IDs were recorded in the sidecar next to the trace file.
            self.assertTrue(trace_path.with_name(trace_path.name + ".sent").exists())
            # The second send finds every event already recorded, so nothing is attempted.
            self.assertEqual(second_code, 0)
            self.assertEqual(second_err, "")
            self.assertEqual(second_out.strip(), "accepted=0 attempted=0 skipped=0")

    def test_send_forwards_retries_and_backoff(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)
            attempts: list[object] = []
            sleeps: list[float] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                attempts.append(request)
                if len(attempts) <= 3:
                    raise urllib.error.URLError("temporary network blip")
                data = getattr(request, "data")
                events = json.loads(data.decode("utf-8"))
                body = json.dumps({"accepted": len(events), "event_ids": [event["id"] for event in events]})
                return FakeHttpResponse(body.encode("utf-8"))

            with patch("bir._sdk.time.sleep", side_effect=lambda seconds: sleeps.append(seconds)):
                with patch("bir._sending._opener.open", side_effect=fake_urlopen):
                    code, out, err = run_cli(
                        "send",
                        "--path",
                        str(trace_path),
                        "--server",
                        "http://server.test",
                        "--retries",
                        "3",
                        "--backoff",
                        "0.25",
                    )

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertEqual(out.strip(), "accepted=6 attempted=6 skipped=0")
            # retries=3 allows four attempts; backoff=0.25 sets the first delay,
            # confirming both CLI options reach send_events.
            self.assertEqual(len(attempts), 4)
            self.assertEqual(sleeps, [0.25, 0.5, 1.0])

    def test_send_forwards_timeout(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)
            timeouts: list[float] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                timeouts.append(timeout)
                data = getattr(request, "data")
                events = json.loads(data.decode("utf-8"))
                body = json.dumps({"accepted": len(events), "event_ids": [event["id"] for event in events]})
                return FakeHttpResponse(body.encode("utf-8"))

            with patch("bir._sending._opener.open", side_effect=fake_urlopen):
                # Omitting --timeout leaves the library default (10.0) in force.
                run_cli("send", "--path", str(trace_path), "--server", "http://server.test")
                # An explicit --timeout reaches the HTTP layer for the batch send.
                run_cli("send", "--path", str(trace_path), "--server", "http://server.test", "--timeout", "2.5")

            self.assertEqual(timeouts, [10.0, 2.5])

    def test_send_forwards_batch_size(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)
            batch_sizes: list[int] = []

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                data = getattr(request, "data")
                events = json.loads(data.decode("utf-8"))
                batch_sizes.append(len(events))
                body = json.dumps({"accepted": len(events), "event_ids": [event["id"] for event in events]})
                return FakeHttpResponse(body.encode("utf-8"))

            with patch("bir._sending._opener.open", side_effect=fake_urlopen):
                code, out, err = run_cli(
                    "send",
                    "--path",
                    str(trace_path),
                    "--server",
                    "http://server.test",
                    "--batch-size",
                    "2",
                )

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertEqual(out.strip(), "accepted=6 attempted=6 skipped=0")
            self.assertEqual(batch_sizes, [2, 2, 2])

    def test_send_rejects_negative_retries(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)

            def fail(*_args: Any, **_kwargs: Any) -> None:
                raise AssertionError("invalid --retries must be rejected before any request")

            with patch("bir._sending._opener.open", side_effect=fail):
                with self.assertRaises(SystemExit) as raised:
                    run_cli("send", "--path", str(trace_path), "--retries", "-1")
            self.assertEqual(raised.exception.code, 2)

    def test_send_rejects_negative_timeout(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)

            def fail(*_args: Any, **_kwargs: Any) -> None:
                raise AssertionError("invalid --timeout must be rejected before any request")

            with patch("bir._sending._opener.open", side_effect=fail):
                with self.assertRaises(SystemExit) as raised:
                    run_cli("send", "--path", str(trace_path), "--timeout", "-1")
            self.assertEqual(raised.exception.code, 2)

    def test_send_rejects_non_positive_batch_size(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)

            def fail(*_args: Any, **_kwargs: Any) -> None:
                raise AssertionError("invalid --batch-size must be rejected before any request")

            with patch("bir._sending._opener.open", side_effect=fail):
                for batch_size in ("0", "-1"):
                    with self.subTest(batch_size=batch_size), self.assertRaises(SystemExit) as raised:
                        run_cli("send", "--path", str(trace_path), "--batch-size", batch_size)
                    self.assertEqual(raised.exception.code, 2)


class AutomationJsonOutputTests(CliBaseTest):
    """The commands a pipeline runs report their result as JSON on request.

    ``eval-gate`` is absent on purpose: it has always emitted JSON only. These
    are the four that printed a human summary line with no machine-readable
    alternative, so a script had to match English to learn what happened.
    """

    def test_send_reports_counts_as_json(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)
            # A server answers with the ids it was sent; inventing them is
            # refused, so the fake echoes what is actually in the store.
            sent = [event.id for event in load_events(str(trace_path))]
            response = FakeHttpResponse(json.dumps({"accepted": len(sent), "event_ids": sent}).encode("utf-8"))

            with patch("bir._sending._opener.open", return_value=response):
                code, out, err = run_cli("send", "--path", str(trace_path), "--server", "http://server.test", "--json")

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            payload = json.loads(out)
            self.assertEqual(set(payload), {"accepted", "attempted", "skipped"})
            self.assertEqual(payload["accepted"], len(sent))
            # ``skipped`` is what the server did not newly accept, so it stays
            # consistent with the counts either side of it.
            self.assertEqual(payload["skipped"], payload["attempted"] - payload["accepted"])

    def test_send_experiment_reports_the_accepted_id_as_json(self) -> None:
        with temporary_workdir() as workdir:
            run_faq_experiment(workdir)
            response = FakeHttpResponse(json.dumps({"accepted": 1, "id": "experiment-1"}).encode("utf-8"))

            with patch("bir._sending._opener.open", return_value=response):
                code, out, err = run_cli(
                    "send-experiment",
                    str(workdir / "faq.jsonl"),
                    "--server",
                    "http://server.test",
                    "--json",
                )

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertEqual(json.loads(out), {"accepted": 1, "experiment_id": "experiment-1"})

    def test_prune_reports_a_dry_run_as_a_field_not_a_sentence(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)

            code, out, err = run_cli("prune", "--path", str(trace_path), "--keep-last", "1", "--json")

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            payload = json.loads(out)
            self.assertEqual(
                set(payload),
                {
                    "removed_traces",
                    "kept_traces",
                    "removed_events",
                    "bytes_reclaimed",
                    "incomplete_tail_bytes",
                    "dry_run",
                },
            )
            self.assertEqual(payload["removed_traces"], 1)
            self.assertEqual(payload["kept_traces"], 1)
            # Without --yes nothing was written, and a script learns that from a
            # boolean rather than from a parenthetical in English.
            self.assertIs(payload["dry_run"], True)

    def test_prune_reports_a_write_as_json(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)

            code, out, _err = run_cli("prune", "--path", str(trace_path), "--keep-last", "1", "--yes", "--json")

            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertIs(payload["dry_run"], False)
            self.assertGreater(payload["bytes_reclaimed"], 0)
            self.assertEqual(len(bir.load_traces(str(trace_path))), 1)

    def test_export_otel_reports_what_it_exported_as_json(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)
            captured: dict[str, Any] = {}

            with patch("bir.integrations.otel._export_traces", _recording_exporter(captured, spans=6)):
                code, out, err = run_cli(
                    "export-otel",
                    "--path",
                    str(trace_path),
                    "--endpoint",
                    "http://collector.test:4318/v1/traces",
                    "--json",
                )

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertEqual(
                json.loads(out),
                {"traces": 2, "spans": 6, "endpoint": "http://collector.test:4318/v1/traces"},
            )

    def test_the_human_summary_is_still_the_default(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)

            code, out, _err = run_cli("prune", "--path", str(trace_path), "--keep-last", "1")

            self.assertEqual(code, 0)
            self.assertIn("removed=1 kept=1", out)
            self.assertIn("dry run", out)
            with self.assertRaises(json.JSONDecodeError):
                json.loads(out)

    def test_a_refused_prune_reports_on_stderr_without_json(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)

            # A usage error is not a result, so it stays a message and a exit
            # code rather than becoming a JSON document a script would parse as
            # success.
            code, out, err = run_cli("prune", "--path", str(trace_path), "--json")

            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("requires at least one selection filter", err)


class SendExperimentCommandTests(CliBaseTest):
    def test_send_experiment_reports_accepted_and_id(self) -> None:
        with temporary_workdir() as workdir:
            run_faq_experiment(workdir)
            experiment_file = workdir / "faq.jsonl"
            response = FakeHttpResponse(json.dumps({"accepted": 1, "id": "experiment-1"}).encode("utf-8"))

            with patch("bir._sending._opener.open", return_value=response):
                code, out, err = run_cli("send-experiment", str(experiment_file), "--server", "http://server.test")

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertEqual(out.strip(), "accepted=1 id=experiment-1")

    def test_send_experiment_missing_file_exits_nonzero(self) -> None:
        with temporary_workdir() as workdir:
            missing = workdir / "absent.jsonl"

            def fail(*_args: Any, **_kwargs: Any) -> None:
                raise AssertionError("send-experiment must not reach the network for a missing file")

            with patch("bir._sending._opener.open", side_effect=fail):
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
                with patch("bir._sending._opener.open", side_effect=fake_urlopen):
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

            with patch("bir._sending._opener.open", side_effect=fail):
                with self.assertRaises(SystemExit) as raised:
                    run_cli("send-experiment", str(experiment_file), "--retries", "-1")
            self.assertEqual(raised.exception.code, 2)

    def test_send_experiment_rejects_non_finite_backoff(self) -> None:
        with temporary_workdir() as workdir:
            run_faq_experiment(workdir)
            experiment_file = workdir / "faq.jsonl"

            def fail(*_args: Any, **_kwargs: Any) -> None:
                raise AssertionError("invalid --backoff must be rejected before any request")

            with patch("bir._sending._opener.open", side_effect=fail):
                with self.assertRaises(SystemExit) as raised:
                    run_cli("send-experiment", str(experiment_file), "--backoff", "inf")
            self.assertEqual(raised.exception.code, 2)


def _recording_exporter(captured: dict[str, Any], *, spans: int = 0):
    """Return a fake ``_export_traces`` that records its call and returns counts.

    The command hands the exporter a path and the options for reading it, rather
    than a list of loaded traces, so that the store is streamed in two passes
    instead of held. ``traces`` is resolved here the way the exporter would, so a
    case can still assert which traces the command selected.
    """

    from bir.integrations.otel import _ExportCounts

    def fake(
        traces: Any,
        *,
        endpoint: Any,
        service_name: Any,
        environment: Any,
        headers: Any,
        timeout: Any,
        include_rotated: Any = False,
        on_invalid: Any = None,
    ) -> Any:
        captured["path"] = traces
        captured["traces"] = bir.load_traces(str(traces), include_rotated=include_rotated)
        captured["endpoint"] = endpoint
        captured["service_name"] = service_name
        captured["environment"] = environment
        captured["headers"] = headers
        captured["timeout"] = timeout
        captured["include_rotated"] = include_rotated
        return _ExportCounts(traces=len(captured["traces"]), spans=spans)

    return fake


class ExportOtelCommandTests(CliBaseTest):
    """``bir export-otel`` fronts the existing OTLP exporter without importing it eagerly."""

    def test_exports_loaded_traces_and_prints_summary(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)
            captured: dict[str, Any] = {}

            with patch("bir.integrations.otel._export_traces", _recording_exporter(captured, spans=6)):
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
            # Defaults: service.name "bir", no environment, no headers, no timeout override.
            self.assertEqual(captured["endpoint"], "http://collector.test:4318/v1/traces")
            self.assertEqual(captured["service_name"], "bir")
            self.assertIsNone(captured["environment"])
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

            with patch("bir.integrations.otel._export_traces", _recording_exporter(captured, spans=6)):
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
                    "--environment",
                    "prod",
                    "--timeout",
                    "5",
                )

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            # Repeated --header folds into a dict; only the first '=' splits, so a
            # value may itself contain '='.
            self.assertEqual(captured["headers"], {"x-api-key": "secret", "x-team": "ml=ops"})
            self.assertEqual(captured["service_name"], "rag-api")
            self.assertEqual(captured["environment"], "prod")
            self.assertEqual(captured["timeout"], 5.0)

    def test_include_rotated_selects_rotated_traces(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_active_and_rotated_trace(trace_path)

            # Default: only the active file's single trace is exported.
            captured: dict[str, Any] = {}
            with patch("bir.integrations.otel._export_traces", _recording_exporter(captured, spans=1)):
                code, _out, err = run_cli(
                    "export-otel", "--path", str(trace_path), "--endpoint", "http://collector.test/v1/traces"
                )
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertEqual(len(captured["traces"]), 1)

            # --include-rotated also exports the rotated sibling.
            captured_rotated: dict[str, Any] = {}
            with patch("bir.integrations.otel._export_traces", _recording_exporter(captured_rotated, spans=2)):
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
                            json.dumps(
                                {"type": "score", "name": "live", "status": "success", "start_time": "T1", "value": 0.5}
                            )
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
                            json.dumps({"type": "trace", "name": "live", "status": "success", "start_time": "T1"})
                            + "\n"
                        )
                    return
                raise KeyboardInterrupt

            with patch("bir.cli.time.sleep", side_effect=fake_sleep):
                code, out, err = run_cli("tail", "--path", str(trace_path))

            self.assertEqual(code, 0)
            self.assertIn("Following", err)
            self.assertIn("live", out)


class TailRotationTests(CliBaseTest):
    """A follow has to survive the store rotating underneath it.

    ``configure(max_bytes=...)`` renames the active file away and starts a new
    one, which a byte offset alone cannot see: the replacement is usually already
    longer than the old offset by the next poll, so a size check reads it as
    ordinary growth and seeks past its beginning. Everything appended to the old
    file since the last poll is in a sibling nothing re-reads. The cases here
    rotate through the real writer rather than renaming by hand, so what they
    drive is what ``configure(max_bytes=...)`` actually does.
    """

    def _write_trace(self, trace_path: Path, name: str, *, max_bytes: int | None, backup_count: int = 3) -> None:
        """Write one trace root through the SDK. ``max_bytes=None`` leaves rotation off."""

        configure(trace_path=str(trace_path), max_bytes=max_bytes, backup_count=backup_count)
        with bir.trace(name=name):
            pass

    def _follow_writing(
        self,
        trace_path: Path,
        names: list[str],
        *,
        max_bytes: int | None,
        backup_count: int = 3,
    ) -> tuple[str, str]:
        """Follow ``trace_path`` while ``names`` are written one per poll."""

        out, err = io.StringIO(), io.StringIO()
        pending = iter(names)

        def should_stop() -> bool:
            name = next(pending, None)
            if name is None:
                return True
            self._write_trace(trace_path, name, max_bytes=max_bytes, backup_count=backup_count)
            return False

        cli._follow_trace(trace_path, out=out, poll_interval=0, should_stop=should_stop, err=err)
        return out.getvalue(), err.getvalue()

    def test_follow_prints_every_event_written_across_rotations(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            self._write_trace(trace_path, "before", max_bytes=None)
            names = [f"live{index:02d}" for index in range(12)]

            # Small enough that a single trace event fills the file, so every
            # write after the first rotates.
            rendered, errors = self._follow_writing(trace_path, names, max_bytes=200)

            self.assertEqual(errors, "")
            self.assertNotIn("before", rendered)
            printed = [line for line in rendered.splitlines() if "live" in line]
            self.assertEqual(len(printed), len(names))
            for name, line in zip(names, printed):
                self.assertIn(name, line)
            # The store did rotate; without that this passes for the wrong reason.
            self.assertTrue((workdir / "traces.jsonl.1").exists())

    def test_follow_drains_the_file_that_was_rotated_away(self) -> None:
        """Events appended between the last poll and the rename still print.

        Those live in the rotated sibling by the time the follow notices, and
        reading only the new active file is what loses them.
        """

        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            self._write_trace(trace_path, "before", max_bytes=None)
            out, err = io.StringIO(), io.StringIO()
            polls = {"n": 0}

            def should_stop() -> bool:
                polls["n"] += 1
                if polls["n"] > 1:
                    return True
                # Two writes inside one poll interval: the first lands in the
                # active file, the second rotates it away and starts a new one.
                self._write_trace(trace_path, "rotated_away", max_bytes=1_000_000)
                self._write_trace(trace_path, "after_rotation", max_bytes=200)
                return False

            cli._follow_trace(trace_path, out=out, poll_interval=0, should_stop=should_stop, err=err)

            rendered = out.getvalue()
            self.assertEqual(err.getvalue(), "")
            self.assertIn("rotated_away", rendered)
            self.assertIn("after_rotation", rendered)
            self.assertLess(rendered.index("rotated_away"), rendered.index("after_rotation"))

    def test_follow_reads_intermediate_files_in_write_order(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            self._write_trace(trace_path, "before", max_bytes=None)
            out, err = io.StringIO(), io.StringIO()
            polls = {"n": 0}
            names = ["step0", "step1", "step2", "step3"]

            def should_stop() -> bool:
                polls["n"] += 1
                if polls["n"] > 1:
                    return True
                # Four rotations inside a single poll interval, so two whole
                # files sit between the one the offset belongs to and the
                # active one. backup_count keeps all of them.
                for name in names:
                    self._write_trace(trace_path, name, max_bytes=200, backup_count=5)
                return False

            cli._follow_trace(trace_path, out=out, poll_interval=0, should_stop=should_stop, err=err)

            rendered = out.getvalue()
            self.assertEqual(err.getvalue(), "")
            printed = [line for line in rendered.splitlines() if "step" in line]
            self.assertEqual(len(printed), len(names))
            for name, line in zip(names, printed):
                self.assertIn(name, line)

    def test_follow_reports_a_rotation_it_could_not_follow(self) -> None:
        """A gap the follow cannot close is said out loud, not passed over.

        Enough rotations inside one poll interval and the file the offset
        belongs to is deleted by ``backup_count``, taking its unread tail with
        it. Nothing can print what is no longer on disk, so the notice goes to
        stderr -- off the event stream on stdout -- and following resumes.
        """

        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            self._write_trace(trace_path, "before", max_bytes=None)
            out, err = io.StringIO(), io.StringIO()
            polls = {"n": 0}

            def should_stop() -> bool:
                polls["n"] += 1
                if polls["n"] > 2:
                    return True
                if polls["n"] == 1:
                    for index in range(6):
                        self._write_trace(trace_path, f"lost{index}", max_bytes=200, backup_count=1)
                    return False
                self._write_trace(trace_path, "resumed", max_bytes=200, backup_count=1)
                return False

            cli._follow_trace(trace_path, out=out, poll_interval=0, should_stop=should_stop, err=err)

            self.assertEqual(err.getvalue().count("was replaced"), 1)
            self.assertIn(str(trace_path), err.getvalue())
            self.assertIn("resumed", out.getvalue())

    def test_follow_survives_a_filesystem_that_reuses_the_inode(self) -> None:
        """A new file wearing the deleted one's inode number is still a new file.

        Rotation frees an inode every time it drops a file past
        ``backup_count``, and ext4 hands that number straight to the new active
        file it creates a moment later; APFS and NTFS never do, so no machine
        sees both behaviours and only some of CI sees this one. Forcing every
        file to report the same device and inode is what makes the case
        reproducible anywhere: what has to carry the distinction then is the
        recorded first line, which no two event files share.
        """

        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            self._write_trace(trace_path, "before", max_bytes=None)
            names = [f"live{index:02d}" for index in range(4)]

            class SameFileEverywhere:
                st_dev = 1
                st_ino = 1

            with patch("bir.cli.os.fstat", return_value=SameFileEverywhere()):
                rendered, errors = self._follow_writing(trace_path, names, max_bytes=200)

            self.assertEqual(errors, "")
            self.assertNotIn("before", rendered)
            printed = [line for line in rendered.splitlines() if "live" in line]
            self.assertEqual(len(printed), len(names))
            for name, line in zip(names, printed):
                self.assertIn(name, line)

    def test_follow_restarts_and_reports_when_the_file_is_rewritten_in_place(self) -> None:
        """A rewrite keeps the inode, so only the content says the file changed.

        Nothing in the SDK truncates the active file, but ``bir prune`` replaces
        it and a person can overwrite it, and both cost the follow whatever it
        had not read yet. It resumes from the start of what is there now and
        says the file was replaced, the same as for a rotation it could not
        follow, because the two leave the same absence.
        """

        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            old = json.dumps({"type": "trace", "name": "old", "status": "success", "start_time": "T0"}) + "\n"
            trace_path.write_text(old * 4, encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            polls = {"n": 0}

            def should_stop() -> bool:
                polls["n"] += 1
                if polls["n"] > 1:
                    return True
                # Truncated and rewritten in place: same inode, different content.
                with trace_path.open("w", encoding="utf-8") as trace_file:
                    trace_file.write(
                        json.dumps({"type": "trace", "name": "fresh", "status": "success", "start_time": "T1"}) + "\n"
                    )
                return False

            cli._follow_trace(trace_path, out=out, poll_interval=0, should_stop=should_stop, err=err)

            self.assertIn("fresh", out.getvalue())
            self.assertNotIn("old", out.getvalue())
            self.assertEqual(err.getvalue().count("was replaced"), 1)

    def test_follow_does_not_report_the_first_event_in_an_empty_store(self) -> None:
        """An empty file identifies nothing, so filling it is not a replacement."""

        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            trace_path.write_text("", encoding="utf-8")

            rendered, errors = self._follow_writing(trace_path, ["first", "second"], max_bytes=None)

            self.assertEqual(errors, "")
            self.assertIn("first", rendered)
            self.assertIn("second", rendered)

    def test_follow_waits_for_a_store_that_does_not_exist_yet(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            out, err = io.StringIO(), io.StringIO()
            polls = {"n": 0}

            def should_stop() -> bool:
                polls["n"] += 1
                if polls["n"] > 1:
                    return True
                self._write_trace(trace_path, "first", max_bytes=None)
                return False

            cli._follow_trace(trace_path, out=out, poll_interval=0, should_stop=should_stop, err=err)

            self.assertEqual(err.getvalue(), "")
            self.assertIn("first", out.getvalue())


class TailStreamingTests(unittest.TestCase):
    """A redirected follow has to show events while it is still running.

    The cases above drive ``_follow_trace`` with a ``StringIO``, which has no
    buffering to get wrong, so they pass whether or not anything is flushed. The
    defect only exists against a real block-buffered stdout: Python held the
    rendered lines until the process exited, and a follow command is ended by a
    signal rather than by exiting, so ``bir tail | grep`` produced nothing for the
    whole run. That needs a subprocess and a pipe to see.
    """

    EVENT = {"type": "trace", "name": "streamed", "status": "success", "start_time": "T1"}

    def test_a_redirected_follow_shows_an_event_before_it_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            trace_path = workdir / "traces.jsonl"
            stdout_path = workdir / "tail.out"
            stderr_path = workdir / "tail.err"

            env = dict(os.environ)
            src = str(Path(bir.__file__).resolve().parent.parent)
            env["PYTHONPATH"] = os.pathsep.join(filter(None, [src, env.get("PYTHONPATH", "")]))

            with stdout_path.open("wb") as out_file, stderr_path.open("wb") as err_file:
                follower = subprocess.Popen(
                    [sys.executable, "-m", "bir", "tail", "--path", str(trace_path)],
                    stdout=out_file,
                    stderr=err_file,
                    env=env,
                )
            try:
                # The follower records where the file ends when it starts, so the
                # event has to be written after it is following or it is skipped
                # as history rather than streamed.
                self._wait_for(follower, stderr_path, "Following", "the follower never started")
                with trace_path.open("a", encoding="utf-8") as trace_file:
                    trace_file.write(json.dumps(self.EVENT) + "\n")

                self._wait_for(follower, stdout_path, "streamed", "the event never reached the redirected stdout")
                # Still running, so what was read came from a flush rather than
                # from the buffer being emptied on the way out.
                self.assertIsNone(follower.poll(), "the follower exited before the output was checked")
            finally:
                follower.terminate()
                follower.wait(timeout=30)

    def _wait_for(self, follower: subprocess.Popen[bytes], path: Path, needle: str, message: str) -> None:
        """Block until ``needle`` is readable in ``path`` while ``follower`` runs."""

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if follower.poll() is not None:
                self.fail(f"{message}: the follower exited with {follower.returncode}")
            if needle in path.read_text(encoding="utf-8", errors="replace"):
                return
            time.sleep(0.01)
        self.fail(message)


class ControlCharacterRenderingTests(CliBaseTest):
    """Recorded text must not be able to steer the terminal reading it.

    A name is data, and often not a literal: a bridge passes the tool the model
    chose and an application passes a route from a request. Printed as stored,
    ``\\x1b[2K`` erases the row above and ``\\x1b[31m`` repaints what follows, so a
    record could misrepresent the output of the command showing it. A bare
    newline splits a table row on its own.

    Escaping belongs to printing, not to recording: the event keeps what the
    application passed, and ``--json`` hands a parser the value as written.
    """

    ESCAPES = "\x1b[2K\x1b[31mFAKE ERROR\x1b[0m"

    def _record(self, trace_path: Path, name: str, *, model: str | None = None) -> None:
        configure(trace_path=trace_path)
        with bir.trace(name=name):
            if model is not None:
                with bir.generation("gen", model=model):
                    pass

    def test_traces_table_escapes_a_name(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            self._record(trace_path, self.ESCAPES)

            code, out, _ = run_cli("traces", "--path", str(trace_path))

            self.assertEqual(code, 0)
            self.assertNotIn("\x1b", out)
            self.assertIn("\\x1b[2K\\x1b[31mFAKE ERROR\\x1b[0m", out)

    def test_a_newline_in_a_name_does_not_split_the_row(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            self._record(trace_path, "two\nlines")

            code, out, _ = run_cli("traces", "--path", str(trace_path))

            self.assertEqual(code, 0)
            # A header and exactly one data row.
            self.assertEqual(len(out.splitlines()), 2)
            self.assertIn("two\\x0alines", out)

    def test_show_escapes_the_name_and_the_model(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            self._record(trace_path, self.ESCAPES, model="m\x1b[31m")
            trace_id = load_traces(str(trace_path))[0].id

            code, out, _ = run_cli("show", trace_id, "--path", str(trace_path))

            self.assertEqual(code, 0)
            self.assertNotIn("\x1b", out)
            self.assertIn("\\x1b[2K", out)
            self.assertIn("model=m\\x1b[31m", out)

    def test_json_keeps_the_value_as_stored(self) -> None:
        # The consumer is a parser, not a terminal. Escaping here would hand a
        # pipeline something other than what was recorded.
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            self._record(trace_path, self.ESCAPES)

            code, out, _ = run_cli("traces", "--path", str(trace_path), "--json")

            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)[0]["name"], self.ESCAPES)

    def test_recording_is_unchanged(self) -> None:
        # Only the printed form differs; the store keeps what was passed.
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            self._record(trace_path, self.ESCAPES)

            self.assertEqual(load_traces(str(trace_path))[0].name, self.ESCAPES)

    def test_tail_escapes_a_name(self) -> None:
        self.assertNotIn(
            "\x1b",
            cli._format_tail_line(
                json.dumps({"type": "trace", "name": self.ESCAPES, "status": "success", "start_time": "T1"})
            )
            or "",
        )

    def test_experiment_show_escapes_its_header_and_rows(self) -> None:
        with temporary_workdir() as workdir:
            result_path = workdir / "experiment.jsonl"
            run_experiment(
                f"exp{self.ESCAPES}",
                dataset=Dataset([DatasetExample(id=f"q{self.ESCAPES}", input={"s": "x"})]),
                task=lambda s: s,
                evaluators=[contains("x")],
                path=result_path,
            )
            experiment_id = load_experiment(result_path).id

            code, out, _ = run_cli("experiment-show", experiment_id, "--dir", str(workdir))

            self.assertEqual(code, 0)
            self.assertNotIn("\x1b", out)
            self.assertIn("\\x1b[2K", out)


class ErrorChannelRenderingTests(CliBaseTest):
    """A diagnostic must not be able to steer the terminal reading it either.

    The cases above cover the rendering path, for strings read out of the local
    store. This is the error channel, and the string that reaches it can come
    from a remote host: a ``bir send`` message embeds the server's own response
    body. That body is chosen by whatever is listening on ``--server``, which on
    a mistyped URL is not a Bir server at all.
    """

    # Erases its own line, moves the cursor up, erases again, and prints a line
    # that reads like the one a successful send prints.
    REPAINT = "\x1b[2K\x1b[A\x1b[2Kaccepted=1 attempted=1 skipped=0\x1b[0m"

    def _record_one(self, trace_path: Path) -> None:
        configure(trace_path=trace_path)
        with bir.trace(name="answer"):
            pass

    def _send_against(self, body: str, *, status: int = 400) -> tuple[int, str, str]:
        """Run ``bir send`` against a server that answers with ``body``."""

        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            self._record_one(trace_path)

            def fake_urlopen(request: object, timeout: float) -> FakeHttpResponse:
                if status < 400:
                    return FakeHttpResponse(body.encode("utf-8"))
                raise urllib.error.HTTPError(
                    url="http://server.test/v1/events/batch",
                    code=status,
                    msg="error",
                    hdrs=None,  # type: ignore[arg-type]
                    fp=io.BytesIO(body.encode("utf-8")),
                )

            with patch("bir._sending._opener.open", side_effect=fake_urlopen):
                return run_cli("send", "--path", str(trace_path), "--server", "http://server.test")

    def test_a_rejected_send_cannot_repaint_the_terminal(self) -> None:
        code, _, err = self._send_against(self.REPAINT)

        self.assertEqual(code, 1)
        self.assertNotIn("\x1b", err)
        self.assertIn("\\x1b[2K\\x1b[A\\x1b[2K", err)
        # One line, so the body cannot append a second that looks like a report.
        self.assertEqual(len(err.splitlines()), 1)

    def test_an_accepted_but_unreadable_response_cannot_repaint_the_terminal(self) -> None:
        # The other half: a 2xx whose body is not a batch response at all still
        # reaches the same channel, by a different message.
        code, _, err = self._send_against(self.REPAINT, status=200)

        self.assertEqual(code, 1)
        self.assertNotIn("\x1b", err)
        self.assertIn("invalid batch response", err)
        self.assertIn("\\x1b[2K", err)

    def test_a_huge_response_body_is_bounded_before_it_is_printed(self) -> None:
        code, _, err = self._send_against("x" * 2_000_000)

        self.assertEqual(code, 1)
        self.assertLess(len(err), 1_000)
        self.assertIn("…[truncated]", err)

    def test_the_otel_install_hint_stays_one_line(self) -> None:
        # The channel escapes a newline like any other control character, so the
        # SDK's own messages do not embed one; this pins that the hint stayed
        # readable rather than acquiring a literal \x0a in the middle. The extra
        # is blocked at the import hook rather than through ``sys.modules`` so
        # the case does not depend on what another test imported first.
        real_import = builtins.__import__

        def blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "opentelemetry" or name.startswith("opentelemetry."):
                raise ImportError(f"No module named {name!r}")
            return real_import(name, *args, **kwargs)

        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            self._record_one(trace_path)
            with patch.object(builtins, "__import__", side_effect=blocked_import):
                code, _, err = run_cli(
                    "export-otel", "--path", str(trace_path), "--endpoint", "http://otlp.test/v1/traces"
                )

        self.assertEqual(code, 1)
        self.assertNotIn("\\x0a", err)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertIn("pip install", err)

    def test_a_diagnostic_is_escaped_whatever_built_it(self) -> None:
        # The escaping is at the print site, so it covers every message on this
        # channel rather than the interpolations someone remembered to guard.
        out = io.StringIO()

        cli._report(f"trace {self.REPAINT} not found", out=out)

        self.assertNotIn("\x1b", out.getvalue())
        self.assertTrue(out.getvalue().startswith("bir: trace \\x1b[2K"))


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


class PruneCommandTests(CliBaseTest):
    def test_before_removes_only_older_whole_traces(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_filterable_traces(trace_path)
            loaded = bir.load_traces(trace_path)  # oldest first: checkout, search, checkout_retry
            cutoff = loaded[1].start_time  # strictly removes the oldest (checkout) only

            code, out, err = run_cli("prune", "--path", str(trace_path), "--before", cutoff, "--yes")

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertIn("removed=1", out)
            self.assertIn("kept=2", out)
            remaining = bir.load_traces(trace_path)
            self.assertEqual({trace.name for trace in remaining}, {"search", "checkout_retry"})
            # Every surviving line is still valid JSONL the loaders can read.
            self.assertEqual(len(bir.load_events(trace_path)), sum(len(t.events) for t in remaining))

    def test_keep_last_keeps_only_n_most_recent(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_filterable_traces(trace_path)
            newest_id = bir.load_traces(trace_path)[-1].id

            code, out, _ = run_cli("prune", "--path", str(trace_path), "--keep-last", "1", "--yes")

            self.assertEqual(code, 0)
            self.assertIn("removed=2", out)
            self.assertIn("kept=1", out)
            remaining = bir.load_traces(trace_path)
            self.assertEqual([trace.id for trace in remaining], [newest_id])

    def test_status_restricts_removal(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_filterable_traces(trace_path)

            code, out, _ = run_cli("prune", "--path", str(trace_path), "--status", "error", "--yes")

            self.assertEqual(code, 0)
            self.assertIn("removed=1", out)
            remaining = bir.load_traces(trace_path)
            self.assertEqual({trace.name for trace in remaining}, {"checkout", "search"})

    def test_keep_last_combines_with_status_restriction(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_filterable_traces(trace_path)

            # keep-last protects the newest two (search, checkout_retry); among the
            # rest only the success-status checkout is left, so only it is removed.
            code, out, _ = run_cli(
                "prune", "--path", str(trace_path), "--keep-last", "2", "--status", "success", "--yes"
            )

            self.assertEqual(code, 0)
            self.assertIn("removed=1", out)
            remaining = bir.load_traces(trace_path)
            self.assertEqual({trace.name for trace in remaining}, {"search", "checkout_retry"})

    def test_dry_run_reports_counts_but_writes_nothing(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)
            original = trace_path.read_bytes()

            code, out, _ = run_cli("prune", "--path", str(trace_path), "--before", "2999-01-01", "--dry-run")

            self.assertEqual(code, 0)
            self.assertIn("removed=2", out)
            self.assertIn("(dry run; pass --yes to apply)", out)
            # The store is untouched and still readable.
            self.assertEqual(trace_path.read_bytes(), original)
            self.assertEqual(len(bir.load_traces(trace_path)), 2)
            self.assertEqual(list(workdir.glob(".*.tmp")), [])

    def test_default_previews_without_yes(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)
            original = trace_path.read_bytes()

            code, out, _ = run_cli("prune", "--path", str(trace_path), "--before", "2999-01-01")

            self.assertEqual(code, 0)
            self.assertIn("(dry run; pass --yes to apply)", out)
            self.assertEqual(trace_path.read_bytes(), original)

    def test_dry_run_overrides_yes(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)
            original = trace_path.read_bytes()

            code, out, _ = run_cli("prune", "--path", str(trace_path), "--before", "2999-01-01", "--yes", "--dry-run")

            self.assertEqual(code, 0)
            self.assertIn("(dry run; pass --yes to apply)", out)
            self.assertEqual(trace_path.read_bytes(), original)

    def test_requires_a_selection_filter(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)
            original = trace_path.read_bytes()

            code, out, err = run_cli("prune", "--path", str(trace_path), "--yes")

            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("at least one selection filter", err)
            self.assertEqual(trace_path.read_bytes(), original)

    def test_rejects_non_positive_keep_last(self) -> None:
        with temporary_workdir() as workdir:
            with self.assertRaises(SystemExit) as raised:
                run_cli("prune", "--path", str(workdir / "traces.jsonl"), "--keep-last", "0")
            self.assertEqual(raised.exception.code, 2)

    def test_rejects_malformed_before(self) -> None:
        with temporary_workdir() as workdir:
            with self.assertRaises(SystemExit) as raised:
                run_cli("prune", "--path", str(workdir / "traces.jsonl"), "--before", "not-a-date")
            self.assertEqual(raised.exception.code, 2)

    def test_empty_store_exits_zero_and_creates_nothing(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"

            code, out, err = run_cli("prune", "--path", str(trace_path), "--before", "2999-01-01", "--yes")

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertIn("removed=0", out)
            # Pruning a never-written store neither creates the file nor a lock file.
            self.assertFalse(trace_path.exists())
            self.assertEqual(list(workdir.iterdir()), [])

    def test_nothing_matched_writes_nothing(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)
            original = trace_path.read_bytes()

            code, out, _ = run_cli("prune", "--path", str(trace_path), "--before", "2000-01-01", "--yes")

            self.assertEqual(code, 0)
            self.assertIn("removed=0", out)
            self.assertEqual(trace_path.read_bytes(), original)
            self.assertEqual(len(bir.load_traces(trace_path)), 2)

    def test_include_rotated_prunes_across_files(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_active_and_rotated_trace(trace_path)  # .1 holds the older trace, active the newer
            rotated_path = trace_path.with_name(trace_path.name + ".1")
            active_before = trace_path.read_bytes()

            code, out, _ = run_cli("prune", "--path", str(trace_path), "--include-rotated", "--keep-last", "1", "--yes")

            self.assertEqual(code, 0)
            self.assertIn("removed=1", out)
            self.assertIn("kept=1", out)
            # The newest trace (active file) survives; the rotated trace is gone.
            remaining = bir.load_traces(trace_path, include_rotated=True)
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0].root.name, "answer")
            self.assertEqual(bir.load_traces(rotated_path), [])
            # The active file, which held no removed trace, is left byte-for-byte intact.
            self.assertEqual(trace_path.read_bytes(), active_before)

    def test_write_failure_leaves_original_intact(self) -> None:
        with temporary_workdir() as workdir:
            trace_path = workdir / "traces.jsonl"
            write_two_traces(trace_path)
            original = trace_path.read_bytes()

            def fail_mid_staging(*args: Any, **_kwargs: Any) -> None:
                destination = args[2]
                destination.write(b"partial staging output\n")
                raise OSError("disk full")

            # A failure after the staging file has received data must leave the
            # original untouched and remove the incomplete sibling temp file.
            with patch("bir._storage._stream_filtered_trace_file", side_effect=fail_mid_staging):
                code, out, err = run_cli("prune", "--path", str(trace_path), "--before", "2999-01-01", "--yes")

            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("bir:", err)
            self.assertEqual(trace_path.read_bytes(), original)
            self.assertEqual(len(bir.load_traces(trace_path)), 2)
            self.assertEqual(list(workdir.glob(".*.tmp")), [])


@contextmanager
def only_bir_env(**values: str) -> Iterator[None]:
    """Run the body with exactly the given ``BIR_*`` variables set, nothing else.

    Every ambient ``BIR_*`` variable is removed first so a developer's real
    environment never leaks into a ``bir config`` env-reporting assertion; the
    supplied names are then set to the given values for the duration of the block.
    """

    cleaned = {key: value for key, value in os.environ.items() if not key.startswith("BIR_")}
    cleaned.update(values)
    with patch.dict(os.environ, cleaned, clear=True):
        yield


class ConfigCommandTests(CliBaseTest):
    def test_table_reports_effective_defaults(self) -> None:
        with only_bir_env():
            code, out, err = run_cli("config")

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), ["SETTING", "VALUE"])
        rows = dict(line.split(None, 1) for line in lines[1:])
        self.assertEqual(rows["capture_inputs"], "false")
        self.assertEqual(rows["capture_outputs"], "false")
        self.assertEqual(rows["enabled"], "true")
        self.assertEqual(rows["sample_rate"], "1.0")
        self.assertEqual(rows["backup_count"], "3")
        self.assertEqual(rows["sample_rules"], "-")
        self.assertEqual(rows["service_name"], "-")
        self.assertEqual(rows["model_prices"], "0")
        self.assertEqual(rows["env_vars_set"], "-")
        # The trace path is resolved to an absolute path ending in the default file.
        self.assertTrue(os.path.isabs(rows["trace_path"]))
        self.assertTrue(rows["trace_path"].endswith(os.path.join(".bir", "traces.jsonl")))

    def test_json_is_deterministic_and_sorted(self) -> None:
        with only_bir_env():
            code, first, _ = run_cli("config", "--json")
            _, second, _ = run_cli("config", "--json")

        self.assertEqual(code, 0)
        self.assertEqual(first, second)
        payload = json.loads(first)
        # Keys are emitted in sorted order by the shared JSON dumper.
        self.assertEqual(list(payload), sorted(payload))
        self.assertEqual(
            set(payload),
            {
                "trace_path",
                "capture_inputs",
                "capture_outputs",
                "enabled",
                "sample_rate",
                "sample_rules",
                "service_name",
                "environment",
                "source",
                "max_bytes",
                "backup_count",
                "max_value_length",
                "max_collection_items",
                "additional_secret_keys",
                "additional_redaction_patterns",
                "model_prices",
                "env_vars_set",
            },
        )
        self.assertIs(payload["capture_inputs"], False)
        self.assertIs(payload["enabled"], True)
        self.assertEqual(payload["sample_rate"], 1.0)
        self.assertEqual(payload["sample_rules"], {})
        self.assertIsNone(payload["service_name"])
        self.assertEqual(payload["env_vars_set"], [])
        self.assertTrue(os.path.isabs(payload["trace_path"]))

    def test_configure_changes_are_reflected(self) -> None:
        with only_bir_env(), temporary_workdir() as workdir:
            trace_path = workdir / "custom" / "traces.jsonl"
            bir.configure(
                trace_path=trace_path,
                capture_inputs=True,
                capture_outputs=True,
                enabled=False,
                sample_rate=0.25,
                sample_rules={"checkout": 0.5},
                service_name="rag-api",
                environment="staging",
                source="checkout-api",
                max_bytes=1024,
                backup_count=5,
                max_value_length=2048,
                max_collection_items=64,
            )

            code, out, _ = run_cli("config", "--json")

        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertIs(payload["capture_inputs"], True)
        self.assertIs(payload["capture_outputs"], True)
        self.assertIs(payload["enabled"], False)
        self.assertEqual(payload["sample_rate"], 0.25)
        self.assertEqual(payload["sample_rules"], {"checkout": 0.5})
        self.assertEqual(payload["service_name"], "rag-api")
        self.assertEqual(payload["environment"], "staging")
        self.assertEqual(payload["source"], "checkout-api")
        self.assertEqual(payload["max_bytes"], 1024)
        self.assertEqual(payload["backup_count"], 5)
        self.assertEqual(payload["max_value_length"], 2048)
        self.assertEqual(payload["max_collection_items"], 64)
        self.assertEqual(payload["trace_path"], str(trace_path.resolve()))

    def test_env_var_presence_is_reported_without_values(self) -> None:
        # A blank value is treated as unset by the SDK and so is not reported.
        with only_bir_env(BIR_SAMPLE_RATE="0.1", BIR_CAPTURE_INPUTS="true", BIR_SOURCE="   "):
            code, out, _ = run_cli("config", "--json")

        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["env_vars_set"], ["BIR_CAPTURE_INPUTS", "BIR_SAMPLE_RATE"])
        # Only names are reported; the values themselves never appear in output.
        self.assertNotIn("0.1", out)

    def test_secrets_are_summarized_not_dumped(self) -> None:
        with only_bir_env():
            bir.configure(
                additional_secret_keys=["my_secret_field"],
                additional_redaction_patterns=[r"topsecret-\d+"],
                model_prices={"gpt-4o": {"input": 0.123456, "output": 7.891011}},
            )

            code, table, _ = run_cli("config")
            _, raw_json, _ = run_cli("config", "--json")

        self.assertEqual(code, 0)
        payload = json.loads(raw_json)
        # Counts only — the count is reported, the contents never are.
        self.assertEqual(payload["additional_secret_keys"], 1)
        self.assertEqual(payload["additional_redaction_patterns"], 1)
        self.assertEqual(payload["model_prices"], 1)
        for rendered in (table, raw_json):
            self.assertNotIn("my_secret_field", rendered)
            self.assertNotIn("topsecret", rendered)
            self.assertNotIn("0.123456", rendered)
            self.assertNotIn("7.891011", rendered)

    def test_is_read_only_and_exits_zero(self) -> None:
        with only_bir_env():
            before = cli._sdk._config
            code, _, _ = run_cli("config")
            code_json, _, _ = run_cli("config", "--json")

        self.assertEqual(code, 0)
        self.assertEqual(code_json, 0)
        # The command never mutates the active configuration.
        self.assertIs(cli._sdk._config, before)


if __name__ == "__main__":
    unittest.main()
