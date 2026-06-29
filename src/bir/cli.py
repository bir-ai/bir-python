"""Stdlib-only command-line interface for the Bir SDK.

``bir`` is installed as a console script (see ``[project.scripts]`` in
``pyproject.toml``) and inspects local traces and experiments and uploads them to
a Bir server. It only builds on the existing public API and the standard library,
so installing the SDK never pulls in CLI-only dependencies.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from . import __version__, _sdk
from ._sdk import (
    LoadedTrace,
    TraceEvent,
    _event_sort_key,
    _prune_trace_store,
    load_events,
    load_traces,
    send_events,
)
from .evals import (
    ExperimentExampleResult,
    ExperimentResult,
    ExperimentSummary,
    compare_experiments,
    list_experiments,
    load_experiment,
    render_experiment_report,
    send_experiment,
)
from .evals import _MISSING_SCORE_POLICIES  # shared missing-score vocabulary
from .evals import _REPORT_FORMATS  # shared report-format vocabulary

_DEFAULT_SERVER = "http://127.0.0.1:8000"
_DEFAULT_EXPERIMENT_DIR = ".bir/experiments"
_TAIL_POLL_INTERVAL = 0.5


def main(argv: list[str] | None = None) -> int:
    """Run the ``bir`` command-line interface and return a process exit code."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] | None = getattr(args, "func", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return 1
    try:
        return handler(args)
    except KeyboardInterrupt:
        return 130
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"bir: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bir",
        description="Inspect local Bir traces and experiments and send them to a server.",
    )
    parser.add_argument("--version", action="version", version=f"bir {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    traces = subparsers.add_parser("traces", help="List local traces.")
    traces.add_argument("--path", help="Trace JSONL file to read (default: .bir/traces.jsonl).")
    traces.add_argument("--limit", type=_positive_int, metavar="N", help="Show at most N most recent traces.")
    traces.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of a table.")
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
        help=(
            "Only list traces whose start time is at or after this ISO datetime "
            "(naive values are treated as UTC)."
        ),
    )
    traces.add_argument(
        "--until",
        type=_iso_datetime,
        metavar="ISO",
        help=(
            "Only list traces whose start time is at or before this ISO datetime "
            "(naive values are treated as UTC)."
        ),
    )
    traces.set_defaults(func=_cmd_traces)

    show = subparsers.add_parser("show", help="Show one recorded trace as an indented event tree.")
    show.add_argument("trace_id", help="ID of the trace to show.")
    show.add_argument("--path", help="Trace JSONL file to read (default: .bir/traces.jsonl).")
    show.add_argument(
        "--include-rotated",
        action="store_true",
        help="Also read size-rotated trace files (oldest first) alongside the active file.",
    )
    show.add_argument("--json", action="store_true", help="Emit a nested JSON tree instead of an indented tree.")
    show.set_defaults(func=_cmd_show)

    stats = subparsers.add_parser(
        "stats",
        help="Summarize local traces: counts, token usage, cost, and latency.",
    )
    stats.add_argument("--path", help="Trace JSONL file to read (default: .bir/traces.jsonl).")
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
        help=(
            "Only list traces whose start time is at or after this ISO datetime "
            "(naive values are treated as UTC)."
        ),
    )
    stats.add_argument(
        "--until",
        type=_iso_datetime,
        metavar="ISO",
        help=(
            "Only list traces whose start time is at or before this ISO datetime "
            "(naive values are treated as UTC)."
        ),
    )
    stats.set_defaults(func=_cmd_stats)

    tail = subparsers.add_parser("tail", help="Follow the trace file and print new events as they are written.")
    tail.add_argument("--path", help="Trace JSONL file to follow (default: .bir/traces.jsonl).")
    tail.set_defaults(func=_cmd_tail)

    experiments = subparsers.add_parser("experiments", help="List local experiments.")
    experiments.add_argument(
        "--dir",
        dest="directory",
        help=f"Experiments directory to read (default: {_DEFAULT_EXPERIMENT_DIR}).",
    )
    experiments.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of a table.")
    experiments.set_defaults(func=_cmd_experiments)

    experiment_show = subparsers.add_parser(
        "experiment-show",
        help="Show one experiment's summary and per-example results.",
    )
    experiment_show.add_argument("experiment_id", help="ID of the experiment to show.")
    experiment_show.add_argument(
        "--dir",
        dest="directory",
        help=f"Experiments directory to read (default: {_DEFAULT_EXPERIMENT_DIR}).",
    )
    experiment_show.add_argument(
        "--json", action="store_true", help="Emit a nested JSON object instead of a table."
    )
    experiment_show.set_defaults(func=_cmd_experiment_show)

    experiment_report = subparsers.add_parser(
        "experiment-report",
        help="Render one experiment to a self-contained HTML or Markdown report file.",
    )
    experiment_report.add_argument("experiment_id", help="ID of the experiment to report on.")
    experiment_report.add_argument(
        "--dir",
        dest="directory",
        help=f"Experiments directory to read (default: {_DEFAULT_EXPERIMENT_DIR}).",
    )
    experiment_report.add_argument(
        "--format",
        dest="report_format",
        choices=_REPORT_FORMATS,
        default="html",
        help="Report format (default: html).",
    )
    experiment_report.add_argument(
        "--output",
        metavar="PATH",
        help="Write the report to PATH instead of stdout.",
    )
    experiment_report.set_defaults(func=_cmd_experiment_report)

    send = subparsers.add_parser("send", help="Send local events to a Bir server.")
    send.add_argument("--path", help="Trace JSONL file to send (default: .bir/traces.jsonl).")
    send.add_argument("--server", default=_DEFAULT_SERVER, help=f"Bir server URL (default: {_DEFAULT_SERVER}).")
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
    send.set_defaults(func=_cmd_send)

    send_experiment_parser = subparsers.add_parser(
        "send-experiment",
        help="Send a saved experiment and its summary to a Bir server.",
    )
    send_experiment_parser.add_argument("path", help="Experiment result JSONL file to send.")
    send_experiment_parser.add_argument(
        "--server",
        default=_DEFAULT_SERVER,
        help=f"Bir server URL (default: {_DEFAULT_SERVER}).",
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
    send_experiment_parser.set_defaults(func=_cmd_send_experiment)

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
        choices=_MISSING_SCORE_POLICIES,
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
    eval_gate.set_defaults(func=_cmd_eval_gate)

    export_otel = subparsers.add_parser(
        "export-otel",
        help="Export local traces to an OTLP endpoint (requires the 'otel' extra).",
    )
    export_otel.add_argument("--path", help="Trace JSONL file to read (default: .bir/traces.jsonl).")
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
        "--timeout",
        type=_non_negative_float,
        metavar="SECONDS",
        help="Per-export timeout in seconds forwarded to the OTLP exporter.",
    )
    export_otel.set_defaults(func=_cmd_export_otel)

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
        help=(
            "Remove traces whose start time is before this ISO datetime "
            "(naive values are treated as UTC)."
        ),
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
    prune.set_defaults(func=_cmd_prune)

    return parser


