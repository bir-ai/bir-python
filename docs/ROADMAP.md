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
recognizes. The P1 came from re-asking the first question; the four items that
remain came one from each of those axes.

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

The P1 from this audit has shipped and is in `CHANGELOG.md`: the event bridges'
unguarded reads of a framework object. The four items below are what is left.

| # | Improvement | Priority | Size | Primary outcome | Depends on |
|---|-------------|----------|------|-----------------|------------|
| 1 | Follow the store across a rotation in `bir tail` | P2 | S | `bir tail` shows every event written while it runs | — |
| 2 | Escape and bound what the CLI prints on its error channel | P2 | S | A server's response cannot repaint the terminal running `bir send` | — |
| 3 | Refresh the OTLP attribute spellings | P3 | S | A backend keying on the current attribute names sees the values | — |
| 4 | Record where the redaction boundary stops | P3 | S | The privacy page states which fields are scanned and which are not | — |

### 1. Follow the store across a rotation in `bir tail`

**Why.** `bir tail` is documented as "Follow the trace file and print new events
as they are written" (`bir/_cli_parser.py:136`) and again in
`docs/site/cli-env.md:43` and `:247-249`. With `configure(max_bytes=...)` set it
does not. Same workload — 200 traces, 400 events, 5 ms apart, followed by a
`bir tail` subprocess for the whole run — with only the rotation limit changed:

```
no rotation
  lines printed     : 400   distinct trace names: 200   coverage: 200/200 traces
rotation at 64 KB
  lines printed     : 300   distinct trace names: 151   coverage: 151/200 traces
rotation at 2 KB
  lines printed     : 7     distinct trace names: 4     coverage: 4/200 traces
```

Nothing is reported and nothing is duplicated; the events simply never appear.

`_emit_new_events` (`bir/cli.py:911-951`) holds a byte offset into the active
path and detects rotation only through `if size < offset` (`:927-929`). Rotation
renames the active file away and starts a new one at zero
(`_rotate_trace_files`, `bir/_storage.py:708-720`), so everything appended to the
old file since the last 0.5 s poll (`_TAIL_POLL_INTERVAL`, `cli.py:59`) is gone —
nothing ever re-reads `traces.jsonl.1` — and once the new file has grown past the
stale offset the heuristic does not fire either, so the tail seeks into the new
file and skips its beginning as well. The loss scales with how often rotation
happens, which is why 2 KB loses 98% and 64 KB loses 25%.

This is two documented features that do not compose. Rotation is first-class
(`configure(max_bytes=..., backup_count=...)`), and every other read command was
taught about rotated siblings through `include_rotated`; the follow command was
not.

**No test pins the current behaviour.** `TailCommandTests`
(`tests/test_cli.py:2005-2078`) drives `_follow_trace` against a single appended
file and the `tail` command against an un-rotated one. No test rotates the store
while a follow is running.

It is P2 rather than P1 because nothing is lost from the store: the same events
are readable with `bir traces --include-rotated` afterwards. What is lost is the
live view, silently.

**Scope.**

- Detect rotation by identity rather than by size — the active path's inode and
  device, re-checked each poll — so a replaced file is recognized even when the
  new one is already larger than the old offset.
- On a detected rotation, drain the tail of the file that was rotated away before
  switching, so the events written between the last poll and the rename are still
  printed in order.
- Decide and record what happens when several rotations land inside one poll
  interval and an intermediate file has already been dropped by `backup_count`:
  saying so once is better than a silent gap.
- Add a case that rotates the store under a live follow and asserts the printed
  stream against what was written.

**Done when** a follow running across rotations prints every event appended while
it was running, asserted by a test that rotates the store mid-follow.

### 2. Escape and bound what the CLI prints on its error channel

**Why.** The previous release established that recorded text must not be able to
steer the terminal reading it, and `_visible` (`bir/_cli_present.py:302-326`)
escapes every table cell and header. The CLI's error channel does not go through
it: `main` prints the exception message raw (`bir/cli.py:92-94`), and one of the
strings that reaches it is a remote server's response body, embedded verbatim at
`bir/_sending.py:114`, `:125`, `:133`, `:135`, `:139`, `:141`, `:159`, and `:170`.
Those eight are the only raised messages in the SDK that interpolate a remote
host's string, and the only ones that interpolate anything untrusted without
`!r` — `repr` escapes control characters, which is why the store loader's
messages, built from a file's own field names, are already safe.

Driving `bir send` against a local HTTP server that answers with a chosen body:

```
400 whose body is an ANSI repaint sequence
  exit                : 1
  stderr bytes        : 105
  ESC bytes on stderr : 4
  stderr (one line, wrapped here):
    'bir: bir server rejected event batch with HTTP 400: '
    '\x1b[2K\x1b[A\x1b[2Kbir: accepted=1 attempted=1 skipped=0\x1b[0m\n'

200 whose body is an ANSI repaint sequence (invalid batch response)
  ESC bytes on stderr : 4

400 whose body is 2 MB
  stderr bytes        : 2000053
```

