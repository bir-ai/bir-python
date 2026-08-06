# Bir Python SDK — Improvement Roadmap

> Current baseline: **v0.3.0**, audited **2026-08-07**.
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

- 17,185 lines of runtime source across 19 dependency-free integration modules
  plus the core, evaluation, storage, transport, and CLI modules;
- 1,598 tests in 45 files at 93.64% branch coverage, with a CI floor, strict
  resource-warning handling, Ruff lint/format, Pyright, strict MkDocs, example
  smoke tests, and hermetic wheel/sdist release verification;
- CI across Linux, Windows, and macOS on Python 3.10–3.14, a free-threaded 3.14
  leg on Linux, plus strict docs and shared-fixture drift checks;
- 13 CLI commands, two conformance matrices covering every shipped integration, a
  published API stability policy guarded against drift, a deprecation mechanism,
  a benchmark harness with baseline comparison, and a recorded decision on
  distributed trace context.

Every item from the 2026-08-06 list shipped. This list is not a continuation of
it: it was re-derived from the current code, from a coverage run, from
`scripts/benchmarks.py --repeat 3`, from driving the CLI against stores built for
the purpose, and from measurements written for this audit. Each item below states
what was run and what it produced.

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
| 1 | Make the log-correlation filter work where it is documented to | P1 | S | The documented one-liner stamps application logs instead of dropping them | — |
| 2 | Stop a store that cannot be written from failing the traced call | P1 | M | A call that succeeded returns its result even when recording fails | — |
| 3 | Make experiment results survive a stopped process | P2 | S | An interrupted run keeps the examples it already finished | — |

## Work item details

### 1. Make the log-correlation filter work where it is documented to

**Why:** `install_trace_id_filter()` with no argument attaches the filter to the
root logger (`src/bir/logging.py:99`), and both its own docstring and the module
example present that as the way to use it — "enough for the common case where
application loggers propagate to root". A logger's filters run only for records
that logger creates. A record from `logging.getLogger("myapp")` propagates to the
root logger's *handlers*, never through the root logger's *filters*, so it is
never stamped.

The documented recipe therefore does not merely lose the ids: the documented
format string cannot render, and the log line is discarded. Running the module
docstring's own example verbatim:

```
install_trace_id_filter()  [documented]    -> LINE LOST
                                              stderr: ValueError: Formatting field not found in record: 'bir_trace_id'
install_trace_id_filter(handler)           -> [trace=4798a778-…] application log line
install_trace_id_filter(app logger)        -> [trace=977224a8-…] application log line
```

Only a record created by the root logger itself is stamped:

```
  myapp    has bir_trace_id: False
  root     has bir_trace_id: True
```

The tests never exercise it. `test_install_defaults_to_root_logger`
(`tests/test_logging.py:149`) asserts only that the filter object appears in
`root.filters`; every behavioral case attaches the filter directly to the logger
that emits, which is the one arrangement where a logger-level filter does run.

**Scope:**

- Make the no-argument call stamp the records an application actually emits.
  Attaching to the root logger's handlers is the arrangement that works today, so
  the fix is a question of what the default should attach to, not of new
  machinery.
- Keep `install_trace_id_filter(logger_or_handler)` working as it does, including
  the returned filter being removable from what it was attached to.
- Say plainly in the docstring which records a given target stamps, since that is
  what was wrong rather than the code alone.
- A test that emits from a child logger through a root handler and asserts the
  record carries the ids — the arrangement the docs recommend, which nothing
  covers now.

**Done when:** the module docstring's example, run verbatim, prints an
application logger's line with the active trace id; a child logger's records are
stamped without naming a target; and the case is covered by a test that emits
rather than one that inspects `filters`.

### 2. Stop a store that cannot be written from failing the traced call

**Why:** `_write_event` (`src/bir/_sdk.py:2002`) appends through `_append_event`
(`src/bir/_storage.py:653`), and an `OSError` from that append propagates out of
the context manager to the caller. When the traced body raised, the SDK already
prefers the caller's own exception (`raise exc from storage_error`,
`src/bir/_sdk.py:1072`) — so the question has been half-answered. When the body
*succeeded* there is no competing exception, and the storage error is what the
caller gets.

Measured against a trace directory with no write permission, every recording
entry point turns a completed call into a failure:

```
@observe (sync)                              -> BROKE: PermissionError
@observe (async)                             -> BROKE: PermissionError
@observe (generator)                         -> BROKE: PermissionError
trace() context manager                      -> BROKE: PermissionError
span() context manager                       -> BROKE: PermissionError
generation() context manager                 -> BROKE: PermissionError
tool_call() context manager                  -> BROKE: PermissionError
retrieval() context manager                  -> BROKE: PermissionError
bir.score()                                  -> BROKE: PermissionError
```

A function that charged an order returned `PermissionError` to its caller. The
conditions that produce it are deployment conditions rather than programming
mistakes: a read-only container filesystem, a full disk, a `.bir/` owned by
another user, an unmounted ephemeral volume. Nothing in the docs decides that
recording failures should surface this way, and no test pins it — the `OSError`
cases in the suite cover `prune`, which is an explicit destructive command the
user invoked and where raising is right.

