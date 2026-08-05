# Bir Python SDK — Improvement Roadmap

> Current baseline: **v0.3.0**, audited **2026-07-31**.
>
> This document contains only work that is still open. Completed work belongs in
> `CHANGELOG.md`; implementation details and copy-paste task prompts belong in
> issues, not in the roadmap. Re-verify every item against the current code before
> starting it because integrations and provider APIs change independently.

## Current baseline

Bir is an alpha-stage, local-first tracing and deterministic-evaluation SDK for
Python 3.10–3.14. The runtime package has no third-party dependencies, ships PEP
561 typing metadata, and records schema-version `1.0` JSONL events.

The v0.3.0 release completed every item from the 2026-06-29 roadmap:

- wheel **and sdist** build, content inspection, clean-environment install, and
  smoke verification;
- `bir stats` filter parity with `bir traces`;
- the `configure(enabled=False)` / `BIR_DISABLED` tracing kill switch;
- sync and async Ollama wrappers;
- safe-by-default `bir prune`;
- the `similarity_above` evaluator;
- read-only, non-leaky `bir config` output;
- `SECURITY.md` and capture/privacy documentation;
- richer OTLP environment, source, and provider attributes;
- per-example sync and async experiment timeouts.

The repository currently also has:

- 21 dependency-free integration modules, including provider wrappers and agent
  framework bridges;
- local trace rotation, cross-process advisory locking, sent-ID bookkeeping,
  retries, redaction, sampling, service metadata, and cost calculation;
- deterministic evaluators, experiment comparison/reporting, and a CLI for local
  inspection, sending, pruning, export, and regression gates;
- CI across Linux, Windows, and macOS on Python 3.10–3.14, plus strict docs and
  shared-fixture drift checks;
- 900+ unit tests, measured statement/branch coverage with a CI floor, strict
  resource-warning handling, Ruff lint/format, Pyright, strict MkDocs, example
  smoke tests, and hermetic wheel/sdist release verification passing at this
  audit.

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
|---|-------------|----------|------|-----------------|------------|
| 1 | Decide distributed trace-context propagation | P2 | M | An explicit, security-reviewed answer for process/service boundaries | — |
| 2 | Add performance regression benchmarks | P2 | M | Trace write, load, prune, send, and eval costs are tracked over time | — |

## Work item details

### 1. Decide distributed trace-context propagation

**Why:** trace/span IDs are intentionally read-only and cannot currently be
injected across process or service boundaries. That is safe and simple for local
tracing, but prevents a single trace from following queue workers or HTTP calls.

**Scope:**

- Write an ADR comparing no propagation, Bir-specific headers, and W3C Trace
  Context interoperability.
- Define trust boundaries, validation, sampling inheritance, collision behavior,
  and interaction with OTLP export before exposing setters.
- Prototype extraction/injection as an opt-in API with strict validation.
- Do not change schema `1.0` until server/dashboard compatibility is agreed.

**Done when:** the repository records an explicit decision; implementation ships
only if the security and cross-repository contract are approved.

### 2. Add performance regression benchmarks

**Why:** local-first usefulness depends on low tracing overhead, while large-store
operations and concurrent eval runners have no tracked performance baseline.

**Scope:**

- Benchmark disabled, sampled-out, and recorded trace overhead.
- Benchmark append/rotation, load/group, prune, send batching, redaction, and
  sync/async experiment execution on fixed synthetic datasets.
- Record time and peak memory separately; keep network/provider calls mocked.
- Run a stable smoke subset in CI and the full suite manually or on a schedule.

**Done when:** representative regressions are visible before release and results
are comparable across commits.

## Sequencing

Both remaining items are now entries on the Beta checklist in
`docs/site/stability.md`, which is where readiness is tracked. Item 2 is the
larger unknown and gates nothing else, so it can run in parallel; item 1 should
be decided before a Beta release claims trace context is stable, and the
bridges' in-process parent mapping is prior art for the trust boundary it has to
draw for parents arriving from another process.

The checklist also carries one item neither of these covers: confirming the
event-schema `1.0` contract, including the event-tree shape the framework
bridges now record, against the current `bir-app` release.

## Explicitly not on the backlog

The following shipped features must not be reopened merely because they appeared
in an older generated roadmap: sdist verification, stats filters, the master kill
switch, Ollama, prune, fuzzy similarity, config inspection, `SECURITY.md`, richer
OTLP attributes, experiment timeouts, both conformance matrices, event-bridge
parenting from the framework's own run ids, and the published API stability
policy. Regressions in those areas are bugs; new scope requires a new issue with
current evidence.
