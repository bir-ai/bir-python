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

| # | Improvement | Priority | Size | Primary outcome | Depends on |
|---|-------------|----------|------|-----------------|------------|
| 1 | Give the deprecation policy a mechanism | P2 | S | A promised warning is code, not prose | — |
| 2 | Extend machine-readable output to the automation commands | P2 | S | A CI pipeline can read what `eval-gate`, `send`, and `prune` did | — |
| 3 | Cover the transport error paths | P2 | S | The code that runs when a server misbehaves is tested | — |
| 4 | Verify free-threaded builds | P3 | S | Python 3.13t/3.14t support is a tested claim or a stated limit | — |

## Work item details

### 1. Give the deprecation policy a mechanism

**Why:** `docs/site/stability.md` promises that a public name keeps working for
one minor release while emitting `DeprecationWarning` and naming its
replacement. There is no such machinery: `DeprecationWarning` and
`warnings.warn` appear nowhere in `src/` or `tests/`. The first deprecation will
therefore invent its own approach under time pressure, which is exactly what a
written policy exists to prevent.

**Scope:**

- Add a small internal helper that emits a `DeprecationWarning` naming the
  replacement and the release it is removed in.
- Add the test pattern that proves a deprecated name still works and still
  warns, so the policy is enforced the way the stability inventory is.
- Note in the release checklist that a deprecation is announced in the changelog
  in the same release it starts warning.

**Done when:** deprecating a name is a two-line change with a ready test, and
the promise on the stability page is executable.

### 2. Extend machine-readable output to the automation commands

**Why:** `--json` exists on six of thirteen commands (`traces`, `show`, `stats`,
`experiments`, `experiment-show`, `config`) and is missing from the ones written
for automation. `eval-gate` exists to fail a build, but a pipeline cannot read
*which* score regressed without parsing prose. `send`, `send-experiment`, and
`prune` report counts a script would want, and `export-otel` reports what it
exported. The stability page already tells users to parse JSON "where offered",
which is thin cover for a gap.

**Scope:**

- Add `--json` to `eval-gate` first: verdict, per-evaluator deltas, and the
  threshold that decided it.
- Then `send`, `send-experiment`, `prune`, and `export-otel`, reporting the
  counts they already print.
- Keep the human table as the default and the JSON shape covered by tests, since
  it becomes a parsed contract the moment it ships.

**Done when:** every command a CI pipeline would run can be read by one.

### 3. Cover the transport error paths

**Why:** `_sending.py` has the lowest coverage in the package (79.7%), and the
gap is not in incidental code — it is in HTTP error handling and the
single-event fallback used against a server without a batch endpoint
(`_post_event`, `_accepted_count_from_response`). That is the code that runs
when a server misbehaves, which is when a user most needs it to behave
predictably. `_eval_persistence.py` (79.2%) is second and has the same shape:
malformed-input branches.

**Scope:**

- Test the fallback path's retryable and permanent HTTP statuses, `URLError`,
  socket timeout, non-2xx bodies, and malformed accepted-count responses.
- Do the same for the experiment-loading validation branches.
- Prefer tests that assert the user-visible message and whether a retry happened,
  not just that a line executed.

**Done when:** both modules clear the package's overall coverage rate, with the
error paths covered by behavior tests rather than line-touching ones.

### 4. Verify free-threaded builds

**Why:** CI covers CPython 3.10–3.14 but no free-threaded build, and 3.14 is the
release where free-threading became officially supported. Nothing here is known
to be broken — `configure()` rebinds an immutable dataclass atomically
(`_sdk.py:391`), writes are serialized under module-level locks
(`_storage.py:116-117`), and per-trace state lives in context variables — so this
is verification, not a known race. But "supports 3.14" currently means "supports
the GIL build of 3.14", which the stability page does not say.

**Scope:**

- Add a `3.14t` CI leg, at minimum for the unit suite.
- Add a concurrency test that writes from several threads at once and asserts
  every event landed exactly once and no line interleaved.
- Either state free-threaded support on the stability page, or state the limit.

**Done when:** the supported-Python claim is precise about which builds it covers
and a test backs it.

## Sequencing

Nothing here is P1 any more: the store-health work that was — a damaged store
being unreadable, and a large one costing memory proportional to its size — has
shipped. All four remaining items are independent and can be picked up in any
order. Item 1 is worth doing before the first Beta deprecation rather than
during it.

One read path was deliberately left alone. `bir export-otel` still loads whole
traces because the exporter takes them as a list; bounding it means changing
that signature, which is a bigger question than the command's memory use.

Beta readiness is tracked on the checklist in `docs/site/stability.md`, not here.
Two of its entries remain outside this repository's reach: confirming the
event-schema `1.0` contract against the current `bir-app` release — including the
event-tree shape the framework bridges record and the `metadata.remote_parent`
shape ADR 0001 proposes — and the release mechanics of raising the version and
the `Development Status` classifier.

## Explicitly not on the backlog

The following shipped features must not be reopened merely because they appeared
in an older generated roadmap: sdist verification, stats filters, the master kill
switch, Ollama, prune, fuzzy similarity, config inspection, `SECURITY.md`, richer
OTLP attributes, experiment timeouts, both conformance matrices, event-bridge
parenting from the framework's own run ids, the published API stability policy,
the performance benchmark harness, the trace-context decision
([ADR 0001](adr/0001-distributed-trace-context.md)), reading a damaged store with
`--skip-invalid`, and streaming the CLI read commands. Regressions in those areas
are bugs; new scope requires a new issue with current evidence.