def _cmd_traces(args: argparse.Namespace) -> int:
    traces = sorted(
        load_traces(args.path, include_rotated=args.include_rotated),
        key=lambda trace: trace.start_time,
        reverse=True,
    )
    traces = _filter_traces(
        traces,
        name=args.name,
        status=args.status,
        since=args.since,
        until=args.until,
    )
    if args.limit is not None:
        traces = traces[: args.limit]

    if args.json:
        _dump_json([_trace_to_dict(trace) for trace in traces], sys.stdout)
        return 0

    if not traces:
        print(f"No traces found in {_resolved_trace_path(args.path)}.")
        return 0

    rows = [
        (
            trace.start_time,
            trace.status,
            _format_ms(trace.duration_ms),
            str(len(trace.events)),
            trace.name,
        )
        for trace in traces
    ]
    _print_table(("START", "STATUS", "DURATION", "EVENTS", "NAME"), rows, sys.stdout)
    return 0


def _filter_traces(
    traces: list[LoadedTrace],
    *,
    name: str | None,
    status: str | None,
    since: datetime | None,
    until: datetime | None,
) -> list[LoadedTrace]:
    """Keep traces matching every supplied filter, preserving order.

    ``name`` matches a case-sensitive substring of ``LoadedTrace.name``; ``status``
    matches it exactly; ``since``/``until`` are inclusive bounds compared against the
    trace ``start_time``. Absent filters match everything and the supplied filters
    combine with AND. ``start_time`` and the bounds are normalized to UTC so naive and
    offset-aware inputs compare consistently.
    """

    since = _as_aware_utc(since) if since is not None else None
    until = _as_aware_utc(until) if until is not None else None

    filtered: list[LoadedTrace] = []
    for trace in traces:
        if name is not None and name not in trace.name:
            continue
        if status is not None and trace.status != status:
            continue
        if since is not None or until is not None:
            start = _as_aware_utc(datetime.fromisoformat(trace.start_time))
            if since is not None and start < since:
                continue
            if until is not None and start > until:
                continue
        filtered.append(trace)
    return filtered


