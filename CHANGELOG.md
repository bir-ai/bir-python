# Changelog

All notable changes to the Bir Python SDK are documented here.

This project follows a small-release workflow while the SDK is early-stage.
Before publishing, verify the release with the SDK release checklist in
`docs/SDK_RELEASE_CHECKLIST.md`.

## Unreleased

### Security

- Expanded best-effort capture redaction to recognize JWTs, AWS access key IDs,
  Google API keys, Slack tokens, and GitHub provider tokens. **CROSS-REPO CONTRACT:
  bir-app's independently maintained redactor and its copy of
  `redaction-cases.json` must be updated to match before or with this change. Do
  not release this SDK change while the bir-app redactor or fixture is out of
  sync.**

### Added

- `run_experiment_async()`, an asynchronous experiment runner for async or sync
  tasks. It accepts coroutine functions, plain sync callables, and sync callables
  that return an awaitable (decided per call with `inspect.isawaitable`), runs up
  to `max_concurrency` examples at once (a positive integer, default `1`), and
  always persists results, JSONL rows, and summary aggregates in dataset order
  regardless of completion order. Evaluator execution, task input binding,
  redaction, `raise_on_error` semantics, `record_traces` trace trees, and the
  persisted JSONL/summary schema match `run_experiment()`. Each example runs in
  its own asyncio task, so concurrent `record_traces=True` runs keep isolated
  trace trees; cancelling the runner cancels and awaits the in-flight example
  tasks and re-raises `CancelledError` without writing a misleading success
  summary. Stdlib only (`asyncio`, `inspect`).
- A structured MkDocs documentation site covering the quickstart, core API,
  privacy and capture, sampling and service metadata, server uploads,
  integrations, evals, CLI, and environment configuration. The documentation
  toolchain is isolated in the optional `docs` dependency extra.
- Bounded retry with exponential backoff for `send_events()`. New `retries`
  (default `2`) and `backoff` (default `0.5`) keyword arguments retry transient
  failures — network errors, timeouts, and HTTP 5xx — sleeping
  `backoff * 2**attempt` seconds between attempts; HTTP 4xx is still raised
  immediately without retry. A healthy send makes a single attempt, so the
  default behavior is unchanged. Stdlib only (`time`).
- Matching bounded retry with exponential backoff for `send_experiment()` and
  `bir send-experiment`. New `retries` (default `2`) and `backoff` (default `0.5`)
  keyword arguments — and non-negative `--retries`/`--backoff` CLI options — retry
  the same transient failures (network errors, timeouts, and HTTP 5xx) sleeping
  `backoff * 2**attempt` seconds between attempts. HTTP 4xx, a missing experiment
  or summary file, and an invalid success response body are still raised
  immediately without retry, and a healthy send still makes one request with no
  sleep, so the default behavior is unchanged. Stdlib only.
- Opt-in `send_events(mark_sent=True)` to make re-sends cheap. Accepted event IDs
  are recorded in a sidecar file next to the trace file (`<trace_path>.sent`) and
  skipped on later sends, so `attempted` reflects only events not yet recorded as
  sent. The sidecar is SDK-local bookkeeping: it never modifies the trace JSONL or
  the event schema, and a missing or corrupt sidecar is treated as empty so it can
  never block a send. Defaults to `False` (nothing recorded), keeping re-sends
  safe via the server's existing event-ID idempotency.
- Opt-in size-based rotation for the local trace file via
  `configure(max_bytes=..., backup_count=...)`. When `max_bytes` is set, the
  active `.bir/traces.jsonl` is rotated on whole-line boundaries before a write
  would exceed the cap (`traces.jsonl` -> `traces.jsonl.1` -> ..), keeping at
  most `backup_count` rotated files (default `3`) and dropping the oldest, so
  every file stays valid JSONL. `load_events()` and `load_traces()` gain an
  additive `include_rotated=True` flag that also reads rotated files oldest-first;
  the default still reads only the active file. `max_bytes` defaults to `None`
  (unlimited), keeping the previous single-file behavior unchanged. Stdlib only.
- Opt-in `send_events(include_rotated=True)` and `bir send --include-rotated` to
  upload size-rotated trace files so rotation can no longer strand unsent events.
  Retained rotated siblings (`traces.jsonl.1` ..) are uploaded oldest-first
  followed by the active file, complete traces stay root-first, and events are
  deduplicated by ID when a rotated file overlaps the active one. `mark_sent`
  keeps anchoring its sidecar to the active trace path, so recorded IDs are
  skipped across the whole selected file set. `bir traces --include-rotated`
  reuses the same flag on the public loader. Defaults to `False` (active file
  only), so existing `send_events()` calls and `bir send` invocations upload only
  the active file as before. Stdlib only.
- Dependency-free AWS Bedrock integration: `trace_converse()` wraps a
  `bedrock-runtime` `converse` call, recording the request `modelId` and the
  Converse `usage` block (`inputTokens`/`outputTokens`/`totalTokens`, deriving the
  total when omitted) without importing `boto3`.
- Dependency-free Google Vertex AI integration: `trace_generate_content()`
  (exported from `bir.integrations` as `trace_vertex_generate_content`) wraps a
  Vertex `GenerativeModel.generate_content` call, recording the model from
  `bir_model` (refined by the response `model_version`) and `usage_metadata` token
  counts without importing `vertexai`.
- Aggregate-score experiment comparison through `compare_experiments()` and
  `ExperimentDiff`, plus a stdlib-only `bir eval-gate` command that exits
  non-zero when a candidate regression exceeds the configured tolerance. The
  comparison takes per-evaluator tolerance overrides via `score_tolerances`
  (repeatable `--score-tolerance NAME=VALUE` on the CLI), which override the
  global `tolerance` only for the named shared evaluators while preserving the
  strict per-evaluator `math.isclose` boundary; non-negative finite values are
  required and an override naming a non-shared evaluator is rejected so typos
  fail loudly. A `missing_score` policy (`--missing-score {ignore,regress}`)
  controls evaluators present only in the baseline: `ignore` (the default)
  reports them without failing, matching the previous behavior, while `regress`
  treats a removed evaluator as a regression because it silently drops coverage.
  `ExperimentDiff.to_dict()` additionally reports `effective_tolerances`,
  `missing_score`, and `regression_reasons` so the gate decision is fully
  machine-readable. Conflicting or malformed CLI assignments are rejected with
  clear errors. Stdlib only.
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

- Local trace append/rotation and sent-ID sidecar merge/replace operations now
  use stdlib advisory file locks in addition to the existing in-process locks.
  Concurrent local processes writing one trace path no longer race rotation or
  lose sent-ID updates; sidecar replacements use unique, cleaned-up temp files.
  POSIX uses `flock` and Windows uses byte-range locking. No runtime dependency,
  public API, event schema, JSON formatting, or default rotation behavior changed.
- CI now installs the optional `docs` extra and runs `mkdocs build --strict`
  once per pull request and push to `main`, catching documentation navigation,
  link, and warning regressions without adding runtime dependencies.
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

- Release verification wheels now include and inspect the complete `bir` package
  tree, including every `bir.integrations` module, and smoke-test those imports
  in a clean environment without provider SDKs.
- Made the Pyright release gate independent of interpreter discovery by keeping
  the checked offline example tests free of runtime Pytest imports. The same
  three smoke scenarios remain collectable by Pytest with per-test SDK and
  temporary-path isolation; Pytest remains a development-only dependency.
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
