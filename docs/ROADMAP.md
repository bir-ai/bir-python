# Bir Python SDK — Improvement Roadmap

> Current baseline: **v0.3.0**, audited **2026-08-06**.
>
> This document contains only work that is still open. Completed work belongs in
> `CHANGELOG.md`; implementation details and copy-paste task prompts belong in
> issues, not in the roadmap. Re-verify every item against the current code before
> starting it because integrations and provider APIs change independently.

## Current baseline

Bir is an alpha-stage, local-first tracing and deterministic-evaluation SDK for
Python 3.10–3.14. The runtime package has no third-party dependencies, ships PEP
561 typing metadata, and records schema-version `1.0` JSONL events.

At this audit the repository has:

- 16,311 lines of runtime source across 19 dependency-free integration modules
  plus the core, evaluation, storage, transport, and CLI modules;
- 1,504 tests in 41 files at 93.29% branch coverage, with a CI floor, strict
  resource-warning handling, Ruff lint/format, Pyright, strict MkDocs, example
  smoke tests, and hermetic wheel/sdist release verification;
- CI across Linux, Windows, and macOS on Python 3.10–3.14, a free-threaded 3.14
  leg on Linux, plus strict docs and shared-fixture drift checks;
- 13 CLI commands, two conformance matrices covering every shipped integration, a
  published API stability policy guarded against drift, a deprecation mechanism,
  a benchmark harness with baseline comparison, and a recorded decision on
  distributed trace context.

This list is not a continuation of the 2026-08-06 list, every item of which
shipped. It was re-derived from the current code, from a coverage run, from
`scripts/benchmarks.py --repeat 3`, from driving all 13 CLI commands against
stores built for the purpose, and from measurements written for this audit. Each
item below states what was run and what it produced.

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
| --- | --- | --- | --- | --- | --- |
| 1 | Stop capture failures escaping into the traced call | P1 | S | A traced call returns its own result whatever the captured value does | — |
| 2 | Keep an unfinished bridge run from hiding every later trace | P1 | M | Traces recorded after an abandoned framework run stay findable | — |
| 3 | Bound the PEM redaction rule | P1 | S | Redaction cost stays linear in the size of the captured value | — |
| 4 | Stop one damaged summary hiding the whole experiment store | P2 | M | Intact experiments stay readable, and a killed write keeps the old summary | — |
| 5 | Compact the `.sent` upload sidecar | P2 | M | Local bookkeeping is bounded by the store, not by history | — |
| 6 | Stop `bir export-otel` materializing the store | P3 | M | The last whole-store read path streams like the others | — |

## Work item details

### 1. Stop capture failures escaping into the traced call

**Why:** `_safe_capture` runs the captured object's own code — `Mapping.items()`
in `_capture_mapping` (`src/bir/_capture.py:108`) and iteration in
`_capture_sequence` (`src/bir/_capture.py:130`) — with no guard, while the same
module already refuses to let `__repr__` escape (`src/bir/_capture.py:156`). An
object whose `items()` or `__iter__` raises is ordinary: a config client, an ORM
row proxy, a lazily-loading result set.

Measured with `configure(capture_inputs=True, capture_outputs=True)` and a
`Mapping` whose `items()` raises `ConnectionError`:

```
captured input   -> raised ConnectionError: backend unavailable
captured output  -> raised ConnectionError: backend unavailable
events recorded: [('trace', 'on_input', 'error')]
bad __repr__ ->  ok
```

Passed as an argument, the decorated body never runs, the caller gets Bir's
exception, and the trace is written with `status="error"` blaming the user's
function for a failure inside the SDK. Returned as a result, the function ran to
completion and produced its value, but the caller gets the exception instead and
no event is written at all. The same object behind a failing `__repr__` records
`<unrepresentable BadRepr>` and returns normally, which `tests/test_sdk.py:4993`
already pins — the capture path has decided this question once and applied the
answer in one of three places.

**Scope:**

- Guard every point where capture invokes code owned by the captured value —
  mapping iteration, sequence iteration, key stringification — the way
  `_safe_repr` is guarded, recording a visible marker instead of propagating.
- Keep the marker distinguishable from an absent value, so a failed capture is
  never read as "nothing was passed".
- Apply it to every capture path: inputs, outputs, event metadata, prompt
  records, and dataset/experiment capture.
- Tests driving a mapping whose `items()` raises and a sequence whose `__iter__`
  raises through `@observe`, each context manager, and `run_experiment`,
  asserting the caller's result and the recorded status.

