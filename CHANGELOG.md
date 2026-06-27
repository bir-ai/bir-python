# Changelog

All notable changes to the Bir Python SDK are documented here.

This project follows a small-release workflow while the SDK is early-stage.
Before publishing, verify the release with the SDK release checklist in
`docs/SDK_RELEASE_CHECKLIST.md`.

## Unreleased

### Added

- `bir.integrations.trace_converse_async` (AWS Bedrock) and
  `bir.integrations.trace_vertex_generate_content_async` (Google Vertex AI), the
  asynchronous counterparts of `trace_converse` and the Vertex
  `trace_generate_content`. Each awaits an async provider coroutine (for example an
  `aioboto3` `bedrock-runtime` `converse`, or a
  `GenerativeModel.generate_content_async`) inside one Bir `generation`, forwards
  arguments unchanged, strips the `bir_`-prefixed options, and returns the awaited
  provider result — recording the same model and
  `inputTokens`/`outputTokens`/`totalTokens` (Bedrock) or `usage_metadata` (Vertex)
  as the sync wrappers, with neither `boto3` nor `vertexai` imported. This completes
  async coverage across every dependency-free provider. Non-streaming only for now:
  the Converse stream (`trace_converse_stream`) and the Vertex `stream=True` surface
  stay synchronous. New public exports — no dependency or schema change, and the sync
  wrappers are byte-for-byte unchanged.
- `set_model(model)` setter on the `generation()` context manager, parallel to
  `set_output`/`set_usage`/`set_cost`/`set_metadata`. The model is read when the
  generation exits, so this records or refines a model only known after the
  provider responds (a streaming refinement, a router-chosen model) without
  passing it to `generation(model=...)` up front. The latest call wins; a
  non-empty string is validated like an event name, and `None` is accepted and
  records no model (clearing any constructor value). Additive method on an
  existing context manager — no new export and no schema change; the dependency-free
  provider integrations now use it in place of writing `gen.model` directly.
- `metadata=` keyword on `@observe()`, recording a static mapping on the trace
  ROOT event the decorated call produces. It is the decorator-side counterpart to
  `trace(metadata=...)` for tagging an entry point (route, tenant, feature flag)
  without rewriting it as a manual `with trace(...)` block. The mapping is redacted
  with the same rules as captured input/output, is attached only when the call
  opens a new trace root (a nested `@observe()` records a span and never carries
  it), and composes with the generator `metadata.generator.*` outcome for observed
  generators. Additive keyword on an existing symbol — no new export, no schema
  change; `@observe()` calls without `metadata` are byte-for-byte unchanged.
- `Typing :: Typed` trove classifier in `pyproject.toml`, so PyPI and tooling
  advertise that the distribution ships inline type annotations. The SDK already
  ships the PEP 561 `py.typed` marker; this is the standard metadata signal that
  pairs with it. Metadata only — no code, API, dependency, schema, or fixture
  change.
