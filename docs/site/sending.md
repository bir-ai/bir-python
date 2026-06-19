# Sending to a Server

Send local events to a running Bir server with `send_events()`:

```python
from bir import send_events

result = send_events("http://127.0.0.1:8000")
print(result.accepted, result.attempted, result.skipped)
```

The helper posts local JSONL events to the server, batching them when supported
and otherwise posting to `/v1/events` one at a time. Complete traces are sent
root-first. It uses only the Python standard library.

`SendEventsResult` reports how many events were attempted, newly accepted, and
skipped by an idempotent server response. Local events are not removed after
sending. Re-sending a file is safe against a Bir server because event IDs are
idempotent.

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

## CLI upload

The same operations are available without writing Python:

```bash
bir send --server http://127.0.0.1:8000
bir send-experiment .bir/experiments/<name>-<id>.jsonl \
  --server http://127.0.0.1:8000
```

See [CLI & Environment Config](cli-env.md) for all commands.
