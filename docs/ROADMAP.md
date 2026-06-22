# Bir Python SDK — Prioritized Improvement Roadmap

Audited against repository state on 2026-06-22. This document is planning
material only; none of the improvements below are implemented here. The previous
edition of this file enumerated 11 improvements that have **all since shipped**
(see the CHANGELOG `Unreleased` section and the git log: hermetic pyright/release
verification, full-tree verification wheel, strict MkDocs CI, interprocess
locks, rotated uploads, `send_experiment` retries, `run_experiment_async`,
per-evaluator eval-gate tolerances, OpenAI Responses wrapper, generator-aware
`@observe()`, and additive custom redaction). This edition starts from that newer
baseline.

## Phase 2 — Assessment

### What already exists

Bir is a zero-runtime-dependency (`dependencies = []`), local-first tracing and
deterministic-evals SDK for Python 3.10+. The public surface is small and
re-exported from `src/bir/__init__.py`: `observe`, `trace`, `span`, `generation`,
`tool_call`, `retrieval`, `score`, `prompt`, `configure`, `load_events`,
`load_traces`, `send_events`, plus the `TraceEvent`/`LoadedTrace`/
`SendEventsResult`/`PromptRecord` dataclasses and `__version__`.

- **Core (`src/bir/_sdk.py`, ~2.5k lines):** sync, async, generator, and
  async-generator `@observe()`; context managers for trace/span/generation/
  tool_call/retrieval with matching `async with`; opt-in input/output capture;
  bounded JSON-safe capture with built-in + user-additive redaction; sampling;
  service/environment metadata; size-based rotation (`max_bytes`/`backup_count`);
  interprocess-locked JSONL writes (`flock`/`msvcrt`); `send_events` with
  retry/backoff, `mark_sent` sidecar, and `include_rotated`; strict line-level
  loaders with schema-`1.0` validation; `BIR_*` environment configuration read at
  import.
- **Evals (`src/bir/evals.py`, ~1.9k lines):** deterministic evaluators,
  `Dataset`, `run_experiment` + `run_experiment_async` (bounded concurrency,
  dataset-ordered persistence), experiment load/list/send with retry/backoff, and
  `compare_experiments`/`ExperimentDiff` with per-evaluator tolerances and a
  missing-score policy.
- **Integrations (`src/bir/integrations/`):** dependency-free provider wrappers
  for OpenAI (Chat Completions + Responses), Anthropic, Google Gemini, Mistral,
  Cohere, LiteLLM, AWS Bedrock Converse, and Vertex AI, plus LangChain and
  LlamaIndex callback handlers. Shared response-shape helpers live in
  `_common.py`. Wrappers forward provider args unchanged, prefix SDK options with
  `bir_`, never import the provider package, and read model/usage/output from
  mapping-or-attribute shapes.
- **CLI (`src/bir/cli.py`):** stdlib-only `bir` console script with `traces`,
  `tail`, `experiments`, `send`, `send-experiment`, and `eval-gate`.
- **CI/release:** unit + example tests on Python 3.10–3.13; pyright and
  `scripts/verify_release.py` (offline wheel build/inspect/smoke covering the full
  package tree incl. integrations and `py.typed`) on 3.12; strict `mkdocs build`;
  tag-driven PyPI Trusted Publishing.
- **Docs:** README, CHANGELOG, a structured MkDocs site under `docs/site/`, and
  `docs/SDK_RELEASE_CHECKLIST.md` / `docs/EVALUATOR_IMPLEMENTATION_GUIDE.md`.

### Conventions and idioms (follow these in every prompt)

- Frozen dataclasses for config and public records; config replaced atomically
  with `dataclasses.replace`. Tests reset via `_reset_config_for_tests()`.
- Centralized `_validate_*` / `_expect_*` helpers reject booleans-as-numbers,
  non-finite floats, negatives, empties, and malformed stored data with clear
  `ValueError`/`TypeError`.
- Trace nesting via `ContextVar`; sync and `async with` paths share event
  construction and preserve exception propagation; a storage failure re-raises the
  user's original exception chained to the storage error.
- Deterministic strict JSON everywhere: `json.dumps(..., sort_keys=True,
  separators=(",", ":"), allow_nan=False)`, one object per line.
- Capture is opt-in; all captured values flow through `_safe_capture` /
  `_safe_repr` / `_safe_error` redaction before persistence.
- Integrations are thin, lazy-import-free of the provider, and reuse `_common.py`.
- Tests are primarily `unittest` with `tempfile`, fake provider objects, and
  patched `urllib`/`time`; `pytest` runs only `tests/test_examples.py`. Each
  integration has its own `tests/test_*_integration.py`.

### Candidate-area status (done vs. missing)

