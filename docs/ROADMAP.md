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

The repository now has:

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

The 2026-08-06 audit re-derived its list from the current code, from a coverage
run, from `scripts/benchmarks.py --repeat 3`, from driving all 13 CLI commands
against stores built for the purpose, and from measurements written for it. Every
item it produced has since shipped, and the counts above are as of that work
rather than as of the audit.

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

None. Every item from the 2026-08-06 audit has shipped: the three P1s — capture
failures escaping into the traced call, the quadratic PEM redaction rule, and the
unfinished bridge run that hid every later trace — plus the damaged experiment
summary that hid the whole experiment store, the unbounded `.sent` upload
sidecar, and the export path that materialized the store. All are in
`CHANGELOG.md`.

Beta readiness is tracked on the checklist in `docs/site/stability.md`, not here.
Its remaining entries are outside this repository's reach or are release
mechanics: confirming the event-schema `1.0` contract against the current
`bir-app` release, writing the migration note for the public changes since
`0.3.0`, and raising the version and the `Development Status` classifier.

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

Two of this audit's own claims needed correcting as the work was done, and the
next audit should expect the same of itself. The bridge item's Done-when was
written before the design work that showed one clause of it could not hold, and
the `export-otel` item implied the whole 18.95 MiB peak was Bir's when roughly
half is the OpenTelemetry SDK's own span and encoding cost. Re-derive every claim
from the current code before writing it down, and state what a measurement
actually attributes.

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
  frames as environment-specific. The one bridge behavior that was a defect — a
  run whose end callback never arrives — was fixed on its own.
- Adding a public streaming read API. `load_events` peaks at 22,761 KiB and
  `load_traces` at 24,162 KiB for 5,000 events in this audit's benchmark run, but
  both are documented to return complete lists and `_iter_trace_events` already
  serves every internal caller. Changing that is an API proposal, not a repair.
