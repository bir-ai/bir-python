"""Argument parser construction and scalar converters for :mod:`bir.cli`.

Dependency direction is one-way: this module imports no CLI orchestration.
:mod:`bir.cli` injects version strings, choice vocabularies, defaults, and
command handlers explicitly, keeping parser construction free of import cycles
and import-time application state.
"""

from __future__ import annotations

import argparse
import math
from collections.abc import Callable, Mapping
from datetime import datetime

_Handler = Callable[[argparse.Namespace], int]


def build_parser(
    *,
    version: str,
    default_server: str,
    default_experiment_dir: str,
    report_formats: tuple[str, ...],
    missing_score_policies: tuple[str, ...],
    handlers: Mapping[str, _Handler],
) -> argparse.ArgumentParser:
    """Build the CLI parser from explicit public data and command handlers."""
    parser = argparse.ArgumentParser(
        prog="bir",
        description="Inspect local Bir traces and experiments and send them to a server.",
    )
    parser.add_argument("--version", action="version", version=f"bir {version}")
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    traces = subparsers.add_parser("traces", help="List local traces.")
    traces.add_argument("--path", help="Trace JSONL file to read (default: .bir/traces.jsonl).")
    traces.add_argument("--limit", type=_positive_int, metavar="N", help="Show at most N most recent traces.")
    traces.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of a table.")
    traces.add_argument(
        "--skip-invalid",
        action="store_true",
        help=(
            "Skip lines that cannot be read instead of refusing the whole store, reporting how many were skipped. Use after an interrupted write left a damaged line."
        ),
    )
    traces.add_argument(
        "--include-rotated",
        action="store_true",
        help="Also read size-rotated trace files (oldest first) alongside the active file.",
    )
    traces.add_argument(
        "--name",
        metavar="SUBSTRING",
        help="Only list traces whose name contains this case-sensitive substring.",
    )
    traces.add_argument(
        "--status",
        choices=("success", "error"),
        help="Only list traces with this status.",
    )
    traces.add_argument(
        "--since",
        type=_iso_datetime,
        metavar="ISO",
        help=("Only list traces whose start time is at or after this ISO datetime (naive values are treated as UTC)."),
    )
    traces.add_argument(
        "--until",
        type=_iso_datetime,
        metavar="ISO",
        help=("Only list traces whose start time is at or before this ISO datetime (naive values are treated as UTC)."),
    )
    traces.set_defaults(func=handlers["traces"])

    show = subparsers.add_parser("show", help="Show one recorded trace as an indented event tree.")
    show.add_argument("trace_id", help="ID of the trace to show.")
    show.add_argument("--path", help="Trace JSONL file to read (default: .bir/traces.jsonl).")
    show.add_argument(
        "--skip-invalid",
        action="store_true",
        help=(
            "Skip lines that cannot be read instead of refusing the whole store, reporting how many were skipped. Use after an interrupted write left a damaged line."
        ),
    )
    show.add_argument(
        "--include-rotated",
        action="store_true",
        help="Also read size-rotated trace files (oldest first) alongside the active file.",
    )
    show.add_argument("--json", action="store_true", help="Emit a nested JSON tree instead of an indented tree.")
    show.set_defaults(func=handlers["show"])

    stats = subparsers.add_parser(
        "stats",
        help="Summarize local traces: counts, token usage, cost, and latency.",
    )
    stats.add_argument("--path", help="Trace JSONL file to read (default: .bir/traces.jsonl).")
    stats.add_argument(
        "--skip-invalid",
        action="store_true",
        help=(
            "Skip lines that cannot be read instead of refusing the whole store, reporting how many were skipped. Use after an interrupted write left a damaged line."
        ),
    )
    stats.add_argument(
        "--include-rotated",
        action="store_true",
        help="Also read size-rotated trace files (oldest first) alongside the active file.",
    )
    stats.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of a table.")
    stats.add_argument(
        "--name",
        metavar="SUBSTRING",
        help="Only list traces whose name contains this case-sensitive substring.",
    )
    stats.add_argument(
        "--status",
        choices=("success", "error"),
        help="Only list traces with this status.",
    )
    stats.add_argument(
        "--since",
        type=_iso_datetime,
        metavar="ISO",
        help=("Only list traces whose start time is at or after this ISO datetime (naive values are treated as UTC)."),
    )
    stats.add_argument(
        "--until",
        type=_iso_datetime,
        metavar="ISO",
        help=("Only list traces whose start time is at or before this ISO datetime (naive values are treated as UTC)."),
    )
    stats.set_defaults(func=handlers["stats"])

    tail = subparsers.add_parser("tail", help="Follow the trace file and print new events as they are written.")
    tail.add_argument("--path", help="Trace JSONL file to follow (default: .bir/traces.jsonl).")
    tail.set_defaults(func=handlers["tail"])

    experiments = subparsers.add_parser("experiments", help="List local experiments.")
    experiments.add_argument(
        "--dir",
        dest="directory",
        help=f"Experiments directory to read (default: {default_experiment_dir}).",
    )
    experiments.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of a table.")
    experiments.set_defaults(func=handlers["experiments"])

    experiment_show = subparsers.add_parser(
        "experiment-show",
        help="Show one experiment's summary and per-example results.",
    )
    experiment_show.add_argument("experiment_id", help="ID of the experiment to show.")
    experiment_show.add_argument(
        "--dir",
        dest="directory",
        help=f"Experiments directory to read (default: {default_experiment_dir}).",
    )
    experiment_show.add_argument("--json", action="store_true", help="Emit a nested JSON object instead of a table.")
    experiment_show.set_defaults(func=handlers["experiment_show"])

    experiment_report = subparsers.add_parser(
        "experiment-report",
        help="Render one experiment to a self-contained HTML or Markdown report file.",
    )
    experiment_report.add_argument("experiment_id", help="ID of the experiment to report on.")
    experiment_report.add_argument(
        "--dir",
        dest="directory",
        help=f"Experiments directory to read (default: {default_experiment_dir}).",
    )
    experiment_report.add_argument(
        "--format",
        dest="report_format",
        choices=report_formats,
        default="html",
        help="Report format (default: html).",
    )
    experiment_report.add_argument(
        "--output",
        metavar="PATH",
        help="Write the report to PATH instead of stdout.",
    )
    experiment_report.set_defaults(func=handlers["experiment_report"])

    send = subparsers.add_parser("send", help="Send local events to a Bir server.")
    send.add_argument("--path", help="Trace JSONL file to send (default: .bir/traces.jsonl).")
    send.add_argument("--server", default=default_server, help=f"Bir server URL (default: {default_server}).")
    send.add_argument(
        "--include-rotated",
        action="store_true",
        help="Also send size-rotated trace files (oldest first) alongside the active file.",
    )
    send.add_argument(
        "--mark-sent",
        action="store_true",
        help=(
            "Record accepted event IDs in a <trace_path>.sent sidecar and skip them on later "
            "sends, for cheap idempotent re-sends."
        ),
    )
    send.add_argument(
        "--batch-size",
        type=_positive_int,
        metavar="N",
        help="Use disk-backed upload preparation and send at most N events per request group.",
    )
    send.add_argument(
        "--retries",
        type=_non_negative_int,
        default=2,
        metavar="N",
        help="Retry transient send failures (network errors, timeouts, HTTP 5xx) up to N times (default: 2).",
    )
    send.add_argument(
        "--backoff",
        type=_non_negative_float,
        default=0.5,
        metavar="SECONDS",
        help="Base seconds for exponential backoff between retries; the delay is backoff * 2**attempt (default: 0.5).",
    )
    send.add_argument(
        "--timeout",
        type=_non_negative_float,
        metavar="SECONDS",
        help="Per-request HTTP timeout in seconds for each send (default: 10).",
    )
    send.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of a summary line.")
    send.set_defaults(func=handlers["send"])

    send_experiment_parser = subparsers.add_parser(
        "send-experiment",
        help="Send a saved experiment and its summary to a Bir server.",
    )
    send_experiment_parser.add_argument("path", help="Experiment result JSONL file to send.")
    send_experiment_parser.add_argument(
        "--server",
        default=default_server,
        help=f"Bir server URL (default: {default_server}).",
    )
    send_experiment_parser.add_argument(
        "--retries",
        type=_non_negative_int,
        default=2,
        metavar="N",
        help="Retry transient send failures (network errors, timeouts, HTTP 5xx) up to N times (default: 2).",
    )
    send_experiment_parser.add_argument(
        "--backoff",
        type=_non_negative_float,
        default=0.5,
        metavar="SECONDS",
        help="Base seconds for exponential backoff between retries; the delay is backoff * 2**attempt (default: 0.5).",
    )
    send_experiment_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a summary line.",
    )
    send_experiment_parser.set_defaults(func=handlers["send_experiment"])

    eval_gate = subparsers.add_parser(
        "eval-gate",
        help="Compare two experiments and fail if an aggregate score regressed.",
    )
    eval_gate.add_argument("baseline", help="Baseline experiment result JSONL file.")
    eval_gate.add_argument("candidate", help="Candidate experiment result JSONL file.")
    eval_gate.add_argument(
        "--tolerance",
        type=float,
        default=0.0,
        help="Maximum aggregate-score change treated as unchanged (default: 0).",
    )
    eval_gate.add_argument(
        "--score-tolerance",
        dest="score_tolerances",
        action="append",
        metavar="NAME=VALUE",
        type=_score_tolerance_assignment,
        help=(
            "Override --tolerance for one shared evaluator; repeatable. VALUE is a "
            "non-negative, finite number. Repeating a NAME with the same value is "
            "allowed; conflicting values are rejected."
        ),
    )
    eval_gate.add_argument(
        "--missing-score",
        choices=missing_score_policies,
        default="ignore",
        help=(
            "Policy for evaluators present only in the baseline: 'ignore' reports "
            "them without failing (default), 'regress' treats them as regressions."
        ),
    )
    eval_gate.add_argument(
        "--per-example",
        dest="per_example",
        action="store_true",
        help=(
            "Add per-example detail under 'example_deltas': for each shared "
            "evaluator, the candidate-minus-baseline delta of every example_id "
            "scored in both runs. Reporting only; does not change the gate result."
        ),
    )
    eval_gate.set_defaults(func=handlers["eval_gate"])

    export_otel = subparsers.add_parser(
        "export-otel",
        help="Export local traces to an OTLP endpoint (requires the 'otel' extra).",
    )
    export_otel.add_argument("--path", help="Trace JSONL file to read (default: .bir/traces.jsonl).")
    export_otel.add_argument(
        "--skip-invalid",
        action="store_true",
        help=(
            "Skip lines that cannot be read instead of refusing the whole store, reporting how many were skipped. Use after an interrupted write left a damaged line."
        ),
    )
    export_otel.add_argument(
        "--include-rotated",
        action="store_true",
        help="Also read size-rotated trace files (oldest first) alongside the active file.",
    )
    export_otel.add_argument(
        "--endpoint",
        required=True,
        help="OTLP/HTTP traces endpoint, e.g. http://localhost:4318/v1/traces.",
    )
    export_otel.add_argument(
        "--header",
        dest="headers",
        action="append",
        metavar="KEY=VALUE",
        type=_header_assignment,
        help="Add an OTLP request header (e.g. backend auth); repeatable. KEY must be non-empty.",
    )
    export_otel.add_argument(
        "--service-name",
        default="bir",
        help="service.name recorded on exported spans (default: bir).",
    )
    export_otel.add_argument(
        "--environment",
        help=(
            "deployment.environment recorded on the exported Resource; overrides any "
            "environment recorded in the traces (default: derived from the traces)."
        ),
    )
    export_otel.add_argument(
        "--timeout",
        type=_non_negative_float,
        metavar="SECONDS",
        help="Per-export timeout in seconds forwarded to the OTLP exporter.",
    )
    export_otel.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a summary line.",
    )
    export_otel.set_defaults(func=handlers["export_otel"])

    prune = subparsers.add_parser(
        "prune",
        help="Remove old or unwanted whole traces from the local store (destructive; safe by default).",
    )
    prune.add_argument("--path", help="Trace JSONL file to prune (default: .bir/traces.jsonl).")
    prune.add_argument(
        "--include-rotated",
        action="store_true",
        help="Also prune size-rotated trace files (oldest first) alongside the active file.",
    )
    prune.add_argument(
        "--before",
        type=_iso_datetime,
        metavar="ISO",
        help=("Remove traces whose start time is before this ISO datetime (naive values are treated as UTC)."),
    )
    prune.add_argument(
        "--keep-last",
        dest="keep_last",
        type=_positive_int,
        metavar="N",
        help="Remove all but the N most recent traces (by start time).",
    )
    prune.add_argument(
        "--status",
        choices=("success", "error"),
        help="Restrict removal to traces with this status.",
    )
    prune.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be removed without writing (this is the default without --yes).",
    )
    prune.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete the selected traces; without it (or with --dry-run) prune only previews.",
    )
    prune.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a summary line.",
    )
    prune.set_defaults(func=handlers["prune"])

    config = subparsers.add_parser(
        "config",
        help="Print the effective resolved SDK configuration (read-only).",
    )
    config.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a table.",
    )
    config.set_defaults(func=handlers["config"])

    return parser


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}") from None
    if number <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")
    return number