| Candidate area | Status |
|---|---|
| PEP 561 `py.typed` marker | **Done** — ships, in package-data, checked by release verifier |
| `BIR_*` environment configuration | **Done** — `BIR_TRACE_PATH/CAPTURE_INPUTS/CAPTURE_OUTPUTS/SAMPLE_RATE/SERVICE_NAME/ENVIRONMENT` |
| CI Python matrix 3.10–3.13 | **Done** |
| `bir` CLI (tail/inspect/list/send) | **Partly done** — list/tail/send exist; single-trace inspect, stats, and validate are missing |
| Trace-file rotation / size cap | **Done** (size-based); time-based not present (deliberately not proposed) |
| `send_events()` retry/backoff + mark-sent | **Done** |
| Additional integrations | **Partly** — Bedrock + Vertex done; OpenAI Agents SDK, Pydantic AI, Instructor, DSPy, CrewAI, Haystack missing |
| Streaming helpers / Anthropic parity | **Partly** — OpenAI (both), Anthropic, Gemini stream (sync); Mistral/Cohere/LiteLLM/Bedrock/Vertex do not; **no async streaming anywhere** |
| Experiment regression detection + CI gate | **Done** — `compare_experiments`, `ExperimentDiff`, `bir eval-gate`, async runner |
| Richer configurable redaction (JWT/AKIA/GCP) | **Done** — built-ins expanded + user-additive keys/patterns |
| OpenTelemetry/OTLP export behind an extra | **Missing** |
| MkDocs documentation site | **Done** + strict CI |

### Genuinely missing or incomplete (this roadmap's focus)

1. **No async support in any integration wrapper.** Every wrapper runs
   `response = create(*args, **kwargs)` synchronously. Passing an async client
   method (`AsyncOpenAI().chat.completions.create`, `AsyncAnthropic().messages.create`,
   `litellm.acompletion`, …) returns an un-awaited coroutine and records garbage.
   Async clients are the dominant production pattern; `@observe()` and
   `run_experiment_async()` already support async, so the integrations are the
   conspicuous gap.
2. **Sync streaming covers only 3 of 8 providers.** Mistral, Cohere, and LiteLLM
   (OpenAI-shaped) plus Bedrock `converse_stream` and Vertex streaming silently
   record the unconsumed stream object's `repr` and no usage.
3. **CLI can't inspect a single trace, summarize usage/cost, or validate a file.**
   `bir traces` only lists; there is no tree view, no aggregate stats, and no
   schema validator for a trace JSONL file.
4. **No public way to read the active trace/span id** for log correlation
   (`_current_trace_id` is private).
5. **No way to enrich an event's metadata after creation** — metadata is fixed at
   context-manager creation; spans carry none at all.
6. **Cost is fully manual.** There is no opt-in, local helper to derive
   generation cost from token usage and user-supplied rates.
7. **No OpenTelemetry/OTLP bridge** to forward local traces into existing
   observability backends.

There are **no P0 correctness or packaging gaps**: the four required gates
(`unittest`, example `pytest`, `pyright`, `verify_release.py`) and the release
workflow are structurally complete. Accordingly this roadmap is honestly weighted
to P1 functional gaps and P2 additive enhancements rather than manufactured
blockers.

## Phase 3 — Prioritized improvements

| # | Improvement | Category | Priority | Size | Risk | Depends on |
|---:|---|---|---|---|---|---|
| 1 | Async provider integration wrappers | integrations | P1 | L | med | — |
| 2 | Sync streaming parity for Mistral, Cohere, LiteLLM | integrations | P1 | M | med | — |
| 3 | `bir show <trace-id>` single-trace tree inspector | dx | P1 | M | low | — |
| 4 | `bir stats` local usage/cost/latency summary | dx | P1 | S | low | — |
| 5 | Public `get_current_trace_id()` / `get_current_span_id()` | core / dx | P2 | S | low | — |
| 6 | Post-creation `set_metadata()` on event context managers | core | P2 | S | low | — |
| 7 | `bir validate` trace-JSONL schema validator | dx / ci | P2 | S | low | — |
| 8 | Optional local cost estimation from a user price table | core | P2 | M | med | — |
| 9 | Bedrock `converse_stream` + Vertex AI streaming parity | integrations | P2 | M | med | 2 (shared idioms) |
| 10 | OpenAI Agents SDK tracing-processor integration | integrations | P2 | M | med | — |
| 11 | OpenTelemetry/OTLP export behind an optional `[otel]` extra | integrations | P2 | L | med–high | — |

### Rationale and acceptance summary

1. **Async provider wrappers.** Highest-leverage functional gap: production LLM
   code is overwhelmingly async, and the sync-only wrappers break on async
   clients. Acceptance: each covered provider gains an awaitable counterpart that
   awaits the provider coroutine, records the same generation, and (where the sync
   wrapper already streams) returns an async iterator that yields events unchanged
   and finalizes on exhaustion/close/error. Trade-off: touches several modules, so
   it is L; mitigated by shared `_common.py` helpers and one async-stream idiom.
