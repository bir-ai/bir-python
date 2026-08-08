# Bir Python SDK — Improvement Roadmap

> Current baseline: **v0.3.0**, audited **2026-08-08**.
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

- 17,763 lines of runtime source across 19 dependency-free integration modules
  plus the core, evaluation, storage, transport, and CLI modules;
- 1,688 tests in 47 files at 93.96% branch coverage, with a CI floor, strict
  resource-warning handling, Ruff lint/format, Pyright, strict MkDocs, example
  smoke tests, and hermetic wheel/sdist release verification;
- CI across Linux, Windows, and macOS on Python 3.10–3.14, a free-threaded 3.14
  leg on Linux, plus strict docs and shared-fixture drift checks;
- 13 CLI commands, two conformance matrices covering every shipped integration, a
  published API stability policy guarded against drift, a deprecation mechanism,
  a benchmark harness with baseline comparison, and a recorded decision on
  distributed trace context.

Every item from the previous list shipped and is in `CHANGELOG.md`. This list is
not a continuation of it. It was re-derived by driving the SDK: a coverage run,
`scripts/benchmarks.py --repeat 3`, the CLI run under a pipe and under a
terminal, a second redaction sweep using value *shapes* rather than credential
formats, and probes written for this audit against `capture_traces`, the
send/prune/sidecar chain, generator finalization, `eval-gate`, environment
precedence, and damaged stores. Each item states what was run and what it
produced.

The previous list was almost entirely redaction, and its closing note said one
sweep is one sample. That held: this audit's redaction finding came from sweeping
a different axis, and the other two came from areas the last audit never drove.

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

Nothing is open. All three items from this audit have shipped and are in
`CHANGELOG.md`: the unflushed `bir tail`, the secret used as a mapping key, and
recorded text steering the terminal.

Two things are worth carrying forward rather than losing with the list.

Both cost questions resolved by asking a cheap question first, and both ended up
cheaper than the code they replaced rather than more expensive. Redaction now
asks whether any rule *could* match before running fourteen of them, which made
ordinary capture about a third faster than it had been; rendering asks
`str.isprintable` before reaching for a pattern, at 4.4x less per cell. When a
rule set grows, the gate in front of it is worth more than the rules are worth
optimizing.

A gate in front of a rule set has to be checked against the rules, not against
itself. The first test written for the redaction gate asked whether redaction
changed the text, which passes for exactly the rules the gate has stopped
reaching; it was only caught by deliberately narrowing the gate and watching the
test stay green. Placing the gate at the shared entry point rather than on the
one new path is what let the existing suite catch a missing marker immediately.

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
are seen, reporting rather than raising a failed trace-store write, flushing each
finished example's result row so an interrupted experiment keeps it, redacting the
credential rather than the scheme in an `Authorization` header, reporting a failed
OTLP export instead of counting the spans it built, redacting fine-grained GitHub
tokens, the password inside a connection URI, and the values in a `Cookie` or
`Set-Cookie` header, leaving an unrepresentable derived cost off an event rather
than raising it at the caller, flushing each batch `bir tail` prints so a
redirected follow is not silent, redacting a secret used as a mapping key, and
escaping control characters when the CLI renders recorded text for a person.
Regressions in those areas are bugs; new scope requires a new issue with current
evidence.

The `bir tail` flush is not a reopening of streaming the CLI read commands: that
was about how much of the store those commands hold in memory, this was about
whether `tail` emits what it has already rendered.

This audit looked at two more things and declined them:

- A byte-order mark at the head of a store. `load_events` refuses it with
  `Invalid JSON in trace file … at line 1`, and `--skip-invalid` skips the line.
  Bir writes the store itself and never emits a BOM, so such a file came from
  somewhere else; reading it as `utf-8-sig` would be a courtesy rather than a
  repair, and the error already names the file and the line.
- Recording contexts accepting any attribute, and the store-outage wording that
  rides on it. Both were declined in the previous audit and neither has moved.
  The wording decline is now firmer, since the derived-cost item closed the two
  routes to it that did not need the attribute bypass.

The four the previous audit declined were re-measured and none of them moved:
`load_events` still peaks at 22,761 KiB and `load_traces` at 24,162 KiB for 5,000
events, and the framework bridges are still the weakest modules at `llamaindex`
82.51%, `langchain` 85.71%, `crewai` 86.79%, and `autogen` 88.41%.

Six areas were checked and found sound, so they need no item:

- `capture_traces`. Configuration was restored in full after a clean block and
  after one whose body raised, including a user-set `trace_path`, the real store
  was never created, and a nested block redirected to the inner file and restored
  on the way out.
- The send, mark-sent, prune, and sidecar chain. A first send accepted 12 events,
  a second attempted 0, pruning to the last 2 traces compacted the sidecar to 172
  bytes, and a third send still attempted 0.
- Generator finalization. Sync and async generators, each fully consumed and each
  closed early, plus one raising mid-stream: five runs, five events, statuses
  `success`/`success`/`error` as documented, none lost.
- `eval-gate`. A 0.90 → 0.50 regression exits 1; no change and an improvement
  exit 0; a candidate that dropped the evaluator entirely exits 0 under the
  default policy and 1 under `--missing-score regress`.
- Environment precedence. `BIR_*` supplies defaults, `configure()` overrides
  them, and an explicit `configure(capture_inputs=False)` wins over
  `BIR_CAPTURE_INPUTS=true`.
- Damaged and foreign stores. A store with CRLF line endings loads normally; a
  BOM and an embedded NUL each fail with the file and line named, and
  `--skip-invalid` skips them.

`grep -rn "TODO\|FIXME\|XXX" src/` is still empty; it stays a dead end.
