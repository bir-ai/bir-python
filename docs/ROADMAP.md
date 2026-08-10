# Bir Python SDK — Improvement Roadmap

> Current baseline: **v0.3.0**, audited **2026-08-10**.
>
> This document contains only work that is still open. Completed work belongs in
> `CHANGELOG.md`; implementation details and copy-paste task prompts belong in
> issues, not in the roadmap. Re-verify every item against the current code before
> starting it because integrations and provider APIs change independently.

## Current baseline

Bir is an alpha-stage, local-first tracing and deterministic-evaluation SDK for
Python 3.10–3.14. The runtime package has no third-party dependencies, ships PEP
561 typing metadata, and records schema-version `1.0` JSONL events.

Measured at this audit on CPython 3.14.6:

- 17,851 lines of runtime source across 19 dependency-free integration modules
  plus the core, evaluation, storage, transport, and CLI modules;
- 1,720 tests (1,719 passing, 1 skipped, 1,930 subtests) in 48 files, running in
  23.4 s under coverage instrumentation at **93.99%** branch coverage, with a CI
  floor of 89%, strict resource-warning handling, Ruff lint/format, Pyright,
  strict MkDocs, example smoke tests, and hermetic wheel/sdist release
  verification;
- CI across Linux, Windows, and macOS on Python 3.10–3.14, a free-threaded 3.14
  leg on Linux, plus strict docs and shared-fixture drift checks;
- 13 CLI commands, two conformance matrices covering every shipped integration, a
  published API stability policy guarded against drift, a deprecation mechanism,
  a benchmark harness with baseline comparison, and a recorded decision on
  distributed trace context.

Every item from the previous list shipped and is in `CHANGELOG.md`. This list is
not a continuation of it.

The last audit's P1 came from asking where a recording path runs code the SDK did
not write, and it answered that question for the *provider* wrappers only. This
one asked it again for the *framework* bridges, which is where the same guard was
still missing, and then went looking on axes the previous four audits had not
used: what reaches the terminal on the CLI's error channel rather than its
rendering path, how `bir tail` and file rotation compose, what the OTLP attribute
names mean against the OpenTelemetry release the extra actually installs, and
which fields redaction is scanning at all rather than which patterns it
recognizes. The P1 came from re-asking the first question, and each of those axes
produced one item. All five have since shipped.

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

Every item from this audit has shipped and is in `CHANGELOG.md`: the P1, the
event bridges' unguarded reads of a framework object; the follow that did not
survive a rotation; the error channel that printed a remote host's response body
raw; the two superseded OTLP attribute spellings; and the undocumented redaction
boundary. **This list is empty. The next change here starts with a new audit.**

## Sequencing

Nothing is queued. What is left below is the record: what must not be reopened,
what was driven and declined, and what was checked and found sound, so the next
audit starts from evidence rather than from a blank page.

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
escaping control characters when the CLI renders recorded text for a person,
guarding the bridges' reads of a provider's response, creating the store's own
files readable only by their owner, guarding the event bridges' reads of a
framework object, following the store across a rotation in `bir tail`, escaping
and bounding what the CLI prints on its error channel, refreshing the OTLP
attribute spellings, and recording where the redaction boundary stops.
Regressions in those areas are bugs; new scope requires a new issue with current
evidence.

The guarded event-bridge reads that shipped from this audit are adjacent to the
earlier "guarding the bridges' reads of a provider's response" and neither
reopens the other. That work covered the direct call wrappers, whose shared
helper `_common._value` it guarded, and it patched one accessor loop in the
LlamaIndex bridge. The follow-up covered the framework bridges' own copies of
that helper, which were never routed through it.

The rotation-following `bir tail` that shipped from this audit is likewise
adjacent to the earlier "flushing each batch `bir tail` prints so a redirected
follow is not silent", and neither reopens the other. That fixed where the bytes
went after they were rendered. This was which events were rendered at all: the
flush was working, and 400 of 400 lines arrived whenever the store did not
rotate.

The escaped error channel that shipped from this audit sits beside "escaping
control characters when the CLI renders recorded text for a person" and does not
reopen it. That covered the rendering path and strings from the local store.
This was the error path and a string a remote host chose; the two shared no
code.