2. **Sync streaming parity (Mistral/Cohere/LiteLLM).** These are OpenAI/near-OpenAI
   shaped, so they reuse the existing `_stream_chat_completion` idiom. Acceptance:
   `stream=True` yields provider chunks unchanged, accumulates text, and reads
   final usage after consumption; non-iterable responses fall back to one-shot
   recording. Bedrock/Vertex are deliberately deferred to #9 (different stream
   APIs).
3. **`bir show`.** The only way to read a recorded trace today is to write Python.
   Acceptance: `bir show <id>` prints the trace's events as an indented tree by
   `parent_id` with type, name, status, duration, model, usage, and score values;
   `--json` emits the structured tree; unknown id exits non-zero with a clear
   message. stdlib only, built on `load_traces`.
4. **`bir stats`.** Quick local cost/usage visibility without a server.
   Acceptance: aggregate across loaded traces — trace count, success/error counts,
   summed input/output/total tokens, summed cost per currency, and mean/p95
   latency; `--json` for scripting; honors `--path`/`--include-rotated`.
5. **`get_current_trace_id()` / `get_current_span_id()`.** Tiny, frequently needed
   for correlating application logs with traces. Acceptance: public functions
   returning the active ids or `None`; re-exported from `bir`; never raise outside
   a trace.
6. **`set_metadata()`.** Lets callers attach context discovered mid-body (e.g. a
   resolved route, a cache hit) to the current generation/tool_call/retrieval/span/
   trace. Acceptance: additive method that merges into the event's metadata,
   redacted at write; spans gain metadata support; no schema change (metadata is
   already free-form on every event).
7. **`bir validate`.** Useful for debugging local files and for guarding the
   cross-repo wire contract. Acceptance: validates each line of a trace JSONL file
   with the existing strict parser, reports every offending line (not just the
   first), and exits non-zero on any error; `--json` summary.
8. **Optional local cost estimation.** Bir intentionally bundles no prices (they
   go stale), but many users want cost dashboards. Acceptance: opt-in
   `configure(model_prices=...)` (user-supplied per-token rates, no bundled
   table); when set, a generation with usage but no explicit cost auto-fills cost;
   explicit `set_cost` always wins; invalid tables fail fast. Trade-off: adds a
   small config surface — justified, opt-in, and consistent with "cost is
   user-provided."
9. **Bedrock/Vertex streaming.** Completes streaming coverage. Acceptance:
   `trace_converse_stream` consumes a Converse stream
   (`contentBlockDelta`/`messageStop`/`metadata.usage`); Vertex streaming consumes
   chunked `generate_content`. Separated from #2 because the event shapes differ.
10. **OpenAI Agents SDK integration.** The candidate list's most stable
    remaining framework target — it exposes a documented tracing-processor hook
    analogous to LangChain callbacks. Acceptance: a dependency-free
    `TracingProcessor`-style adapter mapping agent/LLM/tool spans to Bir
    trace/generation/tool_call events, never importing the Agents SDK. Risk: the
    hook API is younger than LangChain's, so the prompt instructs verifying the
    current public interface first.
11. **OTLP export.** The natural "send Bir to my existing backend" story; deferred
    in the prior roadmap and still unbuilt. Acceptance: an optional `[otel]` extra
    and a lazy-importing exporter that converts loaded Bir traces to OTel spans
    (gen_ai.* attributes for model/usage) and ships them via OTLP; runtime
    `dependencies` stay empty. Trade-off: sizable optional dep + semantic-
    convention mapping, hence L / med–high and last.

## Phase 4 — Standalone Claude Code prompts

### 1. Async provider integration wrappers

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Add awaitable async counterparts to the dependency-free provider wrappers so applications using async provider clients (e.g. AsyncOpenAI, AsyncAnthropic, litellm.acompletion) get the same Bir generation events the sync wrappers produce.

WHY
Every current wrapper runs `response = create(*args, **kwargs)` synchronously, so passing an async client method returns an un-awaited coroutine and records garbage. Async clients are the dominant production pattern, and `@observe()` plus `run_experiment_async()` already support async, leaving the integrations as the conspicuous gap.

