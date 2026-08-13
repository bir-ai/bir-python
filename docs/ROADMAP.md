# Bir Python SDK — Improvement Roadmap

> Current baseline: **v0.3.0**, audited **2026-08-14**.
>
> This document contains only work that is still open. Completed work belongs in
> `CHANGELOG.md`; implementation details and copy-paste task prompts belong in
> issues, not in the roadmap. Re-verify every item against the current code before
> starting it because integrations and provider APIs change independently.

## Current baseline

Bir is an alpha-stage, local-first tracing and deterministic-evaluation SDK for
Python 3.10–3.14. The runtime package has no third-party dependencies, ships PEP
561 typing metadata, and records schema-version `1.0` JSONL events.

Measured at this audit on CPython 3.14.6, macOS 26.5.2 (arm64):

- 19,092 lines of runtime source across 40 modules — 19 dependency-free
  integration modules plus the core, evaluation, storage, transport, and CLI
  modules;
- 1,848 tests (1,847 passing, 1 skipped, 2,167 subtests) in 51 files, running in
  28.0 s wall under coverage instrumentation at **94.73%** branch coverage,
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

The seven previous audits worked the SDK's own material: the code it runs that it
did not write, what it puts on a terminal, which fields redaction is aimed at,
what a full disk leaves behind, what the gate decides under partial failure, what
the transport believes a server said, and what the public API accepts that its
type hints forbid. Every one of them ran inside one process that owned its store.
This one pointed at the operating system underneath: what happens when the
process forks, when the shell's reader goes away, when the working directory moves
under a relative path, when a rewrite is killed half-finished, and when several
processes record into one file at once. Four items, from four surfaces, and the
theme behind the first of them was new — recording is documented never to *break*
a traced call, and nobody had asked whether it can *stop* one. That one, the
audit's only P2, has shipped; shipping it produced a fifth item, about what a
child that *can* record writes into a trace its parent owns, and that has shipped
too. Three are left, all P3.

The concurrency the guardrails actually promise is sound and is recorded below
with its numbers: eight processes appending to one store lost nothing, rotation
under that contention lost nothing, a reader running throughout never failed, and
a `prune --yes` killed mid-rewrite never damaged the file.

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
|---|---|---|---|---|---|
| 1 | A read command piped into a reader that stops reading exits 120 | P3 | S | `bir traces \| head` is an ordinary success, as the CLI's own contract says | — |
| 2 | A relative store path is re-resolved on every append | P3 | S | A process that changes directory keeps recording where it started | — |
| 3 | An interrupted `prune --yes` abandons a staging copy nothing reclaims | P3 | S | The command that reclaims space does not leave more behind than it freed | — |

### 1. A read command piped into a reader that stops reading exits 120

**Why.** `bir traces | head -2` is an ordinary thing to type. Measured against a
10,000-event store, with the reader closing the pipe after two lines:

```
traces                exit=120   bir: [Errno 32] Broken pipe
                                 Exception ignored while flushing sys.stdout: BrokenPipeError
traces --json         exit=120   (same two lines)
show (4,000 events)   exit=120   (same two lines)
show --json           exit=120   (same two lines)
traces > /dev/null    exit=0
```

`main` (`bir/cli.py:124-130`) catches `OSError`, of which `BrokenPipeError` is
one, reports it like a failure and returns 1; the interpreter then fails to flush
`sys.stdout` at shutdown, prints its own line, and replaces the status with 120.
Nothing failed: the command found the store, read it, and printed what the reader
asked for.

`docs/site/cli-env.md:243` states the contract this breaks: commands "print
failures to stderr and exit non-zero for missing or malformed files, server
failures, and failed eval gates" — a closed pipe is none of those. A script under
`set -e` or `pipefail` sees a failure where there is none, and an operator sees
two error lines for using `head`. `stats`, `experiments`, and
`experiment-show` escape it only because their output fits inside the pipe buffer;
they are on the same code path.

The signal path next to it is right, which is what makes this one look like an
oversight rather than a policy: SIGINT delivered while `traces` was rendering
exited 130 with an empty stderr after printing 1,710 lines, and `bir tail` under
SIGINT exits 0.

**Scope.**

- Handle `BrokenPipeError` where the CLI writes, not as a failure: the
  conventional close is to redirect `sys.stdout` to `os.devnull` before the
  interpreter flushes, which is what silences the second line.
- Decide and record the exit code. 141 (128 + SIGPIPE) is what a program killed
  by the signal reports and what a shell pipeline expects; 0 says the command did
  what was asked. Pick one, say why, and write it into the exit-code table in
  `docs/site/cli-env.md`.
- Keep stderr silent in this case only. A write failure that is *not* a closed
  pipe — a full disk on a redirect — must still be reported and still exit
  non-zero.

**Done when** `bir traces | head` prints no error and exits with the code the
docs name for it.