def _as_aware_utc(value: datetime) -> datetime:
    """Return ``value`` as a timezone-aware UTC datetime, assuming UTC when naive.

    Trace start times are recorded in UTC; treating a naive ``--since``/``--until``
    bound as UTC lets it compare against an offset-aware start time without raising.
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _cmd_show(args: argparse.Namespace) -> int:
    trace = next(
        (
            candidate
            for candidate in load_traces(args.path, include_rotated=args.include_rotated)
            if candidate.id == args.trace_id
        ),
        None,
    )
    if trace is None:
        print(
            f"bir: trace {args.trace_id!r} not found in {_resolved_trace_path(args.path)}",
            file=sys.stderr,
        )
        return 1

    children = _children_by_parent_id(trace.events)
    if args.json:
        _dump_json(_event_tree_to_dict(trace.root, children), sys.stdout)
        return 0

    for event, depth in _walk_event_tree(trace.root, children):
        print(_format_event_line(event, depth))
    return 0


def _children_by_parent_id(events: list[TraceEvent]) -> dict[str | None, list[TraceEvent]]:
    """Group events by ``parent_id``, ordering siblings by the SDK's event ordering."""

    children: dict[str | None, list[TraceEvent]] = {}
    for event in sorted(events, key=_event_sort_key):
        children.setdefault(event.parent_id, []).append(event)
    return children


def _walk_event_tree(
    root: TraceEvent, children: dict[str | None, list[TraceEvent]]
) -> list[tuple[TraceEvent, int]]:
    """Flatten the tree under ``root`` into ``(event, depth)`` pairs, parents first.

    A ``seen`` guard keeps a malformed file whose ``parent_id`` links form a cycle
    from recursing forever; each event is emitted at most once.
    """

    ordered: list[tuple[TraceEvent, int]] = []
    seen: set[str] = set()

    def visit(event: TraceEvent, depth: int) -> None:
        if event.id in seen:
            return
        seen.add(event.id)
        ordered.append((event, depth))
        for child in children.get(event.id, []):
            visit(child, depth + 1)

    visit(root, 0)
    return ordered


def _event_tree_to_dict(
    root: TraceEvent, children: dict[str | None, list[TraceEvent]]
) -> dict[str, Any]:
    """Build a nested ``{"event": ..., "children": [...]}`` mapping rooted at ``root``."""

    seen: set[str] = set()

    def build(event: TraceEvent) -> dict[str, Any]:
        seen.add(event.id)
        child_nodes: list[dict[str, Any]] = []
        for child in children.get(event.id, []):
            if child.id in seen:
                continue
            child_nodes.append(build(child))
        return {"event": _event_to_dict(event), "children": child_nodes}

    return build(root)


def _event_to_dict(event: TraceEvent) -> dict[str, Any]:
    """Represent one event with its identity and the salient extras shown in the tree."""

    payload: dict[str, Any] = {
        "id": event.id,
        "parent_id": event.parent_id,
        "type": event.type,
        "name": event.name,
        "status": event.status,
        "start_time": event.start_time,
        "end_time": event.end_time,
        "duration_ms": event.duration_ms,
    }
    if event.model is not None:
        payload["model"] = event.model
    if event.usage is not None:
        payload["usage"] = event.usage
    if event.value is not None:
        payload["value"] = event.value
    return payload


def _format_event_line(event: TraceEvent, depth: int) -> str:
    """Render one tree row: indented type/name/status/duration plus salient extras."""

    parts = [f"{event.type} {event.name} [{event.status}] {_format_ms(event.duration_ms)}"]
    if event.model is not None:
        parts.append(f"model={event.model}")
    if event.usage is not None:
        parts.append(f"usage={_format_usage(event.usage)}")
    if event.value is not None:
        parts.append(f"value={event.value}")
    return "  " * depth + "  ".join(parts)


def _format_usage(usage: dict[str, int | float]) -> str:
    return ", ".join(f"{key}={usage[key]}" for key in sorted(usage))