IMPLEMENTATION GUIDANCE
Add async variants for the providers whose client methods are coroutines: OpenAI Chat Completions and Responses (src/bir/integrations/openai.py), Anthropic Messages (anthropic.py), Google Gemini (google.py), Mistral (mistral.py), Cohere (cohere.py), and LiteLLM (litellm.py). Mirror each existing sync wrapper exactly but `await` the provider call inside `async with generation(...)` (the generation/tool_call/retrieval/trace context managers already implement `__aenter__`/`__aexit__`). Name them with an `_async` suffix (e.g. `trace_chat_completion_async`, `trace_response_async`, `trace_messages_async`, `trace_generate_content_async`, `trace_chat_async`, `trace_completion_async`) and re-export from src/bir/integrations/__init__.py. For providers whose sync wrapper already streams (OpenAI both, Anthropic, Gemini), the async wrapper must return an async iterator (`async def` generator) that yields the provider's async-stream events unchanged via `async for`, accumulates text/usage with the existing `_common.py` helpers, and finalizes model/usage/output in a `finally` on exhaustion/close/error — never buffering the stream. Reuse `_value`, `_usage_tokens`, `_string_or_none`, `_response_output`, and `_is_streamed_response`; add a small async-aware stream detector only if needed (an async stream exposes `__aiter__`). Keep the `bir_`-prefixed option names identical to the sync wrappers. Do not import any provider package; tests use fake async clients/streams.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- Each covered provider has an async wrapper that awaits the provider coroutine and records one generation with model/output/usage when present, returning the provider's awaited result unchanged.
- For providers that stream synchronously today, the async wrapper returns an async iterator that yields the original async events in order and finalizes usage/model/output on exhaustion, `aclose()`, and mid-stream error (re-raised unchanged, persisted error redacted).
- Provider arguments are forwarded unchanged; `bir_*` options are not forwarded.
- Async wrappers require an active trace and work under `@observe()` async functions and `async with bir.trace(...)`.
- Importing every integration module still succeeds with no provider package installed.
- Existing sync wrappers are unchanged.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest), covering the happy
path, edge cases, and any validation/error paths.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add/adjust the relevant section) and add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the
release contract changes.

OUT OF SCOPE
Do not add async support to Bedrock/Vertex (boto3/vertexai async is niche) or to the LangChain/LlamaIndex callback handlers; do not change sync wrapper behavior, import provider packages, calculate prices, or change the event schema.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 2. Sync streaming parity for Mistral, Cohere, and LiteLLM

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Add synchronous streaming support to the Mistral, Cohere, and LiteLLM wrappers so `stream=True` records accumulated output text and final token usage instead of the unconsumed stream object's repr.

WHY
Only OpenAI (Chat + Responses), Anthropic, and Gemini handle `stream=True` today. For Mistral/Cohere/LiteLLM the current wrappers pass the stream iterator to `_response_output`, recording a meaningless generator repr and no usage while the user still consumes the stream.

IMPLEMENTATION GUIDANCE
Follow the existing `_stream_chat_completion` pattern in src/bir/integrations/openai.py. In each of mistral.py, litellm.py, and cohere.py: when `kwargs.get("stream") is True`, delegate to a `_stream_*` helper that opens `generation(...)`, calls the provider, checks `_is_streamed_response(stream)`, and otherwise yields each chunk unchanged while accumulating text and the latest usage, finalizing in a `finally` with `gen.set_output(...)` and `_record_usage(...)`. Mistral and LiteLLM emit OpenAI-shaped chunks, so reuse the chunk-delta/usage logic (`choices[0].delta.content`, `usage` with `prompt_tokens`/`completion_tokens`/`total_tokens`); factor a shared `_chunk_delta_content` into `_common.py` only if it cleanly serves more than one module. Cohere v2 streams typed events (`content-delta` carrying `delta.message.content.text`, and a terminal `message-end`/`stream-end` carrying `delta.usage` or `response.usage.tokens`); read text and usage from those shapes via `_value`/`_usage_tokens`. Keep `bir_`-prefixed options and metadata identical to the non-streaming path. Do not import provider packages; test with fake chunk/event objects.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- `stream=True` on Mistral, Cohere, and LiteLLM returns a lazy iterable that yields the provider's chunks unchanged in order.
- Accumulated text and final usage are recorded after the stream is consumed; the response model refines the request model when chunks carry one.
- A provider that ignores streaming (returns a one-shot response) still records correctly via the existing non-streaming path.
- Mid-stream exceptions produce an error-status generation and are re-raised unchanged with redacted persisted error text.
- Non-streaming behavior for all three wrappers is unchanged; no provider package is imported.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest), covering the happy
path, edge cases, and any validation/error paths.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add/adjust the relevant section) and add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the
release contract changes.

OUT OF SCOPE
Do not add Bedrock `converse_stream` or Vertex streaming (separate event shapes — a different task), do not add async streaming, and do not change the event schema or import provider packages.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 3. `bir show <trace-id>` single-trace tree inspector

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Add a stdlib-only `bir show <trace-id>` CLI subcommand that prints one recorded trace as an indented event tree, with a `--json` structured form.

