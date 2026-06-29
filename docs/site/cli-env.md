# CLI & Environment Config

Installing `bir-sdk` adds a standard-library-only `bir` command for inspecting
local data and sending it to a server.

## Commands

```bash
bir traces                    # list local traces, newest first
bir traces --limit 20 --json  # machine-readable output
bir traces --name checkout --status error --since 2026-01-01  # filter the listing
bir show <trace-id>           # print one trace as an indented event tree
bir show <trace-id> --json    # nested {event, children} JSON tree
bir stats                     # summarize counts, tokens, cost, and latency
bir stats --json              # the same figures as machine-readable JSON
bir stats --status error --since 2026-01-01  # summarize a filtered subset
bir tail                      # follow the local trace file
bir experiments               # list local experiments and scores
bir experiment-show <id>      # one experiment's summary and per-example scores
bir experiment-show <id> --json  # nested experiment JSON for scripts
bir experiment-report <id>    # self-contained HTML report to stdout
bir experiment-report <id> --format markdown --output report.md  # write a file
bir send                      # send events to the default local server
bir send-experiment .bir/experiments/<name>-<id>.jsonl
bir eval-gate baseline.jsonl candidate.jsonl --tolerance 0.01
bir export-otel --endpoint http://localhost:4318/v1/traces  # needs the 'otel' extra
```

| Command | What it does |
| --- | --- |
| `bir traces [--path P] [--limit N] [--json] [--include-rotated] [--name SUBSTRING] [--status {success,error}] [--since ISO] [--until ISO]` | List trace time, status, duration, event count, and name; optionally filtered. |
| `bir show TRACE_ID [--path P] [--include-rotated] [--json]` | Print one trace as an indented event tree, or a nested JSON tree. |
| `bir stats [--path P] [--include-rotated] [--json] [--name SUBSTRING] [--status {success,error}] [--since ISO] [--until ISO]` | Summarize trace counts, token usage, cost per currency, and latency; optionally filtered. |
| `bir tail [--path P]` | Follow a trace file and print new events until interrupted. |
| `bir experiments [--dir D] [--json]` | List local experiment summaries. |
| `bir experiment-show EXPERIMENT_ID [--dir D] [--json]` | Print one experiment's summary and per-example results. |
| `bir experiment-report EXPERIMENT_ID [--dir D] [--format {html,markdown}] [--output PATH]` | Render one experiment to a self-contained HTML or Markdown report. |
| `bir send [--path P] [--server URL] [--include-rotated] [--mark-sent] [--retries N] [--backoff SECONDS] [--timeout SECONDS]` | Send local events and print the upload result. |
| `bir send-experiment PATH [--server URL] [--retries N] [--backoff SECONDS]` | Send a saved experiment and summary, retrying transient failures. |
| `bir eval-gate BASELINE CANDIDATE [--tolerance N]` | Fail when a shared aggregate evaluator regresses past tolerance. |
| `bir export-otel --endpoint URL [--path P] [--include-rotated] [--header KEY=VALUE] [--service-name NAME] [--timeout SECONDS]` | Export local traces to an OTLP endpoint via the optional `otel` extra. |

Every command accepts `--help`. Trace commands use `.bir/traces.jsonl` by
default; experiment listing uses `.bir/experiments`; send commands target
`http://127.0.0.1:8000`.

`bir traces` can narrow the listing before printing: `--name` keeps traces whose
name contains a case-sensitive substring, `--status {success,error}` keeps traces
with that exact status, and `--since`/`--until` keep traces whose start time falls
within those inclusive ISO 8601 bounds (a value without an offset is treated as
UTC; a malformed timestamp exits non-zero). Filters combine with AND, apply to both
the table and `--json`, and are applied before `--limit` so `--limit` counts only
matching traces. With no filters the output is unchanged.

`bir show TRACE_ID` reads the same files as `bir traces`, finds the trace with
that id, and renders its events as a tree ordered by parent/child: each line
shows the event type, name, status, and duration, plus the model and token usage
on generations and the value on scores. `--json` emits a deterministic nested
`{"event": ..., "children": [...]}` tree of the same data for scripts. An unknown
trace id prints nothing to stdout and exits non-zero.

`bir stats` aggregates the same local traces into a one-screen summary: the total
trace count with the success and error splits, summed input/output/total token
usage over generation events, summed cost grouped by currency, and trace latency
count, mean, and p95 (the nearest-rank 95th percentile, computed with the standard
library). Costs in different currencies are reported on their own lines and never
summed together, so a store mixing USD and EUR shows both. `--json` emits the same
figures as a deterministic object for scripts. An empty store exits 0 with zeroed
counts and `-` latency. Latency is read from each trace's root duration, so partial
traces split across rotated files are counted only when `--include-rotated` brings
in their root.

`bir stats` accepts the same `--name`, `--status`, `--since`, and `--until` filters
as `bir traces`, with identical semantics, so you can summarize a subset (e.g.
errors only, or usage since yesterday). Filters combine with AND and apply before
aggregation, so every figure — counts, tokens, cost, and latency — reflects only the
matching traces. An empty filtered result still exits 0 with zeroed counts; with no
filters the output is unchanged.

`bir export-otel` replays local traces to an OpenTelemetry/OTLP endpoint using
the optional `otel` extra (`pip install 'bir-sdk[otel]'`), reading the same files
as `bir traces` (with `--path` and `--include-rotated`). `--endpoint` is required;
`--header KEY=VALUE` is repeatable for backend auth (only the first `=` splits the
key from the value), and `--service-name` and `--timeout` are forwarded to the
exporter. It prints how many traces and spans were exported and exits non-zero
with an install hint when the extra is missing. The export only reads the local
JSONL; it never writes to or alters it.