The refreshed attribute spellings sit beside "richer OTLP attributes" and do not
reopen it either. That added attributes the exporter was not writing. This
changed how two it was already writing are spelled, after the conventions
renamed them; no attribute was added or removed, and both spellings of each
carry the same value. The end of that transition is a real follow-up, but it is
triggered by the extra's floor rising rather than by an audit, and the test
pinning the superseded spellings says so where someone will see it.

The recorded redaction boundary sits beside every redaction entry on that list —
the `Authorization` header, the connection URI, `Cookie` values, GitHub tokens,
a secret used as a mapping key — and reopens none of them. Each of those asked
which *patterns* the recognizer knows. This asked which *fields* it is aimed at,
found the answer coherent and undocumented, and wrote it down without changing
it. A future audit that wants an identity field scanned is making a product
decision, not fixing a defect, and `tests/test_redaction_boundary.py` is where
it has to be made: both columns of the table are pinned, so moving one is a
visible edit rather than a silent drift.

## Declined

Four things were driven and deliberately left off the list.

**A failing evaluator discards the example's output and the other evaluators'
scores.** With `raise_on_error=False`, one evaluator that raises turns the whole
example into an error row: `output=None`, `scores=[]`, and an error message that
does not name which evaluator failed, even though the task succeeded and a second
evaluator had already scored it.

```
  a: status=error output=None scores=[]  error='evaluator exploded'
  b: status=error output=None scores=[]  error='evaluator exploded'
  aggregate scores: {}
```

The guard is at the example boundary (`bir/evals.py:1499-1502`) while the caller's
code runs inside a list comprehension at `:1182` — the same guard-granularity
shape as the event-bridge P1. It is declined because it is already a recorded
decision:
`docs/EVALUATOR_IMPLEMENTATION_GUIDE.md:611-612` says "Evaluator failure: treat
like task failure unless a future explicit option separates task failures from
evaluator failures." That is exactly this behaviour and exactly this future
option. Reopening it needs a product argument, not an audit finding.

**`Dataset.to_jsonl()` truncates as well as redacts.** The default `redact=True`
runs the export through `_safe_capture`, so a dataset does not round-trip:

```
  nesting depth 5: intact
  nesting depth 6: TRUNCATED
```

and with capture limits configured for tracing, the same export mangles keys as
well as values:

```
configure(max_value_length=20, max_collection_items=1)
{"id":"q1","input":{"question":"What is the refund w…[truncated]","…[truncated]":"…[truncated]"}}
```

`_MAX_CAPTURE_DEPTH = 6` (`bir/_capture.py:19`) is not configurable, so the depth
cut applies to every `to_jsonl()` call regardless of settings. Declined because
the method's own docstring already says it uses "the same safe capture behavior as
trace and experiment artifacts", which is the truncating one; the narrower
"redacts common secret-like values" wording in `docs/site/evals-experiments.md:179`
and `docs/site/capture-privacy.md:221-222` is the only gap, and a docs sentence is
too small to carry an item. Recorded here so the next audit does not re-derive it.

**Retry classification for HTTP 429.** `_is_retryable_status`
(`bir/_sending.py:75-78`) retries 5xx only, so a rate-limited `bir send` fails
immediately and `Retry-After` is ignored. Declined because `send_events`'
docstring states the rule outright — "A 4xx response is a permanent rejection and
is raised immediately without retry" — and the ingestion server this talks to is
the local Bir server, which does not rate-limit. Worth revisiting if the SDK ever
sends to a hosted endpoint.

**A naive timestamp shifts on OTLP export.** `_expect_datetime_string`
(`bir/_storage.py:1419-1425`) accepts any string `datetime.fromisoformat` parses,
including a timezone-naive one, and `_iso_to_unix_nano` (`otel.py:565-572`) then
reads it as local time. Exporting the same event with and without the `+00:00`
offset differed by exactly 10,800,000,000,000 ns on this UTC+3 machine. Declined
because Bir's own writer always records an offset, so only a store written by
another tool or edited by hand can produce it, and the docstring at `otel.py:568-569`
scopes its claim to what Bir records. Noted because the loader's acceptance is
wider than the exporter's assumption.