WHY
`bir traces` only lists trace summaries; the sole way to inspect a recorded trace's nested spans/generations/tool calls/scores today is to write Python. A local tree view closes a clear DX gap without a server.

IMPLEMENTATION GUIDANCE
Add the subcommand in src/bir/cli.py's `_build_parser` next to `traces`, with a positional `trace_id`, plus `--path` and `--include-rotated` (mirroring `traces`) and `--json`. Implement `_cmd_show`: call `load_traces(args.path, include_rotated=args.include_rotated)`, find the `LoadedTrace` whose `id == trace_id` (exit 1 with a clear stderr message if absent), and render `trace.events` as a tree using each `TraceEvent.parent_id` under the root. For each node print an indented line with type, name, status, duration (reuse `_format_ms`), and the salient extras already on `TraceEvent`: `model`, `usage`, and `value` for scores. Sort siblings by the module's existing event ordering (reuse `_event_sort_key` from `_sdk` if helpful, or `start_time`). For `--json`, emit a nested `{event, children:[...]}` structure via the existing `_dump_json`. Keep all formatting in the small table/format helper style already in cli.py.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- `bir show <id>` prints the trace's events as an indented tree ordered by parent/child, showing type, name, status, duration, and model/usage/score value where present.
- `bir show <id> --json` prints a deterministic nested JSON tree.
- An unknown trace id exits non-zero with a clear message and prints nothing to stdout.
- `--path` and `--include-rotated` resolve the same files as `bir traces`.
- The command adds no runtime dependency and reuses the public loaders.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest), covering the happy
path, edge cases, and any validation/error paths.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add/adjust the relevant section) and add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the
release contract changes.

OUT OF SCOPE
Do not add color/TUI dependencies, paging, editing/deleting traces, server calls, or a web view; do not change existing subcommands.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 4. `bir stats` local usage/cost/latency summary

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Add a stdlib-only `bir stats` CLI subcommand that aggregates local traces into a quick usage/cost/latency summary, with a `--json` form.

WHY
There is no local way to see totals (how many traces, how many tokens, how much cost, how slow) without writing a script. A compact summary makes the local-first workflow useful for cost/health checks without a server.

IMPLEMENTATION GUIDANCE
Add the subcommand in src/bir/cli.py with `--path`, `--include-rotated`, and `--json`. Implement `_cmd_stats`: load via `load_traces(...)` and `load_events(...)`, then aggregate over events — total traces, success/error trace counts, summed `usage` input/output/total tokens across generation events, summed `cost` per `currency` (do not mix currencies; group by currency code), and latency stats over trace `duration_ms` (count, mean, and p95 computed with stdlib only — sort and index, no numpy). Render a small aligned table with the existing `_print_table`/`_format_ms`/`_format_scores`-style helpers; `--json` emits a deterministic object via `_dump_json`. Empty input prints zeros/`-` and exits 0. Keep currency handling explicit so a deployment mixing USD and EUR shows both lines rather than a wrong sum.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- `bir stats` prints trace count, success/error counts, summed input/output/total tokens, summed cost per currency, and latency count/mean/p95.
- `bir stats --json` emits the same figures as deterministic JSON.
- Costs in different currencies are reported separately, never summed together.
- `--path` and `--include-rotated` behave as in `bir traces`; empty input exits 0 with zeroed output.
- No runtime dependency is added; p95 is computed with the standard library only.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest), covering the happy
path, edge cases, and any validation/error paths.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add/adjust the relevant section) and add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the
release contract changes.

OUT OF SCOPE
Do not add charts, time-bucketing, per-model breakdowns beyond totals (unless trivially free), provider price lookups, server calls, or new dependencies.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 5. Public `get_current_trace_id()` / `get_current_span_id()`

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Expose two public accessors, `get_current_trace_id()` and `get_current_span_id()`, that return the active trace and current parent/span ids (or `None`), for correlating application logs with Bir traces.

WHY
The active ids live only in the private `_current_trace_id` / `_current_parent_id` ContextVars, so applications cannot stamp their own logs/metrics with the trace id. A tiny read-only accessor is a high-value, low-risk DX addition.

IMPLEMENTATION GUIDANCE
In src/bir/_sdk.py add `get_current_trace_id() -> str | None` returning `_current_trace_id.get()` and `get_current_span_id() -> str | None` returning `_current_parent_id.get()` (the parent id is the innermost active span/generation/trace node). Add concise docstrings noting they return `None` outside any trace and that the values are the same ids written to the JSONL `trace_id`/`parent_id` fields. Re-export both from src/bir/__init__.py and add them to `__all__`. Do not expose the ContextVars themselves or any setter. Confirm correctness under nested spans (inside `with span(...)` the span id is returned), `@observe()` (sync and async), and concurrent asyncio tasks (each task sees its own ids).

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- `get_current_trace_id()` / `get_current_span_id()` return the active ids inside a trace and `None` outside one, without raising.
- Inside a nested `span()`/`generation()` the span accessor returns the innermost node's id; the trace accessor returns the root id.
- Values match the `trace_id`/`parent_id` later written to the JSONL for events created at that point.
- Concurrent asyncio tasks observe isolated ids.
- Both symbols are importable from `bir` and listed in `__all__`; no setter or ContextVar is exposed.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest), covering the happy
path, edge cases, and any validation/error paths.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add/adjust the relevant section) and add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the
release contract changes.