### 2. A relative store path is re-resolved on every append

**Why.** `_DEFAULT_TRACE_PATH = Path(".bir/traces.jsonl")` (`bir/_config.py:18`)
is stored as given and passed to `open()` on every append, so the operating
system resolves it against the working directory *at the time of the write*
rather than at the time of the call to `configure()`. A process that changes
directory splits its store in two, silently:

```
configure(enabled=True) in app/, one trace, then os.chdir to elsewhere/, one trace

app/.bir/traces.jsonl        -> ['before-chdir']
elsewhere/.bir/traces.jsonl  -> ['after-chdir']
```

Nothing warns, and `bir traces` run in either directory shows half a picture. The
same holds for any relative path a caller configures explicitly or sets in
`BIR_TRACE_PATH`. Daemonizing (`os.chdir("/")`), a test that changes directory, a
CLI tool that resolves paths from a project root, and anything that runs work in
a temporary directory all reach it.

**Scope.**

- Resolve the configured trace path to an absolute path once, where the
  configuration is built, so every writer in the process agrees on one file.
- Decide and record which resolution: `Path.absolute()` keeps the path the caller
  wrote, while `Path.resolve()` also follows symlinks and normalizes `..`, which
  changes what an operator sees in an error message.
- Leave the CLI's own default relative to the invocation directory, which is what
  a command is expected to do, and say so where the default is documented.
- Decide and record whether this is worth a `Changed` entry: an application that
  relies on the store following its working directory has no other way to get
  that behavior back.

**Done when** a process that changes directory after `configure()` keeps
recording into the store it started with.

### 3. An interrupted `prune --yes` abandons a staging copy nothing reclaims

**Why.** Prune stages the survivors and replaces the original, which is what keeps
it crash-safe — and that half is sound: killed at four different offsets, the
store was intact and loadable every time (24,000 lines before, 24,000 after). What
is not sound is what the killed run leaves. Measured on an 8.56 MB store with
`--keep-last 11990`, SIGKILL 0.6 s in:

```
store 8.56 MB   abandoned staging copy 6.93 MB   abandoned index 3.12 MB
```

The staging copy is a `.traces.jsonl.<pid>.<uuid>.tmp` sibling in the store's own
directory; the index is a `bir-prune-index-*/traces.sqlite3` in `TMPDIR`. Neither
is ever picked up again — three later successful prunes and 50 further recorded
traces left both exactly where they were:

```
after a prune killed mid-run     siblings=['.traces.jsonl.18864....tmp'] index_dirs=1
after successful prune #1        siblings=['.traces.jsonl.18864....tmp'] index_dirs=1
after successful prune #2        siblings=['.traces.jsonl.18864....tmp'] index_dirs=1
after successful prune #3        siblings=['.traces.jsonl.18864....tmp'] index_dirs=1
after recording 50 more traces   siblings=['.traces.jsonl.18864....tmp'] index_dirs=1
```

So the command whose purpose is reclaiming space leaves 10 MB behind to reclaim
0, and running it again does not help. Ctrl-C on a long prune is the ordinary way
to reach this. The staging copy holds recorded events, including whatever capture
was enabled for; it is created `0600` like the store, so this is a space and
confusion problem rather than a privacy one, and `TMPDIR` is swept by the system
eventually while the sibling next to the store is not.

**Scope.**

- Remove stale staging siblings for the store being pruned, at the start of a
  prune, before staging a new one.
- Decide and record how a stale one is recognized. The name carries the writing
  process's pid, so a live pid must not be swept, and an age threshold has to be
  chosen for the rest.
- Include the index directory, or move it under the store rather than `TMPDIR`
  so a single sweep finds both.
- Report what was swept, the way `incomplete_tail_bytes` already reports the
  other thing prune repairs on the way past.

**Done when** a prune that follows an interrupted one reclaims what the
interrupted one left.

## Sequencing

Both fork items have shipped: a child forked out of a recording process records
instead of hanging, and it no longer writes a second copy of an event its parent
opened. What is left inconveniences whoever reads a store or runs a command;
nothing left writes anything wrong into one.

The three are independent; nothing blocks anything. Items 1 and 3 are contained
inside one function each. Item 2 is one line of resolution plus the decision about
which resolution, and is the only one that can change where an existing
application's events land, so it wants its own release note. Item 1 is the one an
operator meets most often — `bir traces | head` is a thing people type every
day — so it is the one to do first.

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
evaluator's name, an example's id, an experiment's name — to be a string, and
re-creating the store's locks in a forked child so a pre-forking worker records
instead of hanging, and writing each event only in the process that opened it so
a fork cannot put one event id in the store twice.
Regressions in those areas are bugs; new scope requires a new issue with current
evidence.

