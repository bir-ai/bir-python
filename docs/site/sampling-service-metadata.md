# Sampling & Service Metadata

## Service metadata

Use `configure()` to tag trace roots with the service and environment that
produced them. Both values are optional, must be non-empty strings, and are
recorded under `metadata.service`.

```python
from bir import configure

configure(service_name="rag-api", environment="production")
```

The same defaults can come from the environment:

```bash
export BIR_SERVICE_NAME=rag-api
export BIR_ENVIRONMENT=production
```

Explicit configuration takes precedence over environment defaults.

## Trace source

Use `configure(source=...)` to tag trace roots with where they originated. The
value is optional, must be a non-empty string, and is recorded under
`metadata.source`. It is the SDK-side counterpart to the `source` the Bir
server and dashboard already filter on (the built-in Playground records
`"playground"`), so SDK traces become filterable by source alongside
product-generated ones.

```python
from bir import configure

configure(source="checkout-api")
```

The same default can come from the environment:

```bash
export BIR_SOURCE=checkout-api
```

The server matches `source` by exact, trimmed value, so pick a stable label. An
explicit `source` in a `trace(metadata={"source": ...})` block still wins over
the configured default.

## Sampling

Use `configure(sample_rate=...)` to bound local trace volume. `sample_rate` is
the probability from `0.0` to `1.0` that a trace is recorded and defaults to
`1.0`.

```python
from bir import configure

configure(sample_rate=0.1)  # record about 10% of traces
```

The decision is made once per trace root and inherited by every event under it.
A sampled-out trace and all its spans, generations, tool calls, retrievals, and
scores write nothing.

Sampling never changes application control flow: a sampled-out function still
runs and still raises its own exceptions; only local JSONL writes are skipped.

Set the same default from the environment with:

```bash
export BIR_SAMPLE_RATE=0.1
```

## Trace file rotation

The local trace file grows without bound by default. Opt in to size-based
rotation:

```python
from bir import configure

configure(max_bytes=5_000_000, backup_count=3)
```

The active file rotates before a write would exceed `max_bytes`:
`traces.jsonl` becomes `traces.jsonl.1`, the previous `.1` becomes `.2`, and so
on. The default `backup_count` is `3`; `backup_count=0` discards the active file
when it fills instead of retaining backups.

Rotation happens on whole-line boundaries, so an event is never split across
files. A single line larger than `max_bytes` is written whole. Rotation uses the
same lock as writes and adds no dependencies.

Reads use only the active file by default. To reconstruct write order across
backups:

```python
from bir import load_events, load_traces

events = load_events(include_rotated=True)
traces = load_traces(include_rotated=True)
```

A trace can cross a rotation boundary, so it may appear incomplete if only part
of the rotated set remains.
