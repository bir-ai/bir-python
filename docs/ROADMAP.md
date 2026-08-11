# Bir Python SDK — Improvement Roadmap

> Current baseline: **v0.3.0**, audited **2026-08-10**.
>
> This document contains only work that is still open. Completed work belongs in
> `CHANGELOG.md`; implementation details and copy-paste task prompts belong in
> issues, not in the roadmap. Re-verify every item against the current code before
> starting it because integrations and provider APIs change independently.

## Current baseline

Bir is an alpha-stage, local-first tracing and deterministic-evaluation SDK for
Python 3.10–3.14. The runtime package has no third-party dependencies, ships PEP
561 typing metadata, and records schema-version `1.0` JSONL events.

Measured at this audit on CPython 3.14.6:

- 18,147 lines of runtime source across 40 modules — 19 dependency-free
  integration modules plus the core, evaluation, storage, transport, and CLI
  modules;
- 1,756 tests (1,755 passing, 1 skipped, 1,969 subtests) in 49 files, running in
  17.1 s wall under coverage instrumentation at **94.59%** branch coverage,
  against a CI floor of 89%, with strict resource-warning handling, Ruff
  lint/format, Pyright, strict MkDocs, example smoke tests, and hermetic
  wheel/sdist release verification;
- CI across Linux, Windows, and macOS on Python 3.10–3.14, a free-threaded 3.14
  leg on Linux, plus strict docs and shared-fixture drift checks;
- 13 CLI commands, two conformance matrices covering every shipped integration, a
  published API stability policy guarded against drift, a deprecation mechanism,
  a benchmark harness with baseline comparison, and a recorded decision on
  distributed trace context.

Every item from the previous list shipped and is in `CHANGELOG.md`. This list is
not a continuation of it.

The previous five audits worked the recording path: where it runs code the SDK
did not write, what reaches the terminal on the CLI's rendering path and then on
its error channel, how `bir tail` and rotation compose, what the OTLP attribute
names mean against the release the extra installs, and which fields redaction is
aimed at. This one went where they did not. The evaluation and experiment half
was driven as a product — the gate's decision under partial failure, the
evaluator-name contract, concurrency at eight workers, report rendering — and it
produced three of the four items it raised, including the P1, which has since
shipped. Storage pressure was driven for real rather than simulated: a 1 MB HFS+
volume filled by an actual recording workload, which produced the fourth. The
remaining axes — Unicode and encoding end to end
(what the store accepts, what survives a locale, what each renderer can encode),
cost, usage and clock arithmetic, and sampling and the kill switch under
contention — produced no item and are recorded below with their numbers.

## Product and engineering guardrails

Every roadmap item must preserve these constraints unless an explicitly approved
breaking release says otherwise:

- Keep `[project].dependencies = []`; optional capabilities belong in extras.
- Keep input/output capture opt-in and built-in redaction non-disableable.
- Do not change `schema_version = "1.0"` or `tests/fixtures/*` without a
  coordinated `bir-app` contract change.
- Keep integrations lazy and usable without importing provider SDKs.
- Preserve sync/async and streaming behavior, including finalization on close and
  error.
- Keep local persistence crash-safe and serialized under the existing advisory
  locks.
- Add tests and user documentation with every public behavior change.
- Treat public API removal or signature changes as a versioned deprecation, not a
  drive-by cleanup.

## Prioritized work

| # | Improvement | Priority | Size | Primary outcome | Depends on |
|---|---|---|---|---|---|
| 1 | `experiment-report --output` destroys the file it cannot finish | P2 | S | A failed render leaves the previous report intact | — |
| 2 | Two evaluators may share one name and one aggregate | P2 | S | An evaluator list that cannot be reported is refused where it is built | — |

### 1. `bir experiment-report --output` destroys the file it cannot finish

**Why.** `_cmd_experiment_report` writes with `output_path.write_text(report,
encoding="utf-8")` (`bir/cli.py:664`). That opens the destination for truncating
write before the content is encoded or a byte reaches the disk, so any failure
leaves a zero-byte file where a good report was.

Driven with a real volume running out of space — the 5-example report rendered
first, then the volume padded to 98 KB free, then the 451 KB report rendered over
it:

```
1. render the 5-example report (volume empty)   exit=0  report.html=  1,540 bytes
   padded; free = 98,304 bytes, report.html still 1,540 bytes
2. re-render the 451 KB report over it          exit=1  report.html=      0 bytes
      stderr: bir: [Errno 28] No space left on device
```

An encode failure does the same on an ordinary filesystem. `os.fsdecode` returns
a surrogate-escaped string for a filename that is not valid UTF-8, which is what
a document-ingestion dataset's `example_id` is when it comes from a filesystem
walk, and `write_text` encodes strictly:

```
1. render the clean experiment       exit=0  report.html=4,821 bytes
2. re-render, ids from a walk        exit=1  report.html=    0 bytes
      stderr: bir: 'utf-8' codec can't encode character '\udcff' in position 4734
```

`render_experiment_report` itself is fine in both cases — it returned 4,835
chars for the same experiment, and `bir experiment-report` without `--output`
wrote all 4,835 to stdout and exited 0. Only the file write is destructive.

It is also the only file-producing path in the SDK that writes in place. The
experiment summary already stages and replaces, and says why:
`_write_experiment_summary` (`bir/_eval_persistence.py:330-351`) — "A plain write
truncates in place, so a process killed part-way through leaves a summary that
parses as nothing … either the new summary is there whole, or the previous one
still is." Measured under the same pressure, that holds: after a failed
re-run on the full volume the previous summary was still 318 bytes, still parsed,
byte-identical. `prune` (`bir/_storage.py:1042-1078`) and the sent-ID sidecar
(`bir/_storage.py:1195-1212`) stage the same way.

No test pins the current behavior. `tests/test_cli.py:1136-1156` writes a report
to a path that does not exist yet and `:1100-1134` writes to stdout; nothing
writes over an existing report and nothing fails a write.

**Scope.**

- Write the report through a sibling temp file and `Path.replace` it, matching
  `_write_experiment_summary`, so the destination is replaced whole or not
  touched.
- Decide and record what happens to the report content when the experiment holds
  a string Python cannot encode. Staging fixes the destructive half; the render
  still fails. Either the renderer escapes an unencodable code point the way
  `_visible` escapes a control character (`bir/_cli_present.py:301-326`), or the
  command reports which example it could not render — silently substituting is
  not an option for a report of what a run produced.
- Create the staged file with the umask's mode, not the `_private_opener` the
  other staged writes use. A rename carries the staged file's mode to the
  destination, and `docs/site/capture-privacy.md:133-136` promises the opposite
  of what that opener would give: `bir experiment-report --output ...` writes "to
  a path you named and keeps the umask's mode, because those are deliberate
  handoffs rather than Bir's own store".

**Done when** a `bir experiment-report --output PATH` that fails for any reason
leaves the previous contents of PATH byte-identical.

### 2. Two evaluators may share one name and one aggregate

**Why.** Thirteen of the fourteen evaluator factories in `bir/evals.py` take
`name` as a keyword-only argument with a fixed default — `exact_match` at
`:143`, `regex_match` at `:218`, `field_equals` at `:277-278`, `field_contains`
at `:300-306`, and so on — so two evaluators of the same kind arrive with the
same name. (`custom_evaluator` at `:252-256` is the exception: its `name` is
required.) Nothing rejects a repeat. `_evaluate_example_output` runs them into a flat
list (`bir/evals.py:1182`), and `aggregate_scores` keys totals by
`score.name` (`bir/_eval_models.py:275`), so both land in one bucket:

```
evaluator names passed in:      ['regex_match', 'regex_match']   # r"^alpha" and r"gamma$"
per-example scores on q0:       [('regex_match', 1.0), ('regex_match', 0.0)]
aggregate_scores:               {'regex_match': 0.5}
persisted summary aggregate:    {'regex_match': 0.5}
```

The mean is over 8 values from 4 examples, and the 0.5 is not a score any
example got on any evaluator. Every downstream reader inherits it. The report
(`bir/_eval_reports.py:127-170`) prints one aggregate row and a per-example cell
that names the same evaluator twice:

```
| Evaluator | Mean |
| regex_match | 0.50 |
| q0 | success | regex_match=1.00 regex_match=0.00 | - |
```

And the gate compares the merged number while `--per-example` reports a
different one. `_example_scores_by_evaluator` (`bir/evals.py:1025-1038`) keys by
name into a dict, so the last score written wins:

```
run A aggregate {'regex_match': 0.5}   run B aggregate {'regex_match': 1.0}
compare deltas  {'regex_match': 0.5}   improved: ['regex_match']
example_deltas  {'regex_match': {'q0': 1.0, 'q1': 1.0, 'q2': 1.0, 'q3': 1.0}}
```

The aggregate moved 0.5 and every example moved 1.0, for the same evaluator name,
in the same diff. That docstring already documents last-write-wins for a
duplicate *example id* and says "uniquely identified datasets never hit that
case"; a duplicate evaluator name reaches the same line and nothing rejects it
anywhere.

No test pins the current behavior, and no documentation mentions it. Nothing in
`docs/site/evals-experiments.md`, `docs/EVALUATOR_IMPLEMENTATION_GUIDE.md`, or
`README.md` says evaluator names must be distinct, and the only `name=` in the
evaluation docs is a single example at `docs/site/evals-experiments.md:288`.

**Scope.**

- Reject duplicate names where the evaluator list is materialized, in
  `run_experiment` and `run_experiment_async`, with an error naming the repeated
  name and pointing at the `name=` argument.
- Decide and record whether `compare_experiments` and `load_experiment` also
  reject a persisted run that already contains duplicates. A run recorded before
  the check exists is still on disk, and refusing to load it is worse than
  reporting what it holds.
- Document the rule where the evaluator factories are listed, since the
  keyword-only `name=` on every factory is the fix a user needs to find.

**Done when** `run_experiment` refuses an evaluator list containing two
evaluators with the same name.

## Sequencing

The two remaining items are independent of each other and of everything that
shipped from this audit. Item 2 is closest to the gate work — it lands in the
same three files (`bir/evals.py`, `bir/_eval_models.py`, `bir/cli.py`) — and is
worth taking while that code is fresh. Item 1 is the smallest piece of work here
and touches one line plus its tests.

The full-disk story is finished: the store's own writer repairs an interrupted
write on its next append, and `bir prune` repairs one nothing is recording into
any more. What is left of it is recorded under "Checked and found sound" —
prune needs free space to free space, which is a property of staging rather than
a defect.

Beta readiness is tracked on the checklist in `docs/site/stability.md`, not here.
Its remaining entries are outside this repository's reach or are release
mechanics: confirming the event-schema `1.0` contract against the current
`bir-app` release, writing the migration note for the public changes since
`0.3.0`, and raising the version and the `Development Status` classifier.

## Explicitly not on the backlog

The following shipped features must not be reopened merely because they appeared
in an older generated roadmap: sdist verification, stats filters, the master kill
switch, Ollama, prune, fuzzy similarity, config inspection, `SECURITY.md`, richer
OTLP attributes, experiment timeouts, both conformance matrices, event-bridge
parenting from the framework's own run ids, the published API stability policy,
the performance benchmark harness, the trace-context decision
([ADR 0001](adr/0001-distributed-trace-context.md)), reading a damaged trace store
with `--skip-invalid`, streaming the CLI read commands, the deprecation mechanism,
machine-readable output for every command that produces a result, coverage of the
transport and experiment-loading error paths, the free-threaded CI leg, guarding
capture against a value whose own code raises, bounding the private-key redaction
rule, reporting events whose trace root is missing, reclaiming a framework run
whose end callback never arrived, reading a damaged experiment store with
`--skip-invalid`, compacting the upload sidecar on prune, streaming
`bir export-otel`, attaching the log-correlation filter where propagated records
are seen, reporting rather than raising a failed trace-store write, flushing each
finished example's result row so an interrupted experiment keeps it, redacting the
credential rather than the scheme in an `Authorization` header, reporting a failed
OTLP export instead of counting the spans it built, redacting fine-grained GitHub
tokens, the password inside a connection URI, and the values in a `Cookie` or
`Set-Cookie` header, leaving an unrepresentable derived cost off an event rather
than raising it at the caller, flushing each batch `bir tail` prints so a
redirected follow is not silent, redacting a secret used as a mapping key, and
escaping control characters when the CLI renders recorded text for a person,
guarding the bridges' reads of a provider's response, creating the store's own
files readable only by their owner, guarding the event bridges' reads of a
framework object, following the store across a rotation in `bir tail`, escaping
and bounding what the CLI prints on its error channel, refreshing the OTLP
attribute spellings, recording where the redaction boundary stops, failing the
gate on a candidate run whose examples failed, pruning a store whose final line
an interrupted write never finished, and repairing that line on the next append
instead of writing the following event onto it.
Regressions in those areas are bugs; new scope requires a new issue with current
evidence.