def _non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a non-negative integer, got {value!r}") from None
    if number < 0:
        raise argparse.ArgumentTypeError(f"expected a non-negative integer, got {value!r}")
    return number


def _non_negative_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a non-negative number, got {value!r}") from None
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError(f"expected a non-negative number, got {value!r}")
    return number


def _iso_datetime(value: str) -> datetime:
    """Parse an ISO 8601 datetime for the ``--since``/``--until`` trace filters.

    Accepts whatever ``datetime.fromisoformat`` does (a bare date, a full timestamp,
    with or without an offset); malformed input fails as a clear argparse error.
    """

    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an ISO 8601 datetime, got {value!r}") from None


def _header_assignment(value: str) -> tuple[str, str]:
    """Parse a ``--header KEY=VALUE`` assignment into a (key, value) pair.

    Only the first ``=`` separates the key from the value, so a value may itself
    contain ``=`` and may be empty; the key must be non-empty. Repeated keys are
    folded later with the last value winning (standard header override).
    """

    key, separator, header_value = value.partition("=")
    if not separator or not key:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE with a non-empty key, got {value!r}")
    return key, header_value


def _score_tolerance_assignment(value: str) -> tuple[str, float]:
    """Parse a ``--score-tolerance NAME=VALUE`` assignment into a (name, value) pair."""

    name, separator, raw_value = value.partition("=")
    if not separator or not name:
        raise argparse.ArgumentTypeError(f"expected NAME=VALUE with a non-empty name, got {value!r}")
    try:
        number = float(raw_value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a numeric tolerance in NAME=VALUE, got {value!r}") from None
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError(f"expected a non-negative, finite tolerance, got {value!r}")
    return name, number
