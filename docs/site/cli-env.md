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
  --tolerance 0.01 --score-tolerance latency_under=0.05 \
  --missing-score regress --failed-examples ignore
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
| `bir tail [--path P]` | Follow a trace file and print new events until interrupted, across rotations. |
| `bir experiments [--dir D] [--json] [--skip-invalid]` | List local experiment summaries. |
| `bir experiment-show EXPERIMENT_ID [--dir D] [--json] [--skip-invalid]` | Print one experiment's summary and per-example results. |
| `bir experiment-report EXPERIMENT_ID [--dir D] [--format {html,markdown}] [--output PATH] [--skip-invalid]` | Render one experiment to a self-contained HTML or Markdown report. |
| `bir send [--path P] [--server URL] [--include-rotated] [--mark-sent] [--batch-size N] [--retries N] [--backoff SECONDS] [--timeout SECONDS] [--json]` | Send local events and print the upload result; optionally use bounded groups. |
| `bir send-experiment PATH [--server URL] [--retries N] [--backoff SECONDS] [--json]` | Send a saved experiment and summary, retrying transient failures. |
| `bir eval-gate BASELINE CANDIDATE [--tolerance N] [--score-tolerance NAME=VALUE] [--missing-score {ignore,regress}] [--failed-examples {ignore,regress}] [--per-example]` | Fail when a shared aggregate evaluator regresses past tolerance, or when more of the candidate's examples failed. |
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
`service.name` plus, when the traces recorded them, the deployment environment (from
`configure(environment=...)`) and `bir.source` (from `configure(source=...)`), and
generation spans gain the provider name when an integration recorded it.
`--environment` sets the deployment environment explicitly and overrides whatever the
traces recorded; without it, the value is derived from the traces and, when one run
mixes environments or sources, the conflicting attribute moves from the `Resource`
onto each span (`bir.environment` / `bir.source`) instead of being dropped. It
exits non-zero with an install hint when the extra is missing. The export only
reads the local JSONL; it never writes to or alters it.

#### Which semantic conventions

The exported attribute names are measured against
**opentelemetry-semantic-conventions 0.65b0**, the release installed alongside
`opentelemetry-sdk` 1.44.0. Two of them were renamed after this exporter was
written, and both spellings are emitted with the same value:

| superseded | current | where |
| --- | --- | --- |
| `deployment.environment` | `deployment.environment.name` | `Resource` |
| `gen_ai.system` | `gen_ai.provider.name` | generation spans |

Point your backend's facet at whichever name it knows. Both are written because
the `otel` extra accepts `opentelemetry-sdk>=1.20` and a backend anywhere in
that range may key on either; emitting only the current spelling would leave the
facet empty for anyone who has not migrated, which is the same silence in the
other direction. The superseded names go when the extra's floor rises past the
release that carries only the replacements.

`gen_ai.request.model`, `gen_ai.usage.input_tokens`, and
`gen_ai.usage.output_tokens` are unchanged in that release. Everything else Bir
exports uses a `bir.*` name and is not a convention.

On success it prints how many traces and spans arrived, and the span count is what
the endpoint accepted rather than what was built. If the endpoint refused or never
answered, the command exits non-zero and says what did not arrive:

```
bir: bir could not export traces to http://localhost:4318/v1/traces: none of 8 span(s) were accepted
```

Nothing is written to stdout in that case, so a pipeline reading the `--json`
contract never sees a span count for spans that were not delivered. A partial
delivery is a failure too and reports how much arrived. This matches `bir send`:
both commands exist to move data somewhere else, and both treat not moving it as
the command failing.

Like the other read commands, it streams: the store is read in two passes and one
trace is held at a time, so peak memory scales with a trace rather than with the
store. Traces are exported in completion order rather than sorted by start time,
which is what allows each to be released as soon as it is built; the events
inside a trace are ordered exactly as before.

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
| `prune` | `{removed_traces, kept_traces, removed_events, bytes_reclaimed, incomplete_tail_bytes, dry_run}` |
| `export-otel` | `{traces, spans, endpoint}` (only on a delivered export; a failed one writes nothing to stdout and exits non-zero) |
| `eval-gate` | `{has_regressions, deltas, regressed, regression_reasons, tolerance, effective_tolerances, failed_example_regression, baseline_example_count, baseline_error_count, candidate_example_count, candidate_error_count, ...}` (always) |
| `config` | The effective configuration |

Two commands have no JSON form. `tail` streams events as they arrive rather than
producing a result, and `experiment-report` renders a document you asked for in
a named format.

`bir tail` flushes each batch it prints, so its output arrives as the events do
whether it is attached to a terminal or redirected. Composing it works the way a
follow command should:

```bash
bir tail | grep error
```

