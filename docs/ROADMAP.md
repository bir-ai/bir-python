# Bir Python SDK — Improvement Roadmap

> Current baseline: **v0.3.0**, audited **2026-08-11**.
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

- 18,611 lines of runtime source across 40 modules — 19 dependency-free
  integration modules plus the core, evaluation, storage, transport, and CLI
  modules;
- 1,797 tests (1,796 passing, 1 skipped, 1,988 subtests) in 49 files, running in
  16.0 s wall under coverage instrumentation at **94.68%** branch coverage,
  against a CI floor of 89%, with strict resource-warning handling, Ruff
  lint/format, Pyright, strict MkDocs, example smoke tests, and hermetic
  wheel/sdist release verification;
- CI across Linux, Windows, and macOS on Python 3.10–3.14, a free-threaded 3.14
  leg on Linux, plus strict docs and shared-fixture drift checks;
- 13 CLI commands, two conformance matrices covering every shipped integration, a
  published API stability policy guarded against drift, a deprecation mechanism,
  a benchmark harness with baseline comparison, and a recorded decision on
  distributed trace context.

Every item from the previous list shipped and is in `CHANGELOG.md`. This list is
not a continuation of it.

The six previous audits worked the *recording* path and then the *evaluation*
one: where recording runs code the SDK did not write, what reaches the terminal
on the CLI's rendering path and then on its error channel, how `bir tail` and
rotation compose, which fields redaction is aimed at, what a full disk leaves
behind, and what the gate decides under partial failure. All of them treated the
server as a fixture. This one pointed the other way and asked what the SDK
*believes*: the transport was driven against real local HTTP servers that answer
in ways the Bir server would not — a redirect, a body larger than the store, a
response whose own numbers are impossible — and that produced two items. The
third came from asking what a long-lived process accumulates, driven by counting
descriptors and threads across 20,000 events, 50 prunes, and a run whose examples
all time out. The fourth came from the public API surface as a contract: what a
caller can pass that the type hints permit and the implementation writes into the
schema anyway. All four have since shipped. Two further axes — `@observe` against
every callable shape its hints allow, and where the trace context is and is not
visible across threads and asyncio — produced no item and are recorded below with
their numbers.

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

Every item this audit raised has shipped and is in `CHANGELOG.md`: the redirect
the transport followed to a host nobody configured; the batch response that could
not describe the request it answered; the thread a serial timed run abandoned per
timed-out example, and the concurrent run that looked hung while it waited on
one; the run that had no budget of its own; and the identity fields a caller
could record with whatever type they were given. **This list is empty. The next
change here starts with a new audit.**

## Sequencing

Nothing is queued. What is left below is the record: what must not be reopened,
what was driven and declined, and what was checked and found sound, so the next
audit starts from evidence rather than from a blank page.

The one decision the last item carried was whether its raise was worth a
deprecation, and it shipped without one: the policy governs public *names*, and
no name changed — the signature, the type hints, and the docstrings all already
said `str`. The reasoning and the migration are in `CHANGELOG.md` rather than
here.

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
attribute spellings, recording where the redaction boundary stops, failing the
gate on a candidate run whose examples failed, pruning a store whose final line
an interrupted write never finished, repairing that line on the next append
instead of writing the following event onto it, writing a report through a
staged file so a failed render keeps the previous one, and refusing an evaluator
list that names the same evaluator twice, refusing a redirect instead of
following it to a host nobody configured, refusing a batch response that cannot
describe the request it answers, bounding the workers a serial timed run
abandons, saying so when a concurrent run is waiting on stuck workers, bounding
the run itself with `total_timeout`, and requiring every identity a caller writes
into a recorded file — a prompt's name and version, a generation's model, an
evaluator's name, an example's id, an experiment's name — to be a string.
Regressions in those areas are bugs; new scope requires a new issue with current
evidence.

The bounded batch response that shipped from this audit sits beside "escaping and
bounding what the CLI prints on its error channel" and reopens none of it. That
work asked what a server's *error* body may do to the operator's terminal, and it
holds: the error path still escapes and stops at 500 characters. This was the
*success* body — read whole and believed — which that work explicitly scoped out,
saying the bound is on what a message shows rather than on what is parsed. The
new bound is on the read, and is derived from the request rather than fixed.

The run budget that shipped from this audit sits beside "experiment timeouts" and
does not reopen them. The per-example timeout records what it should: 60 of 60
examples timed out, were recorded as error rows, and kept dataset order. What
shipped is a bound that work never offered — one on the run rather than on each
example — after the attempt to stretch the per-example one over the run was
measured and rejected for refusing examples that would have passed.