**Done when:** with capture enabled, a traced call whose argument or return value
raises from `items()`, iteration, or `__repr__` returns its own result, records
its own status, and records the marker; no exception raised by a captured value
reaches the caller.

### 2. Keep an unfinished bridge run from hiding every later trace

**Why:** when a framework starts a run with no Bir trace active, the bridge opens
an implicit trace root and enters it (`_implicit_trace_context`, e.g.
`src/bir/integrations/langchain.py:235`, `src/bir/integrations/llamaindex.py:155`).
If the framework never emits the terminal callback, that context is never exited:
its root event is never written, and `_current_trace_id` stays set for the life of
the context. Everything recorded afterwards joins a trace whose root does not
exist, and `_traces_from_events` drops a rootless trace without a word
(`src/bir/_storage.py:310`).

Driving each declared bridge's own generation `start` from
`tests/test_integration_contract.py` with no matching end, then recording one
unrelated `@observe()` call:

```
bridge                     events  roots  load_traces   later_work recorded as
langchain.callbacks             1      0            0                 ['span']
llamaindex.callbacks            1      0            0                 ['span']
openai_agents.processor         1      0            0                 ['span']
pydantic_ai.processor           1      0            0                 ['span']
crewai.event_bus                1      0            0                 ['span']
haystack.tracer                 1      0            0                 ['span']
```

Six of the seven declared bridges; AutoGen hands over a finished call in one
callback and cannot be left open. Against such a store, `bir traces` prints "No
traces found", `bir show <trace_id>` exits 1 with "not found", `bir stats` reports
`traces.total = 0`, and `load_traces()` returns `[]` — while `load_events()` still
returns the events, so the data is on disk and unreachable through every
trace-oriented path. Nothing self-heals. The registry is unbounded too: 20,000
abandoned runs retained 20,001 entries and 21.8 MiB.

The bridge contract already has a case for this state
(`tests/integration_bridge_contract.py:383`), but it asserts only that nothing is
written, and it drives the run inside `contextvars.copy_context()` so the
unbalanced context cannot reach the rest of the suite. A user's process has no
copied context to retreat into.

**Scope:**

- Give the bridges a shared way to finish or discard an abandoned run, so an
  unfinished one cannot own the ambient trace context indefinitely.
- Bound the per-handler `_active_runs` registry so a framework that stops
  emitting terminal callbacks cannot grow it without limit.
- Make a rootless trace visible on the read side instead of silently dropped — at
  minimum a stderr diagnostic from the CLI read commands, in the shape
  `--skip-invalid` already uses to report skipped lines.
- Add a bridge-contract case asserting that after an abandoned run, a later
  unrelated trace is still recorded with its root and found by `load_traces()`.

**Done when:** for every bridge in the matrix, an abandoned run leaves a
subsequent unrelated `@observe()` call recorded as a trace root that
`load_traces()` and `bir traces` find; the handler's registry does not grow
without bound; and events whose trace root is missing are reported rather than
dropped in silence.

### 3. Bound the PEM redaction rule

**Why:** the PEM rule at `src/bir/_capture.py:200` ends with `.*?`, so every
`-----BEGIN … PRIVATE KEY-----` marker that never gets a matching `END` makes the
engine scan the remainder of the value. Cost is the product of the value's size
and the number of unterminated markers. Timing each built-in pattern against 32 KB
and 64 KB of an adversarial payload built from its own prefix:

```
pattern                    32 KB     64 KB   growth
authorization label       0.44ms    0.85ms     2.0x
labeled secret            0.61ms    1.22ms     2.0x
bearer                    0.53ms    1.05ms     2.0x
sk- key                   0.42ms    0.83ms     2.0x
jwt                       0.70ms    1.29ms     1.9x
aws akid                  0.45ms    0.91ms     2.0x
google                    0.57ms    1.16ms     2.0x
slack                     0.37ms    0.73ms     2.0x
github                    0.37ms    0.74ms     2.0x
stripe                    0.42ms    0.82ms     1.9x
b64 86==                  0.54ms    1.09ms     2.0x
PEM block               128.51ms  504.93ms     3.9x  <-- superlinear
card PAN                  0.93ms    1.78ms     1.9x
```

Every other rule is linear; this one quadruples when the input doubles. Doubling
further, on a value that is nothing but unterminated headers: 128,000 characters
→ 2.0 s, 256,000 → 8.4 s, 512,000 → 31.1 s. Timing the PEM pattern by itself
accounts for essentially all of it (121.8 ms of the 129.7 ms at 32,000
characters, 31.8 s of the 31.1 s at 512,000 — the two runs are the same
measurement to within noise).