It also follows the store across a rotation. `configure(max_bytes=...)` renames
the active file away and starts a new one, so a follow that tracked only a byte
offset skipped both the tail of the file that was renamed and the beginning of
its replacement. `bir tail` identifies the file it is reading — by device and
inode, and by its recorded first line, since a filesystem may hand a new file
the inode number a rotation just freed — drains what the renamed file still
owed, and reads any files that rotated in between, in write order, before
continuing with the new active file. No flag is involved: unlike the read
commands, a follow shows what is being written now, and where the store put it
is not something you should have to know.

One gap it cannot close. Rotating more times than `backup_count` keeps deletes
the file the follow was reading before it is read, and nothing can print what is
no longer on disk. That is reported on stderr rather than passed over:

```
bir: .bir/traces.jsonl was replaced; the events it still held were not shown
```

It goes to stderr so it stays out of the event stream on stdout, and the follow
resumes at the start of whatever is at the path now. Seeing it usually means the
store is deleting rotated files faster than the half-second poll can read them;
a larger `max_bytes` or `backup_count` is the fix. The same line appears when a
`bir prune` run rewrites the store being followed, or when anything else
replaces the active file, because from inside a follow all of them leave the
same absence.

### Recorded text cannot steer your terminal

Names, models, and captured values are data, and a name is often not a literal —
a framework bridge passes the tool the model chose, and an application passes a
route from a request. When the CLI prints them for a person, control characters
are escaped as `\x1b`, `\x0a`, and so on, so a recorded value cannot clear a
line, move the cursor, or set a colour in the output of the command reading it,
and a name containing a newline cannot split a table row.

Only the printed form changes. The stored event keeps exactly what the
application passed, `--json` hands a parser the value as written, and
`load_events()` / `load_traces()` return it unchanged.

The same holds for the error channel, where the untrusted string can come from
further away. A `bir: …` diagnostic is built from Bir's own words plus whatever
it is reporting on — a path, a trace or experiment id, a store's field names,
and for `bir send` the response body of whatever is listening on `--server`,
which on a mistyped URL is not a Bir server at all. Every diagnostic is escaped
the same way and is one line, so a response body cannot clear the line above it
and print something that reads like a successful send:

```bash
$ bir send --server http://localhost:9999   # answers 400 with an ANSI sequence
bir: bir server rejected event batch with HTTP 400: \x1b[2K\x1b[A\x1b[2Kaccepted=1 …
```

A response body is also bounded before it reaches a message, so a `--server`
pointed at something that answers with a large document reports the first 500
characters and `…[truncated]` rather than putting the document on your terminal.
The bound applies to what the message shows and to what is read from an error
response; a successful response is still parsed whole, so a large batch's
accepted ids are unaffected.

Escaping stops at the CLI. `send_events()` raises a `RuntimeError` carrying what
the server actually said, so a program catching it logs or matches the bytes
rather than a rendering of them — the same split the SDK draws for recorded
values, which are stored as written and escaped when printed.

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

### Recovering after a full disk

`--skip-invalid` lets you *read* a damaged store. Getting the store back to a
state every reader accepts is `bir prune`'s job, and the case it handles is the
one a full disk leaves.

An event is appended as one whole line ending in a newline, so a file whose last
line has no newline ends in a write that never finished: those bytes were never a
complete event and no reader ever could read them. `bir prune` drops that one
line as it rewrites the store, and says so:

```
$ bir prune --keep-last 500 --yes
bir: dropped an incomplete final line of 133 bytes; a write never finished it, so it was never a readable event
removed=2704 kept=5 events=2704 bytes=899455
```

`--dry-run` previews the same thing without writing (`would drop …`), and
`--json` reports the size as `incomplete_tail_bytes`. It is counted separately
from `removed_events` because it is not an event — no selection filter named it —
and it is already inside `bytes_reclaimed`, which measures the file. The line
goes even when the selection removes no traces, so a repair never depends on
`--keep-last` happening to match.

That opening is exactly one line wide, and nothing else changed:

- a line that was **written whole** and cannot be parsed still refuses, whether
  it is the last line or in the middle — only the missing terminator proves
  nothing was recorded there;
- a fragment in a **rotated** sibling still refuses, because nothing appends to a
  rotated file;
- **`bir send`** still refuses either one. Sending is not the repair path, so
  prune first, then send.

`prune` writes the surviving lines to a sibling staging file and renames it over
the original, which is what makes an interrupted prune safe. It therefore needs
free space for what survives, and on a volume with **no** bytes left it fails
before it can reclaim any:

```
$ bir prune --keep-last 5 --yes
bir: [Errno 28] No space left on device: '.bir/.traces.jsonl.22406.6e808ef2.tmp'
```

`--dry-run` still works there and tells you what is reclaimable. Free anything at
all and the real run goes through: measured on a 1 MB volume the store had filled
completely, 57 KB of headroom was enough to reclaim 876 KB.

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
