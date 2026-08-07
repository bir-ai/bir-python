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

- 17,395 lines of runtime source across 19 dependency-free integration modules
  plus the core, evaluation, storage, transport, and CLI modules;
- 1,627 tests in 47 files at 93.90% branch coverage, with a CI floor, strict
  resource-warning handling, Ruff lint/format, Pyright, strict MkDocs, example
  smoke tests, and hermetic wheel/sdist release verification;
- CI across Linux, Windows, and macOS on Python 3.10–3.14, a free-threaded 3.14
  leg on Linux, plus strict docs and shared-fixture drift checks;
- 13 CLI commands, two conformance matrices covering every shipped integration, a
  published API stability policy guarded against drift, a deprecation mechanism,
  a benchmark harness with baseline comparison, and a recorded decision on
  distributed trace context.

Every item from the 2026-08-06 list shipped, and so has every item from the
2026-08-07 list that replaced it. That list was re-derived from the current code,
from a coverage run, from `scripts/benchmarks.py --repeat 3`, from driving the CLI
against stores built for the purpose, and from measurements written for the audit;
the next one has to be derived the same way rather than continued from it.

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

Nothing is open. Every item from the 2026-08-07 list has shipped — the
log-correlation filter, the store that could not be written, and experiment
results surviving a stopped process — and all three are in `CHANGELOG.md`.

Two of them carry a lesson worth keeping. The store item's Why said no test
pinned the old behavior; that was wrong, because
`test_storage_errors_are_not_swallowed` pinned it deliberately. The experiment
item did not claim there was no such test, and there was one:
`test_cancellation_cleans_up_children_without_writing_summary` pinned that a
cancelled async run left no result file at all, which streaming rows changes.
Check for a pinning test before starting, and when one exists, say whether the
work fills a gap or reverses a decision.

## Sequencing

Nothing is queued. The next list has to be re-derived from the code rather than
continued from this one.

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
are seen, reporting rather than raising a failed trace-store write, and flushing
each finished example's result row so an interrupted experiment keeps it.
Regressions in those areas are bugs; new scope requires a new issue with current
evidence.

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
  86.79%, `autogen` 88.41%, against a 93.90% total — but the missing lines are
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
