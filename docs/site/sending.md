# Sending to a Server

Send local events to a running Bir server with `send_events()`:

```python
from bir import send_events

result = send_events("http://127.0.0.1:8000")
print(result.accepted, result.attempted, result.skipped)
```

By default the helper posts all selected local JSONL events to the batch
endpoint in one request and, when that endpoint is unavailable, posts them to
`/v1/events` one event at a time. Complete traces are sent root-first. It uses
only the Python standard library.

`SendEventsResult` reports how many events were attempted, newly accepted, and
skipped by an idempotent server response. Local events are not removed after
sending. Re-sending a file is safe against a Bir server because event IDs are
idempotent.

## Large stores and batch size

Pass a positive `batch_size` to opt into bounded upload preparation:

```python
result = send_events(
    "http://127.0.0.1:8000",
    include_rotated=True,
    batch_size=250,
)
```

Batches are sent sequentially, so retries and fallback behavior apply to the
current batch rather than rebuilding the whole store. The selected active and
rotated JSONL files are parsed once into a temporary, disk-backed SQLite spool.
The spool preserves root-first trace order, rotated-file chronology,
first-ID-wins deduplication, and orphan order without retaining the complete
event collection in Python memory. It is removed after both successful and
failed sends.

This bounds live event payloads by the configured batch size, not by JSONL file
size. It does not make the entire call constant-memory:
`SendEventsResult.event_ids` contains every ID reported by the server, and
`mark_sent=True` loads the sent-ID sidecar as a set; those ID-only structures
grow with the number of IDs. With `mark_sent=True`, each successful group is
checkpointed immediately, so a later failure can resume without sending those
accepted events again. Public `load_events()` and `load_traces()` still
materialize their documented complete list results.

Omitting `batch_size` preserves the historical single-request path and avoids
creating the temporary spool.

## Retry behavior

Network errors, timeouts, and HTTP 5xx responses are retried with exponential
backoff. HTTP 4xx responses are treated as permanent and raised immediately.

```python
result = send_events(
    "http://127.0.0.1:8000",
    retries=3,
    backoff=1.0,
    timeout=10.0,
)
```

The delay is `backoff * 2**attempt`. Defaults are two retries, a 0.5-second
backoff, and a 10-second timeout. A healthy send makes one request attempt.

## Redirects are refused, not followed

A 3xx answer is reported and nothing is sent:

```
bir: bir server at http://127.0.0.1:9000/v1/events/batch answered HTTP 302 with a
redirect to http://elsewhere/v1/events; bir does not follow redirects, so nothing
was sent. Point the server URL at the address that serves the API.
```

This is deliberate and it is not what `urllib` does by default. Its redirect
handler answers a 301, 302, or 303 on a POST by reissuing the request as a **GET
with no body** at whatever host the `Location` names — so the events would reach
nobody, and the reply of a host you never configured would be parsed as the
upload result. Bir sends through an opener with that handler replaced, so a
redirect is a refusal like any other, on every one of 301, 302, 303, 307, and
308, for both `send_events()` and `send_experiment()`.

If you see this, the server URL is the thing to fix: a reverse proxy that
redirects to a canonical host, a trailing path, or `http` where the server wants
`https`. Point `--server` (or `server_url=`) at the address that serves the API
directly.

Every other default handler is kept, so proxies configured through the usual
environment variables still apply. One consequence is worth naming: an opener
installed globally with `urllib.request.install_opener()` no longer affects where
Bir sends, because Bir uses its own.

## A response has to describe the request

A success response is checked against what was actually posted, not just for
being the right shape. A reply is refused when:

- `accepted` is negative, or larger than the number of events sent;
- `event_ids` holds more ids than events were sent;
- `event_ids` names an id that was not in the request;
- the body is larger than the ids of those events could occupy.

These are reported figures a caller acts on. `bir send` prints `accepted` and
`skipped` and a pipeline gates on them, so a server answering `{"accepted": -5}`
to a three-event send used to print `accepted=-5 attempted=3 skipped=8` — more
skipped than attempted — and exit `0`. And `event_ids` is exactly what
`--mark-sent` writes to the sidecar, so an id the server invented would be
remembered as delivered for good.