Density matters, and honestly so: ~131,000 characters of prose cost 18.8 ms with
no markers, 20.9 ms with 10, 56.1 ms with 100, 534.7 ms with 1,000, and 2.1 s when
the value is nothing else. So a document that mentions a few keys is fine; a value
made largely of unterminated headers is not, and that value can arrive from a
caller. End to end, through the public API with `capture_inputs=True` and
`max_value_length=200`:

```
128,000-character argument, max_value_length=200
  capture on :   2127.6 ms   recorded input = 232 chars
  capture off:      0.3 ms
```

`max_value_length` gives no protection, by design: truncation runs after
redaction so a secret can never be split across the cut
(`src/bir/_capture.py:86`). Two seconds bought a 232-character record. A complete
1,662-character key redacts in 0.17 ms, so the rule's real job is cheap; the cost
is entirely in the case where it finds no `END`.

**Scope:**

- Bound the PEM rule's span so its cost is linear in the size of the captured
  value regardless of how many unterminated headers it contains.
- Keep redaction behavior identical for real keys, including the label variants
  `tests/test_custom_redaction.py:219` pins.
- Add a benchmark case that scales the captured value's *size*: `capture_redaction`
  today repeats one small fixed dict (`scripts/benchmarks.py:67`), so it measures
  per-call cost and no baseline comparison can see this class of regression.
- A test pinning the scaling, not just the correctness.

**Done when:** redacting a 512,000-character value made entirely of unterminated
PEM headers costs within a small constant factor of redacting the same length of
prose, real keys still redact unchanged, and the benchmark harness has a case that
would fail on a regression.

### 4. Stop one damaged summary hiding the whole experiment store

**Why:** `list_experiments` parses every `*.summary.json` in the directory eagerly
(`src/bir/_eval_persistence.py:121`), so one unreadable summary raises for the
whole listing — and `experiment-show` and `experiment-report` both resolve their
target through it. Measured with three experiments whose summaries were valid,
then one truncated to half its length:

```
bir experiments --dir exp                 -> bir: Invalid JSON in experiment summary exp/beta.summary.json (exit 1)
bir experiments --dir exp --json          -> same, exit 1
bir experiment-show <intact-id> --dir exp -> same, exit 1
```

Two intact experiments become unreachable through the CLI because of an unrelated
third file. There is no escape hatch: `bir experiments --skip-invalid` fails with
"unrecognized arguments", while the trace read commands grew exactly that flag for
exactly this failure.

The SDK can produce the state itself. `_write_experiment_summary` is a truncating
`path.write_text` with no temp-and-rename (`src/bir/_eval_persistence.py:253`),
unlike every other writer in the SDK — the sent-ID sidecar
(`src/bir/_storage.py:1128`) and prune's staging (`src/bir/_storage.py:987`) both
stage and replace. A write failing part-way reproduces it deterministically: a
valid 255-byte summary became 127 bytes, `load_experiment_summary()` on it raised,
and `list_experiments()` on its directory raised with it.

A damaged *result* file is correctly scoped by comparison — it fails only
`experiment-show` for that experiment and leaves the listing intact — which is
what the summary path should also do.

**Scope:**

- Write the summary through a temp file and rename, so a killed or failed write
  leaves the previous summary intact.
- Give `experiments`, `experiment-show`, and `experiment-report` the same
  `--skip-invalid` contract the trace read commands have: skip what cannot be
  read, report the count and the first message on stderr, keep `--json` on stdout
  parseable.
- Keep `load_experiment_summary()` and `load_experiment()` strict by default,
  matching the public loaders on the trace side.
- Tests for a truncated summary, a summary that parses but fails validation, and a
  summary write interrupted part-way.

**Done when:** `bir experiments`, `bir experiment-show <intact id>`, and
`bir experiment-report <intact id>` all succeed against a directory holding one
damaged summary and report what they skipped; and an interrupted summary write
leaves the previous summary readable.

### 5. Compact the `.sent` upload sidecar

**Why:** `--mark-sent` records accepted event IDs in `<trace_path>.sent` as one
JSON array. `_record_sent_ids` loads the whole set, merges, sorts, and rewrites
the file on every checkpoint (`src/bir/_storage.py:1128`), and nothing ever
removes an ID. `bir prune` — the command whose entire job is bounding local state
— does not touch it.

Measured by recording 2,000 traces, sending with `mark_sent=True`, pruning to
`--keep-last 1`, and repeating:

