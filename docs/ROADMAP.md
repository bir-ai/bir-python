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
- 898 unit tests, example smoke tests, strict Pyright, strict MkDocs, and
  hermetic wheel/sdist release verification passing at this audit.

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
| 1 | Add measured test coverage and warning hygiene | P0 | M | Regressions and leaked test resources become visible quality-gate failures | — |
| 2 | Add a lightweight Ruff lint/format gate | P1 | S | Consistent code quality across the growing integration surface | — |
| 3 | Bound memory use for large trace stores and uploads | P1 | M-L | Multi-GB JSONL stores can be inspected and sent without loading the full store twice | 1 |
| 4 | Split core implementation into internal modules | P1 | L | Smaller ownership boundaries without changing the public API | 1, 2 |
| 5 | Introduce shared integration conformance tests | P1 | M | Provider wrappers obey one tested sync/async/streaming contract | 1 |
| 6 | Decide distributed trace-context propagation | P2 | M | An explicit, security-reviewed answer for process/service boundaries | 1 |
| 7 | Define beta API and compatibility policy | P2 | M | A documented path from Alpha to Beta with predictable deprecations | 4, 5 |
| 8 | Add performance regression benchmarks | P2 | M | Trace write, load, prune, send, and eval costs are tracked over time | 3, 4 |

## Work item details

### 1. Add measured test coverage and warning hygiene

**Why:** test volume is strong, but CI records neither statement/branch coverage
nor a coverage floor. The suite also emits `ResourceWarning` messages around
mocked HTTP error bodies, which makes genuine resource leaks easier to overlook.

**Scope:**

- Measure source and branch coverage with tooling in the `dev` extra only.
- Publish a CI summary and set the first failure threshold from an audited
  baseline rather than an arbitrary target.
- Close mocked HTTP error/file objects deterministically and run the suite with
  resource warnings promoted to errors where practical.
- Exclude only generated or intentionally unreachable platform branches, with a
  written reason for every exclusion.

**Done when:** CI fails on coverage regression below the committed baseline and
the standard unit suite completes without unexpected resource warnings.

### 2. Add a lightweight Ruff lint/format gate

**Why:** Pyright checks types but not unused imports, common correctness mistakes,
or formatting drift. Twenty-one integrations make manual consistency increasingly
expensive.

**Scope:**

- Add Ruff to the `dev` extra, never to runtime dependencies.
- Start with correctness/import rules and repository-wide formatting.
- Apply one mechanical baseline change, then enforce `ruff check` and
  `ruff format --check` in CI.
- Avoid rules that fight the public docstring style or add noisy suppressions.

**Done when:** both checks run locally and in CI with no source-specific blanket
ignores.

### 3. Bound memory use for large trace stores and uploads

**Why:** `load_events()` materializes the full store, and the send path currently
loads and groups the same data before posting one full batch. Rotation limits file
size but does not bound total memory when rotated files are included.

**Scope:**

- Add an internal validated JSONL iterator shared by loading, pruning, and send.
- Add bounded event batches for upload while preserving root-first ordering,
  rotated-file ordering, ID deduplication, retries, and `mark_sent` semantics.
- Keep existing `load_events()` / `load_traces()` return types and behavior.
- Define server compatibility for partial batch success before changing requests.
- Test large synthetic stores and failures between batches.

**Done when:** peak client memory is bounded by the configured batch/window size,
wire ordering remains deterministic, and existing public loaders are compatible.

### 4. Split core implementation into internal modules

**Why:** `_sdk.py`, `evals.py`, and `cli.py` are approximately 3.4k, 2.5k, and
1.4k lines. They are tested, but persistence, redaction, execution, reporting,
and parsing concerns now share files large enough to slow review and increase
merge conflicts.

**Scope:**

- First extract private persistence/locking, capture/redaction, and validation
  modules from `_sdk.py`.
- Then separate experiment persistence/report rendering from evaluator execution.
- Split CLI parser construction and presentation helpers only after core moves.
- Preserve all imports and re-exports from `bir`, `bir.evals`, and
  `bir.integrations`.
- Make moves in small commits with unchanged fixture bytes and behavior tests.

**Done when:** responsibilities have clear internal boundaries, the public API and
serialized output are unchanged, and no replacement module becomes another
catch-all.

### 5. Introduce shared integration conformance tests

**Why:** provider wrappers independently implement the same difficult lifecycle:
argument forwarding, `bir_` option stripping, sync/async calls, lazy streams,
usage/model extraction, close handling, and redacted errors. Copy-specific tests
are thorough but can still drift in which guarantees they assert.

**Scope:**

- Define reusable contract cases for call wrappers and event-bridge integrations.
- Require every applicable integration to declare supported capabilities.
- Cover normal return, provider error, partially consumed stream, explicit close,
  async cancellation, capture overrides, and no-provider-import behavior.
- Keep provider-specific parsing tests beside each integration.

**Done when:** adding an integration requires passing the common contract matrix
plus its provider-specific cases.

### 6. Decide distributed trace-context propagation

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

### 7. Define beta API and compatibility policy

**Why:** package metadata still marks the SDK Alpha while the public surface and
integration count are substantial. Consumers need to know which names, event
fields, Python versions, and provider versions are stable.

**Scope:**

- Inventory and classify the public API and CLI commands.
- Publish deprecation and supported-Python policies.
- Add an integration compatibility table with last-verified provider versions.
- Define Beta entry criteria: quality gates, documentation, migration notes, and
  contract compatibility with `bir-app`.

**Done when:** a Beta release can be evaluated against a finite checklist instead
of a subjective readiness call.

### 8. Add performance regression benchmarks

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

1. Complete item 1 first so every later refactor has measurable safety.
2. Add item 2 before moving modules, then implement items 3–5 independently in
   small, behavior-preserving changes.
3. Use the evidence from those changes to decide items 6–8 and Beta readiness.

## Explicitly not on the backlog

The following shipped features must not be reopened merely because they appeared
in an older generated roadmap: sdist verification, stats filters, the master kill
switch, Ollama, prune, fuzzy similarity, config inspection, `SECURITY.md`, richer
OTLP attributes, and experiment timeouts. Regressions in those areas are bugs;
new scope requires a new issue with current evidence.
