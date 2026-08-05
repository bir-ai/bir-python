# ADR 0001: Distributed trace-context propagation

- **Status:** Accepted, implementation gated
- **Date:** 2026-08-06
- **Applies to:** `bir-sdk` 0.3.0 and later

## Context

Bir generates trace and span ids locally and exposes them read-only through
`get_current_trace_id()` / `get_current_span_id()`. There is no setter, so a
trace stops at the process boundary: an HTTP call, a queue worker, or a
subprocess starts a new, unrelated trace. Users who span services today have to
correlate by hand, usually by copying the trace id into their own metadata.

Four facts constrain the answer.

**Bir ids are UUIDs.** `_new_id()` returns `str(uuid4())` — 16 random bytes in
the dashed form. A W3C `trace-id` is exactly 16 bytes, so a Bir trace id maps
across without loss. A W3C `parent-id` is 8 bytes, so a Bir span id does not.

**The wire schema does not constrain id format.** `tests/fixtures/event-schema-v1.json`
types `id` and `trace_id` as `{"type": "string", "minLength": 1}`. Adopting a
32-hex-character trace id from a remote caller would not break schema `1.0`, but
it would put two id conventions in one field, and nothing in this repository can
confirm what `bir-app` assumes about the shape.

**`parent_id` must name a local event.** Every consumer builds the event tree by
resolving `parent_id` against ids in the same store. The framework bridges'
`_set_event_parent` states this explicitly: it accepts only an id this process
created. A remote span id can never be a `parent_id`.

**The OTLP exporter already declined to unify the id spaces.** It lets the
OpenTelemetry SDK own the exported span and trace ids and carries Bir's ids as
`bir.trace_id` / `bir.event_id` / `bir.parent_id` attributes. Whatever this ADR
decides has to sit beside that choice without contradicting it.

## Options

### A. No propagation

Keep ids read-only. Users correlate through their own metadata field.

Nothing to secure, nothing to validate, no cross-repository change. It also
leaves the original problem unsolved, and every user who needs it reimplements
the same correlation by hand — badly, since the obvious implementation writes an
unvalidated remote string into local metadata.

### B. A Bir-specific header

Send `Bir-Trace-Context: <trace-uuid>/<span-uuid>` and accept it on the way in.

Ids cross verbatim, so a remote span id stays a full UUID and no width is lost.
But it interoperates with nothing: a service instrumented with OpenTelemetry —
which is most of what Bir users already run alongside — neither sends nor
understands it. Bir would own a propagation format, its versioning, and its
edge cases, for no interoperability in return.

### C. W3C Trace Context

Read and write the standard `traceparent` header.

Interoperates with every OpenTelemetry SDK, service mesh, and cloud tracer,
which is the entire value of propagating at all. The cost is the width mismatch:
a Bir span id must narrow to 8 bytes on the way out, and an incoming
`traceparent` carries a `parent-id` that is not a local event id.

## Decision

**Adopt W3C Trace Context (option C), with the remote context recorded rather
than obeyed.**

Concretely, when the eventual API is enabled for a call:

1. **Trace id is adopted.** A valid incoming `trace-id` becomes the local trace
   id, so events recorded in this process join the caller's trace and a consumer
   grouping by `trace_id` sees one trace across services. This is the whole
   point of propagating; anything less leaves two traces joined by a note.

2. **Span ids stay local.** The incoming `parent-id` is *not* written to
   `parent_id`, because that field must resolve inside the store. It is recorded
   on the local trace root as `metadata["remote_parent"]`, alongside the remote
   sampled flag. The tree stays internally consistent, and the link to the
   caller's span is still recorded.

3. **The remote sampled flag is recorded, never obeyed.** This process applies
   its own `sample_rate` and `sample_rules`. Honoring a caller's flag would let
   any client force full recording on a service it calls — a disk-fill and cost
   amplification vector reachable by anyone who can send a header.