This is the same class as the capture failures already fixed, one layer down:
there it was reading the value, here it is writing the event.

**Scope:**

- Decide and implement what a failed append does to the traced call. It must not
  be "raise into a caller whose call succeeded"; silence is also wrong, since a
  store that is not being written is worth knowing about.
- Keep the existing precedence: a body that raised still surfaces its own
  exception, never Bir's.
- Keep explicit commands strict. `prune`, `send`, and the loaders are invoked for
  their effect and must keep reporting failure.
- Make a persistently unwritable store visible without a per-event cost —
  repeating the same warning for every event of every trace is its own failure.
- Tests covering each entry point above against an unwritable store, and one that
  the body's own exception still wins.

**Done when:** with an unwritable store, every recording entry point returns its
own result (or re-raises its body's own exception), the operator can tell that
recording is failing, and no traced call raises an error that came from Bir.

### 3. Make experiment results survive a stopped process

**Why:** `run_experiment` opens the result file once and writes each example's row
into it as the run proceeds (`src/bir/evals.py:669`), which is the right shape —
`raise_on_error` depends on results being persisted through the failing example.
But the handle keeps default buffering for the whole run, so rows reach the disk
only when the file is closed. A process stopped without unwinding loses all of
them.

Measured on a 20-example run stopped after 10 examples had completed, against the
trace store under the same signal:

```
  SIGTERM: result rows on disk = 0   (10 examples had completed)
  SIGINT:  result rows on disk = 10
  SIGKILL: result rows on disk = 0

  SIGTERM: trace events on disk = 11
```

`SIGINT` survives because Python unwinds and closes the file. `SIGTERM` is how an
orchestrator stops a process — a pod eviction, `docker stop`, a cancelled CI job —
and Python's default handler exits without unwinding, so the buffer goes with it.
The trace store is unaffected because each append opens, writes, and closes.

The cost is proportional to how long a run takes, which for an evaluation over a
model API is the whole point of the feature: an hour of paid model calls can be
on the wrong side of that buffer. The guardrail asking for crash-safe local
persistence is met by the trace store and not by this writer.

**Scope:**

- Get a completed example's row onto the disk before the next example starts, so
  what survives an interruption is what had finished.
- Keep the write cost proportional to examples rather than to their size; an
  evaluation run is not a hot loop, but a flush per row is the bound to stay
  inside.
- Cover the threaded and async runners, which persist through
  `src/bir/_eval_persistence.py:221` rather than the loop above.
- A test that stops a run without unwinding and asserts the completed rows are
  readable.

**Done when:** a run stopped by `SIGTERM` after N examples leaves N readable
result rows, and the sync, threaded, and async runners all behave that way.

## Sequencing

Item 1 is the smallest and is self-contained in one module and its tests; take it
first. Item 2 is next: it is the one that changes a decision rather than a
mechanism, so it wants the most thought, and it touches every recording entry
point. Item 3 is independent of both and can be done whenever.

Nothing here blocks anything else.

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
`--skip-invalid`, compacting the upload sidecar on prune, and streaming
`bir export-otel`. Regressions in those areas are bugs; new scope requires a new
issue with current evidence.

This audit looked at four more things and declined them:

- Recording an early-closed provider stream as an error. A consumer that stops
  reading mid-stream leaves the generation with `status="error"` and an empty
  message, which reads oddly beside `@observe`, where closing a generator early is
  recorded as a *successful* trace. It is not a defect: the shared wrapper
  contract asserts the error status deliberately
  (`tests/integration_contract.py:668`), with the reasoning written down. Whether
  the two should agree is a product question for an issue. The empty message is
  the part worth revisiting there, since `str(GeneratorExit())` is `""` and the
  event says nothing about why it failed.
- Clearing a configured scalar. `configure(service_name=None)` cannot unset a
  previously configured `service_name`, because `None` is the "leave unchanged"
  sentinel; the collection-valued options each document an empty value that
  clears them, and the scalars have no equivalent. Measured: setting then
  "clearing" leaves `checkout-api` in place. That is an API-shape proposal, not a
  repair.
- Raising coverage on the framework bridges for its own sake. They remain the
  package's weakest modules — `llamaindex` 82.51%, `langchain` 85.71%, `crewai`
  86.79%, `autogen` 88.41%, against a 93.64% total — but the missing lines are
  alternative provider-shape readers, which the stability page already frames as
  environment-specific.
- A public streaming read API. `load_events` peaks at 22,761 KiB and `load_traces`
  at 24,162 KiB for 5,000 events in this audit's benchmark run, but both are
  documented to return complete lists and the internal iterator already serves
  every caller inside the package. Changing that is an API proposal.

Two areas were checked and found sound, so they need no item: concurrent recording
into a rotating store from eight threads produced 145 events with no torn lines
and no exceptions, and `run_experiment(max_workers=4, record_traces=True)` gave
each example its own isolated trace. `grep -rn "TODO\|FIXME\|XXX" src/` is still
empty; it stays a dead end.