OUT OF SCOPE
Do not add id setters, context injection/extraction, W3C traceparent propagation, or any cross-process correlation; do not change how ids are generated or stored.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 6. Post-creation `set_metadata()` on event context managers

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Add a `set_metadata(...)` method to the span, generation, tool_call, retrieval, and trace context managers so callers can attach metadata discovered during the body before the event is written.

WHY
Event metadata is currently fixed at context-manager creation, and spans carry none at all, so context discovered mid-body (a resolved route, a cache-hit flag, a request id) cannot be recorded. The event schema already has a free-form `metadata` object on every event, so this is additive with no wire change.

IMPLEMENTATION GUIDANCE
In src/bir/_sdk.py add `set_metadata(self, metadata: Mapping[str, Any]) -> None` (merge/update semantics into an instance dict) to `_Generation`, `_ToolCall` (so `_Retrieval` inherits it), and `_TraceContext`. Give `_Span` a `metadata` attribute and pass it through its `_event(...)` call (spans currently pass no metadata, defaulting to `{}`), then add the same setter. Validate the argument is a `Mapping` (raise `TypeError` otherwise) following the `score()` metadata check. At `__exit__`, the merged metadata must pass through the existing `_safe_capture(...)` redaction exactly like the constructor-supplied metadata, and must compose correctly with generation `prompt` metadata and the retrieval `kind`/trace `service` metadata already injected. Keep merge semantics simple and documented (later keys win). Do not change `score()` (it is not a context manager) unless trivially consistent.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- `with generation(...) as gen: gen.set_metadata({...})` merges into the written generation metadata, redacted, without dropping constructor metadata or the `prompt` block.
- The setter works on span, tool_call, retrieval, and trace context managers (sync and `async with`); spans now persist metadata.
- Non-mapping arguments raise `TypeError`; repeated calls merge with later keys winning.
- Redaction still applies to all merged metadata; no raw secret appears in the JSONL.
- No new public top-level symbol is required; the event schema_version is unchanged.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest), covering the happy
path, edge cases, and any validation/error paths.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add/adjust the relevant section) and add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the
release contract changes.

OUT OF SCOPE
Do not add new event fields or types, do not make metadata mutable after write, and do not change capture defaults or the redaction marker.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 7. `bir validate` trace-JSONL schema validator

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Add a stdlib-only `bir validate [path]` CLI subcommand that validates a trace JSONL file against the schema-1.0 event contract and reports every offending line.

WHY
There is no local way to check whether a trace file is well-formed before reading or sending it, and the schema is a shared contract with the separate bir-app repo. A validator aids debugging and guards that contract.

IMPLEMENTATION GUIDANCE
Add the subcommand in src/bir/cli.py with an optional positional `path` (default to the configured trace path via the existing `_resolved_trace_path`) and `--json`. Implement `_cmd_validate` by reusing the existing strict per-line parser in src/bir/_sdk.py — read the file line by line and run each non-empty line through `_trace_event_from_payload` (or a thin loader that surfaces per-line `ValueError`s) so the rules already enforced by `load_events` (schema_version, required fields, type/status vocab, datetime ordering, JSON-compatibility) are applied. Unlike `load_events`, do not stop at the first bad line: collect `(line_number, message)` for every failure and report them all. Print a human summary (`OK: N events` or a list of `line K: <error>`) and, with `--json`, a deterministic `{valid, event_count, errors:[...]}` object. Exit 0 when valid, 1 when any error is found or the file is missing. If reusing the private parser cleanly requires a tiny internal helper in `_sdk.py`, add one without widening the public API.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- `bir validate` on a well-formed file prints an OK summary and exits 0.
- A file with one or more malformed lines reports each offending line number and reason and exits 1.
- `--json` emits `{valid, event_count, errors}` deterministically.
- A missing file exits non-zero with a clear message.
- Validation rules match the existing strict loader exactly; no runtime dependency is added.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest), covering the happy
path, edge cases, and any validation/error paths.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add/adjust the relevant section) and add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the
release contract changes.

OUT OF SCOPE
Do not introduce a second/divergent schema definition, do not auto-fix or rewrite files, do not validate experiment files (separate format), and do not change the schema or fixtures.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 8. Optional local cost estimation from a user-supplied price table

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Add an opt-in `configure(model_prices=...)` price table that auto-fills a generation's cost from its token usage, shipping no bundled prices.

