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
| 1 | Stop `bir export-otel` materializing the store | P3 | M | The last whole-store read path streams like the others | — |

Shipped since this audit was written: all three P1s — capture failures escaping
into the traced call, the quadratic PEM redaction rule, and the unfinished bridge
run that hid every later trace — plus the damaged experiment summary that hid the
whole experiment store and the unbounded `.sent` upload sidecar. All are in
`CHANGELOG.md`.

One clause of the bridge item's stated goal was not achievable and was not
attempted. It asked that an abandoned run leave *a subsequent `@observe()` call*
recorded as its own root. Nothing distinguishes "the run is still executing" from
"the run is gone" at the moment that call arrives, and treating it as gone would
break the documented, contract-tested rule that an application's work nests under
an open framework run. The two signals that do exist — the framework starting new
top-level work, and the registry filling up — are what shipped, and the state in
between is now reported by the reader instead of being silent. Making the
remaining case recover would require a heuristic (a per-run age bound is the only
candidate) and belongs in an issue with a decision behind it, not here.

## Work item details

### 1. Stop `bir export-otel` materializing the store

**Why:** `export-otel` is the last read command that loads whole traces.
`_read_traces` builds the list (`src/bir/cli.py:178`) and `export_traces_to_otlp`
walks the traces twice — once to resolve the resource context, once to emit
(`src/bir/integrations/otel.py:102`) — so the argument has to be a list even
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

One item is left, and it depends on nothing: a performance ceiling rather than a
defect, and the only one whose fix is confined to a single command.

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
