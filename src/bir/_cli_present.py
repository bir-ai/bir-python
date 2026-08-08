"""Pure CLI projections and rendering helpers for :mod:`bir.cli`.

This module depends only on the trace and experiment value models. It never
imports CLI orchestration, configuration state, integrations, or network code;
callers supply all data and output streams explicitly.
"""

from __future__ import annotations

import json
import re
from typing import Any, TextIO

from ._eval_models import ExperimentExampleResult, ExperimentResult, ExperimentSummary
from ._storage import LoadedTrace, TraceEvent, _event_sort_key

# C0 controls, DEL, and the C1 block. ESC is the one that matters -- it opens the
# sequences that move the cursor, clear a line, or set a colour -- but a bare
# newline or tab breaks a table row just as effectively, and the C1 block is
# treated as escape introducers by some terminals.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _children_by_parent_id(events: list[TraceEvent]) -> dict[str | None, list[TraceEvent]]:
    """Group events by ``parent_id``, ordering siblings by the SDK's event ordering."""

    children: dict[str | None, list[TraceEvent]] = {}
    for event in sorted(events, key=_event_sort_key):
        children.setdefault(event.parent_id, []).append(event)
    return children


def _walk_event_tree(root: TraceEvent, children: dict[str | None, list[TraceEvent]]) -> list[tuple[TraceEvent, int]]:
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


def _event_tree_to_dict(root: TraceEvent, children: dict[str | None, list[TraceEvent]]) -> dict[str, Any]:
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
    return "  " * depth + _visible("  ".join(parts))


def _format_usage(usage: dict[str, int | float]) -> str:
    return ", ".join(f"{key}={usage[key]}" for key in sorted(usage))


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

    # The header is printed rather than tabled, so it escapes its own fields.
    print(_visible(f"{summary.name} ({summary.experiment_id})"), file=out)
    print(
        _visible(f"status={summary.status}  examples={summary.example_count}  errors={summary.error_count}"),
        file=out,
    )
    print(_visible(f"start={summary.start_time}  end={summary.end_time}"), file=out)

    print(file=out)
    if summary.aggregate_scores:
        score_rows = [(name, f"{summary.aggregate_scores[name]:.2f}") for name in sorted(summary.aggregate_scores)]
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


def _config_rows(summary: dict[str, Any]) -> list[tuple[str, str]]:
    """Flatten the config summary into aligned ``(setting, value)`` table rows."""

    env_vars = summary["env_vars_set"]
    return [
        ("trace_path", summary["trace_path"]),
        ("capture_inputs", _format_config_bool(summary["capture_inputs"])),
        ("capture_outputs", _format_config_bool(summary["capture_outputs"])),
        ("enabled", _format_config_bool(summary["enabled"])),
        ("sample_rate", str(summary["sample_rate"])),
        ("sample_rules", _format_config_sample_rules(summary["sample_rules"])),
        ("service_name", _format_config_optional(summary["service_name"])),
        ("environment", _format_config_optional(summary["environment"])),
        ("source", _format_config_optional(summary["source"])),
        ("max_bytes", _format_config_optional(summary["max_bytes"])),
        ("backup_count", str(summary["backup_count"])),
        ("max_value_length", _format_config_optional(summary["max_value_length"])),
        ("max_collection_items", _format_config_optional(summary["max_collection_items"])),
        ("additional_secret_keys", str(summary["additional_secret_keys"])),
        ("additional_redaction_patterns", str(summary["additional_redaction_patterns"])),
        ("model_prices", str(summary["model_prices"])),
        ("env_vars_set", ", ".join(env_vars) if env_vars else "-"),
    ]


def _format_config_bool(value: bool) -> str:
    """Render a config boolean as the same ``true``/``false`` the env vars accept."""

    return "true" if value else "false"


def _format_config_optional(value: Any) -> str:
    """Render an optional config value, showing ``-`` when it is unset (``None``)."""

    return "-" if value is None else str(value)


def _format_config_sample_rules(rules: dict[str, float]) -> str:
    """Render the exact-name sample rules compactly, or ``-`` when none are set."""

    if not rules:
        return "-"
    return " ".join(f"{name}={rules[name]}" for name in sorted(rules))


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
    return _visible("  ".join(part for part in parts if part))


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


def _format_ms(value: float) -> str:
    return f"{value:.1f}ms"


def _format_scores(scores: dict[str, float]) -> str:
    if not scores:
        return "-"
    return " ".join(f"{name}={scores[name]:.2f}" for name in sorted(scores))


def _visible(text: str) -> str:
    """Escape control characters so recorded text cannot steer the terminal.

    Names, models, and captured values are data, and a name is often not a
    literal: a bridge passes the tool the model chose, and an application passes
    a route from a request. Printed as stored, a name of ``\\x1b[2K\\x1b[31m…``
    erases the row above it and repaints what follows, so a record could
    misrepresent the output of the command reading it.

    Escaping happens here, at the point of printing, and never at the point of
    recording: the stored event keeps exactly what the application passed, and
    ``--json`` still hands a parser the value as written. Escaping rather than
    stripping keeps the fact that something odd was recorded visible, which is
    what a person reading a trace wants to know.

    Almost every cell rendered is ordinary text, so the scan is skipped for it.
    ``str.isprintable`` is false for every character the pattern matches and runs
    in C, which measured 4.4x cheaper per cell than reaching for the pattern each
    time; a value it rejects for some other reason merely pays the scan it would
    have paid anyway.
    """

    if text.isprintable():
        return text
    return _CONTROL_CHARACTERS.sub(lambda match: f"\\x{ord(match.group()):02x}", text)


def _print_table(headers: tuple[str, ...], rows: list[tuple[str, ...]], out: TextIO) -> None:
    # Escaped before the widths are measured, so an escaped cell still lines up.
    headers = tuple(_visible(header) for header in headers)
    rows = [tuple(_visible(cell) for cell in row) for row in rows]
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