4. **Extraction is opt-in per call, never ambient.** There is no environment
   variable and no global switch that makes Bir start trusting headers. A
   deployment that wants propagation passes the header at the call site, which
   is also where a deployment knows whether its callers are inside a trust
   boundary.

5. **Injection is always safe to enable.** Emitting a `traceparent` derived from
   local ids leaks nothing beyond the trace id a downstream service would need
   anyway, so the outbound half carries no trust question.

### Trust boundary

Extraction is the only place Bir would take an identifier from a party it does
not control and write it into local storage. Everything an attacker can reach
lives in one header, so the rules are:

| Threat | Mitigation |
| --- | --- |
| Storage corruption or log injection through a crafted id (newlines, quotes, JSON fragments) | Strict `fullmatch` validation against a hex-only character set, applied before the value is used for anything. Note that Python's `$` also matches before a trailing newline, so an anchored `match` is not sufficient — this bug was caught by the prototype's tests |
| Unbounded input pushed toward a JSONL store | The header is rejected on length before it is parsed |
| Forced recording via the sampled flag | The flag is recorded, never applied |
| Grafting events onto another tenant's trace by guessing or replaying a trace id | Not solvable inside the SDK: a valid trace id is a valid trace id. Extraction stays opt-in so a deployment can decline it wherever callers are untrusted, and the ADR does not claim more |
| Learning what this process accepts | Every rejection returns the same "no usable remote context"; the caller cannot distinguish a bad version from a bad id |

### Collisions

A remote trace id is 16 random bytes, the same width Bir already generates, so
adopting one does not raise the collision probability of the local store. The
outbound narrowing of a span id to 8 bytes matches what every W3C sender emits;
the narrowed value is never used to look up a local event, so a collision
downstream cannot corrupt anything here.

### Interaction with OTLP export

The exporter keeps carrying Bir ids as attributes and keeps letting the
OpenTelemetry SDK own exported span ids. Adopting a remote trace id changes only
the value of `bir.trace_id`, not its meaning, so exported spans from two
processes in one Bir trace will carry the same `bir.trace_id` attribute — which
is the correlation the exporter was already built to provide.

## What ships now

The validated primitive, and nothing else:

- `bir/_trace_context.py` — strict `traceparent` parsing and formatting from Bir
  ids. Internal, exported from no public module, and called by no recording
  path.
- `tests/test_trace_context.py` — the W3C shapes plus the hostile ones:
  oversized headers, newlines, null bytes, JSON fragments, uppercase hex,
  reserved versions, all-zero ids, truncated fields.

The parser is built and tested first because it is the trust boundary, and
because writing the rules down is not the same as proving them: the newline case
above was a real defect in the first implementation.

## What gates the rest

The public API — extraction at a call site, injection into outbound headers, and
the `metadata["remote_parent"]` field — ships only after:

1. **`bir-app` confirms multi-process traces render.** A trace whose events
   arrive from two processes may have two root-level events sharing one
   `trace_id`. Nothing in this repository can verify how the dashboard groups
   and draws that. This is the same open cross-repository check as the framework
   bridges' event-tree shape.
2. **The `metadata.remote_parent` shape is agreed** with `bir-app`, since it is
   new content in an existing field rather than a new schema field.
3. **A security review of the enabled-by-default question.** This ADR says
   opt-in per call; if a later release wants a middleware-style default, the
   trust boundary above has to be revisited, not inherited.

Schema `1.0` does not change: no field is added, removed, or retyped.

## Consequences

Users who need cross-service traces today still correlate by hand, and the
answer to "can I set the trace id?" becomes a documented "not yet, and here is
the design" instead of silence. The SDK gains one internal module and no public
surface, so nothing here constrains the deprecation policy in
`docs/site/stability.md`. A Beta release may not claim distributed trace context
as a feature; it may claim the local trace model is stable, which was the open
question this ADR closes.
