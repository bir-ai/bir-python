"""Stdlib-only command-line interface for the Bir SDK.

``bir`` is installed as a console script (see ``[project.scripts]`` in
``pyproject.toml``) and inspects local traces and experiments and uploads them to
a Bir server. It only builds on the existing public API and the standard library,
so installing the SDK never pulls in CLI-only dependencies.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, TextIO

from . import __version__, _sdk
from ._sdk import LoadedTrace, load_traces, send_events
from .evals import ExperimentSummary, list_experiments, send_experiment

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
    traces.set_defaults(func=_cmd_traces)

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

    send = subparsers.add_parser("send", help="Send local events to a Bir server.")
    send.add_argument("--path", help="Trace JSONL file to send (default: .bir/traces.jsonl).")
    send.add_argument("--server", default=_DEFAULT_SERVER, help=f"Bir server URL (default: {_DEFAULT_SERVER}).")
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
    send_experiment_parser.set_defaults(func=_cmd_send_experiment)

    return parser


def _cmd_traces(args: argparse.Namespace) -> int:
    traces = list(reversed(load_traces(args.path)))
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


def _cmd_send(args: argparse.Namespace) -> int:
    result = send_events(args.server, path=args.path)
    print(f"accepted={result.accepted} attempted={result.attempted} skipped={result.skipped}")
    return 0


def _cmd_send_experiment(args: argparse.Namespace) -> int:
    result = send_experiment(args.path, args.server)
    print(f"accepted={result.accepted} id={result.experiment_id}")
    return 0


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


if __name__ == "__main__":
    raise SystemExit(main())
