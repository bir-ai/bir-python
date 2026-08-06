"""Stdlib-only command-line interface for the Bir SDK.

``bir`` is installed as a console script (see ``[project.scripts]`` in
``pyproject.toml``) and inspects local traces and experiments and uploads them to
a Bir server. It only builds on the existing public API and the standard library,
so installing the SDK never pulls in CLI-only dependencies.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from . import __version__, _sdk
from ._cli_parser import build_parser as _build_cli_parser
from ._cli_present import (
    _children_by_parent_id,
    _config_rows,
    _dump_json,
    _event_tree_to_dict,
    _experiment_detail_to_dict,
    _experiment_to_dict,
    _format_event_line,
    _format_ms,
    _format_scores,
    _format_tail_line,
    _print_experiment_detail,
    _print_table,
    _stats_rows,
    _walk_event_tree,
)
from ._sdk import (
    TraceEvent,
    _iter_trace_events,
    _prune_trace_store,
    send_events,
)
from .evals import (
    _MISSING_SCORE_POLICIES,  # shared missing-score vocabulary
    _REPORT_FORMATS,  # shared report-format vocabulary
    ExperimentSummary,
    _list_experiments_skipping_invalid,
    compare_experiments,
    list_experiments,
    load_experiment,
    render_experiment_report,
    send_experiment,
)

_DEFAULT_SERVER = "http://127.0.0.1:8000"
_DEFAULT_EXPERIMENT_DIR = ".bir/experiments"
_TAIL_POLL_INTERVAL = 0.5

# The ``BIR_*`` environment variables the SDK reads at import time (see
# ``_sdk._config_from_env``). ``bir config`` reports which of these are currently
# set so a deployment can confirm what is influencing configuration, without ever
# echoing their values.
_BIR_ENV_VARS = (
    "BIR_TRACE_PATH",
    "BIR_CAPTURE_INPUTS",
    "BIR_CAPTURE_OUTPUTS",
    "BIR_DISABLED",
    "BIR_SAMPLE_RATE",
    "BIR_SERVICE_NAME",
    "BIR_ENVIRONMENT",
    "BIR_SOURCE",
    "BIR_MAX_VALUE_LENGTH",
    "BIR_MAX_COLLECTION_ITEMS",
)


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
    return _build_cli_parser(
        version=__version__,
        default_server=_DEFAULT_SERVER,
        default_experiment_dir=_DEFAULT_EXPERIMENT_DIR,
        report_formats=_REPORT_FORMATS,
        missing_score_policies=_MISSING_SCORE_POLICIES,
        handlers={
            "traces": _cmd_traces,
            "show": _cmd_show,
            "stats": _cmd_stats,
            "tail": _cmd_tail,
            "experiments": _cmd_experiments,
            "experiment_show": _cmd_experiment_show,
            "experiment_report": _cmd_experiment_report,
            "send": _cmd_send,
            "send_experiment": _cmd_send_experiment,
            "eval_gate": _cmd_eval_gate,
            "export_otel": _cmd_export_otel,
            "prune": _cmd_prune,
            "config": _cmd_config,
        },
    )


class _SkippedLines:
    """Collects the lines a lenient read could not parse, for one report.

    Messages are de-duplicated because a command may read the store more than
    once — ``stats`` loads traces and events separately — and one damaged line
    is one damaged line however many passes see it.
    """

    def __init__(self) -> None:
        self._messages: dict[str, None] = {}

    def __call__(self, error: ValueError) -> None:
        self._messages.setdefault(str(error), None)

    def report(self) -> None:
        """Tell the user the view is partial, and why."""

        if not self._messages:
            return
        count = len(self._messages)
        noun = "line" if count == 1 else "lines"
        first = next(iter(self._messages))
        # stderr, so a --json run still writes only JSON to stdout.
        print(f"bir: skipped {count} unreadable {noun}; first: {first}", file=sys.stderr)


class _SkippedSummaries:
    """Collects the experiment summaries a lenient listing could not read.

    The trace-store counterpart counts lines; this counts files, because an
    experiment summary is one JSON document per experiment rather than a line in
    a shared store. Messages are de-duplicated for the same reason: a command may
    list the directory more than once.
    """

    def __init__(self) -> None:
        self._messages: dict[str, None] = {}

    def __call__(self, error: ValueError) -> None:
        self._messages.setdefault(str(error), None)

    def report(self) -> None:
        """Tell the user the listing is partial, and why."""

        if not self._messages:
            return
        count = len(self._messages)
        noun = "summary" if count == 1 else "summaries"
        first = next(iter(self._messages))
        # stderr, so a --json run still writes only JSON to stdout.
        print(f"bir: skipped {count} unreadable experiment {noun}; first: {first}", file=sys.stderr)


@dataclass(frozen=True)
class _TraceSummary:
    """One trace's display figures, kept without retaining its events.

    ``traces`` and ``stats`` need a trace's name, timing, status, how many events
    it holds, and what its generations spent — never the events themselves. A
    summary is a few scalars, where the events behind it carry captured inputs,
    outputs, metadata, and a copy of their own raw payload, so summarizing while
    streaming is what keeps a large store from being read into memory whole.
    """

    id: str
    name: str
    start_time: str
    end_time: str
    status: str
    event_count: int
    usage: dict[str, int | float]
    costs: dict[str, dict[str, int | float]]

    @property
    def duration_ms(self) -> float:
        return _sdk._duration_ms(self.start_time, self.end_time)


@dataclass
class _StoreSummary:
    """One streaming pass over a store: per-trace figures and store-wide totals.

    ``totals`` cover every generation event, including those whose trace root is
    missing (a trace split across an unread rotated file). Unfiltered ``stats``
    reports those totals, and a filtered run sums the surviving summaries
    instead, which is the distinction the pre-streaming implementation drew by
    aggregating either the whole event list or a filtered copy of it.
    """

    traces: list[_TraceSummary]
    totals: _UsageTotals


@dataclass
class _UsageTotals:
    """Token and cost sums accumulated while streaming."""

    input_tokens: int | float = 0
    output_tokens: int | float = 0
    total_tokens: int | float = 0
    costs: dict[str, dict[str, int | float]] = field(default_factory=dict)

    def add(self, event: TraceEvent) -> None:
        if event.type != "generation":
            return
        if event.usage:
            self.input_tokens += event.usage.get("input_tokens", 0)
            self.output_tokens += event.usage.get("output_tokens", 0)
            self.total_tokens += event.usage.get("total_tokens", 0)
        if event.cost:
            # Fall back to the SDK's default currency so a cost recorded without
            # an explicit code still lands in its own bucket rather than nowhere.
            currency = event.currency or "USD"
            bucket = self.costs.setdefault(currency, {"input_cost": 0, "output_cost": 0, "total_cost": 0})
            bucket["input_cost"] += event.cost.get("input_cost", 0)
            bucket["output_cost"] += event.cost.get("output_cost", 0)
            bucket["total_cost"] += event.cost.get("total_cost", 0)

    def usage(self) -> dict[str, int | float]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


def _trace_summary_to_dict(trace: _TraceSummary) -> dict[str, Any]:
    """Render one summary in the same shape ``_trace_to_dict`` emits."""

    return {
        "id": trace.id,
        "name": trace.name,
        "status": trace.status,
        "start_time": trace.start_time,
        "duration_ms": trace.duration_ms,
        "event_count": trace.event_count,
    }


def _report_rootless_events(counts: dict[str, int], roots: dict[str, Any]) -> None:
    """Say how much of the store is not reachable as a trace, and why.

    A trace is built from its root event, so events whose root is absent belong
    to no trace and are dropped by every trace-shaped read. That is invisible
    otherwise: the store holds the events, ``bir traces`` prints "No traces
    found", and nothing says the two facts are related.

    A root goes missing when the process died before the trace closed, when
    rotation dropped the file the root was written to, or when a framework bridge
    never got the terminal callback that would have written it. All three leave
    the same shape, so the report names the shape rather than guessing the cause.
    """

    rootless = [trace_id for trace_id in counts if trace_id not in roots]
    if not rootless:
        return
    events = sum(counts[trace_id] for trace_id in rootless)
    subject = "1 event" if events == 1 else f"{events} events"
    verb, listed = ("has", "is") if events == 1 else ("have", "are")
    trace_noun = "trace" if len(rootless) == 1 else "traces"
    # stderr, so a --json run still writes only JSON to stdout.
    print(
        f"bir: {subject} across {len(rootless)} {trace_noun} {verb} no trace root and {listed} not listed; "
        f"first trace id: {rootless[0]}",
        file=sys.stderr,
    )


def _summarize_store(args: argparse.Namespace) -> _StoreSummary:
    """Summarize the store in one pass, retaining nothing per event.

    Peak memory scales with the number of traces rather than the number of
    events or the size of what they captured.
    """

    accumulated: dict[str, _UsageTotals] = {}
    counts: dict[str, int] = {}
    roots: dict[str, tuple[str, str, str, str]] = {}
    totals = _UsageTotals()

    for event in _iter_store_events(args):
        counts[event.trace_id] = counts.get(event.trace_id, 0) + 1
        totals.add(event)
        if event.type == "generation" and (event.usage or event.cost):
            accumulated.setdefault(event.trace_id, _UsageTotals()).add(event)
        if event.type == "trace" and event.id == event.trace_id:
            roots[event.trace_id] = (event.name, event.start_time, event.end_time, event.status)

    _report_rootless_events(counts, roots)

    summaries = [
        _TraceSummary(
            id=trace_id,
            name=name,
            start_time=start_time,
            end_time=end_time,
            status=status,
            event_count=counts[trace_id],
            usage=accumulated[trace_id].usage() if trace_id in accumulated else _UsageTotals().usage(),
            costs=accumulated[trace_id].costs if trace_id in accumulated else {},
        )
        for trace_id, (name, start_time, end_time, status) in roots.items()
    ]
    # Match the grouped loader's ordering so the two paths agree before any
    # command applies its own sort.
    summaries.sort(key=lambda summary: summary.start_time)
    return _StoreSummary(traces=summaries, totals=totals)


def _iter_store_events(args: argparse.Namespace) -> Iterator[TraceEvent]:
    """Stream the store for a display command, honoring ``--skip-invalid``."""

    if not getattr(args, "skip_invalid", False):
        yield from _iter_trace_events(args.path, include_rotated=args.include_rotated)
        return

    skipped = _SkippedLines()
    yield from _iter_trace_events(args.path, include_rotated=args.include_rotated, on_invalid=skipped)
    skipped.report()


def _cmd_traces(args: argparse.Namespace) -> int:
    traces = sorted(
        _summarize_store(args).traces,
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
        _dump_json([_trace_summary_to_dict(trace) for trace in traces], sys.stdout)
        return 0

    if not traces:
        print(f"No traces found in {_resolved_trace_path(args.path)}.")
        return 0

    rows = [
        (
            trace.start_time,
            trace.status,
            _format_ms(trace.duration_ms),
            str(trace.event_count),
            trace.name,
        )
        for trace in traces
    ]
    _print_table(("START", "STATUS", "DURATION", "EVENTS", "NAME"), rows, sys.stdout)
    return 0


def _filter_traces(
    traces: list[_TraceSummary],
    *,
    name: str | None,
    status: str | None,
    since: datetime | None,
    until: datetime | None,
) -> list[_TraceSummary]:
    """Keep traces matching every supplied filter, preserving order.

    ``name`` matches a case-sensitive substring of the trace name; ``status``
    matches it exactly; ``since``/``until`` are inclusive bounds compared against the
    trace ``start_time``. Absent filters match everything and the supplied filters
    combine with AND. ``start_time`` and the bounds are normalized to UTC so naive and
    offset-aware inputs compare consistently.
    """

    since = _as_aware_utc(since) if since is not None else None
    until = _as_aware_utc(until) if until is not None else None

    filtered: list[_TraceSummary] = []
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
    # Only one trace is printed, so only its events are kept: the rest of the
    # store streams past without being retained.
    wanted = [event for event in _iter_store_events(args) if event.trace_id == args.trace_id]
    trace = next(iter(_sdk._traces_from_events(wanted)), None)
    if trace is None:
        if wanted:
            # The events are there; what is missing is the root that makes them a
            # trace. Saying "not found" would send someone looking for the wrong
            # thing, so name what was found and what it is missing.
            noun = "event" if len(wanted) == 1 else "events"
            print(
                f"bir: trace {args.trace_id!r} has {len(wanted)} recorded {noun} but no trace root, "
                "so it cannot be shown as a tree",
                file=sys.stderr,
            )
            return 1
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


def _cmd_stats(args: argparse.Namespace) -> int:
    summary = _summarize_store(args)
    traces = summary.traces
    totals = summary.totals

    if any(value is not None for value in (args.name, args.status, args.since, args.until)):
        traces = _filter_traces(
            traces,
            name=args.name,
            status=args.status,
            since=args.since,
            until=args.until,
        )
        # Restrict token/cost figures to the surviving traces. This branch only
        # runs when a filter was given, so the no-filter path keeps reporting the
        # store-wide totals (including events whose trace root is absent, e.g.
        # split across an unread rotated file) exactly as before.
        totals = _totals_for(traces)

    stats = _aggregate_stats(traces, totals)

    if args.json:
        _dump_json(stats, sys.stdout)
        return 0

    _print_table(("METRIC", "VALUE"), _stats_rows(stats), sys.stdout)
    return 0


def _totals_for(traces: list[_TraceSummary]) -> _UsageTotals:
    """Sum the token and cost figures the given traces accumulated."""

    totals = _UsageTotals()
    for trace in traces:
        totals.input_tokens += trace.usage["input_tokens"]
        totals.output_tokens += trace.usage["output_tokens"]
        totals.total_tokens += trace.usage["total_tokens"]
        for currency, amounts in trace.costs.items():
            bucket = totals.costs.setdefault(currency, {"input_cost": 0, "output_cost": 0, "total_cost": 0})
            for field_name, amount in amounts.items():
                bucket[field_name] += amount
    return totals


def _aggregate_stats(traces: list[_TraceSummary], totals: _UsageTotals) -> dict[str, Any]:
    """Summarize traces and totals into a JSON-serializable figures mapping.

    Trace-level figures (counts and latency) come from ``traces``; token and cost
    figures come from ``totals``, accumulated while streaming. Costs are grouped
    by currency code and never summed across currencies, so a store mixing USD
    and EUR reports one line each. Latency mean and p95 are ``None`` when there
    are no traces. The same mapping backs both the table and ``--json`` so their
    figures cannot drift apart.
    """

    success = sum(1 for trace in traces if trace.status == "success")
    error = sum(1 for trace in traces if trace.status == "error")

    input_tokens = totals.input_tokens
    output_tokens = totals.output_tokens
    total_tokens = totals.total_tokens
    costs = totals.costs

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


def _read_experiments(args: argparse.Namespace) -> list[ExperimentSummary]:
    """List experiment summaries, honoring ``--skip-invalid``.

    One damaged summary used to raise for the whole directory, which took every
    intact experiment beside it with it. The flag reads past it and says what it
    skipped, exactly as it does for a damaged trace store.
    """

    directory = args.directory or _DEFAULT_EXPERIMENT_DIR
    if not getattr(args, "skip_invalid", False):
        return list_experiments(directory)

    skipped = _SkippedSummaries()
    summaries = _list_experiments_skipping_invalid(directory, on_invalid=skipped)
    skipped.report()
    return summaries


def _cmd_experiments(args: argparse.Namespace) -> int:
    summaries = _read_experiments(args)

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
        (candidate for candidate in _read_experiments(args) if candidate.experiment_id == args.experiment_id),
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
        (candidate for candidate in _read_experiments(args) if candidate.experiment_id == args.experiment_id),
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


def _cmd_send(args: argparse.Namespace) -> int:
    # Only forward --timeout when given so the library's default (10.0) applies otherwise.
    timeout_kwargs = {} if args.timeout is None else {"timeout": args.timeout}
    result = send_events(
        args.server,
        path=args.path,
        include_rotated=args.include_rotated,
        mark_sent=args.mark_sent,
        batch_size=args.batch_size,
        retries=args.retries,
        backoff=args.backoff,
        **timeout_kwargs,
    )
    if args.json:
        # Mirrors SendEventsResult so the CLI and the library report a send the
        # same way; the accepted event ids stay out, since a pipeline wanting
        # them should read the store rather than a command's stdout.
        _dump_json(
            {"accepted": result.accepted, "attempted": result.attempted, "skipped": result.skipped},
            sys.stdout,
        )
        return 0

    print(f"accepted={result.accepted} attempted={result.attempted} skipped={result.skipped}")
    return 0


def _cmd_send_experiment(args: argparse.Namespace) -> int:
    result = send_experiment(args.path, args.server, retries=args.retries, backoff=args.backoff)
    if args.json:
        _dump_json({"accepted": result.accepted, "experiment_id": result.experiment_id}, sys.stdout)
        return 0

    print(f"accepted={result.accepted} id={result.experiment_id}")
    return 0


def _cmd_export_otel(args: argparse.Namespace) -> int:
    headers = dict(args.headers) if args.headers else None
    # The store is handed over as a path rather than loaded here, so the exporter
    # reads it in two streaming passes and holds one trace at a time. The skipped
    # report is de-duplicated, so seeing a damaged line once per pass says it once.
    skipped = _SkippedLines() if getattr(args, "skip_invalid", False) else None
    try:
        # Imported lazily so the CLI keeps importing without the optional 'otel'
        # extra: the otel module itself imports cleanly (its opentelemetry imports
        # are deferred), so a missing extra surfaces as an ImportError from the
        # export call below rather than at CLI import time.
        from .integrations.otel import _export_traces

        counts = _export_traces(
            _resolved_trace_path(args.path),
            endpoint=args.endpoint,
            service_name=args.service_name,
            environment=args.environment,
            headers=headers,
            timeout=args.timeout,
            include_rotated=args.include_rotated,
            on_invalid=skipped,
        )
    except ImportError as exc:
        print(f"bir: {exc}", file=sys.stderr)
        return 1
    if skipped is not None:
        skipped.report()
    if args.json:
        _dump_json({"traces": counts.traces, "spans": counts.spans, "endpoint": args.endpoint}, sys.stdout)
        return 0

    print(f"exported {counts.traces} trace(s) ({counts.spans} spans) to {args.endpoint}")
    return 0


def _cmd_prune(args: argparse.Namespace) -> int:
    # Safe by default twice over: a bare ``bir prune`` with no selection filter is
    # rejected so the store can never be wiped by accident, and even with a filter
    # nothing is written unless ``--yes`` is given (``--dry-run`` always wins,
    # forcing a preview), so a confirmation-free run only reports what it would do.
    if args.before is None and args.keep_last is None and args.status is None:
        print(
            "bir: prune requires at least one selection filter (--before, --keep-last, or --status)",
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
    if args.json:
        # ``dry_run`` is a field rather than a suffix on a sentence, so a script
        # can tell a preview from a write without matching English.
        _dump_json(
            {
                "removed_traces": result.removed_traces,
                "kept_traces": result.kept_traces,
                "removed_events": result.removed_events,
                "bytes_reclaimed": result.bytes_reclaimed,
                "dry_run": result.dry_run,
            },
            sys.stdout,
        )
        return 0

    summary = (
        f"removed={result.removed_traces} kept={result.kept_traces} "
        f"events={result.removed_events} bytes={result.bytes_reclaimed}"
    )
    if result.dry_run:
        summary += " (dry run; pass --yes to apply)"
    print(summary)
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    summary = _config_summary(_sdk._config)
    if args.json:
        _dump_json(summary, sys.stdout)
        return 0
    _print_table(("SETTING", "VALUE"), _config_rows(summary), sys.stdout)
    return 0


def _config_summary(config: _sdk._Config) -> dict[str, Any]:
    """Summarize the active config into a JSON-serializable, non-leaky mapping.

    The same mapping backs both the human-readable table and ``--json`` so their
    fields cannot drift apart. Secret-bearing configuration is reduced to counts
    only: the additional redaction rules and the local ``model_prices`` table are
    reported as sizes, never as patterns or prices, so a value that could leak a
    credential or a private rate is never printed. ``trace_path`` is resolved to an
    absolute path the way a write would, and ``env_vars_set`` lists the ``BIR_*``
    variable names currently set (to a non-blank value) without their values.
    """

    return {
        "trace_path": str(config.trace_path.resolve()),
        "capture_inputs": config.capture_inputs,
        "capture_outputs": config.capture_outputs,
        "enabled": config.enabled,
        "sample_rate": config.sample_rate,
        "sample_rules": {name: rate for name, rate in config.sample_rules},
        "service_name": config.service_name,
        "environment": config.environment,
        "source": config.source,
        "max_bytes": config.max_bytes,
        "backup_count": config.backup_count,
        "max_value_length": config.max_value_length,
        "max_collection_items": config.max_collection_items,
        "additional_secret_keys": len(config.additional_secret_keys),
        "additional_redaction_patterns": len(config.additional_redaction_patterns),
        "model_prices": len(config.model_prices),
        "env_vars_set": _bir_env_vars_set(),
    }


def _bir_env_vars_set() -> list[str]:
    """Return the ``BIR_*`` variable names currently set to a non-blank value, sorted.

    Only names are returned, never values, so a secret-bearing value (such as a
    custom trace path) is never echoed. The SDK's own blank-is-unset rule is reused
    so this reports exactly the variables that actually influence configuration.
    """

    return sorted(name for name in _BIR_ENV_VARS if _sdk._env_value(name) is not None)


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
            raise ValueError(f"conflicting --score-tolerance values for {name!r}: {existing} and {value}")
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


def _resolved_trace_path(path_arg: str | None) -> Path:
    """Resolve the trace path the way ``load_traces`` does, honoring configuration."""

    if path_arg is not None:
        return Path(path_arg)
    return _sdk._config.trace_path


if __name__ == "__main__":
    raise SystemExit(main())
