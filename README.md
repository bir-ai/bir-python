# Bir Python SDK

Minimal, zero-runtime-dependency, local-first tracing and evals for Python LLM
applications.

Bir records traces, spans, generations, tool calls, retrievals, and scores to
local JSONL without requiring a server. Start locally, evaluate deterministic
regressions, and send events to a Bir server when you want to inspect them in a
dashboard.

## Installation

```bash
python -m pip install bir-sdk
```

The distribution name is `bir-sdk`; the import name is `bir`. Runtime
installation has no third-party dependencies. Bir also ships inline type
annotations and a PEP 561 `py.typed` marker.

An opt-in `otel` extra (`pip install 'bir-sdk[otel]'`) adds an OpenTelemetry/OTLP
exporter that forwards recorded traces to an existing observability backend; the
runtime install stays dependency-free without it. See
[Forwarding traces to OpenTelemetry](#forwarding-traces-to-opentelemetry).

## Quickstart

```python
from bir import generation, observe, score


@observe()
def answer_question(question: str) -> str:
    with generation("local.llm", model="demo-model") as gen:
        response = f"Answer: {question}"
        gen.set_output(response)
        gen.set_usage(input_tokens=12, output_tokens=24)
    score("helpfulness", 0.82)
    return response
```

Events are written to `.bir/traces.jsonl` by default. Input and output capture
is disabled unless you explicitly enable it.

Inspect them from the command line without a server: `bir traces` lists recorded
traces, `bir show <trace-id>` prints one trace as an indented tree of its spans,
generations, tool calls, and scores, and `bir stats` summarizes trace counts,
token usage, cost per currency, and latency (count, mean, and p95) for a quick
cost or health check (add `--json` to any of them for a structured form). The
same commands run as `python -m bir <command>` when the `bir` console script
isn't on `PATH` (fresh venvs, `pipx run`, CI). See
[CLI & environment](docs/site/cli-env.md).

When capture is on, Bir redacts common secret-like fields and text before
anything is written. Those built-in rules always apply and cannot be turned off,
but you can widen them for your own credential names and formats with
`configure(additional_secret_keys=[...], additional_redaction_patterns=[...])`.
See [capture and privacy](docs/site/capture-privacy.md).

## Correlating your logs with traces

The easy path is the `bir.logging` filter. Attach `BirTraceIdFilter` once (the
`install_trace_id_filter()` helper adds it to the root logger) and every log record
gains `bir_trace_id` / `bir_span_id` attributes that any formatter can render — no
per-call plumbing:

```python
import logging

from bir import observe
from bir.logging import install_trace_id_filter

install_trace_id_filter()
logging.basicConfig(
    format="%(asctime)s %(levelname)s [trace=%(bir_trace_id)s span=%(bir_span_id)s] %(message)s"
)


@observe()
def answer(question: str) -> str:
    logging.info("handling question")  # the ids are stamped automatically
    return "ok"
```

Inside a trace the stamped values equal `get_current_trace_id()` /
`get_current_span_id()`; outside any trace they are `None` and nothing raises. The
filter only annotates records — it never drops them — and reads from the same
task-local context as the accessors, so each asyncio task and thread sees its own
ids. Pass a specific logger or handler to `install_trace_id_filter(target)` to scope
it; attaching to a handler is the surest way to stamp every record it emits,
including ones propagated from child loggers.

If you prefer to stamp ids by hand, read them directly with
`get_current_trace_id()` and `get_current_span_id()` and pass them through `extra=`:

```python
import logging

from bir import get_current_span_id, get_current_trace_id, observe


@observe()
def answer(question: str) -> str:
    logging.info(
        "handling question",
        extra={"trace_id": get_current_trace_id(), "span_id": get_current_span_id()},
    )
    return "ok"
```

`get_current_trace_id()` returns the active trace root id and
`get_current_span_id()` the innermost open span, generation, or tool call (the
trace root when none is open). The values match the `trace_id`/`parent_id` later
written to the JSONL, and each asyncio task and thread sees its own ids. The
accessors are read-only — there is no setter and no context is exposed for
injection or cross-process propagation.

## Attaching metadata discovered mid-body

Every trace-work context manager — `trace()`, `span()`, `generation()`,
`tool_call()`, and `retrieval()` — exposes `set_metadata(...)` so you can record
context that only becomes known while the body runs (a resolved route, a
cache-hit flag, a request id) before the event is written:

```python
from bir import generation, observe


@observe()
def answer(question: str) -> str:
    with generation("local.llm", model="demo-model", metadata={"provider": "demo"}) as gen:
        response = f"Answer: {question}"
        gen.set_metadata({"route": "fast", "cache_hit": False})
        gen.set_output(response)
    return response
```

`set_metadata()` merges into any metadata passed at creation time — later keys
win, including across repeated calls — and the merged metadata is redacted before
it is written with the same rules as captured input and output, so secret-like
fields never reach the JSONL. It works with both `with` and `async with`, and the
argument must be a mapping.

`generation()` additionally exposes `set_model(...)` for the common case where
the model is only known after the provider responds (a streaming refinement, a
router-chosen model). The model is read when the generation exits, so a later
`set_model()` wins over an earlier one or over `generation(model=...)`. A
non-empty string is validated like an event name, and `None` records no model
(clearing any constructor value):

```python
with generation("router.chat") as gen:
    response = call_router(question)
    gen.set_model(response.model)
    gen.set_output(response.text)
```

To tag a traced entry point with static metadata without rewriting it as a manual
`with trace(...)` block, pass `metadata=` to `@observe()`. It is the
decorator-side counterpart to `trace(metadata=...)` and is recorded, redacted, on
the trace root the call produces:

```python
from bir import observe


@observe(metadata={"route": "/checkout", "tenant": "acme"})
def checkout() -> str:
    return "done"
```

The metadata is attached only when the decorated call opens a new trace root; a
nested `@observe()` call records a span and never carries this trace-level
metadata. For observed generators it composes with the recorded
`metadata.generator.*` outcome. The argument must be a mapping, and secret-like
keys and values are redacted before they reach the JSONL.

## Estimating cost from a local price table

Cost is user-provided by default — Bir bundles no prices because provider prices
go stale. If you would rather not call `set_cost()` on every call, supply your own
per-token rates once with `configure(model_prices=...)` and Bir fills the cost
from token usage:

```python
from bir import configure, generation, observe

configure(
    model_prices={
        "gpt-4o-mini": {"input": 0.00000015, "output": 0.0000006},
        "mistral-large": {"input": 0.000002, "output": 0.000006, "currency": "EUR"},
    }
)


@observe()
def answer(question: str) -> str:
    with generation("chat", model="gpt-4o-mini") as gen:
        gen.set_usage(input_tokens=1000, output_tokens=400)
        # No set_cost(): input_cost/output_cost/total_cost are derived from the rates.
    return "ok"
```

Each entry sets a non-negative `input` and/or `output` per-token rate plus an
optional `currency` (default `USD`). The cost is derived only for a generation
that has the matching token counts and no explicit `set_cost()` — an explicit cost
always wins, and a usage without the needed token split is left without a cost.
The table is validated at `configure()` time, ships no bundled prices, and keeping
the rates current is your responsibility. With no table configured, cost behavior
is unchanged. See [core API](docs/site/core-api.md).

## Tracing generators and streaming

`@observe()` also traces generator and async-generator functions across their
full iteration, not just their creation. The wrapper stays lazy — the body does
not run and nothing is written until the first iteration — and the trace stays
open from the first `next()`/`await __anext__()` through exhaustion, so spans and
generations created in the body (for example while consuming a streamed LLM
response) attach to it:

```python
from bir import generation, observe


@observe()
def stream_answer(question: str):
    with generation("local.llm", model="demo-model") as gen:
        chunks = []
        for token in ("Ans", "wer", "!"):
            chunks.append(token)
            yield token
        gen.set_output("".join(chunks))
```

The trace is finalized when the generator is exhausted (a successful trace),
raises (a redacted error, re-raised unchanged to the consumer), or is closed or
cancelled early. An early `close()`/`aclose()` or a cancellation is recorded as a
successful trace whose `metadata.generator.outcome` is `"closed"`, and it resets
all trace context so nothing leaks into later work. `send`/`throw`/`close` (and
`asend`/`athrow`/`aclose`) and the body's `finally` blocks all behave exactly as
they would without the decorator, and concurrent async generators running in
separate tasks stay isolated. Yielded values are never buffered; with output
capture enabled only a bounded yielded-item count is recorded under
`metadata.generator.items`.

The optional [provider integrations](docs/site/integrations.md) ship async
counterparts (`trace_chat_completion_async`, `trace_messages_async`,
`trace_completion_async`, and so on) for async clients such as `AsyncOpenAI`,
`AsyncAnthropic`, `litellm.acompletion`, and the async Mistral and Cohere
clients. Each awaits the provider coroutine inside an active trace and records one
generation; with `stream=True` they instead resolve to an async iterator you
consume with `async for`, never buffering the stream, across every async wrapper
with a streaming surface (OpenAI Chat Completions and Responses, Anthropic,
Gemini, Mistral, Cohere, and LiteLLM). AWS Bedrock (`trace_converse_async`) and
Vertex AI (`trace_generate_content_async`) ship async counterparts for their
non-streaming calls; their streaming surfaces stay synchronous. The synchronous
wrappers likewise accept
`stream=True` — yielding the provider's chunks unchanged and recording the
accumulated text and final token usage once the stream is consumed — across
OpenAI (Chat Completions and Responses), Anthropic, Gemini, Mistral, Cohere,
LiteLLM, and Vertex AI. For structured-output workflows built on Instructor,
`trace_create` wraps an Instructor-patched client's `create` call and records
model and token usage from the raw completion regardless of whether Instructor
returns the parsed model directly or a `(parsed_model, completion)` tuple. For
[DSPy](https://dspy.ai/) programs, `trace_lm` (and the async `trace_lm_async`)
wraps a `dspy.LM` instance's request method (`lm.forward`/`lm.aforward`) and
records model and token usage from the LiteLLM-style response. AWS Bedrock's Converse stream is a distinct method rather
than a `stream=True` flag, so it has its own `trace_converse_stream` wrapper that
yields the stream's events unchanged and records the same way.

For agent frameworks, Bir ships dependency-free callback handlers that map a
framework's own events into Bir traces without importing the framework:
`BirCallbackHandler` for LangChain, `BirLlamaIndexHandler` for LlamaIndex,
`BirAgentsTracingProcessor` for the OpenAI Agents SDK, `BirPydanticAIHandler` for
Pydantic AI, and `BirCrewAIHandler` for CrewAI. The Agents processor implements the
SDK's tracing-processor interface, turning an agent run into a Bir trace whose model
spans become generations and tool spans become tool calls; register it with
`agents.add_trace_processor(BirAgentsTracingProcessor())`. The Pydantic AI handler
hooks Pydantic AI's OpenTelemetry instrumentation as an OTel span processor (no
`pydantic_ai` or `opentelemetry` import), mapping each instrumented agent run's
spans the same way; register it on the tracer provider Pydantic AI uses. The CrewAI
handler bridges CrewAI's event bus (no `crewai` import): forward each
`(source, event)` to `BirCrewAIHandler.on_event` and each crew run becomes a Bir
trace whose task and agent steps are spans, LLM calls are generations, and tool uses
are tool calls.

## Local persistence and concurrency

Trace appends and size-based rotation are serialized across threads and local
processes that write the same trace path. Opt-in sent-ID bookkeeping uses a
separate lock around sidecar merge and replacement, so concurrent workers and
`bir send` processes preserve the union of accepted IDs. The implementation is
stdlib only: it uses `flock` on POSIX and byte-range locking on Windows, with
stable hidden lock files beside the trace and sidecar files.

These locks are advisory, so every writer must use Bir's persistence path.
Cross-host coordination and filesystems that do not implement normal local
advisory-lock semantics are not supported; use one local trace path per host in
those deployments. Lock files may remain on disk and must not be deleted while
Bir processes are active.

By default `send_events()` and `bir send` upload only the active trace file. Pass
`include_rotated=True` (or `bir send --include-rotated`) to also upload retained
size-rotated files oldest-first, deduplicated by event ID, so rotation does not
strand unsent events. Both `send_events()`/`bir send` and
`send_experiment()`/`bir send-experiment` retry transient failures (network
errors, timeouts, and HTTP 5xx) with bounded exponential backoff via `retries`
and `backoff`, while HTTP 4xx and malformed inputs fail immediately. See
[server uploads](docs/site/sending.md).

## Testing your instrumentation

To assert on the traces your own code produces, use `bir.testing.capture_traces()`.
It redirects trace writes to a private temporary file for the duration of a `with`
block and hands back a handle that reads the captured events and traces back in
memory — so your tests never touch your real `.bir/` directory:

```python
from bir.testing import capture_traces

def test_answer_is_instrumented():
    with capture_traces() as captured:
        answer_question("hello")

    recorded = captured.traces()[0]
    assert recorded.name == "answer_question"
    assert [event.type for event in recorded.events] == ["trace", "generation"]
```

`captured.events()` returns the flat list of recorded events and
`captured.traces()` groups them into traces, both read through the same public
loaders as `load_events()` / `load_traces()`. Only the active `trace_path` is
swapped; capture opt-in, sampling, and redaction stay exactly as configured, so a
captured event is identical to a real write. The previous configuration (including
a user-set `trace_path`) is restored when the block exits — even if the body
raises — and the temporary file is removed. Like `configure()`, it mutates
process-global config for the block's duration, so it is not meant to run
concurrently across threads. See [Core API](docs/site/core-api.md).

## Forwarding traces to OpenTelemetry

If you already run an OpenTelemetry backend, you can replay locally recorded Bir
traces as OpenTelemetry spans and ship them over OTLP. This is opt-in and never
runs on its own: nothing imports `opentelemetry` until you call the exporter, and
it only reads your local JSONL — it never writes to or alters it.

Install the extra, then forward loaded traces:

```bash
python -m pip install 'bir-sdk[otel]'
```

```python
from bir import load_traces
from bir.integrations.otel import export_traces_to_otlp

export_traces_to_otlp(
    load_traces(),
    endpoint="http://localhost:4318/v1/traces",
    service_name="rag-api",
)
```

`export_traces_to_otlp()` also accepts a single `LoadedTrace`, an iterable of
them, or a path to a trace file (loaded via `load_traces`). Each Bir trace
becomes one OpenTelemetry trace: the trace root maps to a root span and every
other event maps to a child span linked by `parent_id`, carrying over start/end
times and `success`/`error` status. Attributes follow the GenAI semantic
conventions where they exist (`gen_ai.request.model`,
`gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens`) with `bir.*`
attributes for the rest (event type, score value, token totals, cost, and the
originating Bir ids). Pass `headers=` for backend auth, or inject your own
configured `span_exporter=` for a different transport. Calling the exporter
without the extra installed raises a clear error pointing you to
`pip install 'bir-sdk[otel]'`.

The same export runs from the terminal without writing Python:

```bash
bir export-otel --endpoint http://localhost:4318/v1/traces --service-name rag-api
bir export-otel --endpoint http://localhost:4318/v1/traces \
  --header "x-api-key=secret" --include-rotated
```

`bir export-otel` loads local traces (honoring `--path` and `--include-rotated`
like `bir traces`) and forwards them through the same exporter, printing how many
traces and spans were sent. `--endpoint` is required; `--header KEY=VALUE` is
repeatable for backend auth, and `--service-name` and `--timeout` are passed
through. Without the `otel` extra it exits non-zero with the same install hint.

## Evaluations and experiments

Bir ships deterministic, local-first evaluators and an experiment runner that
scores a task over a dataset and persists per-example results and a summary under
`.bir/experiments/`. Use `run_experiment()` for synchronous tasks. Pass
`max_workers=N` to run examples concurrently inside a thread pool — useful for
I/O-bound sync tasks such as network LLM calls behind a synchronous client:

```python
from bir.evals import Dataset, DatasetExample, contains, run_experiment

result = run_experiment(
    "prompt-v1",
    dataset=Dataset([DatasetExample(id="q1", input={"question": "Hi"})]),
    task=answer,
    evaluators=[contains("Hi")],
    max_workers=8,
)
```

Use `run_experiment_async()` when your task is a coroutine such as an async
provider client:

```python
import asyncio

from bir.evals import Dataset, DatasetExample, contains, run_experiment_async


async def answer(question: str) -> str:
    ...  # await your async model client


result = asyncio.run(
    run_experiment_async(
        "prompt-v1",
        dataset=Dataset([DatasetExample(id="q1", input={"question": "Hi"})]),
        task=answer,
        evaluators=[contains("Hi")],
        max_concurrency=8,
    )
)
```

`run_experiment_async()` runs up to `max_concurrency` examples concurrently while
keeping results, JSONL rows, and summary aggregates in dataset order. It accepts
async tasks, plain sync callables, and sync callables that return an awaitable,
and otherwise matches `run_experiment()`. See
[local evals and experiments](docs/site/evals-experiments.md).

Inspect persisted experiments from the command line without a server:
`bir experiments` lists every experiment under `.bir/experiments/`, and
`bir experiment-show <experiment-id>` prints one experiment's summary
(evaluator aggregates) and its per-example scores and statuses (add `--json` to
either for a structured form, or `--dir` to point at another experiments
directory).

Compare a candidate against a baseline and gate CI on regressions with
`compare_experiments()` or `bir eval-gate`. A global `tolerance` bounds how far a
shared evaluator may drop, `score_tolerances` (and repeatable
`--score-tolerance NAME=VALUE`) override that per evaluator, and `missing_score`
(`--missing-score {ignore,regress}`) decides whether an evaluator dropped from
the candidate fails the gate:

```bash
bir eval-gate baseline.jsonl candidate.jsonl \
  --tolerance 0.01 --score-tolerance latency_under=0.05 --missing-score regress
```

The command exits `1` exactly when the policy reports a regression and prints a
machine-readable diff with `effective_tolerances`, `missing_score`, and
`regression_reasons`.

## Documentation

The documentation site is published at
<https://bir-ai.github.io/bir-python/> and covers the
[quickstart](docs/site/quickstart.md),
[core API](docs/site/core-api.md),
[capture and privacy](docs/site/capture-privacy.md),
[server uploads](docs/site/sending.md),
[optional integrations](docs/site/integrations.md), and
[local evals and experiments](docs/site/evals-experiments.md).

Build it locally with the isolated documentation extra:

```bash
python -m pip install -e ".[docs]"
mkdocs build --strict
```

CI installs the same `docs` extra and runs the strict build once on every pull
request and push to `main`, so invalid navigation, links reported by MkDocs,
and build warnings block the change. A separate workflow rebuilds the site
behind the same `--strict` gate and deploys it to GitHub Pages on every push to
`main`, so a docs change that fails the strict build is never published.

For local SDK development, install `.[dev]` and see the
[release checklist](docs/SDK_RELEASE_CHECKLIST.md).

```bash
python -m pip install -e ".[dev]" pyright
pyright
python scripts/verify_release.py
```

Release verification builds the wheel without network access from the complete
`bir` package tree, checks its contents and RECORD hashes, then installs it into
a clean virtual environment. The installed-wheel smoke test imports `bir.evals`,
`bir.cli`, and every optional integration module without installing provider
SDKs.

The checked example tests use only standard-library test utilities, so Pyright's
release gate is hermetic whether tooling is installed in a repository `.venv` or
in CI's active interpreter. Pytest remains optional development tooling.

## License

Bir is licensed under the [Apache License 2.0](LICENSE).