def _cmd_stats(args: argparse.Namespace) -> int:
    traces = load_traces(args.path, include_rotated=args.include_rotated)
    events = load_events(args.path, include_rotated=args.include_rotated)

    if any(value is not None for value in (args.name, args.status, args.since, args.until)):
        traces = _filter_traces(
            traces,
            name=args.name,
            status=args.status,
            since=args.since,
            until=args.until,
        )
        # Restrict token/cost aggregation to events belonging to the surviving
        # traces. This branch only runs when a filter was given, so the no-filter
        # path keeps aggregating the full event list (including events whose trace
        # root is absent, e.g. split across an unread rotated file) byte for byte.
        surviving_ids = {trace.id for trace in traces}
        events = [event for event in events if event.trace_id in surviving_ids]

    stats = _aggregate_stats(traces, events)

    if args.json:
        _dump_json(stats, sys.stdout)
        return 0

    _print_table(("METRIC", "VALUE"), _stats_rows(stats), sys.stdout)
    return 0


def _aggregate_stats(traces: list[LoadedTrace], events: list[TraceEvent]) -> dict[str, Any]:
    """Summarize traces and events into a JSON-serializable figures mapping.

    Trace-level figures (counts and latency) come from ``traces``; token and cost
    figures are summed over generation events in ``events``. Costs are grouped by
    currency code and never summed across currencies, so a store mixing USD and
    EUR reports one line each. Latency mean and p95 are ``None`` when there are no
    traces. The same mapping backs both the table and ``--json`` so their figures
    cannot drift apart.
    """

    success = sum(1 for trace in traces if trace.status == "success")
    error = sum(1 for trace in traces if trace.status == "error")

    input_tokens: int | float = 0
    output_tokens: int | float = 0
    total_tokens: int | float = 0
    costs: dict[str, dict[str, int | float]] = {}
    for event in events:
        if event.type != "generation":
            continue
        if event.usage:
            input_tokens += event.usage.get("input_tokens", 0)
            output_tokens += event.usage.get("output_tokens", 0)
            total_tokens += event.usage.get("total_tokens", 0)
        if event.cost:
            # Fall back to the SDK's default currency so a cost recorded without
            # an explicit code still lands in its own bucket rather than nowhere.
            currency = event.currency or "USD"
            bucket = costs.setdefault(currency, {"input_cost": 0, "output_cost": 0, "total_cost": 0})
            bucket["input_cost"] += event.cost.get("input_cost", 0)
            bucket["output_cost"] += event.cost.get("output_cost", 0)
            bucket["total_cost"] += event.cost.get("total_cost", 0)

    durations = sorted(trace.duration_ms for trace in traces)
    latency: dict[str, Any] = {
        "count": len(durations),
        "mean": (sum(durations) / len(durations)) if durations else None,
        "p95": _percentile(durations, 95) if durations else None,
    }

    return {
        "traces": {"total": len(traces), "success": success, "error": error},
        "tokens": {"input": input_tokens, "output": output_tokens, "total": total_tokens},
        "cost": [{"currency": currency, **costs[currency]} for currency in sorted(costs)],
        "latency_ms": latency,
    }


def _percentile(sorted_values: list[float], percentile: float) -> float:
    """Return the nearest-rank ``percentile`` of pre-sorted ``sorted_values``.

    Uses the nearest-rank method (sort, then index) so only the standard library
    is needed: the ordinal rank ``ceil(percentile / 100 * N)`` selects the 1-based
    position whose value is returned, clamped into range for safety. Callers pass
    a non-empty list.
    """

    size = len(sorted_values)
    rank = math.ceil(percentile / 100 * size)
    index = min(max(rank, 1), size) - 1
    return sorted_values[index]


def _stats_rows(stats: dict[str, Any]) -> list[tuple[str, str]]:
    """Flatten the stats mapping into aligned ``(metric, value)`` table rows."""

    traces = stats["traces"]
    tokens = stats["tokens"]
    latency = stats["latency_ms"]
    rows: list[tuple[str, str]] = [
        ("traces", str(traces["total"])),
        ("success", str(traces["success"])),
        ("error", str(traces["error"])),
        ("input_tokens", str(tokens["input"])),
        ("output_tokens", str(tokens["output"])),
        ("total_tokens", str(tokens["total"])),
    ]
    if stats["cost"]:
        for entry in stats["cost"]:
            value = (
                f"input={_format_cost(entry['input_cost'])} "
                f"output={_format_cost(entry['output_cost'])} "
                f"total={_format_cost(entry['total_cost'])}"
            )
            rows.append((f"cost[{entry['currency']}]", value))
    else:
        rows.append(("cost", "-"))
    rows.append(("latency_count", str(latency["count"])))
    rows.append(("latency_mean", _format_ms(latency["mean"]) if latency["mean"] is not None else "-"))
    rows.append(("latency_p95", _format_ms(latency["p95"]) if latency["p95"] is not None else "-"))
    return rows