WHY
Bir deliberately treats cost as user-provided and bundles no price table (provider prices go stale). Many users still want local cost dashboards; letting them supply their own per-token rates yields cost without Bir owning a list of prices.

IMPLEMENTATION GUIDANCE
Extend the frozen `_Config` in src/bir/_sdk.py with an immutable `model_prices` mapping (default empty) and add a `model_prices` keyword to `configure(...)`, validated once into a normalized, hashable structure: a mapping of model name -> rates with non-negative finite `input` and/or `output` per-token rates (and an optional explicit `currency`, default "USD"). Reuse the existing `_validate_non_negative_number` style; reject booleans, negatives, NaN/inf, unknown rate keys, and oversized tables with clear errors. In `_Generation.__exit__`, when `self.cost is None`, `self.usage` is present, and `self.model` matches a configured price, derive `input_cost`/`output_cost`/`total_cost` via the existing `set_cost(...)` path (so the same validation, currency handling, and total derivation apply) before building the event. An explicit `set_cost(...)` by the caller must always win — never overwrite a user-set cost. Add the configured-rates note to `_reset_config_for_tests()` coverage. This is purely local and opt-in: with no `model_prices` set, behavior is byte-for-byte unchanged. Keep the surface minimal (one config field) and document the trade-off that rates are the user's responsibility.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- With no `model_prices` configured, generation cost behavior is unchanged.
- With a price table set, a generation that has usage and a matching model but no explicit cost gets `input_cost`/`output_cost`/`total_cost` filled from the rates, in the configured currency.
- An explicit `gen.set_cost(...)` always takes precedence and is never overwritten.
- Invalid tables (negative/non-finite/boolean rates, unknown keys, non-mapping, oversized) raise clear `ValueError`/`TypeError` at `configure()` time.
- No price data is bundled in the package; the feature is stdlib-only and the event schema_version is unchanged.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest), covering the happy
path, edge cases, and any validation/error paths.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add/adjust the relevant section) and add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the
release contract changes.

OUT OF SCOPE
Do not bundle any provider price list, fetch prices over the network, add tiered/cached pricing, change the cost fields/schema, or make pricing the default.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 9. Bedrock `converse_stream` + Vertex AI streaming parity

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Add synchronous streaming support to the AWS Bedrock and Google Vertex AI wrappers so their streaming APIs record accumulated output text and final token usage.

WHY
The Mistral/Cohere/LiteLLM streaming work (if already done) leaves Bedrock and Vertex without streaming because their stream shapes differ from the OpenAI-style chunk. Completing them gives uniform streaming coverage across all provider wrappers.

IMPLEMENTATION GUIDANCE
For Bedrock (src/bir/integrations/bedrock.py): add a `trace_converse_stream` wrapper (and re-export it) for `bedrock-runtime`'s `converse_stream`. The Converse stream returns a response whose `"stream"` is an iterable of typed events — accumulate text from `contentBlockDelta.delta.text`, capture the stop on `messageStop`, and read usage from the terminal `metadata.usage` (`inputTokens`/`outputTokens`/`totalTokens`). Yield events unchanged and finalize in a `finally`. Keep the request `modelId` as the model. For Vertex (src/bir/integrations/vertexai.py): support `stream=True` on `trace_generate_content` (Vertex returns an iterator of response chunks) — accumulate `chunk.text`/candidate text and read final `usage_metadata` (`prompt_token_count`/`candidates_token_count`/`total_token_count`), preferring a chunk `model_version` for the model over `bir_model`. Reuse `_value`/`_usage_tokens`/`_string_or_none`/`_is_streamed_response`; fall back to one-shot recording when the call did not actually stream. Do not import boto3 or vertexai; use fake stream objects in tests.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- `trace_converse_stream(...)` yields the Converse stream events unchanged and records accumulated text plus `inputTokens`/`outputTokens`/`totalTokens` from the terminal metadata event.
- Vertex `trace_generate_content(..., stream=True)` yields chunks unchanged and records accumulated text plus `usage_metadata` token counts, refining the model from a chunk `model_version` when present.
- A non-streaming response on either path still records via the existing one-shot logic.
- Mid-stream errors yield an error-status generation and re-raise unchanged with redacted persisted error text.
- Neither provider SDK is imported, and non-streaming behavior is unchanged.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest), covering the happy
path, edge cases, and any validation/error paths.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add/adjust the relevant section) and add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the
release contract changes.

OUT OF SCOPE
Do not add async streaming, do not implement Bedrock `invoke_model_with_response_stream` (raw model stream — a different API), and do not change the event schema or import provider packages.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 10. OpenAI Agents SDK tracing-processor integration

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Add a dependency-free OpenAI Agents SDK integration that maps Agents SDK traces/spans into Bir trace/generation/tool_call events via the SDK's tracing-processor hook.

