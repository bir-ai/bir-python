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

- ~15,800 lines of runtime source across 19 dependency-free integration modules
  plus the core, evaluation, storage, transport, and CLI modules;
- 1,413 tests in 36 files at 92.3% branch coverage, with a CI floor, strict
  resource-warning handling, Ruff lint/format, Pyright, strict MkDocs, example
  smoke tests, and hermetic wheel/sdist release verification;
- CI across Linux, Windows, and macOS on Python 3.10–3.14, plus strict docs and
  shared-fixture drift checks;
- two conformance matrices covering every shipped integration, a published API
  stability policy guarded against drift, a benchmark harness with baseline
  comparison, and a recorded decision on distributed trace context.

Everything on the 2026-07-31 list shipped or was decided. This audit re-derived
the list below from the current code and from measurements taken with
`scripts/benchmarks.py`; it is not a continuation of the previous one.

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

None. Every item from the 2026-08-06 audit has shipped.

Beta readiness is tracked on the checklist in `docs/site/stability.md`, not here.
Its remaining entries are outside this repository's reach or are release
mechanics: confirming the event-schema `1.0` contract against the current
`bir-app` release, writing the migration note for the public changes since
`0.3.0`, and raising the version and the `Development Status` classifier.

Two things the audit turned up that are decisions rather than defects, and so
belong in issues rather than here:

- Retiring the flat re-exports in `bir.integrations.__all__`, where `trace_chat`
  resolves to whichever provider the package imported last. The deprecation
  machinery for it exists; whether to spend a user's migration budget on it does
  not follow from the code.
- Bounding `bir export-otel`, the one read path still loading whole traces,
  which means changing the exporter's list-taking signature.

Two of the audit's own claims were wrong and were corrected as the items were
worked: `eval-gate` already emitted JSON, and the concurrent-write test the
free-threading item asked for already existed in `test_sdk.py`. The next audit
should re-derive its list from the current code and verify each claim against it
before writing it down.

## Explicitly not on the backlog

The following shipped features must not be reopened merely because they appeared
in an older generated roadmap: sdist verification, stats filters, the master kill
switch, Ollama, prune, fuzzy similarity, config inspection, `SECURITY.md`, richer
OTLP attributes, experiment timeouts, both conformance matrices, event-bridge
parenting from the framework's own run ids, the published API stability policy,
the performance benchmark harness, the trace-context decision
([ADR 0001](adr/0001-distributed-trace-context.md)), reading a damaged store with
`--skip-invalid`, streaming the CLI read commands, the deprecation mechanism,
machine-readable output for every command that produces a result, coverage of the
transport and experiment-loading error paths, and the free-threaded CI leg. Regressions in those areas
are bugs; new scope requires a new issue with current evidence.
