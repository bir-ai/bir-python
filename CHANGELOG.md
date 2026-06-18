# Changelog

All notable changes to the Bir Python SDK are documented here.

This project follows a small-release workflow while the SDK is early-stage.
Before publishing, verify the release with the SDK release checklist in
`docs/SDK_RELEASE_CHECKLIST.md`.

## Unreleased

### Added

- A stdlib-only `bir` command-line interface, installed as a console script, for
  inspecting local traces and experiments and sending them to a server without
  writing a script. Subcommands: `bir traces`, `bir tail`, `bir experiments`,
  `bir send`, and `bir send-experiment`, with `--json` output on `traces` and
  `experiments` for scripting. The CLI builds on the existing public API and
  adds no runtime dependencies.
- Environment-variable defaults for SDK configuration so deployments can
  configure Bir without code changes. `BIR_TRACE_PATH`, `BIR_CAPTURE_INPUTS`,
  `BIR_CAPTURE_OUTPUTS`, `BIR_SAMPLE_RATE`, `BIR_SERVICE_NAME`, and
  `BIR_ENVIRONMENT` provide the defaults read once at import time. Explicit
  `configure(...)` arguments still take precedence, capture stays disabled
  unless explicitly enabled, and invalid values raise a clear error.
- Shipped a PEP 561 `py.typed` marker so downstream type checkers (mypy,
  pyright) use the SDK's inline type annotations instead of ignoring them.

### Changed

- `scripts/verify_release.py` now builds and installs its verification wheel
  under the `bir-sdk` distribution name and asserts that
  `importlib.metadata.version("bir-sdk")` matches the project version, so
  distribution-name drift fails release verification instead of being masked by
  a wheel named `bir`.
- `scripts/verify_release.py` now ships the `[project.scripts]` console-script
  entry point in its verification wheel and asserts the installed `bir` command
  is invokable, so console-script regressions fail release verification.
- CI now runs the SDK unit tests and example smoke tests against Python 3.10,
  3.11, 3.12, and 3.13 (previously only 3.12) so version-specific regressions
  across the advertised support range surface before release.

### Fixed

- `bir.__version__` now reads the published `bir-sdk` distribution metadata
  instead of `bir`. Installed packages report their real version instead of
  silently falling back to a hardcoded string; the fallback applies only when
  running from source (`PYTHONPATH=src`) without an install.

## 0.1.1 - 2026-06-18

### Changed

- Relicensed the project from FSL-1.1-ALv2 to the Apache License 2.0.
- Updated project links to the `bir-ai/bir-python` GitHub repository.

## 0.1.0 - 2026-06-17

Initial local MVP SDK release.

### Added

- `@observe()` decorator for sync and async (coroutine) Python functions, producing the same trace and span events on both paths.
- `trace()` context manager for manually scoped root traces.
- Nested `span()` context manager.
- `generation()` context manager with optional model, usage, and user-provided cost fields.
- `tool_call()` context manager for external function or tool usage.
- `retrieval()` context manager for RAG lookups using the existing tool call event contract.
- `async with` support for the `generation()`, `tool_call()`, `retrieval()`, and `trace()` context managers, matching `span()` and recording the same events as their sync `with` form.
- `prompt()` helper for attaching prompt name, version, and optional prompt payload metadata to generation events.
- `BirCallbackHandler` for dependency-free LangChain callback tracing.
- LangChain token usage extraction from common `usage_metadata` and `response_metadata` response shapes.
- `score()` helper for attaching evaluation scores to active traces, with optional redacted `metadata` for evaluator reasoning or thresholds.
- Local JSONL trace storage at `.bir/traces.jsonl` by default.
- `load_events()` and `load_traces()` helpers for reading local JSONL traces.
- `send_events()` helper for posting local events to the Bir FastAPI ingestion server.
- `SendEventsResult.attempted` and `SendEventsResult.skipped` for clearer upload summaries.
- Validation that generation usage and cost setters include at least one field.
- Thread-safe local trace writes within a single SDK process.
- Opt-in input and output capture.
- Best-effort redaction for common secret-like keys and text patterns.
- `bir.evals` deterministic evaluators: `exact_match()`, `contains()`, `regex_match()`, `json_valid()`, `field_equals()`, `field_contains()`, `latency_under()`, `cost_under()`, `numeric_between()`, and `custom_evaluator()`.
- `bir.evals.answer_context_overlap()` deterministic RAG faithfulness heuristic that scores answer/context word overlap.
- `bir.evals.retrieved_context_contains()` deterministic RAG retrieval check that scores whether an expected string appears in the retrieved `contexts` list.
- `bir.evals.answer_contains_citation()` deterministic RAG citation check that scores whether an answer (a plain string or the `answer` field of a dict) contains a bracketed citation marker such as `[1]` or `[doc-1]`, with an optional `pattern` override for custom citation formats.
- Local JSONL dataset loading and experiment result writing through `Dataset` and `run_experiment()`.
- `Dataset.to_jsonl(..., redact=False)` for intentional raw dataset export while keeping redaction enabled by default.

### Notes

- `@observe()` traces coroutine functions, and the `span()`, `generation()`, `tool_call()`, `retrieval()`, and `trace()` context managers all support `async with`, producing the same events on the sync and async paths.
- Server-side ingestion and dashboard viewing are separate local MVP components.
- Cost values are explicit user-provided values; Bir does not calculate provider pricing.
