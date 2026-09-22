# Schema `1.0` contract corpus

What a consumer of Bir's local trace store has to handle, recorded by the SDK
rather than written by hand. Every line in these files came out of the public API
into a real trace store; only the event ids and the clock are replaced, so the
same bytes come out on every machine.

| File | What it holds |
| --- | --- |
| `core-events.jsonl` | one successful trace covering every event type the schema allows — trace, span, tool call, retrieval (a tool call with `metadata.kind = "retrieval"`), generation with model/usage/cost/prompt, score — and one failed trace carrying the error on both the generation and its root |
| `bridge-events.jsonl` | the event tree each shipped framework bridge records for a root run with a nested generation, one trace per bridge, including the turn span AG2 inserts between them |
| `MANIFEST.json` | per-file checksums, event counts, per-type counts, and the checksum of the schema they were recorded against |
| `CROSS_REPO.json` | whether a real consumer has ever been run against this corpus. It says no |

## How it is guarded

`scripts/contract.py check` re-records the corpus in this process and compares it
byte for byte against what is committed, then validates the result against
[`../fixtures/event-schema-v1.json`](../fixtures/event-schema-v1.json) and the
structural rules below. `tests/test_schema_contract.py` runs the same comparison
inside the unit-test job, and CI runs the script in the fixture drift guard, so a
change to what the SDK records cannot merge without the corpus changing with it —
and a corpus edited by hand cannot merge at all, because re-recording will not
reproduce it.

Changing this corpus is therefore a deliberate act:

```bash
python scripts/contract.py export     # re-record after an intended change
python scripts/contract.py check      # what CI runs
```

## The rules a consumer relies on

Beyond each event matching the schema:

- every event id is unique across the corpus;
- a `trace` event is a root: its `parent_id` is `null` and its `id` equals its
  `trace_id`;
- every other event has a non-empty `parent_id` naming an event **in the same
  file and the same trace**;
- every trace has exactly one root, and following `parent_id` from any event
  reaches it without looping;
- all events of one trace belong together however they are interleaved in the
  file — events are written when they *finish*, so a child is written before its
  parent and a store is never in tree order.

`scripts/contract.py verify <dir>` checks all of that with nothing but the
standard library, and does not import the SDK, so a consumer repository can
vendor the bundle and run the same verification on its own copy.

One thing a consumer must not assume is the line terminator. The store is
appended to in text mode, so a store written on Windows ends its lines with
`\r\n` while every other platform writes `\n` — and a store that has been
through `bir prune` is rewritten with `\n` wherever it was written. Read a
store line by line and strip, rather than splitting on a fixed terminator. The
corpus here is recorded in the canonical `\n` form on every platform, which is
what its checksums are taken over.

## Relationship to `../fixtures/`

[`tests/fixtures/`](../fixtures/) holds the four files the SDK and the product
repo keep byte-for-byte identical, with a checksum manifest shared between both
repos; changing one is a paired change in two repositories. This directory is
SDK-owned and machine-generated, so it can grow with what the SDK records without
a coordinated release. The two are joined at the schema: this corpus is recorded
and verified against `tests/fixtures/event-schema-v1.json`, and `MANIFEST.json`
records that schema's checksum, so the corpus cannot quietly be verified against
a different schema than the one the shared fixture set pins.

When a consumer repository is reachable, the corpus is handed over with
`python scripts/contract.py bundle --out <dir>` — schema, corpus, manifest, the
verifier, and a README of what the consumer is expected to do — and the result is
recorded in `CROSS_REPO.json`.