Item 4 sits beside "pruning a store whose final line an interrupted write never
finished" and reopens none of it. That work asked what prune does with a store the
*writer* left half-finished, and it holds: the fragment is dropped and reported
through `incomplete_tail_bytes`. This asks what prune leaves when *prune* is the
thing interrupted, which is the other side of the same command and was never
driven — the earlier "zero leftover `bir-prune-index-*` directories" measurement
counted successful runs only.

Item 2 sits beside "streaming the CLI read commands" and "escaping and bounding
what the CLI prints on its error channel", and reopens neither. Streaming is what
makes the closed pipe reachable at all rather than what makes it fail, and the
error-channel work asked what a *message* may contain, not what the process does
when the reader leaves.

The bounded batch response that shipped from the previous audit sits beside
"escaping and bounding what the CLI prints on its error channel" and reopens none
of it. That work asked what a server's *error* body may do to the operator's
terminal, and it holds: the error path still escapes and stops at 500 characters.
That was the *success* body — read whole and believed — which that work explicitly
scoped out, saying the bound is on what a message shows rather than on what is
parsed. The bound it added is on the read, and is derived from the request rather
than fixed.

The run budget that shipped from the previous audit sits beside "experiment
timeouts" and does not reopen them. The per-example timeout records what it
should: 60 of 60 examples timed out, were recorded as error rows, and kept dataset
order. What shipped is a bound that work never offered — one on the run rather
than on each example — after the attempt to stretch the per-example one over the
run was measured and rejected for refusing examples that would have passed.

## Declined

Ten things were driven and deliberately left off the list. The first eight were
declined by earlier audits; their mechanisms were re-checked against the current
source here rather than re-driven end to end, and every one is still present. The
last two are new and were driven here.

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
mangles keys as well as values. `_MAX_CAPTURE_DEPTH` is still `6` and still not
configurable (`bir/_capture.py`). Still declined: the method's own docstring
already says it uses the same safe capture behavior as trace and experiment
artifacts, and the narrower "redacts common secret-like values" wording elsewhere
is the only gap. A docs sentence is too small to carry an item.

**Retry classification for HTTP 429.** `_is_retryable_status` still reads
`return 500 <= status < 600`, so a rate-limited `bir send` fails immediately and
`Retry-After` is ignored. Still declined because `send_events`' docstring states
the rule outright and the ingestion server this talks to is the local Bir server,
which does not rate-limit. Worth revisiting if the SDK ever sends to a hosted
endpoint — the redirect refusal that shipped from the previous audit is the first
evidence that the transport is exposed to more than that one server.

**A naive timestamp shifts on OTLP export.** `_expect_datetime_string`
(`bir/_storage.py`) accepts any string `datetime.fromisoformat` parses, including
a timezone-naive one, and `_iso_to_unix_nano` (`bir/integrations/otel.py`, still
present) then reads it as local time — 10,800,000,000,000 ns apart on this UTC+3
machine. Still declined because Bir's own writer always records an offset, so only
a store written by another tool or edited by hand can produce it.

**A currency code is stored and grouped exactly as given**, so six recordings that
all meant dollars report as five lines in `bir stats`. Still declined:
case-folding or trimming would make the stored value differ from what the
application passed, which is the opposite of the boundary this codebase drew for
identity fields, and validating an ISO-4217 shape would reject calls that work
today. The rule that matters — costs are never summed across currencies — holds
exactly.

**A non-UTF-8 byte in a store cannot be skipped**, because
`_iter_trace_events_from_file` still decodes strictly and iterates outside the
`try`, so the failure escapes past `--skip-invalid`. Still declined because Bir
cannot produce that store: every writer goes through `json.dumps` with the default
`ensure_ascii=True`, so a torn write cuts between ASCII characters.

**`install_trace_id_filter()` attaches a new filter every call.** Declined because
it is a documented decision, not an oversight: the function's own docstring says
the duplication is harmless and names the way to avoid it.

**A trace does not follow work handed to a plain thread.** Declined because it is
correct contextvar behaviour, the accessors and `span()` agree in every case
measured, and the error names the fix.

Two more were driven for the first time here and left off.

**A store path that blocks on open blocks the application.** `trace_path` pointing
at a FIFO with no reader attached blocked the traced call for the whole four
seconds the harness allowed and was still waiting — and it does so while holding
both the in-process lock and the advisory file lock, so it stops every other
recorder in the process and any other process sharing that store. Declined
because that is what a FIFO does: the same configuration works when a reader is
attached, which is presumably why someone would choose it, and opening
non-blockingly or refusing a non-regular path would remove a configuration that
works today. The five other non-file paths driven beside it are all handled
correctly and are under "Checked and found sound".