def _format_cost(value: int | float) -> str:
    return f"{value:.6f}"


def _cmd_experiments(args: argparse.Namespace) -> int:
    summaries = list_experiments(args.directory) if args.directory else list_experiments()

    if args.json:
        _dump_json([_experiment_to_dict(summary) for summary in summaries], sys.stdout)
        return 0

    if not summaries:
        print(f"No experiments found in {args.directory or _DEFAULT_EXPERIMENT_DIR}.")
        return 0

    rows = [
        (
            summary.experiment_id,
            summary.name,
            summary.status,
            str(summary.example_count),
            str(summary.error_count),
            _format_scores(summary.aggregate_scores),
        )
        for summary in summaries
    ]
    _print_table(("ID", "NAME", "STATUS", "EXAMPLES", "ERRORS", "SCORES"), rows, sys.stdout)
    return 0


def _cmd_experiment_show(args: argparse.Namespace) -> int:
    directory = args.directory or _DEFAULT_EXPERIMENT_DIR
    summary = next(
        (
            candidate
            for candidate in list_experiments(directory)
            if candidate.experiment_id == args.experiment_id
        ),
        None,
    )
    if summary is None:
        print(
            f"bir: experiment {args.experiment_id!r} not found in {directory}",
            file=sys.stderr,
        )
        return 1

    experiment = load_experiment(_resolve_experiment_result_path(summary, directory))

    if args.json:
        _dump_json(_experiment_detail_to_dict(summary, experiment), sys.stdout)
        return 0

    _print_experiment_detail(summary, experiment, sys.stdout)
    return 0


def _cmd_experiment_report(args: argparse.Namespace) -> int:
    directory = args.directory or _DEFAULT_EXPERIMENT_DIR
    summary = next(
        (
            candidate
            for candidate in list_experiments(directory)
            if candidate.experiment_id == args.experiment_id
        ),
        None,
    )
    if summary is None:
        print(
            f"bir: experiment {args.experiment_id!r} not found in {directory}",
            file=sys.stderr,
        )
        return 1

    experiment = load_experiment(_resolve_experiment_result_path(summary, directory))
    report = render_experiment_report(experiment, format=args.report_format)

    if args.output:
        output_path = Path(args.output)
        if output_path.parent != Path("."):
            output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
        print(f"Wrote {args.report_format} report to {output_path}")
        return 0

    sys.stdout.write(report)
    return 0


def _resolve_experiment_result_path(summary: ExperimentSummary, directory: str) -> Path:
    """Resolve the result JSONL path for ``summary`` within ``directory``.

    The summary records the result path captured when the experiment ran, which
    may be absolute or relative to the run's working directory. Prefer that path
    when it still resolves, but fall back to the summary's own directory so an
    experiment directory that was moved or is read from elsewhere still loads.
    """

    result_path = Path(summary.result_path)
    if result_path.exists():
        return result_path
    return Path(directory) / result_path.name


def _experiment_detail_to_dict(summary: ExperimentSummary, experiment: ExperimentResult) -> dict[str, Any]:
    """Build the deterministic ``--json`` object backing ``experiment-show``."""

    return {
        "id": summary.experiment_id,
        "name": summary.name,
        "status": summary.status,
        "start_time": summary.start_time,
        "end_time": summary.end_time,
        "example_count": summary.example_count,
        "error_count": summary.error_count,
        "aggregate_scores": summary.aggregate_scores,
        "results": [_experiment_example_to_dict(result) for result in experiment.results],
    }


def _experiment_example_to_dict(result: ExperimentExampleResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "example_id": result.example_id,
        "status": result.status,
        "scores": {score.name: score.value for score in result.scores},
        "error": result.error,
    }
    if result.trace_id is not None:
        payload["trace_id"] = result.trace_id
    return payload