## Declined

Eight things were driven and deliberately left off the list. The first four were
declined by earlier audits and still hold; the next two were declined by the
previous audit and re-confirmed here; the last two are new.

**A failing evaluator discards the example's output and the other evaluators'
scores.** With `raise_on_error=False`, one evaluator that raises turns the whole
example into an error row: `output=None`, `scores=[]`, and an error message that
does not name which evaluator failed, even though the task succeeded and a second
evaluator had already scored it. Still declined for the same reason: it is a
recorded decision, not a defect.
`docs/EVALUATOR_IMPLEMENTATION_GUIDE.md` says "Evaluator failure: treat like task
failure unless a future explicit option separates task failures from evaluator
failures." Reopening it needs a product argument.

**`Dataset.to_jsonl()` truncates as well as redacts.** The default `redact=True`
runs the export through `_safe_capture`, so a dataset does not round-trip past
nesting depth 6, and with capture limits configured for tracing the same export
mangles keys as well as values. `_MAX_CAPTURE_DEPTH = 6` (`bir/_capture.py:19`)
is not configurable. Still declined: the method's own docstring already says it
uses the same safe capture behavior as trace and experiment artifacts, and the
narrower "redacts common secret-like values" wording elsewhere is the only gap. A
docs sentence is too small to carry an item.

**Retry classification for HTTP 429.** `_is_retryable_status`
(`bir/_sending.py:75-78`) retries 5xx only, so a rate-limited `bir send` fails
immediately and `Retry-After` is ignored. Still declined because `send_events`'
docstring states the rule outright and the ingestion server this talks to is the
local Bir server, which does not rate-limit. Worth revisiting if the SDK ever
sends to a hosted endpoint — and the redirect that shipped from this audit is
the first evidence that the transport is exposed to more than that one server.

**A naive timestamp shifts on OTLP export.** `_expect_datetime_string`
(`bir/_storage.py`) accepts any string `datetime.fromisoformat` parses, including
a timezone-naive one, and `_iso_to_unix_nano` (`bir/integrations/otel.py`) then
reads it as local time — 10,800,000,000,000 ns apart on this UTC+3 machine. Still
declined because Bir's own writer always records an offset, so only a store
written by another tool or edited by hand can produce it.

**A currency code is stored and grouped exactly as given**, so six recordings
that all meant dollars report as five lines in `bir stats`. Still declined:
case-folding or trimming would make the stored value differ from what the
application passed, which is the opposite of the boundary this codebase drew for
identity fields, and validating an ISO-4217 shape would reject calls that work
today. The rule that matters — costs are never summed across currencies — holds
exactly.

**A non-UTF-8 byte in a store cannot be skipped**, because
`_iter_trace_events_from_file` decodes strictly and iterates outside the `try`,
so the failure escapes past `--skip-invalid`. Still declined because Bir cannot
produce that store: every writer goes through `json.dumps` with the default
`ensure_ascii=True`, so a torn write cuts between ASCII characters.

Two more were driven for the first time here and left off.

**`install_trace_id_filter()` attaches a new filter every call.** Three calls
left three filters on the root handler, each doing the same two contextvar reads
per record. Declined because it is a documented decision, not an oversight: the
function's own docstring says "Calling this more than once attaches independent
filters; each stamps the same attributes, so the duplication is harmless but you
can avoid it by reusing the returned instance." The stamping itself was driven
and is correct — `None` outside a trace, the trace id inside it, a distinct span
id inside a nested span, `None` again in a plain thread — so the only cost is
work proportional to how many times an application calls it, which the docstring
already tells it not to do.

**A trace does not follow work handed to a plain thread.** Recording inside a
`threading.Thread` or a `ThreadPoolExecutor` worker started from within a trace
finds no active trace, and `bir.span()` raises
`RuntimeError: bir.span() requires an active trace`. Declined because it is
correct contextvar behaviour, it is consistent — the accessors and `span()` agree
in every case measured — and the error names the fix. `docs/site/stability.md`
already says per-trace state lives in context variables, which are per-thread.
The full table is under "Checked and found sound"; a docs example showing
`contextvars.copy_context()` around a submitted job would help, and is a docs
sentence rather than an item.

