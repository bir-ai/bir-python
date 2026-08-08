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

- 17,649 lines of runtime source across 19 dependency-free integration modules
  plus the core, evaluation, storage, transport, and CLI modules;
- 1,671 tests in 47 files at 93.94% branch coverage, with a CI floor, strict
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

| # | Improvement | Priority | Size | Primary outcome | Depends on |
| --- | --- | --- | --- | --- | --- |
| 2 | Redact a secret used as a mapping key | P2 | S | The one position redaction does not reach is closed | — |
| 3 | Stop recorded text from steering the terminal | P3 | S | A name cannot erase or repaint a `bir` command's output | — |

Item 1, the unflushed `bir tail`, has shipped and is in `CHANGELOG.md`. Numbering
is kept so the remaining items stay citable. Neither depends on the other: item 2
has a cost question to settle before it lands, and item 3 is a rendering change
rather than a validation one.

## Work item details

### 2. Redact a secret used as a mapping key

**Why:** A mapping key is the one position a secret survives. Sweeping twelve
value *shapes* rather than credential formats, eleven were redacted and one was
not:

```
  {"sk-ABCD…": "value"}          -> {"sk-ABCD1234efgh5678": "value"}    leaks
  ("sk-ABCD…", "b")              -> ["[redacted]", "b"]
  {"sk-ABCD…"}  (a set)          -> ["[redacted]"]
  Cfg(api_key='sk-ABCD…')        -> "Cfg(api_key=[redacted])"
  b"sk-ABCD…"                    -> "b'[redacted]'"
  raise … from ValueError(…)     -> cause message redacted
  eight levels deep              -> cut at [max_depth] before it appears
```

It reaches the trace file. A function taking `{"sk-…": {"remaining": 3}}` and
returning `{"sk-…": "exhausted"}` records the key verbatim in both `input` and
`output`.

`_safe_key` (`src/bir/_capture.py:320`) is `str(value)` and nothing else. Its
immediate neighbour `_safe_repr` (`:327`) is the same shape and *does* call
`_redact_secret_text`. Keys already drive detection — `_is_secret_key` is what
makes `{"api_key": …}` redact its value — so the key is read, just never
rewritten.

The shape is narrower than a header: it needs a dict keyed by the credential
itself, such as a per-token rate-limit map or a token-to-session cache. That is
why it is a P2 and the header leaks were P1s. Nothing pins it; no test references
`_safe_key`.

**Scope:**

- Redact the rendered key text the way every other captured string is redacted.
- Keep `_is_secret_key` detection working on the original key name; it decides
  whether the *value* is replaced and must not start matching `[redacted]`.
- Settle the cost first. This adds a redaction pass per mapping entry, where
  today there is one per value, and the last three redaction items each moved a
  benchmark. Measure `capture_redaction` and a wide-mapping case before choosing
  the spelling — redacting only keys that could contain a secret may be the way
  in, since most keys are short identifiers.

**Done when:** a secret is redacted wherever it appears in a captured value,
including as a mapping key, and the per-entry cost is measured and stated.

### 3. Stop recorded text from steering the terminal

**Why:** Recorded text is printed to the terminal exactly as stored, control
characters included. A trace recorded under the name
`\x1b[2K\x1b[31mFAKE ERROR\x1b[0m` comes back out of `bir traces` intact — shown
here through `cat -v`, which is the only reason the escapes are visible:

```
  START                             STATUS   DURATION  EVENTS  NAME
  2026-08-08T03:05:25.719851+00:00  success  0.0ms     1       ok
  2026-08-08T03:05:25.719254+00:00  success  0.2ms     1       ^[[2K^[[31mFAKE ERROR^[[0m
```

On a real terminal `\x1b[2K` erases the line the cursor is on, so a row can wipe
the row above it, and `\x1b[31m` repaints what follows. A name containing a
newline splits the table row in two on its own.

`_validate_event_name` (`src/bir/_config.py:169`) checks that a name is a
non-empty string and nothing more, which is the right place to be permissive: a
name is data, and rejecting an odd one would refuse to record a call over its
label. The problem is at the other end, where the data is printed.

Names are not always literals. `tool_call(name=…)` in a bridge takes the tool the
model chose, `trace(name=…)` often takes a route or an operation from a request,
and `generation(model=…)` takes what the provider returned. Those are outside
inputs arriving in a field the CLI prints, and `bir traces` and `bir show` are
read with a terminal attached.

Worth being honest about the ceiling: this is display spoofing and log injection,
not execution. It earns a P3 rather than a P2 because reading it wrong costs
trust in the output rather than a credential.

**Scope:**

- Escape or strip control characters when rendering, not when recording. The
  stored event keeps what the application passed; only the printed form changes.
- Cover both renderers — the `traces`/`experiments` tables and the `show` event
  tree — and the fields that carry outside data: names, models, and any captured
  value that reaches a terminal.
- Leave `--json` alone. Its consumer is a parser, not a terminal, and escaping
  there would corrupt the value a pipeline reads.

**Done when:** no recorded value can move the cursor, clear a line, or set a
colour in the output of a `bir` command, and `--json` still round-trips the value
as stored.

## Sequencing

The two that are left are independent and can go in any order.

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
`Set-Cookie` header, and leaving an unrepresentable derived cost off an event
rather than raising it at the caller, and flushing each batch `bir tail` prints
so a redirected follow is not silent. Regressions in those areas are bugs; new
scope requires a new issue with current evidence.

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