`--include-rotated` on `bir traces`, `bir show`, `bir stats`, `bir send`, and
`bir export-otel` also reads
size-rotated trace files (`traces.jsonl.1` ..) created by
`configure(max_bytes=...)`, oldest-first alongside the active file. It is off by
default, so these commands operate on the active file only unless the flag is
passed. `bir send --include-rotated` deduplicates events by ID when a rotated
file overlaps the active one.

`bir experiment-show EXPERIMENT_ID` reads the same `--dir` directory as
`bir experiments` (default `.bir/experiments`), finds the experiment with that id,
and prints its summary header (name, status, example and error counts, run times),
a table of evaluator aggregate means, and a per-example table of id, status, and
scores. `--json` emits a deterministic nested object with the summary fields and a
`results` list of per-example `example_id`, `status`, `scores`, and `error`. An
unknown experiment id prints nothing to stdout and exits non-zero.

`bir experiment-report EXPERIMENT_ID` resolves the experiment the same way as
`bir experiment-show` (the same `--dir` directory, the same non-zero exit and
clean stdout for an unknown id) and renders it to a self-contained report
combining the summary, the evaluator aggregate means, and a per-example table of
id, status, and scores. `--format` chooses `html` (default; a standalone document
with inline styles and no external assets) or `markdown`. The report is written to
stdout, or to `--output PATH` (creating parent directories) with a confirmation
line on stdout instead. Output is deterministic and every experiment-derived
string is escaped for the chosen format, so already-redacted example text cannot
inject markup. The same rendering is available in Python as
`bir.evals.render_experiment_report`.

`bir send` exposes the same options as `send_events()`. `--mark-sent` records the
event IDs the server accepts in a `<trace_path>.sent` sidecar and skips them on
later sends, so re-running a send is cheap and idempotent (off by default; the
sidecar never touches the trace JSONL). `--retries` (default `2`), `--backoff`
seconds (default `0.5`), and `--timeout` seconds (default `10`) tune the same
transient-failure handling described below, accept non-negative values only, and
the delay between attempts is `backoff * 2**attempt`. See
[Sending to a Server](sending.md).

`bir send-experiment` retries transient upload failures (network errors,
timeouts, and HTTP 5xx) with exponential backoff. `--retries` (default `2`) and
`--backoff` seconds (default `0.5`) accept non-negative values only, and the
delay between attempts is `backoff * 2**attempt`. HTTP 4xx, a missing file, and
an invalid server response fail immediately. See
[Sending to a Server](sending.md#retry-behavior).

Commands print failures to stderr and exit non-zero for missing or malformed
files, server failures, and failed eval gates. JSON output on `traces`, `show`,
`stats`, `experiments`, and `experiment-show` is suitable for scripts.

## Environment configuration

Bir reads these variables once when the `bir` package is imported:

| Variable | Meaning | Default |
| --- | --- | --- |
| `BIR_TRACE_PATH` | Local trace JSONL path. | `.bir/traces.jsonl` |
| `BIR_CAPTURE_INPUTS` | Enable input capture. | `false` |
| `BIR_CAPTURE_OUTPUTS` | Enable output capture. | `false` |
| `BIR_DISABLED` | Master kill switch: a truthy value records nothing (inverse of `enabled`). | `false` |
| `BIR_SAMPLE_RATE` | Trace recording probability from `0.0` to `1.0`. | `1.0` |
| `BIR_SERVICE_NAME` | Service name on trace roots. | unset |
| `BIR_ENVIRONMENT` | Deployment environment on trace roots. | unset |
| `BIR_SOURCE` | Trace source tag on trace roots (`metadata.source`). | unset |
| `BIR_MAX_VALUE_LENGTH` | Truncate captured strings longer than this many characters. | unlimited |
| `BIR_MAX_COLLECTION_ITEMS` | Keep at most this many items of a captured list/mapping. | unlimited |

```bash
export BIR_TRACE_PATH=/var/log/bir/traces.jsonl
export BIR_CAPTURE_INPUTS=false
export BIR_CAPTURE_OUTPUTS=false
export BIR_DISABLED=0
export BIR_SAMPLE_RATE=0.1
export BIR_SERVICE_NAME=rag-api
export BIR_ENVIRONMENT=production
export BIR_SOURCE=checkout-api
export BIR_MAX_VALUE_LENGTH=10000
export BIR_MAX_COLLECTION_ITEMS=100
```

Boolean values accept `1`, `true`, `yes`, and `on`, or `0`, `false`, `no`, and
`off`, case-insensitively. `BIR_DISABLED` is the master kill switch and the
inverse of the `enabled` setting: a truthy value turns all recording off (every
primitive still runs your code and still raises, but nothing is written), while
an explicit `configure(enabled=...)` always wins over it. `BIR_MAX_VALUE_LENGTH`
and `BIR_MAX_COLLECTION_ITEMS`
take a non-negative integer and bound captured values only (truncating after
redaction); see [Capture & Privacy](capture-privacy.md#limiting-capture-size).
Invalid values raise a configuration error.

Explicit calls take precedence:

```python
from bir import configure

configure(sample_rate=1.0, environment="staging")
```

Capture remains disabled unless explicitly enabled. See
[Capture & Privacy](capture-privacy.md) before recording application payloads.