The size limit is derived from the request rather than fixed, because a batch's
accepted ids are legitimately long: it allows an id's worth per event sent plus
room for the envelope. A reply in proportion is still read and parsed whole; one
that cannot be the ids of what was sent is not read at all. Measured against a
server answering a one-event send with a 200 MB body, in a client process of its
own: peak RSS 755 MB before, 34 MB after.

A refusal raises, so nothing is reported as accepted. With `batch_size` set,
batches are posted in sequence and each one's accepted ids are recorded as it
completes, so a refusal part-way leaves the batches that already succeeded marked
and raises for the rest — the same as any other mid-run failure.

## Mark accepted events locally

Pass `mark_sent=True` to avoid requesting already accepted events on later
sends:

```python
send_events("http://127.0.0.1:8000", mark_sent=True)
send_events("http://127.0.0.1:8000", mark_sent=True)  # skips recorded IDs
```

Accepted IDs are recorded in `<trace_path>.sent`, such as
`.bir/traces.jsonl.sent`. The sidecar never modifies trace JSONL or the event
schema. A missing or corrupt sidecar is treated as empty. With the default
`mark_sent=False`, no local bookkeeping is written.

### What bounds the sidecar

The sidecar is bounded by the store, not by everything ever sent: `bir prune`
(and `_prune_trace_store`) drops IDs for events the store no longer holds, since
an ID naming an event that is gone can never be matched by a later send. A
deployment that prunes on a schedule therefore keeps a sidecar proportional to
the traces it retains rather than to its whole history.

Compaction reads every file for that trace path, including size-rotated siblings
a prune without `--include-rotated` left alone, so an ID is dropped only when its
event is in none of them. A dry run changes nothing. The sidecar stays advisory
throughout: it is compacted only after the prune has already succeeded, and a
sidecar that cannot be read or written leaves the prune's own result unaffected —
at worst it stays the size it already was. Without pruning, the sidecar still
grows with the number of IDs recorded.

## Upload size-rotated files

By default `send_events()` uploads only the active trace file, so events stranded
in size-rotated siblings created by `configure(max_bytes=...)` are left behind.
Pass `include_rotated=True` to also upload the retained rotated files:

```python
send_events("http://127.0.0.1:8000", include_rotated=True)
```

Rotated files (`traces.jsonl.1` ..) are uploaded oldest-first, followed by the
active file, preserving write-order chronology. Complete traces are still sent
root-first, and events are deduplicated by ID when a rotated file overlaps the
active one, so each event is sent once. `mark_sent=True` keeps anchoring its
sidecar to the active trace path, so recorded IDs are skipped across the whole
selected file set. The default stays `False` (active file only), leaving existing
behavior unchanged.

## CLI upload

The same operations are available without writing Python:

```bash
bir send --server http://127.0.0.1:8000
bir send --include-rotated --server http://127.0.0.1:8000
bir send --mark-sent --server http://127.0.0.1:8000
bir send --batch-size 250 --server http://127.0.0.1:8000
bir send --retries 3 --backoff 1.0 --timeout 10 \
  --server http://127.0.0.1:8000
bir send-experiment .bir/experiments/<name>-<id>.jsonl \
  --server http://127.0.0.1:8000
bir send-experiment .bir/experiments/<name>-<id>.jsonl \
  --retries 3 --backoff 1.0
```

`bir send` exposes the same knobs as `send_events()`: `--batch-size` opts into a
positive per-request event bound (off by default), `--mark-sent` records
accepted IDs in the `<trace_path>.sent` sidecar and skips them on later sends, and
`--retries` (default `2`), `--backoff` (default `0.5`), and `--timeout` (default
`10`) tune transient-failure handling. `bir send-experiment` shares the same
bounded retry behavior: `--retries` and `--backoff`; these retry values are
non-negative. See
[CLI & Environment Config](cli-env.md) for all commands and
[local evals and experiments](evals-experiments.md#upload-an-experiment) for the
Python API.

To forward traces to an OpenTelemetry backend instead of a Bir server, use
`bir export-otel` (or `export_traces_to_otlp()`); see
[CLI & Environment Config](cli-env.md). Like `send_events()`, it is invoked for
its effect: it returns the number of spans the endpoint accepted and raises
`RuntimeError` if any of them were not, naming the endpoint and how much of the
export arrived.