- `bir.integrations.crewai.BirCrewAIHandler`, a dependency-free bridge that records
  [CrewAI](https://www.crewai.com/) crew runs as Bir traces. CrewAI's lowest-coupling
  observability seam is its event bus (`crewai.utilities.events.crewai_event_bus`),
  which emits typed start/completed/failed events for crews, tasks, agent executions,
  LLM calls, and tool usage; forward each `(source, event)` the bus emits to
  `handler.on_event` and each crew run becomes a Bir trace. Events are read by duck
  typing — tolerant of field changes across CrewAI versions — and classified by their
  `event.type`: a crew-kickoff event opens a Bir trace root, task and agent-execution
  events become structural spans, LLM-call events become generations carrying the
  model and token usage, and tool-usage events become tool-call events; a
  `*_failed`/`*_error` event closes its node with error status. Crew, task, and agent
  nodes are tracked by their framework id so concurrent and nested runs stay isolated,
  while LLM-call and tool-usage events (which CrewAI emits without a correlation id)
  are paired by a per-thread last-in-first-out stack. Input/output capture follows the
  same opt-in settings as every other integration (overridable per handler with
  `capture_inputs`/`capture_outputs`), and `crewai` is never imported.

- `bir.integrations.dspy`: new dependency-free DSPy integration. `trace_lm` and
  `trace_lm_async` wrap a `dspy.LM` instance's request method
  (`lm.forward`/`lm.aforward`), which returns the LiteLLM-style response, and
  record one generation with model and token usage. The request model is read
  from the bound `LM` instance (`lm.model`) or an explicit `model` keyword and
  refined from the response's `model` when present; `dspy` is never imported.

- `bir.integrations.pydantic_ai.BirPydanticAIHandler`, a dependency-free bridge
  that records [Pydantic AI](https://ai.pydantic.dev/) agent runs as Bir traces.
  Pydantic AI's lowest-coupling observability seam is its OpenTelemetry
  instrumentation (`Agent(instrument=True)`), so the handler implements the OTel
  `SpanProcessor` interface (`on_start`/`on_end`/`shutdown`/`force_flush`) and is
  registered on the tracer provider Pydantic AI uses. Spans are read by duck
  typing — tolerant of attribute-key changes across instrumentation versions — and
  classified by `gen_ai.operation.name` (falling back to the span name): an
  agent-run span opens a Bir trace root, a `chat` span becomes a generation
  carrying the model and token usage, and an `execute_tool` span becomes a
  tool-call event; every other span becomes a Bir span. Failures (OTel `ERROR`
  status or a recorded `exception` event) are recorded with error status, active
  runs are tracked by OpenTelemetry span id so concurrent and nested runs stay
  isolated, and input/output capture follows the same opt-in settings as every
  other integration (overridable per handler with `capture_inputs`/`capture_outputs`).
  Neither `pydantic_ai` nor `opentelemetry` is imported.

- `bir.integrations.instructor`: new dependency-free Instructor integration.
  `trace_create` and `trace_create_async` wrap an Instructor-patched client's
  `chat.completions.create` callable and record one generation with model and
  token usage. Both the direct parsed-model return shape and the
  `(parsed_model, raw_completion)` tuple from `create_with_completion` are
  handled automatically; `instructor` is never imported.

- `run_experiment()` now accepts an opt-in `max_workers` keyword argument (positive
  integer, default `1`). When `max_workers > 1`, examples run concurrently inside a
  `concurrent.futures.ThreadPoolExecutor`, giving a large speedup for I/O-bound
  synchronous tasks such as network LLM calls behind a sync client. Results, JSONL
  rows, and summary aggregates are always written in dataset order regardless of
  completion order. All existing semantics — `raise_on_error`, `record_traces` trace
  isolation, redaction, and the on-disk schema — are unchanged. Requires no new
  dependencies (stdlib only). The default `max_workers=1` is byte-for-byte identical
  to the previous behavior.

- `bir.testing.capture_traces()` context manager (and its `CapturedTraces` handle)
  for asserting on your own instrumentation in tests. It redirects trace writes to
  a private temporary file for the duration of a `with` block and reads the
  captured events/traces back in memory through the public `load_events` /
  `load_traces` loaders, then restores the previous configuration (including a
  user-set `trace_path`) on exit — even if the body raises — and removes the temp
  file. Only *where* events are written changes: capture opt-in, sampling, and
  redaction are untouched, so a captured event matches a real write. Scoped to the
  `bir.testing` submodule to keep the top-level API small; stdlib `tempfile` only,
  with no new dependency, schema, or fixture change.
- GitHub Pages deploy workflow (`.github/workflows/docs-deploy.yml`) that
  rebuilds the MkDocs site behind the same `mkdocs build --strict` gate and
  publishes it to <https://bir-ai.github.io/bir-python/> on every push to `main`
  (and on manual `workflow_dispatch`). The deploy job depends on the strict
  build, so a docs change that fails `--strict` is never published. The existing
  PR-time strict-build gate in `ci.yml` is unchanged, and the default `mkdocs`
  theme and `docs` extra are kept (no code, dependency, schema, or fixture
  change).
- `bir export-otel` CLI subcommand that replays local traces to an OTLP endpoint
  through the existing `bir.integrations.otel.export_traces_to_otlp` exporter. It
  reads the same files as `bir traces` (`--path`, `--include-rotated`), requires
  `--endpoint`, accepts a repeatable `--header KEY=VALUE` for backend auth plus
  `--service-name` and `--timeout` passthrough, and prints how many traces and
  spans were exported. The exporter is imported lazily, so the CLI keeps working
  without the optional `otel` extra; running the command without it exits
  non-zero with the `pip install 'bir-sdk[otel]'` install hint. Stdlib only; the
  opentelemetry packages stay in the `otel` extra, with no schema or fixture
  change.
- Async `stream=True` support for the async Mistral and Cohere `trace_chat_async`
  wrappers and the async LiteLLM `trace_completion_async` wrapper. They now resolve
  to a lazy async iterator that yields the provider's stream events unchanged via
  `async for` and finalizes the model, accumulated output, and final token usage
  when the stream is exhausted, `aclose()`d, or raises mid-stream — completing
  async streaming coverage alongside OpenAI, Anthropic, and Gemini. A provider that
  ignores streaming and returns a one-shot response still records via the
  non-streaming path. Stdlib only; no new dependency, schema, or fixture change.
- `python -m bir` module entry point that dispatches to the same
  `bir.cli:main` as the `bir` console script, for invoking the CLI when the
  console script isn't on `PATH` (fresh venvs, `pipx run`, CI). Both paths share
  one implementation, so behavior and exit codes are identical. Stdlib only; no
  new dependency, schema, or fixture change.

### Fixed

- Release verification wheel metadata now preserves `pyproject.toml` optional
  extras (`dev`, `docs`, and `otel`) as extra-scoped `Requires-Dist` entries
  while keeping the base install free of unconditional runtime dependencies.

### Security

- Expanded best-effort capture redaction to recognize Stripe secret/restricted
  keys (`sk_live_`/`sk_test_`/`rk_live_`/`rk_test_`), Azure storage-style account
  keys (88-character base64 ending in `==`), and PEM private-key blocks
  (`-----BEGIN ... PRIVATE KEY-----` ... `-----END ... PRIVATE KEY-----`). These
  are additive built-in rules: no existing rule is weakened and the new patterns
  are anchored to avoid over-redacting benign text. **CROSS-REPO CONTRACT:
  bir-app's independently maintained redactor and its copy of
  `redaction-cases.json` must be updated to match before or with this change. Do
  not release this SDK change while the bir-app redactor or fixture is out of
  sync.**

## 0.2.0 - 2026-06-24

### Security

- Expanded best-effort capture redaction to recognize JWTs, AWS access key IDs,
  Google API keys, Slack tokens, and GitHub provider tokens. **CROSS-REPO CONTRACT:
  bir-app's independently maintained redactor and its copy of
  `redaction-cases.json` must be updated to match before or with this change. Do
  not release this SDK change while the bir-app redactor or fixture is out of
  sync.**

### Added

- `configure(source=...)` and the `BIR_SOURCE` environment variable, which tag
  trace roots with `metadata.source`. This is the SDK-side counterpart to the
  `source` field the Bir server and dashboard already filter on (the product's
  Playground records `"playground"`), so SDK-generated traces become filterable
  by origin alongside product-generated ones. The value must be a non-empty
  string, is recorded only on trace roots, and an explicit `source` in a
  `trace(metadata=...)` block still wins. No event-schema or fixture change
  (`metadata.source` is already part of the `1.0` event metadata the server
  reads). Stdlib only.
- `bir.integrations.otel.export_traces_to_otlp`, an opt-in OpenTelemetry/OTLP
  exporter for replaying locally recorded Bir traces to an existing
  observability backend. Install it with the new optional `otel` extra
  (`pip install 'bir-sdk[otel]'`); normal runtime installs stay dependency-free,
  and OpenTelemetry packages are imported lazily only when the exporter is
  called. The exporter accepts a `LoadedTrace`, an iterable of loaded traces, or a
  trace-file path loaded through `load_traces`, then maps each Bir trace to one
  OpenTelemetry trace with parent/child span relationships, original
  timestamps, success/error status, GenAI semantic-convention attributes for
  model and token usage, and `bir.*` attributes for event ids, event type,
  scores, total tokens, cost, and currency. It can build the default OTLP/HTTP
  exporter from `endpoint`, `headers`, and `timeout`, or use an injected
  `span_exporter` for custom transports and tests. The integration is re-exported
  from `bir.integrations`, documented in the README, included in release package
  verification, and covered by tests for dependency isolation, span-tree shape,
  attribute mapping, exporter wiring, and error handling. No schema or fixture
  change.
- `bir.integrations.openai_agents.BirAgentsTracingProcessor`, a dependency-free
  bridge that implements the OpenAI Agents SDK tracing-processor interface
  (`on_trace_start`/`on_trace_end`, `on_span_start`/`on_span_end`, `shutdown`,
  `force_flush`). Register it with `agents.add_trace_processor(...)` and each agent
  run's trace becomes a Bir trace root; spans are mapped by their `span_data.type`
  — model spans (`generation`, `response`) to generations with model and token
  usage when present, tool spans (`function`, `mcp_tools`) to tool calls, and every
  other kind (`agent`, `handoff`, `guardrail`, `custom`, ...) to a span — with
  failed spans recorded as errors. Active traces and spans are tracked by their
  Agents id so concurrent and nested runs stay isolated, and input/output capture
  follows the same opt-in settings as the other integrations, overridable per
  processor with `capture_inputs`/`capture_outputs`. The processor never imports the
  `openai-agents` package, so it adds no runtime dependency, and it introduces no
  schema or fixture change. Re-exported from `bir.integrations`.
- `configure(model_prices=...)`, an opt-in, local-only price table that fills a
  generation's `input_cost`/`output_cost`/`total_cost` from its token usage. Each
  entry maps a model name to a non-negative, finite `input` and/or `output`
  per-token rate plus an optional `currency` (default `USD`); Bir bundles no
  prices, so the rates are the user's responsibility. Cost is derived only for a
  generation that has usage, a matching model, and no explicit `set_cost(...)`
  (which always wins and is never overwritten), routing through the same cost
  validation, currency handling, and total derivation as a manual `set_cost`. The
  table is validated once at `configure()` time — a non-mapping table, a
  non-string or empty model name, a non-mapping or empty rate entry, an unknown
  rate key, a boolean/negative/non-finite rate, an invalid currency, or an
  over-large table raises `ValueError`/`TypeError` immediately. Stdlib-only and
  fully opt-in: with no table configured, generation cost behavior is byte-for-byte
  unchanged. No new public top-level symbol, runtime dependency, schema, or fixture
  change.
- `set_metadata(...)` method on the `trace()`, `span()`, `generation()`,
  `tool_call()`, and `retrieval()` context managers, so metadata discovered while
  the body runs (a resolved route, a cache-hit flag, a request id) can be recorded
  before the event is written. It merges into any metadata supplied at creation
  with a plain update — later keys win, including across repeated calls — and the
  merged metadata is redacted at `__exit__` with the same rules as constructor
  metadata, composing with the generation `prompt` block, the retrieval `kind`,
  and the trace `service` metadata already injected. Spans, which previously
  carried no metadata, now persist it. Works with both `with` and `async with`,
  and the argument must be a mapping (a `TypeError` is raised otherwise). No new
  public top-level symbol, runtime dependency, schema, or fixture change.
- `get_current_trace_id()` and `get_current_span_id()` public accessors that
  return the active trace id and innermost open span/generation/tool-call id (or
  `None` outside any trace), for stamping application logs and metrics so they can
  be correlated with Bir traces. The values are exactly the `trace_id`/`parent_id`
  written to the JSONL for an event created at that point, and are read from the
  task-local context, so concurrent asyncio tasks and threads each see their own
  ids. They are read-only — no setter or context object is exposed, and no
  cross-process propagation is added. No new dependency, schema, or fixture change.
- `bir stats` command that aggregates local traces into a quick usage, cost, and
  health summary: the total trace count with success/error splits, summed
  input/output/total token usage over generation events, summed cost grouped by
  currency (different currencies are reported separately and never summed), and
  trace latency count, mean, and p95. The p95 is the nearest-rank 95th percentile
  computed with the standard library only. `--json` emits the same figures as a
  deterministic object, `--path`/`--include-rotated` resolve the same files as
  `bir traces`, and an empty store exits 0 with zeroed counts. It reuses the
  public `load_traces`/`load_events` loaders and adds no runtime dependency or
  schema change.
- `bir show <trace-id>` command that prints one recorded trace as an indented
  event tree ordered by parent/child, showing each event's type, name, status,
  and duration plus the model and token usage on generations and the value on
  scores. `--json` emits a deterministic nested `{"event", "children"}` tree for
  scripts, and `--path`/`--include-rotated` resolve the same files as
  `bir traces`. An unknown trace id exits non-zero and prints nothing to stdout.
  It reuses the public `load_traces` loader and adds no runtime dependency, schema
  change, or fixture change.
- Synchronous streaming for the Mistral, Cohere, and LiteLLM wrappers. Passing
  `stream=True` to `trace_chat` (`bir.integrations.mistral`),
  `bir.integrations.cohere.trace_chat`, or `trace_completion`
  (`bir.integrations.litellm`) now returns a lazy iterable that yields the
  provider's chunks unchanged in order and records the accumulated output text and
  final token usage once the stream is consumed, instead of recording the
  unconsumed stream object's repr with no usage. Mistral and LiteLLM read
  OpenAI-shaped chunks (`choices[0].delta.content` and `usage` with
  `prompt_tokens`/`completion_tokens`/`total_tokens`); Cohere v2 reads its typed
  events (`content-delta` text at `delta.message.content.text` and the terminal
  `message-end`/`stream-end` usage). The response model refines the request model
  when chunks carry one. A provider that ignores streaming and returns a one-shot
  response still records via the non-streaming path, and a mid-stream error
  produces an error-status generation re-raised unchanged with the persisted error
  text redacted. This matches the existing OpenAI/Anthropic/Gemini sync streaming
  behavior; the async Mistral/Cohere/LiteLLM wrappers do not stream yet. No new
  dependency, schema, or fixture change.
- Synchronous streaming for the AWS Bedrock and Google Vertex AI wrappers,
  completing sync streaming coverage across the provider wrappers. A new
  `trace_converse_stream` (`bir.integrations.bedrock`, re-exported from
  `bir.integrations`) wraps a `bedrock-runtime` `converse_stream` call: it returns
  a lazy iterable that yields the Converse stream's events (the items of the
  response `stream` member) unchanged and records the accumulated
  `contentBlockDelta.delta.text`, the `messageStop` stop reason (as
  `metadata.stop_reason`), and the terminal `metadata` event's
  `inputTokens`/`outputTokens`/`totalTokens`, keeping the request `modelId` as the
  model. Passing `stream=True` to `trace_generate_content`
  (`bir.integrations.vertexai`) yields Vertex's `GenerationResponse` chunks
  unchanged and records the accumulated text (each chunk's `text`, falling back to
  the first candidate's text parts), refining the model from a chunk
  `model_version` and reading the final `usage_metadata`
  (`prompt_token_count`/`candidates_token_count`/`total_token_count`). A call that
  did not actually stream still records via the one-shot path, and a mid-stream
  error produces an error-status generation re-raised unchanged with the persisted
  error text redacted. Neither provider SDK is imported, and the async wrappers
  and non-streaming behavior are unchanged. No new dependency, schema, or fixture
  change.
- Async counterparts for the dependency-free provider wrappers, for applications
  using async provider clients (`AsyncOpenAI`, `AsyncAnthropic`, the `google-genai`
  async client, `litellm.acompletion`, and the async Mistral and Cohere clients).
  Each is named with an `_async` suffix and mirrors its sync wrapper exactly but
  awaits the provider coroutine inside one Bir generation:
  `trace_chat_completion_async` and `trace_response_async`
  (`bir.integrations.openai`), `trace_messages_async`
  (`bir.integrations.anthropic`), `trace_generate_content_async`
  (`bir.integrations.google`), `trace_chat_async` (`bir.integrations.mistral` and
  `bir.integrations.cohere`), and `trace_completion_async`
  (`bir.integrations.litellm`). Arguments are forwarded unchanged, the awaited
  provider result is returned unchanged, and `bir_*` options are never forwarded.
  For the surfaces that stream synchronously today (OpenAI Chat Completions and
  Responses, Anthropic, Gemini), passing `stream=True` resolves to an async
  iterator that yields the provider's async-stream events unchanged via
  `async for` and finalizes the model, output, and usage when the stream is
  exhausted, closed (`aclose()`), or raises mid-stream (re-raised unchanged, the
  persisted error redacted) — never buffering the stream. The wrappers require an
  active trace and work under async `@observe()` functions and
  `async with bir.trace(...)`. Re-exported from `bir.integrations`; sync wrappers
  are unchanged. No new dependency, schema, or fixture change. AWS Bedrock,
  Vertex AI, and the LangChain/LlamaIndex callback handlers are unchanged.
- `configure()` now accepts two additive redaction options,
  `additional_secret_keys` and `additional_redaction_patterns`, so applications
  can teach Bir their own credential field names and secret text formats. They
  only ever widen redaction: the built-in rules and the `[redacted]` marker can
  never be disabled, replaced, or reordered, and there is no switch to turn
  defaults off. `additional_secret_keys` is an iterable of extra mapping-key
  names matched by whole-name, case-insensitive equality (treating `-` and `_`
  as equivalent), distinct from the built-in substring rules.
  `additional_redaction_patterns` is an iterable of regex strings and/or
  compiled `re.Pattern` objects whose every match is replaced with `[redacted]`,
  running after every built-in text pattern. Both are validated and compiled once
  during `configure()` (empty keys/patterns, invalid regexes, non-string entries,
  bytes patterns, and over-large lists raise `ValueError`/`TypeError`
  immediately), and the rules flow through every existing capture and persistence
  path (captured inputs/outputs, repr fallbacks, error text, prompt and score
  metadata, integration inputs/outputs, and dataset/experiment files). Passing
  either argument replaces the previously configured additional rules of that
  kind (an empty iterable clears them); omitting it leaves them unchanged. Stdlib
  only (`re`); no schema, fixture, or dependency change.
- `@observe()` now traces generator and async-generator functions across their
  whole iteration lifetime instead of closing the trace when the generator object
  is created. The wrapper stays lazy (no body runs and nothing is written until
  the first iteration), the root trace spans the first `next`/`asend` through
  exhaustion so child spans and generations created in the body attach to it, and
  it is finalized on exhaustion (success), on an exception from the body (recorded
  as a redacted error and re-raised unchanged), or on an early
  `close`/`aclose`/cancellation (recorded as a success whose
  `metadata.generator.outcome` is `"closed"`). `send`/`throw`/`close`,
  `asend`/`athrow`/`aclose`, and the body's `finally` blocks are all preserved,
  contextvars never leak between iterations or into later work, and concurrent
  async generators in separate tasks stay isolated. Output capture stays opt-in
  and records only a bounded yielded-item count under `metadata.generator.items`
  rather than buffering the stream. Existing sync-function and coroutine behavior
  is unchanged. Stdlib only (`inspect`, `contextvars`).
- `trace_response()` in `bir.integrations.openai`, a dependency-free wrapper for
  OpenAI's Responses API (`client.responses.create`). It forwards arguments
  unchanged, returns the provider response object, and records one generation with
  the model, aggregated `output_text` (falling back to the full response shape),
  and `input_tokens`/`output_tokens`/`total_tokens` usage. With `stream=True` it
  returns a lazy iterable that yields the provider's events unchanged, assembles
  output only from `response.output_text.delta` events, and finalizes the model and
  usage from the terminal `response.completed` event on exhaustion, close, or error.
  Re-exported from `bir.integrations`. Chat Completions support
  (`trace_chat_completion`) is unchanged.
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
