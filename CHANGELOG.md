# Changelog

All notable changes to the Bir Python SDK are documented here.

This project follows a small-release workflow while the SDK is early-stage.
Before publishing, verify the release with the SDK release checklist in
`../../docs/SDK_RELEASE_CHECKLIST.md`.

## 0.1.0 - Unreleased

Initial local MVP SDK release.

### Added

- `@observe()` decorator for sync Python functions.
- `trace()` context manager for manually scoped root traces.
- Nested `span()` context manager.
- `generation()` context manager with optional model, usage, and user-provided cost fields.
- `tool_call()` context manager for external function or tool usage.
- `retrieval()` context manager for RAG lookups using the existing tool call event contract.
- `prompt()` helper for attaching prompt name, version, and optional prompt payload metadata to generation events.
- `BirCallbackHandler` for dependency-free LangChain callback tracing.
- `score()` helper for attaching evaluation scores to active traces, with optional redacted `metadata` for evaluator reasoning or thresholds.
- Local JSONL trace storage at `.bir/traces.jsonl` by default.
- `load_events()` and `load_traces()` helpers for reading local JSONL traces.
- `send_events()` helper for posting local events to the Bir FastAPI ingestion server.
- `SendEventsResult.attempted` and `SendEventsResult.skipped` for clearer upload summaries.
- Opt-in input and output capture.
- Best-effort redaction for common secret-like keys and text patterns.
- `bir.evals` deterministic evaluators: `exact_match()`, `contains()`, `regex_match()`, `json_valid()`, `field_equals()`, `field_contains()`, `latency_under()`, `cost_under()`, `numeric_between()`, and `custom_evaluator()`.
- `bir.evals.answer_context_overlap()` deterministic RAG faithfulness heuristic that scores answer/context word overlap.
- Local JSONL dataset loading and experiment result writing through `Dataset` and `run_experiment()`.
- `Dataset.to_jsonl(..., redact=False)` for intentional raw dataset export while keeping redaction enabled by default.

### Notes

- Async tracing is intentionally not part of the first SDK release.
- Server-side ingestion and dashboard viewing are separate local MVP components.
- Cost values are explicit user-provided values; Bir does not calculate provider pricing.
