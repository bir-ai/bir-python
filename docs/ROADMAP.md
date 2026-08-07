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

- 17,639 lines of runtime source across 19 dependency-free integration modules
  plus the core, evaluation, storage, transport, and CLI modules;
- 1,670 tests in 47 files at 93.94% branch coverage, with a CI floor, strict
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
`scripts/benchmarks.py --repeat 3`, the CLI run against stores and experiments
built for the purpose, a redaction sweep over twenty credential formats, and
probes written for this audit. Each item states what was run and what it
produced.

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

Nothing is open. Every item from this audit has shipped and is in
`CHANGELOG.md`: the `Authorization` credential leak, the export that reported
success without exporting anything, the credential formats redaction did not
recognize, the `Cookie` header, and the derived cost that could fail the call it
was recording.

Two things are worth carrying into the next audit rather than losing with the
list.

Redaction was where the work was: four of the five items were in it, and every
one was found by the same twenty-format sweep rather than by a report. One sweep
is one sample, not a clean bill.

Three of the five items grew once they were measured. The `Authorization` fix
also had to make redaction idempotent, the `Cookie` and connection-URI rules were
one item that turned out to be two defects with different shapes, and the derived
cost turned out to share a root with two unvalidated sums that were dropping whole
events. An item's Why is where the evidence stops, not where the defect does.

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
are seen, reporting rather than raising a failed trace-store write, flushing
each finished example's result row so an interrupted experiment keeps it,
redacting the credential rather than the scheme in an `Authorization` header,
reporting a failed OTLP export instead of counting the spans it built, redacting
fine-grained GitHub tokens, the password inside a connection URI, and the values
in a `Cookie` or `Set-Cookie` header, and leaving an unrepresentable derived cost
off an event rather than raising it at the caller.
Regressions in those areas are bugs; new scope requires a new issue with current
evidence. The two entries about `bir export-otel` are separate pieces of work and
neither reopens the other: streaming was about the memory the export holds, and
the delivery report was about whether it tells the truth.

This audit looked at five more things and declined them:

- Recording contexts accepting any attribute. The classes have no `__slots__` and
  no properties, so assignment bypasses the validating setters: `gen.usage =
  {"input_tokens": -5}` writes a negative token count that `set_usage` rejects,
  `gen.model = 12345` writes a non-string model, and `r.documents = [...]` on a
  retrieval is silently dropped because the reader is `set_documents`. Every
  shipped integration assigns `gen.model` directly but guards its own value
  through `_string_or_none`, and the documentation uses the setters throughout.
  Closing this means slots or properties on six public context types — an
  API-shape proposal, not a repair.
- A serialization failure reported as a store outage. `gen.usage = {"input_tokens":
  float("inf")}` produces `bir could not write to the trace store … Recording is
  paused and events are being dropped`, immediately followed by `bir resumed
  writing`. The store was never the problem and recording was never paused. The
  breadth of that `except Exception` is deliberate and documented
  (`src/bir/_sdk.py:2025`); only the wording of the diagnosis is wrong, and the
  path is reachable only through the bypass above, so it rides on that decision.
  That last claim is now load-bearing rather than incidental: the derived-cost
  item closed the two routes to it that did *not* need the bypass, an
  unvalidated `total_cost` and an unvalidated `total_tokens`, both of which were
  dropping whole events and blaming the store for it.
- Redacting personal data. Emails and government identifiers pass through
  unchanged. That matches the documented scope, which is credentials, and
  redacting every email would destroy legitimate trace content.
  `additional_secret_keys` and `additional_redaction_patterns` already cover the
  applications that need it.
- CLI argument asymmetry. `eval-gate` takes result *paths* while
  `experiment-show` and `experiment-report` take experiment *ids*, and
  `eval-gate` prints JSON by default where every other command prints a table.
  Both are consistent within each command and the errors are clear. It is a UX
  proposal for an issue.
- Float noise in machine-readable output. `bir traces --json` reports
  `"duration_ms": 0.11699999999999999` beside a neighbouring `0.106`. Cosmetic;
  consumers parse it as a float either way.

The four the previous audit declined were re-measured where measuring was cheap
and none of them moved: `load_events` still peaks at 22,761 KiB and `load_traces`
at 24,162 KiB for 5,000 events, and the framework bridges are still the weakest
modules at `llamaindex` 82.51%, `langchain` 85.71%, `crewai` 86.79%, and
`autogen` 88.41%. The early-closed-stream status and the inability to clear a
configured scalar are unchanged and remain API questions for issues.

Five areas were checked and found sound, so they need no item:

- Rotation. Forty traces against `max_bytes=4000, backup_count=3` produced four
  files, and `--include-rotated` agreed across commands: `bir traces` and
  `bir stats` both reported 20 traces with it and 5 without.
- Redaction of the formats it does claim. Eighteen of the twenty credential
  shapes swept are replaced, including the AWS, Google, Slack, Stripe, JWT, PEM,
  and Luhn-checked card cases, plus everything the four shipped redaction items
  repaired. The two that survive are the personal data declined below, which is
  deliberate.
- The documented capture path against values JSON cannot hold.
  `set_output(float("inf"))` records `'inf'`, `set_output(b"bytes")` and an
  arbitrary object record their `repr`, and a `set` records a list. No event was
  dropped and nothing raised.
- Log correlation. With `install_trace_id_filter()`, records from `myapp` and
  `myapp.sub` both carried the trace id inside a trace and `None` outside it.
- Sampling. `sample_rate=0.0` wrote 0 events and `1.0` wrote 20;
  `sample_rules={"noisy": 0.0}` dropped all ten `noisy` traces and kept all ten
  `important` ones.

Benchmarks were stable and unchanged against the previous audit's run.
`grep -rn "TODO\|FIXME\|XXX" src/` is still empty; it stays a dead end.
