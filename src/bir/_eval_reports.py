"""Deterministic HTML and Markdown rendering for experiment results.

This presentation-only module depends on :mod:`bir._eval_models` and the
standard library.  It never imports evaluator execution, persistence, the CLI,
or the public :mod:`bir.evals` compatibility surface.
"""

from __future__ import annotations

from html import escape as _html_escape

from ._eval_models import EvalResult, ExperimentResult

# Self-contained report formats supported by render_experiment_report() and the
# ``bir experiment-report`` CLI command.
_REPORT_FORMATS = ("html", "markdown")

_REPORT_CSS = (
    "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;"
    "margin:2rem;color:#1a1a1a;}"
    "h1{font-size:1.5rem;}"
    "h2{font-size:1.1rem;margin-top:2rem;}"
    "table{border-collapse:collapse;margin-top:0.5rem;}"
    "th,td{border:1px solid #d0d0d0;padding:0.35rem 0.6rem;text-align:left;"
    "font-size:0.9rem;vertical-align:top;}"
    "th{background:#f4f4f4;}"
    "table.meta th{width:8rem;}"
)


def render_experiment_report(result: ExperimentResult, *, format: str = "html") -> str:
    """Render one experiment to a self-contained report string.

    The report bundles the run summary, the per-evaluator aggregate means, and a
    per-example table of statuses and scores into a single string with no external
    assets, so a local-first user can share or archive an experiment result
    without standing up the server or dashboard. ``format`` selects ``"html"``
    (the default, a complete standalone HTML document with inline styles) or
    ``"markdown"``.

    Output is deterministic for a given experiment: evaluators are ordered by
    name and examples follow their persisted dataset order, so re-rendering the
    same result yields a byte-identical string. Only already-persisted (already
    redacted) values are rendered, and every experiment-derived string is escaped
    for the chosen format, so example data cannot inject markup. Built with the
    standard library only.
    """

    if format not in _REPORT_FORMATS:
        valid = ", ".join(_REPORT_FORMATS)
        raise ValueError(f"render_experiment_report format must be one of: {valid}")
    if format == "markdown":
        return _render_experiment_report_markdown(result)
    return _render_experiment_report_html(result)


def _render_experiment_report_html(result: ExperimentResult) -> str:
    """Render ``result`` as a complete standalone HTML document.

    Every experiment-derived string is passed through :func:`html.escape`, so
    example ids, evaluator names, and error text cannot inject markup.
    """

    aggregate_scores = result.aggregate_scores
    example_count = len(result.results)
    error_count = sum(1 for example in result.results if example.status == "error")
    title = f"Experiment Report: {result.name}"

    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{_html_escape(title)}</title>",
        f"<style>{_REPORT_CSS}</style>",
        "</head>",
        "<body>",
        f"<h1>{_html_escape(title)}</h1>",
        '<table class="meta">',
    ]
    for label, value in (
        ("ID", result.id),
        ("Status", result.status),
        ("Examples", str(example_count)),
        ("Errors", str(error_count)),
        ("Start", result.start_time),
        ("End", result.end_time),
    ):
        parts.append(f"<tr><th>{_html_escape(label)}</th><td>{_html_escape(value)}</td></tr>")
    parts.append("</table>")

    parts.append("<h2>Evaluator aggregates</h2>")
    if aggregate_scores:
        parts.append("<table>")
        parts.append("<thead><tr><th>Evaluator</th><th>Mean</th></tr></thead>")
        parts.append("<tbody>")
        for name in sorted(aggregate_scores):
            mean = _format_report_score(aggregate_scores[name])
            parts.append(f"<tr><td>{_html_escape(name)}</td><td>{_html_escape(mean)}</td></tr>")
        parts.append("</tbody>")
        parts.append("</table>")
    else:
        parts.append("<p>No evaluator scores.</p>")

    parts.append("<h2>Examples</h2>")
    parts.append("<table>")
    parts.append("<thead><tr><th>Example</th><th>Status</th><th>Scores</th><th>Error</th></tr></thead>")
    parts.append("<tbody>")
    for example in result.results:
        scores = _format_report_example_scores(example.scores)
        error = example.error or "-"
        parts.append(
            "<tr>"
            f"<td>{_html_escape(example.example_id)}</td>"
            f"<td>{_html_escape(example.status)}</td>"
            f"<td>{_html_escape(scores)}</td>"
            f"<td>{_html_escape(error)}</td>"
            "</tr>"
        )
    parts.append("</tbody>")
    parts.append("</table>")
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts) + "\n"


def _render_experiment_report_markdown(result: ExperimentResult) -> str:
    """Render ``result`` as a self-contained Markdown document.

    Table cells escape the pipe separator and collapse newlines so example text
    cannot break the table structure.
    """

    aggregate_scores = result.aggregate_scores
    example_count = len(result.results)
    error_count = sum(1 for example in result.results if example.status == "error")

    lines: list[str] = [
        f"# Experiment Report: {_markdown_inline(result.name)}",
        "",
        f"- **ID:** {_markdown_inline(result.id)}",
        f"- **Status:** {_markdown_inline(result.status)}",
        f"- **Examples:** {example_count}",
        f"- **Errors:** {error_count}",
        f"- **Start:** {_markdown_inline(result.start_time)}",
        f"- **End:** {_markdown_inline(result.end_time)}",
        "",
        "## Evaluator aggregates",
        "",
    ]
    if aggregate_scores:
        lines.append("| Evaluator | Mean |")
        lines.append("| --- | --- |")
        for name in sorted(aggregate_scores):
            lines.append(f"| {_markdown_cell(name)} | {_format_report_score(aggregate_scores[name])} |")
    else:
        lines.append("No evaluator scores.")
    lines.append("")
    lines.append("## Examples")
    lines.append("")
    lines.append("| Example | Status | Scores | Error |")
    lines.append("| --- | --- | --- | --- |")
    for example in result.results:
        scores = _format_report_example_scores(example.scores)
        error = example.error or "-"
        lines.append(
            f"| {_markdown_cell(example.example_id)} | {_markdown_cell(example.status)} "
            f"| {_markdown_cell(scores)} | {_markdown_cell(error)} |"
        )
    lines.append("")
    return "\n".join(lines)


def _format_report_score(value: float) -> str:
    return f"{value:.2f}"


def _format_report_example_scores(scores: list[EvalResult]) -> str:
    if not scores:
        return "-"
    return " ".join(f"{score.name}={score.value:.2f}" for score in sorted(scores, key=lambda score: score.name))


def _collapse_newlines(text: str) -> str:
    return text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _markdown_inline(text: str) -> str:
    return _collapse_newlines(text)


def _markdown_cell(text: str) -> str:
    return _collapse_newlines(text).replace("|", "\\|")