The first body erases its own line, moves the cursor up, erases again, and prints
`bir: accepted=1 attempted=1 skipped=0` — which is character-for-character the
line `_cmd_send` prints on success (`cli.py:688`). A failed send can be made to
look like a successful one on the operator's screen. The third shows the other
half: `_read_http_error_body` (`_sending.py:81-87`) and the success-path
`response.read()` are unbounded, so a misdirected `--server` URL pointing at
something that returns a large body puts all of it on stderr inside one exception
message.

This is adjacent to the shipped "escaping control characters when the CLI renders
recorded text" but is not the same work. That fix covers the *rendering* path —
`_print_table` and the experiment header — for strings read out of the local
store. This is the *error* path, for a string a remote host chose, and it reaches
`sys.stderr` without passing through `_visible` at all.

**No test pins the current behaviour.** `ControlCharacterRenderingTests`
(`tests/test_cli.py:2123-2226`) covers `traces`, `show`, `tail`, and
`experiment-show` on stdout, plus the `--json` exemption. Nothing covers `bir: …`
on stderr, and nothing drives `bir send` against a hostile body.

**Scope.**

- Escape control characters in what `main` prints for an exception, keeping the
  same `\xNN` spelling `_visible` already uses so the two channels read alike.
- Bound how much of a server response is read into an error message, and say that
  it was truncated.
- Decide whether the escaping belongs at the print site in `cli.py` or at the
  message-building sites in `_sending.py`. The print site covers every future
  message for free; the build site keeps the library's own exception readable to
  a caller who is not a terminal. Say which and why — a library exception a
  program catches is not the same audience as a line on a screen.
- Sweep the other strings that reach `bir: {exc}` from outside the process for
  the same treatment; `Dataset.__post_init__` (`bir/_eval_models.py:158-159`)
  interpolates example ids from a file without `repr`, though no CLI command
  loads a dataset today.

**Done when** a server response body containing control characters reaches the
terminal escaped and length-bounded, asserted by a test driving `bir send`
against a server that returns one.

### 3. Refresh the OTLP attribute spellings

**Why.** `bir/integrations/otel.py:14-20` and `README.md:478-481` claim the
exported attributes "follow the GenAI semantic conventions where they exist". Two
of the names the exporter writes are superseded spellings in the OpenTelemetry
release the `otel` extra installs (`opentelemetry-sdk` 1.44.0 with
`opentelemetry-semantic-conventions` 0.65b0):

```
GEN_AI_SYSTEM        -> gen_ai.system
    Deprecated: Replaced by `gen_ai.provider.name`
DEPLOYMENT_ENVIRONMENT -> deployment.environment
    Deprecated: Replaced by `deployment.environment.name`
```

`deployment.environment.name` is now a *stable* constant
(`opentelemetry.semconv.attributes.deployment_attributes.DEPLOYMENT_ENVIRONMENT_NAME`),
not an incubating one. The exporter writes `deployment.environment` at
`otel.py:180` and `gen_ai.system` at `otel.py:440`. The other three GenAI names
it uses — `gen_ai.request.model`, `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens` — are unchanged in spelling; the same package marks
them only as having moved to the GenAI conventions repository.

The consequence is quiet: a backend that keys its environment facet on
`deployment.environment.name` or its provider facet on `gen_ai.provider.name`
sees no value, and the traces look like they lack the metadata rather than like
they spell it differently.

**Tests pin the current spellings**, so this reverses a decision rather than
filling a hole: `tests/test_otel_integration.py:245`, `:263`, `:586`, `:600`, and
`:627`. That is the right shape for it — the spellings were chosen deliberately
when the exporter was written, and the conventions moved underneath them.

**Scope.**

- Decide the policy first and write it down: track the current conventions,
  emit both spellings through a transition, or pin the older ones and say why.
  A one-line note in `docs/site/cli-env.md` beside the attribute list is the
  deliverable either way, because "which conventions version" is the question a
  reader wiring up a backend actually has.
- Whatever is decided, make the claim in `otel.py`'s module docstring and
  `README.md:478-481` say which conventions release it is measured against, so the
  next audit can re-check it against a version rather than against "current".
- Update the pinning tests to the decision.
- Re-check the remaining names against the same release while touching this; the
  three checked here were sound.

**Done when** the exported attribute names and the documented conventions claim
agree with a named OpenTelemetry semantic-conventions release, with the pinning
tests updated to match.

### 4. Record where the redaction boundary stops

**Why.** `docs/site/capture-privacy.md:25-80` enumerates what redaction catches
in exhaustive detail and warns that recognition is best-effort. It never says
which *fields* are scanned. Sweeping a `sk-live-…` credential through every
public surface that accepts a string and writes it to the store:

```
  configure(service_name=secret)              LEAKED
  configure(environment=secret)               LEAKED
  configure(source=secret)                    LEAKED
  trace(name=secret)                          LEAKED
  trace(metadata={'note': secret})            redacted
  trace(metadata={secret: 'v'})               redacted
  span.set_metadata({'note': secret})         redacted
  generation(metadata={'note': secret})       redacted
  generation.set_model(secret)                LEAKED
  generation.set_output({'answer': secret})   redacted
  tool_call(name=secret)                      LEAKED
  score(metadata={'note': secret})            redacted
  score(name=secret)                          LEAKED
  prompt(template/variables/rendered=secret)  redacted
  retrieval.add_document(text=secret)         redacted
  traced call raises with secret in message   redacted
  langchain tags/metadata/serialized id       redacted
  generation(model=secret) with prices        LEAKED

8 of 18 surfaces wrote the secret verbatim
```

The split is coherent: payloads and metadata are scanned, identity fields are
not. Three of the leaking surfaces are operator-set constants and are nobody's
surprise. The rest are the event `name`, the score name, and `model` — and two of
those the SDK fills in from a third party rather than from the developer:

```
openai bridge, provider-echoed model
   generation  name='openai.chat.completions' model='sk-live-…'
langchain bridge, framework-supplied tool name
   tool_call   name='sk-live-…'
   trace       name='sk-live-…'
```

`event.model` comes from `_value(response, "model")` on whatever the provider
echoed back (`bir/integrations/openai.py:232`), and `event.name` from
`_callback_name(serialized, kwargs, ...)` on whatever the framework announced
(`bir/integrations/langchain.py:261-279`). The SDK already makes this argument
itself, in `_visible`'s docstring (`_cli_present.py:305-307`): "Names, models,
and captured values are data, and a name is often not a literal: a bridge passes
the tool the model chose." It escapes those names for the terminal and does not
scan them for credentials.

This is not the same axis as the redaction work already declined or shipped.
Every previous sweep asked *which patterns* are recognized — credential formats,
value shapes, encoding and normalisation, non-ASCII spellings. This one asks
which *fields* the recognizer is pointed at, and the answer is undocumented.

Redacting names outright would be the wrong fix: names are the primary index for
reading a trace, and a redacted tool name destroys the record it belongs to. So
the deliverable here is a decision, not a behaviour change — with the possible
exception of `model`, which is a closed vocabulary in practice and the one
identity field a third party supplies wholesale.

**No test pins either answer.** `tests/test_redaction_parity.py` and
`tests/test_custom_redaction.py` cover pattern recognition; nothing asserts that
an identity field is or is not scanned.

**Scope.**

- State the boundary in `docs/site/capture-privacy.md`: captured inputs, outputs,
  metadata (keys and values), documents, and error messages are scanned; the
  event `name`, `model`, score name, and the configured `service.*` / `source`
  are recorded as given.
- Decide separately on `model`, the one identity field read from a provider's
  response rather than chosen by the developer, and record the reasoning either
  way.
- Add a test that pins whichever boundary is chosen, so the next change to
  `_capture` cannot move it without saying so.

**Done when** the privacy page names the fields redaction scans and the fields it
does not, and a test asserts that boundary.

## Sequencing

Items 1 and 2 are independent of each other, both small, and both in the CLI —
reasonable to take together. Items 3 and 4 are documentation-and-decision work
whose main cost is agreeing on the answer; neither blocks anything. Nothing here
blocks anything else.

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
files readable only by their owner, and guarding the event bridges' reads of a
framework object. Regressions in those areas are bugs; new scope requires a new
issue with current evidence.

The guarded event-bridge reads that shipped from this audit are adjacent to the
earlier "guarding the bridges' reads of a provider's response" and neither
reopens the other. That work covered the direct call wrappers, whose shared
helper `_common._value` it guarded, and it patched one accessor loop in the
LlamaIndex bridge. The follow-up covered the framework bridges' own copies of
that helper, which were never routed through it.

Two of the items above are adjacent to entries on that list and neither reopens
one.

Item 1 sits beside "flushing each batch `bir tail` prints so a redirected follow
is not silent". That fixed where the bytes went after they were rendered. This is
which events are rendered at all: the flush is working, and 400 of 400 lines
arrive when the store does not rotate.

Item 2 sits beside "escaping control characters when the CLI renders recorded
text for a person". That covers the rendering path and strings from the local
store. This is the error path and a string a remote host chose; the two share no
code.

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
error surface in the sweep under item 5 — ten of them — redacted the credential,
including a secret used as a mapping key and one inside a framework bridge's
`tags` and `serialized_id`.

The previously declined memory profile of the loaders was re-measured and has not
moved: `load_events` peaks at 22,761 KiB and `load_traces` at 24,162 KiB for 5,000
events, identical to the last four audits. The rest of the benchmark run is not
comparable to earlier ones; the machine was under load from the concurrency
sweeps.

`grep -rn "TODO\|FIXME\|XXX" src/` is still empty; it stays a dead end.