```
round1: sidecar 156,016 B / 4,000 ids
pruned: store 695 B (1 trace); sidecar unchanged at 156,016 B / 4,000 ids
round2: store 695 B  sidecar 312,016 B  ids  8,000
round3: store 695 B  sidecar 468,016 B  ids 12,000
round4: store 695 B  sidecar 624,016 B  ids 16,000
round5: store 695 B  sidecar 780,016 B  ids 20,000
```

The sidecar reached 1,122× the size of the store it describes and grows linearly
forever, every entry of it naming an event that no longer exists. Each send reads,
merges, sorts, and rewrites all of it. The docs say the loaded ID set "grow[s]
with the number of IDs" (`docs/site/sending.md:45`), which is true of one send;
nothing says the file never shrinks, and nothing offers a way to shrink it.

**Scope:**

- Drop sidecar entries for events no longer present when prune rewrites the store,
  under the existing lock ordering (trace lock first, sidecar lock second —
  `src/bir/_storage.py:120`).
- Keep the sidecar advisory: a missing or corrupt one still means "nothing sent",
  so compaction can never block or fail a send.
- Document what bounds it, on the sending page and in `send_events`' docstring.
- Tests: a prune that removes traces removes their IDs; a prune that keeps a trace
  keeps its ID; a re-send after compaction still skips everything still in the
  store.

**Done when:** after `bir prune`, the sidecar holds IDs only for events still in
the selected store files, and a repeated record/send/prune cycle leaves it bounded
by the store rather than by history.

### 6. Stop `bir export-otel` materializing the store

**Why:** `export-otel` is the last read command that loads whole traces.
`_read_traces` builds the list (`src/bir/cli.py:150`) and `export_traces_to_otlp`
walks the traces twice — once to resolve the resource context, once to emit
(`src/bir/integrations/otel.py:101`) — so the argument has to be a list even
though the signature already accepts an iterable. Measured on a 1.54 MiB store of
4,000 events across 2,000 traces, the shape the repo's own `cli_*` benchmark cases
build, exporting to a local sink:

```
bir stats        peak  2.12 MiB
bir traces       peak  1.95 MiB
bir export-otel  peak 18.95 MiB
```

Nine times the streaming commands and twelve times the store on disk. The
streaming work that fixed `traces`, `show`, and `stats` deliberately left this
one, and the harness cannot see it: there is no `export_otel` benchmark case.

**Scope:**

- Let the export make two streaming passes over the store instead of holding it,
  keeping the public `LoadedTrace`, iterable, and path argument forms working
  unchanged.
- Keep the `--json` summary's `traces` count correct without retaining the traces.
- Add an `export_otel` benchmark case beside `cli_traces`/`cli_stats`/`cli_show`
  so the ceiling stays visible.

**Done when:** `bir export-otel` peak memory on the benchmark store is in the same
order as `bir stats`, exported span counts and attributes are unchanged, and the
benchmark harness covers it.

## Sequencing

Items 1 and 3 both live in the capture path, are independent of everything else,
and are the smallest changes here; take them first — 1 because it turns a working
call into a failure, 3 because it is one regex plus a benchmark case. Item 2 is
next and is the largest correctness change, touching six bridges, the shared
contract, and the read side's silent drop. Items 4 and 5 are independent of each
other and of the rest: 4 continues the trace-side `--skip-invalid` work on the
experiment store, 5 belongs with prune. Item 6 is last — a performance ceiling
rather than a defect, and the only item whose fix is confined to one command.

Nothing here blocks anything else, so the order is about payoff rather than
dependency.

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
transport and experiment-loading error paths, and the free-threaded CI leg.
Regressions in those areas are bugs; new scope requires a new issue with current
evidence.

This audit also declined three things it looked at:

- Retiring the flat re-exports in `bir.integrations.__all__`, where `trace_chat`
  resolves to whichever provider the package imported last. It is a product
  decision about a user's migration budget, not engineering work, and it belongs
  in an issue. The previous audit reached the same conclusion.
- Raising coverage on the framework bridges for its own sake. They are the
  package's weakest modules — `llamaindex` 82.45%, `langchain` 85.52%, `crewai`
  87.10%, `autogen` 88.41%, against a 93.29% total — but reading the missing lines
  shows them to be alternative provider-shape readers (`get_content`/`get_text`
  fallbacks, alternate usage key spellings), which the stability page already
  frames as environment-specific. Item 2 covers the one bridge behavior that is a
  defect.
- Adding a public streaming read API. `load_events` peaks at 22,761 KiB and
  `load_traces` at 24,162 KiB for 5,000 events in this audit's benchmark run, but
  both are documented to return complete lists and `_iter_trace_events` already
  serves every internal caller. Changing that is an API proposal, not a repair.