def _print_experiment_detail(summary: ExperimentSummary, experiment: ExperimentResult, out: TextIO) -> None:
    """Render the experiment header, evaluator aggregates, and per-example results."""

    print(f"{summary.name} ({summary.experiment_id})", file=out)
    print(
        f"status={summary.status}  examples={summary.example_count}  errors={summary.error_count}",
        file=out,
    )
    print(f"start={summary.start_time}  end={summary.end_time}", file=out)

    print(file=out)
    if summary.aggregate_scores:
        score_rows = [
            (name, f"{summary.aggregate_scores[name]:.2f}")
            for name in sorted(summary.aggregate_scores)
        ]
        _print_table(("EVALUATOR", "MEAN"), score_rows, out)
    else:
        print("No evaluator scores.", file=out)

    print(file=out)
    result_rows = [
        (
            result.example_id,
            result.status,
            _format_scores({score.name: score.value for score in result.scores}),
            result.error or "-",
        )
        for result in experiment.results
    ]
    _print_table(("EXAMPLE", "STATUS", "SCORES", "ERROR"), result_rows, out)


def _cmd_send(args: argparse.Namespace) -> int:
    # Only forward --timeout when given so the library's default (10.0) applies otherwise.
    timeout_kwargs = {} if args.timeout is None else {"timeout": args.timeout}
    result = send_events(
        args.server,
        path=args.path,
        include_rotated=args.include_rotated,
        mark_sent=args.mark_sent,
        retries=args.retries,
        backoff=args.backoff,
        **timeout_kwargs,
    )
    print(f"accepted={result.accepted} attempted={result.attempted} skipped={result.skipped}")
    return 0


def _cmd_send_experiment(args: argparse.Namespace) -> int:
    result = send_experiment(args.path, args.server, retries=args.retries, backoff=args.backoff)
    print(f"accepted={result.accepted} id={result.experiment_id}")
    return 0


def _cmd_export_otel(args: argparse.Namespace) -> int:
    traces = load_traces(args.path, include_rotated=args.include_rotated)
    headers = dict(args.headers) if args.headers else None
    try:
        # Imported lazily so the CLI keeps importing without the optional 'otel'
        # extra: the otel module itself imports cleanly (its opentelemetry imports
        # are deferred), so a missing extra surfaces as an ImportError from the
        # export call below rather than at CLI import time.
        from .integrations.otel import export_traces_to_otlp

        exported = export_traces_to_otlp(
            traces,
            endpoint=args.endpoint,
            service_name=args.service_name,
            headers=headers,
            timeout=args.timeout,
        )
    except ImportError as exc:
        print(f"bir: {exc}", file=sys.stderr)
        return 1
    print(f"exported {len(traces)} trace(s) ({exported} spans) to {args.endpoint}")
    return 0


def _cmd_prune(args: argparse.Namespace) -> int:
    # Safe by default twice over: a bare ``bir prune`` with no selection filter is
    # rejected so the store can never be wiped by accident, and even with a filter
    # nothing is written unless ``--yes`` is given (``--dry-run`` always wins,
    # forcing a preview), so a confirmation-free run only reports what it would do.
    if args.before is None and args.keep_last is None and args.status is None:
        print(
            "bir: prune requires at least one selection filter "
            "(--before, --keep-last, or --status)",
            file=sys.stderr,
        )
        return 1

    before = _as_aware_utc(args.before) if args.before is not None else None
    write = args.yes and not args.dry_run
    result = _prune_trace_store(
        args.path,
        include_rotated=args.include_rotated,
        before=before,
        keep_last=args.keep_last,
        status=args.status,
        dry_run=not write,
    )
    summary = (
        f"removed={result.removed_traces} kept={result.kept_traces} "
        f"events={result.removed_events} bytes={result.bytes_reclaimed}"
    )
    if result.dry_run:
        summary += " (dry run; pass --yes to apply)"
    print(summary)
    return 0


def _cmd_eval_gate(args: argparse.Namespace) -> int:
    diff = compare_experiments(
        args.baseline,
        args.candidate,
        tolerance=args.tolerance,
        score_tolerances=_collect_score_tolerances(args.score_tolerances),
        missing_score=args.missing_score,
        per_example=args.per_example,
    )
    _dump_json(diff.to_dict(), sys.stdout)
    return 1 if diff.has_regressions else 0


