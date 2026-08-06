# CLI & Environment Config

Installing `bir-sdk` adds a standard-library-only `bir` command for inspecting
local data and sending it to a server.

## Commands

```bash
bir traces                    # list local traces, newest first
bir traces --limit 20 --json  # machine-readable output
bir traces --name checkout --status error --since 2026-01-01  # filter the listing
bir traces --skip-invalid     # read a store an interrupted write damaged
bir show <trace-id>           # print one trace as an indented event tree
bir show <trace-id> --json    # nested {event, children} JSON tree
bir stats                     # summarize counts, tokens, cost, and latency
bir stats --json              # the same figures as machine-readable JSON
bir stats --status error --since 2026-01-01  # summarize a filtered subset
bir prune --before 2026-01-01            # preview removing traces older than a date
bir prune --keep-last 500 --yes          # keep only the 500 newest traces (writes)
bir tail                      # follow the local trace file
bir experiments               # list local experiments and scores
bir experiment-show <id>      # one experiment's summary and per-example scores
bir experiment-show <id> --json  # nested experiment JSON for scripts
bir experiment-report <id>    # self-contained HTML report to stdout
bir experiment-report <id> --format markdown --output report.md  # write a file
bir send                      # send events to the default local server
bir send-experiment .bir/experiments/<name>-<id>.jsonl
bir eval-gate baseline.jsonl candidate.jsonl --tolerance 0.01
bir eval-gate baseline.jsonl candidate.jsonl \
  --tolerance 0.01 --score-tolerance latency_under=0.05 --missing-score regress
bir export-otel --endpoint http://localhost:4318/v1/traces  # needs the 'otel' extra
bir prune --keep-last 500 --yes --json   # machine-readable result for a script
bir config                    # print the effective resolved configuration
bir config --json             # the same fields as machine-readable JSON
```

| Command | What it does |
| --- | --- |
| `bir traces [--path P] [--limit N] [--json] [--include-rotated] [--skip-invalid] [--name SUBSTRING] [--status {success,error}] [--since ISO] [--until ISO]` | List trace time, status, duration, event count, and name; optionally filtered. |
| `bir show TRACE_ID [--path P] [--include-rotated] [--json] [--skip-invalid]` | Print one trace as an indented event tree, or a nested JSON tree. |
| `bir stats [--path P] [--include-rotated] [--json] [--skip-invalid] [--name SUBSTRING] [--status {success,error}] [--since ISO] [--until ISO]` | Summarize trace counts, token usage, cost per currency, and latency; optionally filtered. |
| `bir prune [--path P] [--include-rotated] [--before ISO] [--keep-last N] [--status {success,error}] [--dry-run] [--yes] [--json]` | **Destructive.** Remove whole old/unwanted traces from the local store. Safe by default. |
| `bir tail [--path P]` | Follow a trace file and print new events until interrupted. |
| `bir experiments [--dir D] [--json] [--skip-invalid]` | List local experiment summaries. |
| `bir experiment-show EXPERIMENT_ID [--dir D] [--json] [--skip-invalid]` | Print one experiment's summary and per-example results. |
| `bir experiment-report EXPERIMENT_ID [--dir D] [--format {html,markdown}] [--output PATH] [--skip-invalid]` | Render one experiment to a self-contained HTML or Markdown report. |
| `bir send [--path P] [--server URL] [--include-rotated] [--mark-sent] [--batch-size N] [--retries N] [--backoff SECONDS] [--timeout SECONDS] [--json]` | Send local events and print the upload result; optionally use bounded groups. |
| `bir send-experiment PATH [--server URL] [--retries N] [--backoff SECONDS] [--json]` | Send a saved experiment and summary, retrying transient failures. |
| `bir eval-gate BASELINE CANDIDATE [--tolerance N] [--score-tolerance NAME=VALUE] [--missing-score {ignore,regress}] [--per-example]` | Fail when a shared aggregate evaluator regresses past tolerance. |
| `bir export-otel --endpoint URL [--path P] [--include-rotated] [--skip-invalid] [--header KEY=VALUE] [--service-name NAME] [--environment ENV] [--timeout SECONDS] [--json]` | Export local traces to an OTLP endpoint via the optional `otel` extra. |
| `bir config [--json]` | Print the effective resolved SDK configuration (read-only). |

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