Item 1 sits beside "escaping control characters when the CLI renders recorded
text for a person" and does not reopen it. That covered what a rendered *string*
may contain when a terminal reads it; this is about a *file* being truncated
before anything is rendered into it, and it fails identically when nothing is
wrong with the text at all — the ENOSPC case renders pure ASCII.

The gate that shipped from this audit sits beside the declined "A failing
evaluator discards the example's output and the other evaluators' scores" below
and does not reopen it. That asks whether an evaluator that raises should void
the example; the gate work asked whether the gate should notice a voided example
at all, and the answer does not depend on what voided it. The two compose in one
direction only: while that decision stands, an evaluator failure also shrinks the
gate's denominator, and it is no longer silent when it does.

## Declined

Six things were driven and deliberately left off the list. The first four were
declined by earlier audits, re-measured here, and still hold.

**A failing evaluator discards the example's output and the other evaluators'
scores.** With `raise_on_error=False`, one evaluator that raises turns the whole
example into an error row: `output=None`, `scores=[]`, and an error message that
does not name which evaluator failed, even though the task succeeded and a second
evaluator had already scored it. The guard is at the example boundary
(`bir/evals.py:1499-1502`) while the caller's code runs inside a list
comprehension at `:1182`. Still declined for the same reason: it is a recorded
decision, not a defect. `docs/EVALUATOR_IMPLEMENTATION_GUIDE.md:611-612` says
"Evaluator failure: treat like task failure unless a future explicit option
separates task failures from evaluator failures." Reopening it needs a product
argument.

**`Dataset.to_jsonl()` truncates as well as redacts.** The default `redact=True`
runs the export through `_safe_capture`, so a dataset does not round-trip.
Re-measured on the current code:

```
nesting depth 6: intact
nesting depth 7: TRUNCATED, replaced with [max_depth]
```

and with capture limits configured for tracing, the same export mangles keys as
well as values:

```
configure(max_value_length=20, max_collection_items=1)
{"expected":"x","id":"q1","input":{"question":"What is the refund w…[truncated]",
 "…[truncated]":"…[truncated]"},"metadata":{}}
```

`_MAX_CAPTURE_DEPTH = 6` (`bir/_capture.py:19`) is not configurable, so the depth
cut applies to every `to_jsonl()` call regardless of settings. Still declined:
the method's own docstring already says it uses the same safe capture behavior as
trace and experiment artifacts, which is the truncating one, and the narrower
"redacts common secret-like values" wording in
`docs/site/evals-experiments.md:179` and `docs/site/capture-privacy.md:258-259`
is the only gap. A docs sentence is too small to carry an item.

**Retry classification for HTTP 429.** `_is_retryable_status`
(`bir/_sending.py:75-78`) retries 5xx only, so a rate-limited `bir send` fails
immediately and `Retry-After` is ignored. Still declined because `send_events`'
docstring states the rule outright — a 4xx response is a permanent rejection
raised without retry — and the ingestion server this talks to is the local Bir
server, which does not rate-limit. Worth revisiting if the SDK ever sends to a
hosted endpoint.

**A naive timestamp shifts on OTLP export.** `_expect_datetime_string`
(`bir/_storage.py:1419-1425`) accepts any string `datetime.fromisoformat` parses,
including a timezone-naive one, and `_iso_to_unix_nano` (`bir/integrations/otel.py:610-617`)
then reads it as local time. Re-measured on this UTC+3 machine:

```
2026-08-10T12:00:00+00:00  ->  1786363200000000000
2026-08-10T12:00:00        ->  1786352400000000000
difference                      10800000000000 ns
```

Still declined because Bir's own writer always records an offset —
`_now()` is `datetime.now(timezone.utc).isoformat()` (`bir/_sdk.py:2289-2290`) —
so only a store written by another tool or edited by hand can produce it, and the
docstring at `otel.py:613-615` scopes its claim to what Bir records. Noted again
because the loader's acceptance is wider than the exporter's assumption.

The next two were driven for the first time by this audit.