def _collect_score_tolerances(
    assignments: list[tuple[str, float]] | None,
) -> dict[str, float] | None:
    """Fold repeated ``--score-tolerance`` assignments into a single mapping.

    Repeating a NAME with the same value is idempotent; a NAME repeated with a
    different value is a conflict and fails clearly. Returns ``None`` when no
    overrides were supplied so the default global tolerance applies unchanged.
    """

    if not assignments:
        return None
    collected: dict[str, float] = {}
    for name, value in assignments:
        existing = collected.get(name)
        if existing is not None and existing != value:
            raise ValueError(
                f"conflicting --score-tolerance values for {name!r}: {existing} and {value}"
            )
        collected[name] = value
    return collected


def _cmd_tail(args: argparse.Namespace) -> int:
    path = _resolved_trace_path(args.path)
    print(f"Following {path} (press Ctrl-C to stop)", file=sys.stderr)
    try:
        _follow_trace(path, out=sys.stdout, poll_interval=_TAIL_POLL_INTERVAL, should_stop=lambda: False)
    except KeyboardInterrupt:
        print(file=sys.stderr)
    return 0


def _follow_trace(
    path: Path,
    *,
    out: TextIO,
    poll_interval: float,
    should_stop: Callable[[], bool],
) -> None:
    """Print trace events appended to ``path`` until ``should_stop`` returns True.

    Following starts at the current end of the file so only newly written events
    are shown, then polls for appended complete lines. ``should_stop`` is checked
    after each poll so callers (and tests) can end the loop deterministically; the
    ``tail`` command passes a predicate that never stops and relies on Ctrl-C.
    """

    offset = path.stat().st_size if path.exists() else 0
    while True:
        offset = _emit_new_events(path, offset, out)
        if should_stop():
            return
        time.sleep(poll_interval)


def _emit_new_events(path: Path, offset: int, out: TextIO) -> int:
    """Print complete event lines written past ``offset`` and return the new offset."""

    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return offset
    if size < offset:
        # The file was truncated or rotated; restart from the beginning.
        offset = 0
    if size <= offset:
        return offset

    with path.open("rb") as trace_file:
        trace_file.seek(offset)
        data = trace_file.read()

    last_newline = data.rfind(b"\n")
    if last_newline == -1:
        # Only a partial line is available; wait for it to be completed.
        return offset

    complete = data[: last_newline + 1]
    for line in complete.decode("utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        rendered = _format_tail_line(stripped)
        if rendered is not None:
            print(rendered, file=out)
    return offset + len(complete)


def _format_tail_line(line: str) -> str | None:
    """Format one raw JSON event line for ``tail`` output, or skip unparsable lines."""

    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    parts = [
        str(payload.get("start_time", "")),
        str(payload.get("type", "event")),
        str(payload.get("name", "")),
        str(payload.get("status", "")),
    ]
    if payload.get("type") == "score" and "value" in payload:
        parts.append(f"value={payload['value']}")
    return "  ".join(part for part in parts if part)


def _trace_to_dict(trace: LoadedTrace) -> dict[str, Any]:
    return {
        "id": trace.id,
        "name": trace.name,
        "status": trace.status,
        "start_time": trace.start_time,
        "duration_ms": trace.duration_ms,
        "event_count": len(trace.events),
    }


def _experiment_to_dict(summary: ExperimentSummary) -> dict[str, Any]:
    return {
        "id": summary.experiment_id,
        "name": summary.name,
        "status": summary.status,
        "example_count": summary.example_count,
        "error_count": summary.error_count,
        "aggregate_scores": summary.aggregate_scores,
    }


def _resolved_trace_path(path_arg: str | None) -> Path:
    """Resolve the trace path the way ``load_traces`` does, honoring configuration."""

    if path_arg is not None:
        return Path(path_arg)
    return _sdk._config.trace_path


def _format_ms(value: float) -> str:
    return f"{value:.1f}ms"


def _format_scores(scores: dict[str, float]) -> str:
    if not scores:
        return "-"
    return " ".join(f"{name}={scores[name]:.2f}" for name in sorted(scores))


def _print_table(headers: tuple[str, ...], rows: list[tuple[str, ...]], out: TextIO) -> None:
    columns = list(zip(*([headers, *rows]))) if rows else [(header,) for header in headers]
    widths = [max(len(cell) for cell in column) for column in columns]

    def render(cells: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths)).rstrip()

    print(render(headers), file=out)
    for row in rows:
        print(render(row), file=out)


def _dump_json(data: Any, out: TextIO) -> None:
    json.dump(data, out, indent=2, sort_keys=True)
    out.write("\n")


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


if __name__ == "__main__":
    raise SystemExit(main())