`bir prune` is the **destructive** counterpart that reclaims space: it removes
whole traces from the local store so a long-lived `.bir/traces.jsonl` (and its
rotated siblings) does not grow without bound. It operates on whole traces and
never splits one across the keep/drop boundary. It scans the selected JSONL
files one event at a time into a temporary, disk-backed SQLite trace index, then
uses disk-backed removal membership while streaming surviving lines to sibling
staging files. Its Python working set is bounded by the largest individual event
or line, a bounded SQLite cache, and small per-source staging bookkeeping rather
than a collection that grows with the store's event or trace count. Every staging
file is completed before any source is replaced, and replacements remain atomic
under the same advisory lock an append takes, so a concurrent writer can never
interleave and a selection, parsing, or staging failure leaves every source file
intact. Temporary index and staging files are removed after success or failure.
A successful prune also compacts the `--mark-sent` upload sidecar, dropping IDs
for events the store no longer holds so it stays bounded by the retained traces
rather than by everything ever sent; see
[sending](sending.md#what-bounds-the-sidecar).
Selection: `--before ISO` removes
traces whose start time precedes the cutoff, `--keep-last N` removes all but the
N most recent, and `--status
{success,error}` restricts removal to that status (`--before` and `--keep-last`
combine by union; `--status` is applied as a further restriction). It is **safe by
default in two ways**: a bare `bir prune` with no selection filter is rejected so
the store can never be wiped by accident, and even with a filter it only *previews*
unless you pass `--yes` — without it (or with `--dry-run`, which always wins) it
prints what it would remove and writes nothing. The summary is
`removed=<traces> kept=<traces> events=<dropped> bytes=<reclaimed>` (a dry run adds
`(dry run; pass --yes to apply)`). `--include-rotated` extends pruning to the
size-rotated siblings; without it only the active file is rewritten. An empty store
and a run that matches nothing both exit 0 and write nothing. Pruning never touches
experiments, only traces.

`bir export-otel` replays local traces to an OpenTelemetry/OTLP endpoint using
the optional `otel` extra (`pip install 'bir-sdk[otel]'`), reading the same files
as `bir traces` (with `--path` and `--include-rotated`). `--endpoint` is required;
`--header KEY=VALUE` is repeatable for backend auth (only the first `=` splits the
key from the value), and `--service-name`, `--environment`, and `--timeout` are
forwarded to the exporter. The exported OpenTelemetry `Resource` records
`service.name` plus, when the traces recorded them, `deployment.environment` (from
`configure(environment=...)`) and `bir.source` (from `configure(source=...)`), and
generation spans gain `gen_ai.system` when an integration recorded the provider.
`--environment` sets `deployment.environment` explicitly and overrides whatever the
traces recorded; without it, the value is derived from the traces and, when one run
mixes environments or sources, the conflicting attribute moves from the `Resource`
onto each span (`bir.environment` / `bir.source`) instead of being dropped. It
prints how many traces and spans were exported and exits non-zero with an install
hint when the extra is missing. The export only reads the local JSONL; it never
writes to or alters it.

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

`bir send` exposes the same options as `send_events()`. `--batch-size N` opts
into disk-backed upload preparation and sequential request groups containing at
most `N` events; it accepts positive integers and is off by default, preserving
the historical single-request path. `--mark-sent` records the event IDs the
server accepts in a `<trace_path>.sent` sidecar and skips them on later sends, so
re-running a send is cheap and idempotent (off by default; the sidecar never
touches the trace JSONL). `--retries` (default `2`), `--backoff` seconds (default
`0.5`), and `--timeout` seconds (default `10`) tune the same transient-failure
handling described below, accept non-negative values only, and the delay between
attempts is `backoff * 2**attempt`. See
[Sending to a Server](sending.md).

`bir send-experiment` retries transient upload failures (network errors,
timeouts, and HTTP 5xx) with exponential backoff. `--retries` (default `2`) and
`--backoff` seconds (default `0.5`) accept non-negative values only, and the
delay between attempts is `backoff * 2**attempt`. HTTP 4xx, a missing file, and
an invalid server response fail immediately. See
[Sending to a Server](sending.md#retry-behavior).

`bir config` answers "what configuration is active right now?" without a Python
REPL. It prints the effective, resolved settings of the live SDK configuration —
the absolute `trace_path`, the `capture_inputs`/`capture_outputs` flags, the
`enabled` master switch, `sample_rate` and any exact-name `sample_rules`, the
`service_name`/`environment`/`source` trace metadata, the `max_bytes`/`backup_count`
rotation settings, and the `max_value_length`/`max_collection_items` capture-size
limits — followed by which `BIR_*` environment variables are currently set. It
reflects everything an explicit `configure(...)` call changed in-process. It is
strictly **read-only**: it never mutates configuration and always exits 0. To keep
it non-leaky, the additional redaction rules and the local `model_prices` table are
reported as **counts only** (never the patterns or prices themselves), and only the
**names** of set `BIR_*` variables are listed, never their values. `--json` emits
the same fields as a deterministic, sorted object for scripts.

Commands print failures to stderr and exit non-zero for missing or malformed
files, server failures, and failed eval gates. JSON output on `traces`, `show`,
`stats`, `experiments`, `experiment-show`, and `config` is suitable for scripts.

### Machine-readable output

Every command reports its result as JSON on request, so a script never has to
match English. `eval-gate` always emits JSON — it exists to be read by a build —
and the rest print a human summary by default and JSON with `--json`:

| Command | `--json` shape |
| --- | --- |
| `traces` | Array of `{id, name, status, start_time, duration_ms, event_count}` |
| `show` | Nested `{event, children}` tree |
| `stats` | `{traces, tokens, cost, latency_ms}` |
| `experiments` | Array of experiment summaries |
| `experiment-show` | One experiment with its per-example results |
| `send` | `{accepted, attempted, skipped}` |
| `send-experiment` | `{accepted, experiment_id}` |
| `prune` | `{removed_traces, kept_traces, removed_events, bytes_reclaimed, dry_run}` |
| `export-otel` | `{traces, spans, endpoint}` |
| `eval-gate` | `{has_regressions, deltas, regressed, regression_reasons, tolerance, effective_tolerances, ...}` (always) |
| `config` | The effective configuration |

Two commands have no JSON form. `tail` streams events as they arrive rather than
producing a result, and `experiment-report` renders a document you asked for in
a named format.

A usage error stays a message on stderr and a non-zero exit code even under
`--json`, so a script never parses a failure as a successful result:

```bash
$ bir prune --json          # no selection filter given
bir: prune requires at least one selection filter (--before, --keep-last, or --status)
$ echo $?
1
```

### Reading a damaged store

Bir appends each event as one JSON line. If a process is killed, runs out of
disk, or is otherwise interrupted part-way through a write, the file can end
with a half-written line. Every reader validates line by line and refuses a
store it cannot read completely, so one damaged line makes the whole store
unreadable even though the events before it are intact:

```
$ bir traces
bir: Invalid JSON in trace file .bir/traces.jsonl at line 6
```

`--skip-invalid` reads past those lines and tells you what it skipped:

```
$ bir traces --skip-invalid
bir: skipped 1 unreadable line; first: Invalid JSON in trace file .bir/traces.jsonl at line 6
START                             STATUS   DURATION  EVENTS  NAME
2026-08-05T22:48:00.911001+00:00  success  0.1ms     2       request-1
2026-08-05T22:48:00.910221+00:00  success  0.6ms     2       request-0
```

The report goes to stderr, so `--json` still writes only JSON to stdout. The
flag is available on `traces`, `show`, `stats`, and `export-otel` — the commands
that only display what they read.

An experiment store fails the same way and answers it the same way. An experiment
is a result JSONL plus a `*.summary.json` beside it, and the listing reads every
summary in the directory, so one it cannot parse refuses the whole directory —
which also blocks `experiment-show` and `experiment-report`, since both find their
target through the listing:

```
$ bir experiments --dir .bir/experiments
bir: Invalid JSON in experiment summary .bir/experiments/beta.summary.json
```

`--skip-invalid` is available on `experiments`, `experiment-show`, and
`experiment-report`:

```
$ bir experiments --skip-invalid
bir: skipped 1 unreadable experiment summary; first: Invalid JSON in experiment summary .bir/experiments/beta.summary.json
ID                                    NAME   STATUS   EXAMPLES  ERRORS  SCORES
7bc09786-5c90-4604-83cf-2012d377593f  gamma  success  3         0       exact_match=1.00
92db3b13-87ab-41af-a7ef-b9971cfd9205  alpha  success  3         0       exact_match=1.00
```

A damaged *result* file needs no flag: it is read only by the experiment that
owns it, so it fails `experiment-show` for that one experiment and leaves the
listing alone.

`send` and `prune` deliberately have no such flag. Skipping a line while
uploading would silently fail to send recorded data, and skipping one while
pruning would delete traces the command could not fully account for; both keep
refusing a store they cannot read completely. Once you have recovered what you
need, remove the damaged line from the JSONL file to make the store whole again.

`load_events()` and `load_traces()` are likewise strict and unchanged: a program
building on them gets all the recorded events or an error, never a silently
partial list.

### Events with no trace root

A trace's root event is written when the trace closes, so a trace that never
closes leaves its child events on disk with no root. Because a trace is resolved
through its root, those events belong to no trace and are not listed:

```
$ bir traces
bir: 2 events across 1 trace have no trace root and are not listed; first trace id: b23aa02e-...
No traces found in .bir/traces.jsonl.
```

`traces` and `stats` report this on stderr, so `--json` output stays parseable,
and `show` says the same thing for a specific id rather than reporting it as
missing:

```
$ bir show b23aa02e-...
bir: trace 'b23aa02e-...' has 2 recorded events but no trace root, so it cannot be shown as a tree
```

The store is intact; the trace is not. A root goes missing when the process died
before the trace closed, when `configure(max_bytes=...)` rotation dropped the
file the root was written to, or when a framework integration never received the
callback that would have closed the run. The events themselves are still
returned by `load_events()`.

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