**A currency code is stored and grouped exactly as given.** `_validate_currency`
(`bir/_config.py:298-303`) accepts any non-empty string with no normalization and
no length bound, and `_UsageTotals.add` (`bir/cli.py:257-273`) buckets by that
exact string. Six recordings that all meant dollars:

```
$ bir stats --json
   ' USD'          total=2.0
   'US Dollars'    total=2.0
   'USD'           total=4.0
   'Usd'           total=2.0
   'usd'           total=2.0
```

A 5,000-character currency is also accepted. Declined because the alternatives
are worse than the symptom. Case-folding or trimming would make the stored value
differ from what the application passed, which is the opposite of the boundary
this codebase just wrote down for identity fields — a `model` and an event `name`
are recorded as given for the same reason. Validating an ISO-4217 shape would
reject `set_cost(currency=...)` calls that work today, for a field whose only
consumer is a grouping key in one table. The rule that matters — costs are never
summed across currencies — holds exactly, and it is the rule that would be unsafe
to break.

**A non-UTF-8 byte in a store cannot be skipped.** `_iter_trace_events_from_file`
(`bir/_storage.py:225-248`) decodes strictly and iterates the file inside the
`with`, but outside the `try`, so a decode failure escapes past the `on_invalid`
callback that `--skip-invalid` installs. One byte flipped to `0xFF` inside a JSON
string on line 2 of a 3-line store:

```
bir traces                 exit 1  bir: 'utf-8' codec can't decode byte 0xff in position 470
bir traces --skip-invalid  exit 1  bir: 'utf-8' codec can't decode byte 0xff in position 470
bir stats / show / export-otel     the same, 0 of 3 traces readable
bir prune                  exit 1  the same; the store was left at its 990 bytes
```

The message also names neither the file nor the line, where every other read
error names both. Declined because Bir cannot produce this store. Every writer
goes through `json.dumps` with the default `ensure_ascii=True`, so what lands on
disk is pure ASCII whatever was recorded — measured with a name of
`"grüße 日本語 🙂"`, which round-tripped byte-identical from an 845-byte store that
`raw.isascii()` reports as True, with no Unicode normalization applied. A torn
write therefore cuts between ASCII characters and cannot leave a partial
multi-byte sequence, which is the one damage mode `--skip-invalid` exists for. A
store that fails to decode came from another tool, an editor, or the disk, and
none of those is a case the flag advertises.

Windows-specific paths were **not** driven. `_InterProcessFileLock`
(`bir/_storage.py:125-171`) takes a different branch there — `msvcrt.locking` with
`LK_LOCK` rather than `fcntl.flock` — and the two have different behavior under
sustained contention. Nothing here can measure that, so nothing is claimed about
it either way; CI's Windows leg is the only evidence this audit has. The
free-threaded build was likewise not driven: no free-threaded interpreter is
installed on this machine (`sysconfig.get_config_var("Py_GIL_DISABLED")` is `0`),
so `tests/test_free_threading.py` ran on the GIL build, where it proves the
uninteresting half by its own docstring's admission at `:10-12`. A clock that
steps backwards was not driven either: `_now()` reads the wall clock at an
event's start and again at its end with no monotonic guard, and the loader
rejects the result outright — `Trace file … line 1 has end_time before
start_time` (`bir/_storage.py:1329`) makes the whole store unreadable — but no
means of stepping the system clock was available here, so whether the writer can
be made to produce that event is untested and nothing is claimed about it.
`scripts/verify_release.py` was run and passed; its one historical
no-output failure did not reproduce, and no cause is invented for it here.

## Checked and found sound

Seven areas were driven and need no item. The numbers are here so the next audit
can see what this one's coverage actually was.

**The trace store filling a real disk.** A 1 MB HFS+ volume, the default
unbounded single-file configuration, and 4,000 traces recorded until
`f_bavail` reached 0. All 4,000 calls returned normally, no exception reached the
caller, 2,709 traces were written, and one `ERROR bir:` line named `[Errno 28]`
and said recording was paused — one message for the outage, not one per dropped
event. This is the "reporting rather than raising a failed trace-store write"
feature doing exactly what it says. Pruning the store it leaves behind, and
repairing it on the next append, both shipped from this audit. What remains is
that prune stages the survivors before replacing the original, so it needs free
space to free space: on a volume with literally no bytes left it fails at the
staging file with a clear `[Errno 28]`, while `--dry-run` still reports what is
reclaimable. 57 KB of headroom was enough to reclaim 876 KB. That is a property
of staging, which is what makes an interrupted prune safe, rather than a
defect.

