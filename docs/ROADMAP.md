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

- 17,561 lines of runtime source across 19 dependency-free integration modules
  plus the core, evaluation, storage, transport, and CLI modules;
- 1,656 tests in 47 files at 93.92% branch coverage, with a CI floor, strict
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

| # | Improvement | Priority | Size | Primary outcome | Depends on |
| --- | --- | --- | --- | --- | --- |
| 5 | Redact the credentials in a `Cookie` header | P2 | S | A captured cookie header stops carrying a live session | — |
| 4 | Keep derived cost from failing the traced call | P3 | XS | The last unguarded raise leaves the recording path | — |

Three of the original four have shipped and are in `CHANGELOG.md`: the
`Authorization` credential leak, the export that reported success without
exporting anything, and the credential formats redaction did not recognize.
Numbering is kept so the ones that are left stay citable.

Item 5 was not on the original list, and that was a bookkeeping error rather than
a judgement: the audit's own sweep recorded `Cookie:` as leaking, and the list
then covered neither it nor a reason for leaving it out. It is written up below
against the code as it now stands.

## Work item details

### 5. Redact the credentials in a `Cookie` header

**Why:** A cookie header passes through untouched. Measured through
`_safe_capture` after the three shipped redaction items:

```
  Cookie: session=abc123; auth_token=xyz789      -> unchanged
  Set-Cookie: sid=9f8e7d; HttpOnly               -> unchanged
```

A session cookie is a live credential — it is what a request presents *instead*
of a password — so this is the same class as the `Authorization` header that has
just been repaired, in the other header an HTTP client sends.

Neither of the two rules that look like they should catch it does. The labeled
rule lists `token`, but `\b` does not fall between `auth` and `token`, so
`auth_token=` is not a match; and `session=` is not a listed name at all.

The shape is what makes this its own item rather than another name on that list.
A cookie header is a `;`-separated sequence of `name=value` pairs, and the shared
value pattern stops at `;` — correctly, since that is what keeps a redaction from
swallowing the rest of a sentence. So the rule has to understand the header
rather than add a keyword.

**Scope:**

- Replace every cookie value in a `Cookie` or `Set-Cookie` header while keeping
  the cookie names, which are what make the trace readable, and keeping the
  `Set-Cookie` attributes (`HttpOnly`, `Path`, `Expires`) that are not values.
- Stay linear in the size of the value, and pin it the way the private-key and
  URI rules are pinned. A `;`-separated list invites a repeated group, which is
  where the URI rule went quadratic.
- Fixture cases, and the documented list updated to name the header. That means
  another `scripts/fixtures.py sync` and a paired commit in the `bir` repo.

**Done when:** a captured `Cookie` header carries no values, the cookie names and
`Set-Cookie` attributes survive, and the documented list names it.

### 4. Keep derived cost from failing the traced call

**Why:** `_Generation.__exit__` calls `_fill_cost_from_prices()`
(`src/bir/_sdk.py:1637`) before it builds the event, outside the guard that keeps
a recording failure away from the caller. `configure(model_prices=...)` validates
each price is finite and non-negative — it correctly rejects a negative, `inf`, or
non-numeric price — but the *product* of a price and a token count is not
bounded, and `set_cost` rejects the `inf` it produces. Measured:

```
  configure(model_prices={"m": {"input": 1e308, "output": 1e308}})
  @observe def chat(): … set_usage(input_tokens=1000, output_tokens=1000)

  chat() -> ValueError: bir input_cost must be finite
```

The traced function loses its return value to an exception raised by the
bookkeeping about it. A price of `1e305` stays finite and records normally, so
the threshold is absurd and no real price table reaches it.

The reason to fix it anyway is that the invariant is now explicit. The previous
release established that a store that cannot be written must not decide whether
a call succeeded, and `_write_event` is wrapped accordingly; this is the one
remaining path in the exit sequence that can still raise into the caller. It is
cheap to close and it stops the invariant from being true only by accident.

**Scope:**

- Derive no cost rather than raising when the product is not finite.
- Keep an explicit `set_cost()` raising: that is a caller passing bad values to a
  documented setter, which is a programming error and not bookkeeping.

**Done when:** no price table and token count can make a traced call raise, and
`set_cost()` still validates as it does today.

## Sequencing

Neither of the two left depends on the other. Item 5 changes the shared redaction
fixture again, which means another `scripts/fixtures.py sync` and another paired
commit in the `bir` repo; that is the only coordination cost on this list. Item 4
touches nothing outside `src/bir/_sdk.py`.

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
redacting the credential rather than the scheme in an `Authorization` header, and
reporting a failed OTLP export instead of counting the spans it built, and
redacting fine-grained GitHub tokens and the password inside a connection URI.
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
- Redaction of the formats it does claim. Seventeen of the twenty credential
  shapes swept are replaced, including the AWS, Google, Slack, Stripe, JWT, PEM,
  and Luhn-checked card cases, plus everything the three shipped items above
  repaired. Of the three that survive, two are the personal data declined below;
  the third is item 5.
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
