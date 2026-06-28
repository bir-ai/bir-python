# CLI & Environment Config

Installing `bir-sdk` adds a standard-library-only `bir` command for inspecting
local data and sending it to a server.

## Commands

```bash
bir traces                    # list local traces, newest first
bir traces --limit 20 --json  # machine-readable output
bir show <trace-id>           # print one trace as an indented event tree
bir show <trace-id> --json    # nested {event, children} JSON tree
bir stats                     # summarize counts, tokens, cost, and latency
bir stats --json              # the same figures as machine-readable JSON
bir tail                      # follow the local trace file
bir experiments               # list local experiments and scores
bir experiment-show <id>      # one experiment's summary and per-example scores
bir experiment-show <id> --json  # nested experiment JSON for scripts
bir send                      # send events to the default local server
bir send-experiment .bir/experiments/<name>-<id>.jsonl
bir eval-gate baseline.jsonl candidate.jsonl --tolerance 0.01
bir export-otel --endpoint http://localhost:4318/v1/traces  # needs the 'otel' extra
```

| Command | What it does |
| --- | --- |
| `bir traces [--path P] [--limit N] [--json] [--include-rotated]` | List trace time, status, duration, event count, and name. |
| `bir show TRACE_ID [--path P] [--include-rotated] [--json]` | Print one trace as an indented event tree, or a nested JSON tree. |
| `bir stats [--path P] [--include-rotated] [--json]` | Summarize trace counts, token usage, cost per currency, and latency. |
| `bir tail [--path P]` | Follow a trace file and print new events until interrupted. |
| `bir experiments [--dir D] [--json]` | List local experiment summaries. |
| `bir experiment-show EXPERIMENT_ID [--dir D] [--json]` | Print one experiment's summary and per-example results. |
| `bir send [--path P] [--server URL] [--include-rotated] [--mark-sent] [--retries N] [--backoff SECONDS] [--timeout SECONDS]` | Send local events and print the upload result. |
| `bir send-experiment PATH [--server URL] [--retries N] [--backoff SECONDS]` | Send a saved experiment and summary, retrying transient failures. |
| `bir eval-gate BASELINE CANDIDATE [--tolerance N]` | Fail when a shared aggregate evaluator regresses past tolerance. |
| `bir export-otel --endpoint URL [--path P] [--include-rotated] [--header KEY=VALUE] [--service-name NAME] [--timeout SECONDS]` | Export local traces to an OTLP endpoint via the optional `otel` extra. |

Every command accepts `--help`. Trace commands use `.bir/traces.jsonl` by
default; experiment listing uses `.bir/experiments`; send commands target
`http://127.0.0.1:8000`.

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
| `BIR_SAMPLE_RATE` | Trace recording probability from `0.0` to `1.0`. | `1.0` |
| `BIR_SERVICE_NAME` | Service name on trace roots. | unset |
| `BIR_ENVIRONMENT` | Deployment environment on trace roots. | unset |
| `BIR_SOURCE` | Trace source tag on trace roots (`metadata.source`). | unset |

```bash
export BIR_TRACE_PATH=/var/log/bir/traces.jsonl
export BIR_CAPTURE_INPUTS=false
export BIR_CAPTURE_OUTPUTS=false
export BIR_SAMPLE_RATE=0.1
export BIR_SERVICE_NAME=rag-api
export BIR_ENVIRONMENT=production
export BIR_SOURCE=checkout-api
```

Boolean values accept `1`, `true`, `yes`, and `on`, or `0`, `false`, `no`, and
`off`, case-insensitively. Invalid values raise a configuration error.

Explicit calls take precedence:

```python
from bir import configure

configure(sample_rate=1.0, environment="staging")
```

Capture remains disabled unless explicitly enabled. See
[Capture & Privacy](capture-privacy.md) before recording application payloads.
