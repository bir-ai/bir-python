# Bir Python SDK — Improvement Roadmap

> Current baseline: **v0.3.0**, audited **2026-08-09**.
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

- 17,813 lines of runtime source across 19 dependency-free integration modules
  plus the core, evaluation, storage, transport, and CLI modules;
- 1,714 tests in 47 files at 93.98% branch coverage, with a CI floor, strict
  resource-warning handling, Ruff lint/format, Pyright, strict MkDocs, example
  smoke tests, and hermetic wheel/sdist release verification;
- CI across Linux, Windows, and macOS on Python 3.10–3.14, a free-threaded 3.14
  leg on Linux, plus strict docs and shared-fixture drift checks;
- 13 CLI commands, two conformance matrices covering every shipped integration, a
  published API stability policy guarded against drift, a deprecation mechanism,
  a benchmark harness with baseline comparison, and a recorded decision on
  distributed trace context.

Every item from the previous list shipped and is in `CHANGELOG.md`. This list is
not a continuation of it.

The last two audits found their work in output: buffering, escaping, and where
redaction did and did not reach. So this one drove the other direction — what the
SDK accepts *from* providers and frameworks — along with concurrency, capture
limits, report rendering, evaluation comparison, sampling, file permissions, and
deployment-shaped store conditions. The P1 below came from the first of those and
would not have surfaced from any amount of further output sweeping.

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
| 2 | Decide what the trace store's file permissions should be | P3 | S | A store of captured payloads is not world-readable by default | — |

Item 1, the unguarded read of a provider's response, has shipped and is in
`CHANGELOG.md`. Numbering is kept so the one that is left stays citable.

Its lesson generalizes past the bridges: the SDK had guarded this invariant
twice, and both guards sat on the last step of recording. Asking where a
recording path runs code the SDK did not write is a better way to find the next
one than asking where it writes.

## Work item details

### 2. Decide what the trace store's file permissions should be

**Why:** Everything the SDK writes is created with the process umask, which on a
default umask of `022` means world-readable:

```
  dir  0o755  .bir
  file 0o644  .bir/traces.jsonl
  file 0o644  .bir/experiments/e.jsonl
  file 0o644  .bir/experiments/e.summary.json
```

Those files hold captured inputs and outputs. Redaction is documented as
best-effort, and `docs/site/capture-privacy.md` tells users to "keep capture
opt-in for sensitive payloads and review what your application records" — which
is an acknowledgement that a store can hold things worth protecting. On a shared
CI runner, a multi-user host, or a container with a sidecar under a different
uid, `0644` is readable by anyone on the box.

This is a P3 and deliberately framed as a decision rather than a repair, because
the trade-off is real in both directions. `0600` is what tools holding
credentials use, and it is the safer default. It would also break a legitimate
arrangement — a collector running as another user reading the store — and a user
who wants it can already set their umask. What should not happen is the current
state, where the default was inherited rather than chosen and is written down
nowhere.

**Scope:**

- Choose the default and say why in `docs/site/capture-privacy.md`; the decision
  is the deliverable, and either answer is defensible if it is recorded.
- If it tightens, apply it to the trace store, its rotated siblings, the sent
  sidecar, and experiment result and summary files — a store is only as private
  as its least private file.
- Leave the directory alone unless the file mode alone is insufficient, and do
  not chmod files that already exist: a user who widened them meant it.

**Done when:** the mode the SDK creates its files with is a written-down decision
rather than whatever the umask gave it, and every file it writes agrees with it.

## Sequencing

One item is left and it depends on nothing.

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
escaping control characters when the CLI renders recorded text for a person, and
guarding the bridges' reads of a provider's response. Regressions in those areas
are bugs; new scope requires a new issue with current evidence.

The guarded response read is adjacent to the earlier "guarding capture against a
value whose own code raises" and neither reopens the other. That guard is inside
`_safe_capture` and protects Bir from a value it has already been given; this one
covers the bridges reading a provider's object to produce that value, before
anything reaches the guard.

This audit looked at three more things and declined them:

- The Markdown report passing content through. `render_experiment_report(format="markdown")`
  escapes the pipe separator and collapses newlines, so recorded text cannot
  break the table, but `<script>` and Markdown link syntax survive into the
  document. The HTML renderer escapes every experiment-derived string and was
  checked interpolation by interpolation; the Markdown one's narrower contract is
  written into its docstring, and raw HTML passing through Markdown is a property
  of the format rather than of this renderer. It is a product question about what
  a `.md` artifact promises, which belongs in an issue.
- Redaction of non-ASCII spellings. `api_key：value` written with a fullwidth
  colon is not matched, because the labeled rule asks for an ASCII `:` or `=`.
  The same sweep showed `SK-` uppercase, a zero-width character inside a token,
  a base64-wrapped key, and a URL-encoded one all passing through — but none of
  those is a string that would work as a credential, so the rules being
  ASCII-literal is a reasonable shape for formats that are themselves
  ASCII-literal. Widening them is a proposal with a false-positive budget
  attached.
- Recording contexts accepting any attribute, and the store-outage wording that
  rides on it. Declined in both previous audits and unmoved.

The previously declined memory profile of the loaders was re-measured and has not
moved: `load_events` still peaks at 22,761 KiB and `load_traces` at 24,162 KiB
for 5,000 events, identical to the last three audits. Benchmark timings from this
run are not comparable to earlier ones — the machine was loaded, and every case
came in high, including ones this audit did not touch.

Seven areas were checked and found sound, so they need no item:

- Concurrency. Four processes writing 150 traces each produced exactly 1,200
  events and 600 traces with every worker accounted for, and the same run against
  an 8 KB rotation limit left four files that all loaded without a torn line.
- Capture limits. With `max_collection_items=3` and `max_value_length=20`, long
  strings, long lists, and wide mappings each truncate with their marker, a
  secret past the item limit is dropped rather than shown, a secret inside a
  truncated string is redacted before the cut, and one eight levels deep is cut
  at `[max_depth]` before it appears.
- The HTML report. Every experiment-derived string reaches `html.escape`, checked
  interpolation by interpolation; a recorded `<script>` renders as `&lt;script&gt;`.
- `compare_experiments`. Identical runs report no regression, `0.9 → 0.899999`
  reports one, the same pair under `tolerance=0.01` does not, and two experiments
  with no shared example ids produce no per-example deltas rather than a spurious
  regression.
- Sampling rules. A per-name rule of `0.0` suppresses that name under a global
  rate of `1.0`, a rule of `1.0` keeps it under a global rate of `0.0`, and a
  global `0.0` with no rules records nothing.
- Deployment-shaped store conditions. A parent path that is a file, a trace path
  that is a directory, a symlink to `/dev/null`, a read-only parent, and a
  300-character filename each leave the traced call succeeding.
- File contents under those conditions. Nothing was written where it could not
  be, and nothing raised at the caller.

`grep -rn "TODO\|FIXME\|XXX" src/` is still empty; it stays a dead end.