WHY
The OpenAI Agents SDK is a widely used agent framework with a documented tracing-processor interface (analogous to the LangChain callback surface Bir already supports). Bridging it lets agent runs appear as Bir traces without importing the Agents SDK.

IMPLEMENTATION GUIDANCE
First verify the current public tracing-processor interface of the OpenAI Agents SDK (the method names and span/trace objects it passes); implement against that, adapting if the surface has shifted. Model the implementation on src/bir/integrations/langchain.py: a `BirAgentsTracingProcessor` class with the processor lifecycle methods (e.g. on trace start/end and span start/end) that opens a Bir `_trace_context` for an agent run root and maps spans by kind — model/LLM spans to `generation(...)`, tool/function spans to `tool_call(...)`, and others to `span(...)` — tracking active runs by span id in a dict exactly as the LangChain handler tracks `run_id`. Read model and token usage from the span's data via mapping-or-attribute access and the `_common.py`-style helpers; honor `capture_inputs`/`capture_outputs` overrides. Put the class in a new module (e.g. src/bir/integrations/openai_agents.py) and re-export it from src/bir/integrations/__init__.py. Never import the `agents`/`openai-agents` package; tests pass fake trace/span objects matching the interface you verified.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- An agent run processed by the handler produces a Bir trace whose nested events map model spans to generations (with model/usage when present) and tool spans to tool calls.
- Span start/end and error paths open and close the matching Bir context managers, preserving redaction and capture-opt-in behavior.
- Concurrent or nested spans are tracked by id without leaking context between runs.
- Importing the module and constructing the processor succeed with no Agents SDK installed.
- The integration adds no runtime dependency and follows the existing callback-handler idioms.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest), covering the happy
path, edge cases, and any validation/error paths.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add/adjust the relevant section) and add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the
release contract changes.

OUT OF SCOPE
Do not import or require the Agents SDK, do not implement other frameworks (Pydantic AI, DSPy, CrewAI, Haystack) in this change, and do not change the event schema. If verification shows the tracing-processor interface is not yet stable enough to target safely, stop and report that finding instead of guessing.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 11. OpenTelemetry/OTLP export behind an optional `[otel]` extra

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Add an optional OpenTelemetry/OTLP exporter, behind a new `[otel]` extra, that converts loaded Bir traces into OpenTelemetry spans and ships them to an OTLP endpoint.

WHY
Teams that already run an observability backend want Bir's local traces forwarded there. An opt-in OTLP bridge provides that without compromising the zero-runtime-dependency, local-first defaults.

IMPLEMENTATION GUIDANCE
Add a new module (e.g. src/bir/integrations/otel.py) exposing a small function/class such as `export_traces_to_otlp(traces, *, endpoint=..., service_name=...)` that lazily imports `opentelemetry` (sdk + OTLP exporter) inside the function body and raises a clear, actionable error if the extra is not installed — mirroring how provider wrappers avoid hard imports. Map each `LoadedTrace`/`TraceEvent` to an OTel span: the trace root to a root span and child events to child spans using `parent_id`; set start/end from the ISO timestamps; set status from `success`/`error`; and attach attributes using GenAI semantic conventions where they exist (e.g. `gen_ai.request.model`, `gen_ai.usage.input_tokens`/`output_tokens`) plus `bir.*` attributes for the rest (event type, score value). Accept already-loaded traces (and/or a path that it loads via `load_traces`). Add `[project.optional-dependencies] otel = [...]` to pyproject.toml; keep `dependencies = []`. Guard the test so it skips cleanly when opentelemetry is absent, or use a minimal fake tracer/exporter to assert the mapping without a real backend. Document that this is opt-in and never imported by default.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- A new `[otel]` optional extra is declared; runtime `dependencies` stays empty and importing `bir` never imports opentelemetry.
- The exporter converts a Bir trace into a parent/child OTel span tree with correct timestamps, status, and model/usage/score attributes (GenAI semantic conventions where applicable).
- Calling the exporter without the extra installed raises a clear, actionable error.
- The exporter accepts loaded traces (and optionally a trace path) and does not alter the local JSONL.
- Tests assert the span mapping without requiring a live OTLP backend and skip cleanly when opentelemetry is unavailable; `verify_release.py` still passes with no provider/otel packages installed.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest), covering the happy
path, edge cases, and any validation/error paths.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add/adjust the relevant section) and add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the
release contract changes.

OUT OF SCOPE
Do not make OpenTelemetry a runtime dependency, do not auto-export on every write, do not add live span context propagation into the core SDK, and do not change the Bir event schema or fixtures.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```