**The experiment summary under the same pressure.** With the volume padded to
zero free space, re-running an experiment onto an existing summary raised
`OSError: [Errno 28]` from the staging file, and the previous summary was still
318 bytes, still parsed, byte-identical. `_write_experiment_summary`
(`bir/_eval_persistence.py:330-351`) stages and replaces, and it holds.

**Experiment concurrency at eight workers.** 200 examples with randomized
per-example sleeps, run at `max_workers=1` and `max_workers=8`. Both produced 200
results in dataset order, identical aggregates, and JSONL rows in identical
order: `rows_serial == rows_parallel`, 200 and 200. Ordering, aggregation, and
persistence do not depend on completion order.

**Sampling and the kill switch under contention.** Eight recorder threads against
one flipper calling `configure(enabled=…)` every 0.5 ms for 3 s: 149,377 traces
attempted, 0 exceptions escaped to a caller, 7,009 lines written, 0 unparseable,
7,009 distinct event ids. 579 child events were left without a root, which is the
documented semantic of a per-event kill switch — `bir/_sdk.py:257-261` and
`docs/site/sampling-service-metadata.md:113-114` both say a trace already in
flight when recording is disabled stops writing immediately, so an operator who
disables mid-trace keeps the part already written — and the read commands
already report those events. Sampling, decided once per trace root, produced none: 4,000
traces at `sample_rate=0.25` gave 1,020 roots (25.5%), 1,020 child events, 0
children whose root was sampled out, and 0 roots with no child. Flipped
deterministically inside a live trace, both behave as their docstrings say: a
trace in flight when recording is disabled writes nothing at all, a trace started
while disabled stays off after re-enabling, and a sampling rate changed mid-trace
does not alter the decision already made.

**Unicode end to end.** A name of `"grüße 日本語 🙂"` produced an 845-byte store
that is pure ASCII, round-tripped byte-identical through `load_traces`, and was
not normalized. A surrogate-escaped string of the kind `os.fsdecode` returns
(`'report-\udcff.pdf'`) was recorded through `trace`, `generation`, `retrieval`,
`span` metadata, and a dataset `example_id` without raising; the store stayed
ASCII; `load_events`, `load_traces`, `bir traces`, `bir show`, `bir stats`, and
all three `--json` forms exited 0 with valid UTF-8 on stdout. The human-rendered
tables write the escaped byte back out unchanged, which round-trips the filename
rather than losing it, and was identical under an unset locale, `LANG=en_US.UTF-8`,
and `LC_ALL=C`. Every file the SDK opens passes `encoding="utf-8"` explicitly —
10 of 10 text opens across `_storage.py`, `_eval_persistence.py`, and
`_eval_models.py` — so no read or write depends on the locale. The one command
that cannot survive it is `experiment-report --output`, which is item 1.

**Very large usage figures.** `set_usage(input_tokens=2**63)` and
`set_usage(input_tokens=10**30)` were both accepted, and `bir stats --json`
reported their exact sum, `{'input': 1000000000009223372036854775808, ...}`,
which re-parsed to the same integer: Python ints throughout, no float rounding
anywhere in the accumulation (`bir/cli.py:257-273`).

**A read-only store directory.** With the trace directory at mode `0o500` and the
store file already present, recording continued normally and both traces were
readable afterwards — the append re-opens an existing file, which POSIX permits.
`bir prune` raised `[Errno 13] Permission denied` on the staging file it could
not create there, which is the documented behavior for a command invoked for its
effect.

The previously declined memory profile of the loaders was re-measured and has not
moved: `load_events` peaks at 22,761 KiB and `load_traces` at 24,162 KiB for
5,000 events, identical to the last five audits.

The OTLP dual-spelling transition is unchanged: the extra still installs
`opentelemetry-sdk` 1.44.0 with `opentelemetry-semantic-conventions` 0.65b0, the
same release the last audit measured against, so the superseded
`deployment.environment` and `gen_ai.system` spellings pinned by
`tests/test_otel_integration.py:231-281` still have a live consumer and stay.

`grep -rn "TODO\|FIXME\|XXX" src/` is still empty; it stays a dead end.