**Recording from `__del__` during final interpreter teardown reports a confusing
failure.** An object whose finalizer records at shutdown produces
`bir could not write to the trace store at traces.jsonl: name 'open' is not
defined. Recording is paused and events are being dropped` — the module globals
are already gone. Declined as cosmetic: the guardrail holds, since nothing raises
into the finalizer and nothing is corrupted, and recording during teardown cannot
be made to work in general. The message's promise that it will report "again when
writing recovers" is what makes it read oddly, and suppressing the report under
`sys.is_finalizing()` is the two-line remedy if it ever bothers anyone.
`atexit` handlers, which run earlier, record normally — measured.

Windows-specific paths were **not** driven, and this audit's subject makes that
gap wider than usual: `os.fork` does not exist there, `_InterProcessFileLock`
takes the `msvcrt.locking` branch, and a closed pipe surfaces differently. Nothing
here can measure any of it, so nothing is claimed about it either way; CI's
Windows leg is the only evidence. The free-threaded build was likewise not driven:
no free-threaded interpreter is installed on this machine
(`sysconfig.get_config_var("Py_GIL_DISABLED")` is `0`). A clock that steps
backwards was not driven, for the reason earlier audits recorded: no means of
stepping the system clock was available. TLS was not driven: nothing in this audit
opened a socket at all.

## Checked and found sound

Seven areas were driven and need no item. The numbers are here so the next audit
can see what this one's coverage actually was.

**Several processes recording into one store.** Eight processes appending 500
traces each to one file: 4,000 lines, 4,000 distinct names, 0 unparsed, nothing
missing, 1.25 s wall. The same eight with rotation at 64 KB under every writer:
21 files, 4,000 lines, 0 missing, 0 torn, and no file left without its final
newline. The advisory lock does what the guardrail says it does, across processes
and not only across threads.

**Reading a store while it is being written.** A reader looping
`load_events()` against a store four processes were appending to: 2,875 calls
during the run, 0 failures. A reader never observes the in-flight tail as a
damaged line.

**`bir prune --yes` against live writers, and against a kill.** Prune run while
four processes appended: no unparsed lines, no torn file, the store loads. Prune
SIGKILLed at four offsets: the store is exactly as it was (24,000 lines before,
24,000 after) and loads back every time. What the killed run leaves behind is
item 4; the file it was rewriting is never the casualty.

**`bir tail` while the file is replaced underneath it.** A prune during a follow
is reported rather than silently swallowed —
`bir: ... was replaced; the events it still held were not shown` — and the four
events written after the replacement were printed. Exit 0 on Ctrl-C.

**Signals during the CLI's own work.** SIGINT delivered to `bir traces` mid-render
exits 130 with an empty stderr after printing 1,710 lines; the same for `bir show`
after 2,346. `bir tail` exits 0 on SIGINT and is killed by SIGTERM as any program
is. An earlier attempt that signalled 50 ms in measured CPython's import machinery
instead, which is why every case here waits until the command is inside its own
code.

**A store path that is not an ordinary file.** A directory, a path whose parent is
a regular file, a 300-character path component, and `/dev/null` all let the traced
call return and report the failure on the `bir` logger, exactly as the full-disk
and permission cases already did:

```
trace_path is a directory      the traced call returned   LOG ERROR bir could not write ... [Errno 21] Is a directory
its parent is a regular file   the traced call returned   LOG ERROR bir could not write ... [Errno 17] File exists
a 300-character component      the traced call returned   LOG ERROR bir could not write ...
trace_path is /dev/null        the traced call returned   LOG ERROR bir could not write ... [Errno 1] Operation not permitted
trace_path is a symlink        the traced call returned   (wrote through the link)
```

The FIFO is the sixth and is under "Declined".

**Who can read what the SDK creates.** Under `umask 022`: the trace store `0600`,
the experiment JSONL `0600`, its `.summary.json` `0600`, and
`Dataset.to_jsonl()`'s output `0644` — the last being the documented handoff to a
path the caller named. The one SDK-created file that is not `0600` is the
`.traces.jsonl.lock` sibling at `0644`, and it is 0 bytes: nothing is ever written
to it, so the private-file decision that shipped earlier is intact where it
claimed to be.

The loaders' memory profile was re-measured with `tracemalloc` on a 5,000-event
store: `load_events` peaks at 9,799 KiB and `load_traces` at 10,432 KiB. Earlier
audits quoted 22,761 KiB and 24,162 KiB for the same event count; that figure was
taken on a different store shape and by a different method, so no trend is claimed
from the difference — only that both loaders are still linear and neither holds
the file.

The OTLP dual-spelling transition is unchanged: the extra still installs
`opentelemetry-sdk` 1.44.0 with `opentelemetry-semantic-conventions` 0.65b0, so
the superseded `deployment.environment` and `gen_ai.system` spellings pinned by
`tests/test_otel_integration.py` still have a live consumer and stay.

`grep -rn "TODO\|FIXME\|XXX" src/` is still empty; it stays a dead end.
