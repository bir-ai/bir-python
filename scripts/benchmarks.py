"""Performance benchmarks for the Bir Python SDK.

Bir's usefulness rests on tracing being cheap enough to leave on, and on
store-wide operations staying bounded as a local store grows. Neither had a
tracked baseline, so a regression could only be noticed by feel. This script
measures both on fixed synthetic data and emits results that compare across
commits.

Run the fast subset the way CI does::

    python scripts/benchmarks.py --smoke

Record a baseline and check a later commit against it::

    python scripts/benchmarks.py --json baseline.json
    python scripts/benchmarks.py --baseline baseline.json

Time and peak memory are measured in separate passes: ``tracemalloc`` roughly
doubles allocation cost, so timing it would report the profiler rather than the
SDK. Every case prepares a fresh temporary store before each repeat, so repeats
never inherit the file a previous repeat grew, and no case touches the network:
the send benchmark stubs the HTTP call and measures the batching and bookkeeping
around it.

Stdlib only, like the package it measures.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

import bir  # noqa: E402
import bir.evals as evals  # noqa: E402
from bir._sdk import _prune_trace_store, _reset_config_for_tests, _safe_capture  # noqa: E402

# Bumped when the JSON result shape changes, so a stale baseline is rejected
# instead of silently compared against different fields.
RESULT_SCHEMA = 1

# Peak-memory growth smaller than this is not reported as a regression: the
# cheapest cases peak at a couple of kilobytes, where an interpreter's own
# allocation noise swamps any percentage.
MEMORY_NOISE_KIB = 64.0

# A payload with credentials in it, so the capture benchmark measures redaction
# doing real work rather than walking a clean dict.
SECRET_PAYLOAD: dict[str, Any] = {
    "messages": [{"role": "user", "content": "What is Bir?"}],
    "api_key": "sk-benchmark-secret-value",
    "headers": {"authorization": "Bearer benchmark-token"},
    "nested": {"password": "hunter2", "safe": ["a", "b", "c"]},
}


@contextlib.contextmanager
def temporary_store() -> Iterator[Path]:
    """Run one repeat in a throwaway directory holding its own trace file."""

    previous = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="bir-benchmark-") as directory:
        workdir = Path(directory)
        os.chdir(workdir)
        _reset_config_for_tests()
        try:
            yield workdir
        finally:
            os.chdir(previous)
            _reset_config_for_tests()


def write_events(count: int, *, capture: bool = False) -> None:
    """Record ``count`` traces, each with one nested generation."""

    bir.configure(capture_inputs=capture, capture_outputs=capture)
    for index in range(count):
        with bir.trace(f"request-{index}"):
            with bir.generation("llm", model="gpt-4o-mini", input={"prompt": "hello"}) as gen:
                gen.set_output("hi")
                gen.set_usage(input_tokens=11, output_tokens=4)


def dataset(size: int) -> evals.Dataset:
    return evals.Dataset(
        [
            evals.DatasetExample(id=f"example-{index}", input={"question": "hello"}, expected="hi")
            for index in range(size)
        ]
    )


class _StubResponse:
    """The minimal HTTP response ``send_events`` reads."""

    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _StubResponse:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


# -- benchmark bodies ---------------------------------------------------------
#
# Each ``prepare`` does its setup, then returns the callable that is timed. Work
# outside the returned callable is never measured.


def prepare_trace_disabled(workdir: Path, size: int) -> Callable[[], object]:
    bir.configure(enabled=False)

    @bir.observe()
    def handled(index: int) -> int:
        return index

    def body() -> None:
        for index in range(size):
            handled(index)

    return body


def prepare_trace_sampled_out(workdir: Path, size: int) -> Callable[[], object]:
    bir.configure(sample_rate=0.0)

    @bir.observe()
    def handled(index: int) -> int:
        return index

    def body() -> None:
        for index in range(size):
            handled(index)

    return body


def prepare_trace_recorded(workdir: Path, size: int) -> Callable[[], object]:
    bir.configure(capture_inputs=False, capture_outputs=False)

    @bir.observe()
    def handled(index: int) -> int:
        return index

    def body() -> None:
        for index in range(size):
            handled(index)

    return body


def prepare_generation_recorded(workdir: Path, size: int) -> Callable[[], object]:
    return lambda: write_events(size)


def prepare_capture_redaction(workdir: Path, size: int) -> Callable[[], object]:
    def body() -> None:
        for _ in range(size):
            _safe_capture(SECRET_PAYLOAD)

    return body


def prepare_store_rotation(workdir: Path, size: int) -> Callable[[], object]:
    # Small enough that a run crosses the rotation threshold many times, which is
    # what this case is here to measure.
    bir.configure(max_bytes=8_192, backup_count=3)
    return lambda: write_events(size)


def prepare_load_events(workdir: Path, size: int) -> Callable[[], object]:
    write_events(size)
    return lambda: bir.load_events()


def prepare_load_traces(workdir: Path, size: int) -> Callable[[], object]:
    write_events(size)
    return lambda: bir.load_traces()


def prepare_prune(workdir: Path, size: int) -> Callable[[], object]:
    write_events(size)
    # Dry run so every repeat prunes the same store: selection and validation are
    # the cost being measured, and rewriting once would empty it for the rest.
    return lambda: _prune_trace_store(keep_last=size // 2, dry_run=True)


def prepare_send_batched(workdir: Path, size: int) -> Callable[[], object]:
    batch_size = 100
    write_events(size)
    event_ids = [event.id for event in bir.load_events()]

    # One canned response per batch, built here so the stub adds nothing but a
    # list lookup to the measurement: the cost being measured is Bir's batching,
    # ordering, and bookkeeping around the call, not a fake server.
    responses = [
        json.dumps({"accepted": len(chunk), "event_ids": chunk}).encode("utf-8")
        for chunk in (event_ids[start : start + batch_size] for start in range(0, len(event_ids), batch_size))
    ]

    def body() -> None:
        pending = iter(responses)
        with patch(
            "bir._sdk.urllib.request.urlopen",
            side_effect=lambda *_args, **_kwargs: _StubResponse(next(pending)),
        ):
            bir.send_events("http://127.0.0.1:9", batch_size=batch_size)

    return body


def prepare_experiment_sync(workdir: Path, size: int) -> Callable[[], object]:
    examples = dataset(size)

    def body() -> None:
        evals.run_experiment(
            "benchmark",
            dataset=examples,
            task=lambda question: "hi",
            evaluators=[evals.exact_match()],
            path=str(workdir / "experiments" / "sync.jsonl"),
        )

    return body


def prepare_experiment_async(workdir: Path, size: int) -> Callable[[], object]:
    examples = dataset(size)

    async def task(question: str) -> str:
        return "hi"

    def body() -> None:
        asyncio.run(
            evals.run_experiment_async(
                "benchmark",
                dataset=examples,
                task=task,
                evaluators=[evals.exact_match()],
                path=str(workdir / "experiments" / "async.jsonl"),
                max_concurrency=4,
            )
        )

    return body


@dataclass(frozen=True)
class Benchmark:
    """One measured case and the work it performs per run."""

    name: str
    group: str
    prepare: Callable[[Path, int], Callable[[], object]]
    size: int
    smoke_size: int
    # Whether CI runs this case. The smoke subset is chosen for stability on a
    # shared runner, not for coverage: it is a canary, not the baseline.
    smoke: bool = True

    def units(self, *, smoke: bool) -> int:
        return self.smoke_size if smoke else self.size


BENCHMARKS: tuple[Benchmark, ...] = (
    Benchmark("trace_disabled", "tracing", prepare_trace_disabled, size=20_000, smoke_size=500),
    Benchmark("trace_sampled_out", "tracing", prepare_trace_sampled_out, size=20_000, smoke_size=500),
    Benchmark("trace_recorded", "tracing", prepare_trace_recorded, size=2_000, smoke_size=100),
    Benchmark("generation_recorded", "tracing", prepare_generation_recorded, size=1_000, smoke_size=50),
    Benchmark("capture_redaction", "capture", prepare_capture_redaction, size=5_000, smoke_size=200),
    Benchmark("store_rotation", "storage", prepare_store_rotation, size=1_000, smoke_size=50),
    Benchmark("load_events", "storage", prepare_load_events, size=5_000, smoke_size=200),
    Benchmark("load_traces", "storage", prepare_load_traces, size=5_000, smoke_size=200),
    Benchmark("prune_keep_last", "storage", prepare_prune, size=2_000, smoke_size=100),
    Benchmark("send_batched", "transport", prepare_send_batched, size=2_000, smoke_size=100),
    Benchmark("experiment_sync", "evals", prepare_experiment_sync, size=500, smoke_size=25),
    Benchmark("experiment_async", "evals", prepare_experiment_async, size=500, smoke_size=25),
)


@dataclass(frozen=True)
class Result:
    name: str
    group: str
    units: int
    seconds_best: float
    seconds_median: float
    peak_kib: float

    @property
    def per_unit_us(self) -> float:
        return self.seconds_best / self.units * 1_000_000

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "group": self.group,
            "units": self.units,
            "seconds_best": self.seconds_best,
            "seconds_median": self.seconds_median,
            "per_unit_us": self.per_unit_us,
            "peak_kib": self.peak_kib,
        }


def measure(benchmark: Benchmark, *, units: int, repeat: int) -> Result:
    """Time a case over ``repeat`` fresh runs, then measure its peak memory once."""

    durations: list[float] = []
    for _ in range(repeat):
        with temporary_store() as workdir:
            body = benchmark.prepare(workdir, units)
            start = time.perf_counter()
            body()
            durations.append(time.perf_counter() - start)

    with temporary_store() as workdir:
        body = benchmark.prepare(workdir, units)
        tracemalloc.start()
        try:
            body()
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

    return Result(
        name=benchmark.name,
        group=benchmark.group,
        units=units,
        seconds_best=min(durations),
        seconds_median=statistics.median(durations),
        peak_kib=peak / 1024,
    )


def selected(names: Sequence[str], *, smoke: bool) -> list[Benchmark]:
    chosen = [benchmark for benchmark in BENCHMARKS if not smoke or benchmark.smoke]
    if names:
        wanted = set(names)
        unknown = wanted - {benchmark.name for benchmark in BENCHMARKS}
        if unknown:
            raise SystemExit(f"unknown benchmark(s): {', '.join(sorted(unknown))}")
        chosen = [benchmark for benchmark in chosen if benchmark.name in wanted]
    return chosen


def environment() -> dict[str, Any]:
    """Describe the machine, so results are only compared where that is valid."""

    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": sys.platform,
        "machine": platform.machine(),
        "commit": git_commit(),
    }


def git_commit() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError:
        return None
    return completed.stdout.strip() or None


def render_table(results: Sequence[Result]) -> str:
    header = f"{'BENCHMARK':<22}{'UNITS':>8}{'BEST (ms)':>12}{'MEDIAN (ms)':>13}{'PER UNIT (us)':>15}{'PEAK (KiB)':>12}"
    lines = [header]
    for result in results:
        lines.append(
            f"{result.name:<22}{result.units:>8}"
            f"{result.seconds_best * 1000:>12.2f}{result.seconds_median * 1000:>13.2f}"
            f"{result.per_unit_us:>15.2f}{result.peak_kib:>12.1f}"
        )
    return "\n".join(lines)


def compare(results: Sequence[Result], baseline_path: Path, tolerance: float) -> int:
    """Report each case against a recorded baseline; return the exit status."""

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if baseline.get("schema") != RESULT_SCHEMA:
        raise SystemExit(f"baseline uses result schema {baseline.get('schema')!r}, expected {RESULT_SCHEMA}")

    recorded = {entry["name"]: entry for entry in baseline.get("results", [])}
    baseline_python = baseline.get("environment", {}).get("python")
    if baseline_python != platform.python_version():
        print(f"warning: baseline ran on Python {baseline_python}, this run is {platform.python_version()}")

    regressions: list[str] = []
    print(f"\n{'BENCHMARK':<22}{'TIME':>12}{'MEMORY':>12}")
    for result in results:
        previous = recorded.get(result.name)
        if previous is None:
            print(f"{result.name:<22}{'new':>12}{'new':>12}")
            continue
        if previous["units"] != result.units:
            # Two runs over different amounts of work are not comparable, and
            # quietly dividing them would read as a huge win or loss.
            print(f"{result.name:<22}{'size differs':>24}")
            regressions.append(
                f"{result.name}: baseline measured {previous['units']} units, this run measured {result.units}"
            )
            continue

        time_change = _change(previous["seconds_best"], result.seconds_best)
        memory_change = _change(previous["peak_kib"], result.peak_kib)
        print(f"{result.name:<22}{time_change:>11.1f}%{memory_change:>11.1f}%")
        if time_change > tolerance:
            regressions.append(f"{result.name}: {time_change:.1f}% slower")
        # Percentages on a kilobyte-scale peak are noise, so a memory regression
        # has to be visible in absolute terms before it counts.
        if memory_change > tolerance and result.peak_kib - previous["peak_kib"] > MEMORY_NOISE_KIB:
            regressions.append(f"{result.name}: {memory_change:.1f}% more peak memory")

    if regressions:
        print(f"\nregressions beyond the {tolerance:.0f}% tolerance:")
        for regression in regressions:
            print(f"  {regression}")
        return 1
    print(f"\nno regression beyond the {tolerance:.0f}% tolerance")
    return 0


def _change(before: float, after: float) -> float:
    if before <= 0:
        return 0.0
    return (after - before) / before * 100


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--smoke", action="store_true", help="Run the small, CI-stable subset.")
    parser.add_argument("--only", action="append", default=[], metavar="NAME", help="Run only this benchmark.")
    parser.add_argument("--repeat", type=int, default=None, metavar="N", help="Timed runs per benchmark.")
    parser.add_argument("--json", type=Path, default=None, metavar="PATH", help="Write results as JSON.")
    parser.add_argument("--baseline", type=Path, default=None, metavar="PATH", help="Compare against a results file.")
    parser.add_argument(
        "--tolerance",
        type=float,
        default=25.0,
        metavar="PCT",
        help="Percent a benchmark may regress before --baseline fails (default: 25).",
    )
    arguments = parser.parse_args(argv)

    repeat = arguments.repeat if arguments.repeat is not None else (1 if arguments.smoke else 5)
    if repeat < 1:
        raise SystemExit("--repeat must be at least 1")

    benchmarks = selected(arguments.only, smoke=arguments.smoke)
    if not benchmarks:
        raise SystemExit("no benchmarks selected")

    results = [
        measure(benchmark, units=benchmark.units(smoke=arguments.smoke), repeat=repeat) for benchmark in benchmarks
    ]

    print(render_table(results))

    if arguments.json is not None:
        payload = {
            "schema": RESULT_SCHEMA,
            "smoke": arguments.smoke,
            "repeat": repeat,
            "environment": environment(),
            "results": [result.to_json() for result in results],
        }
        arguments.json.parent.mkdir(parents=True, exist_ok=True)
        arguments.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {arguments.json}")

    if arguments.baseline is not None:
        return compare(results, arguments.baseline, arguments.tolerance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
