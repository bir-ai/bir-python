"""The benchmark harness must keep running, and keep refusing bad comparisons.

Benchmarks rot quietly: nothing fails when a case stops measuring what it claims
to, because the number it prints still looks like a number. These tests run the
harness end to end on its smallest sizes and check the parts that decide whether
a recorded baseline means anything — that results carry the work they measured,
and that a comparison refuses inputs it cannot honestly judge.
"""

from __future__ import annotations

import contextlib
import functools
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]


@functools.lru_cache(maxsize=1)
def load_benchmarks() -> ModuleType:
    """Load ``scripts/benchmarks.py`` by path (``scripts/`` is not a package).

    The module is registered in ``sys.modules`` before it executes because the
    dataclasses it defines resolve their own module while being built.
    """

    script_path = REPO_ROOT / "scripts" / "benchmarks.py"
    spec = importlib.util.spec_from_file_location("bir_benchmarks", script_path)
    assert spec is not None and spec.loader is not None, f"cannot load {script_path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_main(module: ModuleType, argv: list[str]) -> tuple[int, str]:
    """Run the harness, keeping its table out of the test output."""

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        status = module.main(argv)
    return status, output.getvalue()


class BenchmarkRegistryTests(unittest.TestCase):
    """Every declared case is coherent before it is ever run."""

    def setUp(self) -> None:
        self.benchmarks = load_benchmarks()

    def test_case_names_are_unique(self) -> None:
        names = [benchmark.name for benchmark in self.benchmarks.BENCHMARKS]
        self.assertEqual(sorted(names), sorted(set(names)))

    def test_every_case_declares_positive_work(self) -> None:
        for benchmark in self.benchmarks.BENCHMARKS:
            with self.subTest(benchmark=benchmark.name):
                self.assertGreater(benchmark.smoke_size, 0)
                # The smoke subset exists to stay fast on a shared runner, so it
                # must never be the heavier of the two.
                self.assertLessEqual(benchmark.smoke_size, benchmark.size)
                self.assertEqual(benchmark.units(smoke=True), benchmark.smoke_size)
                self.assertEqual(benchmark.units(smoke=False), benchmark.size)

    def test_the_roadmapped_operations_are_all_measured(self) -> None:
        names = {benchmark.name for benchmark in self.benchmarks.BENCHMARKS}

        # Disabled, sampled-out, and recorded tracing, plus the store-wide
        # operations whose cost grows with a local store.
        self.assertLessEqual(
            {
                "trace_disabled",
                "trace_sampled_out",
                "trace_recorded",
                "capture_redaction",
                "store_rotation",
                "load_events",
                "load_traces",
                "prune_keep_last",
                "send_batched",
                "experiment_sync",
                "experiment_async",
            },
            names,
        )

    def test_unknown_case_names_are_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            self.benchmarks.selected(["no-such-benchmark"], smoke=True)


class BenchmarkRunTests(unittest.TestCase):
    """A run produces results that describe the work they measured."""

    def setUp(self) -> None:
        self.benchmarks = load_benchmarks()

    def test_smoke_run_writes_comparable_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            results_path = Path(directory) / "results.json"
            status, table = run_main(
                self.benchmarks,
                ["--smoke", "--only", "trace_disabled", "--repeat", "1", "--json", str(results_path)],
            )

            self.assertEqual(status, 0)
            self.assertIn("trace_disabled", table)

            payload = json.loads(results_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], self.benchmarks.RESULT_SCHEMA)
            self.assertTrue(payload["smoke"])
            self.assertIn("python", payload["environment"])

            (result,) = payload["results"]
            self.assertEqual(result["name"], "trace_disabled")
            # A result without the work it measured cannot be compared to
            # anything, and a zero duration means the body never ran.
            self.assertGreater(result["units"], 0)
            self.assertGreater(result["seconds_best"], 0.0)
            self.assertGreaterEqual(result["peak_kib"], 0.0)
            self.assertGreater(result["per_unit_us"], 0.0)

    def test_every_case_runs(self) -> None:
        """Run all of them, because the one that rots is the one nobody runs.

        A case that stubs an SDK internal stops stubbing it the moment that
        internal moves, and the benchmark then does for real what it meant to
        fake — ``send_batched`` patches the transport's opener and would
        otherwise try to reach a server. Nothing about the printed number says
        so, and running a single case here left that to CI to discover. The
        whole smoke subset costs about a second at ``--repeat 1``.
        """

        with tempfile.TemporaryDirectory() as directory:
            results_path = Path(directory) / "results.json"
            status, _table = run_main(self.benchmarks, ["--smoke", "--repeat", "1", "--json", str(results_path)])

            self.assertEqual(status, 0)
            payload = json.loads(results_path.read_text(encoding="utf-8"))
            measured = {result["name"]: result for result in payload["results"]}
            self.assertEqual(
                sorted(measured),
                sorted(benchmark.name for benchmark in self.benchmarks.BENCHMARKS if benchmark.smoke),
            )
            for name, result in measured.items():
                with self.subTest(benchmark=name):
                    self.assertGreater(result["units"], 0)
                    self.assertGreater(result["seconds_best"], 0.0)

    def test_the_cases_outside_the_smoke_subset_run_too(self) -> None:
        # They are out of the subset for stability on a shared runner, not
        # because they may rot unnoticed. The only one is export_otel, which
        # needs the optional extra it is excluded for.
        outside = [benchmark.name for benchmark in self.benchmarks.BENCHMARKS if not benchmark.smoke]
        if importlib.util.find_spec("opentelemetry") is None:
            self.skipTest("the otel extra is not installed")

        for name in outside:
            with self.subTest(benchmark=name):
                # Without --smoke, since that flag is what excludes them.
                status, table = run_main(self.benchmarks, ["--only", name, "--repeat", "1"])

                self.assertEqual(status, 0)
                self.assertIn(name, table)

    def test_repeat_must_be_positive(self) -> None:
        with self.assertRaises(SystemExit):
            run_main(self.benchmarks, ["--smoke", "--only", "trace_disabled", "--repeat", "0"])