Windows-specific paths were **not** driven. `_InterProcessFileLock`
(`bir/_storage.py:125-171`) takes a different branch there — `msvcrt.locking` with
`LK_LOCK` rather than `fcntl.flock` — and the two have different behavior under
sustained contention. Nothing here can measure that, so nothing is claimed about
it either way; CI's Windows leg is the only evidence this audit has. The
free-threaded build was likewise not driven: no free-threaded interpreter is
installed on this machine (`sysconfig.get_config_var("Py_GIL_DISABLED")` is `0`),
so `tests/test_free_threading.py` ran on the GIL build. A clock that steps
backwards was not driven either, for the reason the previous audit recorded: no
means of stepping the system clock was available. `scripts/verify_release.py` was
run and passed; its one historical no-output failure did not reproduce, and no
cause is invented for it here. TLS was not driven: every server stood up for this
audit was plain `http` on `127.0.0.1`, so nothing is claimed about certificate
verification, which `urllib` would perform with the default context.

## Checked and found sound

Six areas were driven and need no item. The numbers are here so the next audit
can see what this one's coverage actually was.

**`@observe` against every callable shape its hints permit.** Thirteen shapes, all
recording exactly one trace root and propagating what they should: a plain
function, a generator consumed whole, a generator abandoned after two of five, an
async generator, a `functools.partial`, a callable object, a bound method, a
`staticmethod`, a `classmethod`, a function decorated twice (which records
`span+trace`), a recursive function at depth 5 (`span×5 + trace` under one root),
a function returning a context manager, and a function that raises (recorded
`error`, exception propagated). None raised at the caller, and none failed to
record.

**Where the trace context is visible, and whether the primitives agree about it.**

```
where                    trace_id seen  span_id seen  bir.span() works
inline                   True           True          True
threading.Thread         False          False         RuntimeError
ThreadPoolExecutor       False          False         RuntimeError
coroutine                True           True          True
asyncio.to_thread        True           True          True
```

The accessors and `span()` agree in every row: there is no case where
`get_current_trace_id()` reports a trace that `span()` then refuses. The
`RuntimeError` names the fix. The log-correlation filter matches the same table
exactly — `None` outside, the ids inside, `None` again in a plain thread.

**What a long-lived process accumulates.** 10,000 traces of two events each with
rotation at 32 KB: file descriptors 4 → 4, threads 1 → 1, four rotated files as
configured. Fifty `prune` runs over a 200-trace store: descriptors 4 → 4, and
zero leftover `bir-prune-index-*` temporary directories. The only place anything
accumulated was the serial timed run's abandoned workers, which has shipped.

**`BIR_*` environment parsing.** Eleven values driven through a fresh
interpreter. Every malformed one is rejected before the process can record
anything, with a message naming the variable: `BIR_SAMPLE_RATE=abc`,
`BIR_DISABLED=maybe`, `BIR_MAX_VALUE_LENGTH=-1`, and `BIR_MAX_VALUE_LENGTH=1e3`
all exit 1. `BIR_CAPTURE_INPUTS=TRUE` is accepted case-insensitively, and an empty
value is treated as unset. One cosmetic inconsistency, too small to carry an
item: `BIR_SAMPLE_RATE=2` raises `bir sample_rate must be between 0.0 and 1.0`,
which is the only one of the eleven that does not name the variable.

**The `prompt` and `score` primitives on the value axis.** `score` rejects a
`bool`, a `NaN`, and a call outside any trace, and accepts `1e308`. A prompt
records only the template's SHA-256 unless capture is asked for, redacts a
credential inside a captured `rendered` string
(`{"rendered": "Hi [redacted]"}`), guards a variable whose `__repr__` raises
(`"<unrepresentable X>"`), and rejects a non-mapping `variables`. The two
identity fields were the one gap, and the sweep that shipped with their fix
closed it at five more entry points and pinned the rest of the surface in
`tests/test_identity_field_types.py`.

**A server that answers slowly or not at all.** A 200 that sends its headers, a
`Content-Length` of 1,000,000, and then one byte every 30 s was cut off by the
socket timeout after exactly 2.0 s and reported as a transient send failure with
the endpoint named. The timeout reaches the body read, not only the connect.

The previously declined memory profile of the loaders was re-measured and has not
moved: `load_events` peaks at 22,761 KiB and `load_traces` at 24,162 KiB for
5,000 events, identical to the last six audits.

The OTLP dual-spelling transition is unchanged: the extra still installs
`opentelemetry-sdk` 1.44.0 with `opentelemetry-semantic-conventions` 0.65b0, so
the superseded `deployment.environment` and `gen_ai.system` spellings pinned by
`tests/test_otel_integration.py` still have a live consumer and stay.

`grep -rn "TODO\|FIXME\|XXX" src/` is still empty; it stays a dead end.