Windows-specific paths were **not** driven. `_InterProcessFileLock`
(`bir/_storage.py:125-172`) takes a different branch there — `msvcrt.locking` with
`LK_LOCK` rather than `fcntl.flock` — and the two have different behaviour under
sustained contention. Nothing here can measure that, so nothing is claimed about
it either way; CI's Windows leg is the only evidence this audit has. The
free-threaded build was likewise not driven: no free-threaded interpreter is
installed on this machine (`sysconfig.get_config_var("Py_GIL_DISABLED")` is `0`),
so `tests/test_free_threading.py` ran on the GIL build, where it proves the
uninteresting half by its own docstring's admission.

## Checked and found sound

Eight areas were driven and need no item. The numbers are here so the next audit
can see what this one's coverage actually was.

**Capture against a value whose own code fails, on eight axes.** A generator that
raises mid-iteration, a `__len__` that raises, a `Mapping` whose `__getitem__`
raises, one whose `items()` raises, a `__repr__` that raises, a mapping key whose
`__str__` raises, a value whose `__eq__` and `__hash__` both raise, and a
self-referential list. All eight recorded two events and none reached the caller;
the fallbacks were `[uncapturable]`, `<unrepresentable ClassName>`, and
`[max_depth]` as designed. This is the guard the event-bridge P1 said was on the
right end of the wrong operation — it works exactly as advertised where it is.

**Prune running concurrently with four writers.** Four processes wrote 200 traces
each while a fifth ran `bir prune --keep-last 20` eight times against the same
store. All 800 traces were written, all eight prunes exited 0, 780 traces were
removed, and the store ended with 40 lines: 40 parsed, 0 unparseable, 20 complete
traces, and every remaining event's trace root present. `_append_event`
(`_storage.py:658-678`) re-opens the file inside the lock on every write, so a
`replace()` between two writes is picked up rather than written into an
unlinked inode.

**Rotation integrity.** 120 traces against a 4 KB limit with `backup_count=5`
produced six files and 64 lines; every line was valid JSON, every line parsed as
an event, and 32 complete traces loaded with `include_rotated=True`. No torn line
at any boundary.

**`prune` compacting the sent sidecar.** 80 events with all 80 ids recorded in the
sidecar, pruned to `--keep-last 10`: 20 events remain and the sidecar holds
exactly those 20 ids, `sidecar == kept`.

**OTLP export shape.** A store containing a nested trace, an event whose parent is
absent, and a two-node cycle exported 7 spans for 7 events. The orphan and both
cycle nodes were reparented to the root, all spans shared one OpenTelemetry trace
id, generation spans were `SpanKind.CLIENT`, every end time was at or after its
start, and the Resource carried `service.name`, `deployment.environment`, and
`bir.source` from the trace roots.

**OTLP export against foreign field shapes.** Timestamps with no offset, year
9999, year 0001, an empty event name, and a `metadata.service.environment` that is
an object instead of a string all exported 2 spans without raising. A `usage`
value that is a nested object never reaches the exporter at all — the loader
rejects it first (`TypeError: bir usage.input_tokens must be an int or float`).

**`bir.testing.capture_traces`.** It inherits `capture_inputs` and `service_name`
from the surrounding configuration, nests correctly (the inner block sees only its
own event and restores the outer block's path), restores the previous
configuration object exactly on exit and on a raising body, snapshots its events
so they survive the block, removes its temporary file, and left the real store
empty throughout.

**The redaction rules on the value axis.** Every metadata, output, document, and
error surface in the redaction sweep — ten of them — redacted the credential,
including a secret used as a mapping key and one inside a framework bridge's
`tags` and `serialized_id`.

The previously declined memory profile of the loaders was re-measured and has not
moved: `load_events` peaks at 22,761 KiB and `load_traces` at 24,162 KiB for 5,000
events, identical to the last four audits. The rest of the benchmark run is not
comparable to earlier ones; the machine was under load from the concurrency
sweeps.

`grep -rn "TODO\|FIXME\|XXX" src/` is still empty; it stays a dead end.