class BenchmarkComparisonTests(unittest.TestCase):
    """A comparison only reports a regression it can stand behind."""

    def setUp(self) -> None:
        self.benchmarks = load_benchmarks()
        self.result = self.benchmarks.Result(
            name="trace_disabled",
            group="tracing",
            units=500,
            seconds_best=0.2,
            seconds_median=0.2,
            peak_kib=1024.0,
        )

    def write_baseline(self, directory: str, entry: dict[str, object], *, schema: int | None = None) -> Path:
        payload = {
            "schema": self.benchmarks.RESULT_SCHEMA if schema is None else schema,
            "smoke": True,
            "repeat": 1,
            "environment": {"python": "3.12.0"},
            "results": [entry],
        }
        path = Path(directory) / "baseline.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def compare(self, path: Path, tolerance: float = 25.0) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = self.benchmarks.compare([self.result], path, tolerance)
        return status, output.getvalue()

    def test_matching_result_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_baseline(
                directory,
                {"name": "trace_disabled", "units": 500, "seconds_best": 0.2, "peak_kib": 1024.0},
            )

            status, report = self.compare(path)

            self.assertEqual(status, 0)
            self.assertIn("no regression", report)

    def test_slower_run_is_a_regression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_baseline(
                directory,
                {"name": "trace_disabled", "units": 500, "seconds_best": 0.1, "peak_kib": 1024.0},
            )

            status, report = self.compare(path)

            self.assertEqual(status, 1)
            self.assertIn("slower", report)

    def test_kilobyte_scale_memory_noise_is_not_a_regression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            noisy = self.benchmarks.Result(
                name="trace_disabled",
                group="tracing",
                units=500,
                seconds_best=0.2,
                seconds_median=0.2,
                peak_kib=6.0,
            )
            self.result = noisy
            # Tripled in percentage terms, but only four kilobytes in absolute
            # terms, which is below the harness's noise floor.
            path = self.write_baseline(
                directory,
                {"name": "trace_disabled", "units": 500, "seconds_best": 0.2, "peak_kib": 2.0},
            )

            status, _report = self.compare(path)

            self.assertEqual(status, 0)

    def test_real_memory_growth_is_a_regression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_baseline(
                directory,
                {"name": "trace_disabled", "units": 500, "seconds_best": 0.2, "peak_kib": 100.0},
            )

            status, report = self.compare(path)

            self.assertEqual(status, 1)
            self.assertIn("peak memory", report)

    def test_a_different_amount_of_work_is_not_compared(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_baseline(
                directory,
                {"name": "trace_disabled", "units": 20_000, "seconds_best": 8.0, "peak_kib": 1024.0},
            )

            status, report = self.compare(path)

            # Eight seconds against two tenths would read as a huge win; the
            # runs measured different amounts of work and are not comparable.
            self.assertEqual(status, 1)
            self.assertIn("units", report)

    def test_a_case_missing_from_the_baseline_is_reported_as_new(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_baseline(
                directory,
                {"name": "load_events", "units": 500, "seconds_best": 0.2, "peak_kib": 1024.0},
            )

            status, report = self.compare(path)

            self.assertEqual(status, 0)
            self.assertIn("new", report)

    def test_a_baseline_from_another_result_schema_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_baseline(
                directory,
                {"name": "trace_disabled", "units": 500, "seconds_best": 0.2, "peak_kib": 1024.0},
                schema=self.benchmarks.RESULT_SCHEMA + 1,
            )

            with self.assertRaises(SystemExit):
                self.compare(path)


if __name__ == "__main__":
    unittest.main()
