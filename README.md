# Bir Python SDK

Minimal local tracing SDK for Python LLM applications.

Bir records traces, spans, generations, tool calls, and scores to local JSONL
without requiring a server. Start locally, then send events to the Bir FastAPI
server when you want to inspect them in the dashboard.

## Installation

After the first package release:

```bash
python -m pip install bir-sdk
```

The distribution is published on PyPI as `bir-sdk`; the import name is `bir`
(e.g. `from bir import observe`).

Bir ships inline type annotations and a PEP 561 `py.typed` marker, so type
checkers such as mypy and pyright use the SDK's types in your code without any
extra stub packages.

For local development from this repository:

```bash
python -m pip install -e ".[dev]"
```

## Quickstart

```python
from bir import generation, observe, retrieval, score, span


@observe()
def answer_question(question: str) -> str:
    with span("retrieve_context"):
        with retrieval("search_docs", query=question) as result:
            result.add_document(id="doc-1", text="local context")
            documents = ["local context"]

    with generation("local.llm", model="demo-model") as gen:
        response = f"{documents[0]}: {question}"
        gen.set_output(response)
        gen.set_usage(input_tokens=12, output_tokens=24)
        gen.set_cost(input_cost=0.000012, output_cost=0.000048)

    score("helpfulness", 0.82)
    return response
```

Trace, span, tool call, generation, and score events are written as JSONL to:

```text
.bir/traces.jsonl
```

Writes to the local trace file are serialized within the SDK process so
multi-threaded sync applications keep the JSONL file line-delimited and
parseable.

## Manual Trace Contexts

Use `trace()` when your workflow is easier to wrap with a context manager than a
decorator:

```python
from bir import generation, score, span, trace

with trace("answer_question", metadata={"kind": "manual"}):
    with span("draft_answer"):
        with generation("local.llm", model="demo-model") as gen:
            response = "ok"
            gen.set_output(response)
    score("helpfulness", 0.82)
```

## Read Local Traces

You can also read local traces back from the same file:

```python
from bir import load_traces

for trace in load_traces():
    print(trace.name, trace.status, trace.duration_ms)
    for event in trace.events:
        print(event.type, event.name)
```

## Send Events To The Server

To send local events to a running Bir server:

```python
from bir import send_events

result = send_events("http://127.0.0.1:8000")
print(result.accepted, result.attempted, result.skipped)
```

`send_events()` posts local JSONL events to the Bir server, batching them into a
single request when the server supports it and otherwise posting them to
`/v1/events` one at a time. It uses the Python standard library, reports how many
local events were attempted, how many were newly accepted, and how many were
skipped by an idempotent server response,
raises `RuntimeError` when the server rejects an event or cannot be reached, and
does not remove local events after sending. Re-sending the same file is safe
against the Bir server because duplicate event IDs are treated as already
ingested.
Complete traces are sent root-first so the server receives the trace event before
its spans, tool calls, generations, and scores.

## Command Line

Installing the SDK adds a `bir` command (a console script) for inspecting local
traces and experiments and uploading them to a server without writing a script.
It is built entirely on the standard library and the public API, so it adds no
runtime dependencies.

```bash
bir traces                    # list local traces, newest first
bir traces --limit 20 --json  # machine-readable JSON for scripting
bir tail                      # follow .bir/traces.jsonl and print new events
bir experiments               # list local experiments and their scores
bir send                      # POST local events to http://127.0.0.1:8000
bir send-experiment .bir/experiments/<name>-<id>.jsonl
```

| Command | What it does |
| --- | --- |
| `bir traces [--path P] [--limit N] [--json]` | List traces: start time, status, duration, event count, and name. |
| `bir tail [--path P]` | Follow the trace file and print each new event as it is written (Ctrl-C to stop). |
| `bir experiments [--dir D] [--json]` | List experiments: id, name, status, example and error counts, and aggregate scores. |
| `bir send [--path P] [--server URL]` | Send local events and report how many were accepted, attempted, and skipped. |
| `bir send-experiment PATH [--server URL]` | Send a saved experiment and its summary, and report the accepted count and id. |

Every command accepts `--help`. `traces`, `tail`, and `send` read
`.bir/traces.jsonl` by default; pass `--path` (or set `BIR_TRACE_PATH`) to use
another file. `experiments` reads `.bir/experiments` by default; pass `--dir` to
override. `send` and `send-experiment` default to `http://127.0.0.1:8000` and
take `--server` to target another host. `--json` (on `traces` and `experiments`)
emits a JSON array for piping into other tools. Commands print errors to stderr
and exit non-zero when a file is missing or malformed or the server cannot be
reached, so they compose cleanly in scripts.

## Privacy And Capture

Input and output capture is disabled by default. Enable it globally with `configure()`
or for a single function with `@observe(capture_inputs=True, capture_outputs=True)`.
Common secret-like fields such as `api_key`, `authorization`, `password`, `secret`,
and `token` are redacted before events are written.
Captured strings, fallback object representations, and captured error messages
are also scanned for common secret-like text patterns before events are written.
Redaction is best-effort, so keep capture opt-in for sensitive payloads and review
what your application records.

```python
from bir import configure

configure(capture_inputs=True, capture_outputs=True)
```

The same defaults can be set from the environment so a deployment can enable
capture without code changes. `BIR_CAPTURE_INPUTS` and `BIR_CAPTURE_OUTPUTS`
accept boolean-like values (`1`/`true`/`yes`/`on` or `0`/`false`/`no`/`off`,
case-insensitive), and `BIR_TRACE_PATH` overrides the default
`.bir/traces.jsonl` location. These variables are read once at import time and
are overridden by any explicit `configure()` argument. Capture stays disabled
unless an environment variable or a `configure()` call enables it.

```bash
export BIR_CAPTURE_INPUTS=true
export BIR_CAPTURE_OUTPUTS=true
export BIR_TRACE_PATH=/var/log/bir/traces.jsonl
```

Captured values are normalized to JSON-compatible data before writing. Non-finite
floats such as `NaN` and `Infinity` are stored as strings, and deeply nested
values are truncated. `score()` requires a finite numeric value and accepts
optional `metadata` (for example `score("faithfulness", 0.4, metadata={"reason":
"answer cites no context"})`) that is redacted with the same rules before it is
written to the score event. Generation token usage and generation cost require at
least one field, and all provided values must be non-negative finite numbers.

Generation cost is user-provided. Bir records explicit cost values and defaults
the currency to `USD`; it does not calculate provider pricing automatically.

## Service Metadata

Use `configure()` to tag traces with the service and environment that produced
them. Both values are optional, must be non-empty strings, and are recorded on
trace root events under `metadata.service` so the server and dashboard can
filter traces by service and environment.

```python
from bir import configure

configure(service_name="rag-api", environment="production")
```

`BIR_SERVICE_NAME` and `BIR_ENVIRONMENT` set the same values from the
environment; an explicit `configure()` argument takes precedence.

```bash
export BIR_SERVICE_NAME=rag-api
export BIR_ENVIRONMENT=production
```

## Sampling

Use `configure(sample_rate=...)` to keep local trace volume bounded under load.
`sample_rate` is the probability (`0.0` to `1.0`) that a trace is recorded and
defaults to `1.0`, which records every trace. The decision is made once per
trace root and inherited by every event under it, so a sampled-out trace and all
of its spans, generations, tool calls, retrievals, and scores write nothing.

Sampling never changes control flow: a sampled-out function still runs and still
raises its own exceptions; only the local JSONL writes are skipped.

```python
from bir import configure

configure(sample_rate=0.1)  # record about 10% of traces
```

`BIR_SAMPLE_RATE` sets the same default from the environment (a float between
`0.0` and `1.0`); an explicit `configure(sample_rate=...)` argument takes
precedence.

```bash
export BIR_SAMPLE_RATE=0.1  # record about 10% of traces
```

## Retrieval

Use `retrieval()` to record RAG lookups with the existing `tool_call` event
contract. It sets `metadata.kind` to `retrieval`, stores the query at
`input.query` when input capture is enabled, and stores retrieved records at
`output.documents` when output capture is enabled.
Document `rank` values must be non-negative integers, and document `score`
values must be non-negative finite numbers.

```python
from bir import retrieval

with retrieval("vector_search", query=question) as result:
    result.add_document(
        id="doc-1",
        rank=1,
        score=0.82,
        source="docs",
        text="Bir records local traces with JSONL.",
    )
```

## Prompt Versions

Use `prompt()` to attach prompt identity and version metadata to a generation.
Prompt template text, variables, and rendered prompts are not captured unless
you opt in.

```python
from bir import generation, prompt

answer_prompt = prompt(
    "answer_question",
    version="v1",
    template="Answer using this context: {context}",
    variables={"context": "local context"},
)

with generation("local.llm", model="demo-model", prompt=answer_prompt) as gen:
    gen.set_output("ok")
```

The generation event records `metadata.prompt.name`, `metadata.prompt.version`,
and a `metadata.prompt.template_sha256` when a template is provided. To inspect
the actual prompt payload locally, opt in explicitly:

```python
answer_prompt = prompt(
    "answer_question",
    version="v1",
    template="Answer using this context: {context}",
    variables={"context": "local context"},
    capture_template=True,
    capture_variables=True,
    capture_rendered=True,
)
```

Captured prompt fields use the same best-effort redaction as other captured
payloads. After sending traces to the local server, the dashboard shows prompt
metadata on generation details without requiring you to inspect the raw event
JSON.

## LangChain Callback

Use `BirCallbackHandler` to record LangChain callback events as Bir traces
without adding LangChain as a Bir dependency:

```python
from bir import configure
from bir.integrations.langchain import BirCallbackHandler

configure(capture_inputs=True, capture_outputs=True)

result = chain.invoke(
    {"question": "What is Bir?"},
    config={"callbacks": [BirCallbackHandler()]},
)
```

Root chains become trace events, nested chains become spans, LLM/chat model
callbacks become generation events, retrievers become retrieval tool calls, and
tools become tool call events. Direct model calls without an active chain create
a small implicit trace root. The handler records token usage from common
LangChain response shapes including `llm_output.token_usage`,
`usage_metadata`, and `response_metadata.token_usage`.

## Mistral

Use `trace_chat()` to record Mistral chat completions without adding `mistralai`
as a Bir dependency:

```python
from bir import trace
from bir.integrations.mistral import trace_chat

with trace("chat"):
    response = trace_chat(
        client.chat.complete,
        model="mistral-small-latest",
        messages=[{"role": "user", "content": "What is Bir?"}],
    )
```

The wrapper forwards positional and keyword arguments unchanged, returns the
Mistral response untouched, and records the response model, token usage, and
`model_dump()` output when capture settings allow it.

## Cohere

Use `trace_chat()` to record Cohere v2 chat calls without adding `cohere` as a
Bir dependency:

```python
from bir import trace
from bir.integrations.cohere import trace_chat

with trace("chat"):
    response = trace_chat(
        client.chat,
        model="command-a-03-2025",
        messages=[{"role": "user", "content": "What is Bir?"}],
    )
```

The wrapper forwards positional and keyword arguments unchanged, returns the
Cohere response untouched, records the request model, and reads token usage from
`response.usage.tokens` when present.

## Anthropic

Use `trace_messages()` to record Anthropic Messages calls without adding
`anthropic` as a Bir dependency:

```python
from bir import trace
from bir.integrations.anthropic import trace_messages

with trace("chat"):
    response = trace_messages(
        client.messages.create,
        model="claude-haiku-4-5",
        max_tokens=512,
        messages=[{"role": "user", "content": "What is Bir?"}],
    )
```

The wrapper forwards positional and keyword arguments unchanged, returns the
Anthropic response untouched, records the response model, and reads token usage
from `response.usage` when present.

Streaming is supported. Pass `stream=True` and the wrapper returns the event
stream unchanged, then records one generation once the stream is consumed: it
accumulates output text from `content_block_delta` events and reads token usage
from the `message_start` and `message_delta` events.

```python
with trace("chat"):
    stream = trace_messages(
        client.messages.create,
        model="claude-haiku-4-5",
        max_tokens=512,
        messages=[{"role": "user", "content": "What is Bir?"}],
        stream=True,
    )
    for event in stream:
        ...  # consume the stream as usual; chunks pass through unchanged
```

## Google Gemini

Use `trace_generate_content()` to record Gemini `generate_content` calls without
adding `google-genai` (or the legacy `google-generativeai`) as a Bir dependency:

```python
from bir import trace
from bir.integrations.google import trace_generate_content

with trace("chat"):
    response = trace_generate_content(
        client.models.generate_content,
        model="gemini-2.5-flash",
        contents="What is Bir?",
    )
```

The wrapper forwards positional and keyword arguments unchanged, returns the
Gemini response untouched, records the request model (Gemini responses carry no
top-level model), and reads token usage from `response.usage_metadata` when
present.

Streaming is supported. Pass `stream=True` and the wrapper returns the chunk
stream unchanged, then records one generation once the stream is consumed: it
accumulates output text from each chunk's `.text` and reads token usage from
`usage_metadata` on the final chunk.

```python
with trace("chat"):
    stream = trace_generate_content(
        client.models.generate_content,
        model="gemini-2.5-flash",
        contents="What is Bir?",
        stream=True,
    )
    for chunk in stream:
        ...  # consume the stream as usual; chunks pass through unchanged
```

## AWS Bedrock

Use `trace_converse()` to record Amazon Bedrock Converse calls without adding
`boto3` as a Bir dependency:

```python
from bir import trace
from bir.integrations.bedrock import trace_converse

with trace("chat"):
    response = trace_converse(
        client.converse,
        modelId="anthropic.claude-3-5-sonnet-20240620-v1:0",
        messages=[{"role": "user", "content": [{"text": "What is Bir?"}]}],
    )
```

`client` is a `boto3` `bedrock-runtime` client. The wrapper forwards positional
and keyword arguments unchanged, returns the Converse response untouched, records
the request `modelId` (Converse responses carry no model), and reads token usage
from the response `usage` block (`inputTokens`/`outputTokens`/`totalTokens`) when
present.

## Google Vertex AI

Use `trace_generate_content()` to record Vertex AI generative-model calls without
adding `vertexai` (the `google-cloud-aiplatform` package) as a Bir dependency:

```python
from bir import trace
from bir.integrations.vertexai import trace_generate_content

with trace("chat"):
    response = trace_generate_content(
        model.generate_content,
        "What is Bir?",
        bir_model="gemini-1.5-flash",
    )
```

`model` is a `vertexai.generative_models.GenerativeModel`. Because Vertex binds
the model to the `GenerativeModel` instance rather than passing it to
`generate_content`, pass `bir_model` to record which model was used; when the
response carries a resolved `model_version` it refines that value. The wrapper
forwards positional and keyword arguments unchanged, returns the Vertex response
untouched, and reads token usage from `response.usage_metadata`
(`prompt_token_count`/`candidates_token_count`/`total_token_count`) when present.

This wrapper is also exported from `bir.integrations` as
`trace_vertex_generate_content` so it does not collide with the Google Gemini
wrapper of the same name.

## Local Evals And Experiments

Bir includes a small deterministic evaluation layer for local regression checks.
It does not require a server or an LLM judge.

```python
from bir.evals import Dataset, DatasetExample, contains, exact_match, latency_under, run_experiment


dataset = Dataset(
    [
        DatasetExample(
            id="q1",
            input={"question": "What is Bir?"},
            expected="An observability SDK",
        )
    ]
)


def answer_question(question: str) -> str:
    return "Bir is an observability SDK."


result = run_experiment(
    "quickstart",
    dataset=dataset,
    task=answer_question,
    evaluators=[
        contains(),
        exact_match("Bir is an observability SDK."),
        latency_under(1000),
    ],
)

print(result.aggregate_scores)
```

Datasets can be stored as JSONL:

```json
{"id":"q1","input":{"question":"What is Bir?"},"expected":"An observability SDK"}
```

Load and run them locally:

```python
from bir.evals import Dataset, contains, list_experiments, load_experiment, run_experiment, send_experiment

dataset = Dataset.from_jsonl("questions.jsonl")
result = run_experiment("prompt-v1", dataset=dataset, task=answer_question, evaluators=[contains()])
loaded = load_experiment(result.path)
summaries = list_experiments()
```

Compare a candidate run with a persisted baseline using aggregate evaluator
means. Deltas outside the tolerance are improved or regressed, while evaluators
that exist in only one run are reported separately rather than compared:

```python
from bir.evals import compare_experiments

diff = compare_experiments("baseline.jsonl", "candidate.jsonl", tolerance=0.01)
print(diff.to_dict())
if diff.has_regressions:
    raise SystemExit(1)
```

The CLI provides the same CI gate. It prints the diff as JSON and exits `1`
exactly when a shared evaluator drops by more than the tolerance:

```console
bir eval-gate baseline.jsonl candidate.jsonl --tolerance 0.01
```

`Dataset.to_jsonl()` redacts common secret-like values by default when exporting
examples. If you intentionally need to preserve raw dataset payloads, opt out
explicitly:

```python
dataset.to_jsonl("questions.jsonl", redact=False)
```

Experiment results are written to `.bir/experiments/*.jsonl` by default, with
one result row per example. Bir also writes a sibling `.summary.json` file with
the experiment id, status, example count, error count, aggregate scores, and
result path so local runs can be listed without scanning every result row.
Available deterministic evaluators are `exact_match()`, `contains()`,
`regex_match()`, `json_valid()`, `field_equals()`, `field_contains()`,
`latency_under()`, `cost_under()`, `numeric_between()`,
`retrieved_context_contains()`, `answer_context_overlap()`,
`answer_contains_citation()`, and `custom_evaluator()`.

Upload a completed experiment to a running Bir server so the dashboard can show
the experiment list and per-example result detail:

```python
send_experiment(result.path, "http://127.0.0.1:8000")
```

To connect local experiment rows back to traces, opt in when running the
experiment:

```python
result = run_experiment(
    "prompt-v1",
    dataset=dataset,
    task=answer_question,
    evaluators=[contains()],
    record_traces=True,
)
```

`record_traces=True` writes one trace per dataset example and records evaluator
outputs as score events on that trace.

Use threshold evaluators for local gates:

```python
from bir.evals import cost_under, latency_under, numeric_between

evaluators = [
    latency_under(1000),
    cost_under(0.05),
    numeric_between(min_value=0.0, max_value=1.0),
]
```

`latency_under()` uses measured task duration from `run_experiment()`.
`cost_under()` checks explicit cost fields returned by your task, either
`{"total_cost": 0.01}` or `{"cost": {"total_cost": 0.01}}`. Bir records only the
cost values you provide and does not calculate provider pricing.
`numeric_between()` checks numeric task outputs, or a numeric field when
`field=` is provided.

Use structured output evaluators for JSON-like task results:

```python
from bir.evals import field_contains, field_equals, numeric_between

evaluators = [
    field_contains("answer", "observability"),
    field_equals("citations[0].id", "doc-1"),
    numeric_between(min_value=0.7, max_value=1.0, field="confidence"),
]
```

Field paths support dot paths and list indexes, such as `answer`,
`usage.total_tokens`, and `items[0].name`. Missing paths produce a `0.0` score
with failure metadata instead of failing the experiment.

Use `custom_evaluator()` for local checks that are specific to your task:

```python
from bir.evals import EvalResult, custom_evaluator

has_citation = custom_evaluator(
    "has_citation",
    lambda output, expected: "[1]" in str(output),
)

debuggable = custom_evaluator(
    "debuggable",
    lambda output, expected: EvalResult(
        name="debuggable",
        value=1.0,
        metadata={"expected": expected},
    ),
)
```

Custom evaluators may return `bool`, `int`, `float`, or `EvalResult`. Exceptions
from custom evaluator functions surface normally during development.

Use `retrieved_context_contains()` to check retrieval quality without an LLM
judge:

```python
from bir.evals import retrieved_context_contains

evaluators = [
    retrieved_context_contains("observability"),
]
```

`retrieved_context_contains()` reads the `contexts` list from a structured RAG
output such as `{"answer": "...", "contexts": ["doc text", ...]}` and scores
`1.0` when `expected` appears in one of the retrieved strings. Missing or empty
`contexts` produce a `0.0` score with failure metadata instead of failing the
experiment. Pass `case_sensitive=False` for case-insensitive matching. This is a
deterministic retrieval check, not proof that the answer used the context.

Use `answer_context_overlap()` to flag answers that may not be grounded in the
retrieved context, also without an LLM judge:

```python
from bir.evals import answer_context_overlap

evaluators = [
    answer_context_overlap(0.5),
]
```

`answer_context_overlap()` reads the same structured RAG output
(`{"answer": "...", "contexts": ["doc text", ...]}`) and scores `1.0` when at
least `min_ratio` of the answer's word tokens also appear in the retrieved
contexts. It is a deterministic faithfulness heuristic, not proof of
faithfulness: paraphrased but faithful answers can score low, and unfaithful
answers that reuse context words can score high. Missing answers or contexts
produce a `0.0` score with failure metadata instead of failing the experiment.

Use `answer_contains_citation()` to check that an answer cites a source, also
without an LLM judge:

```python
from bir.evals import answer_contains_citation

evaluators = [
    answer_contains_citation(),
]
```

`answer_contains_citation()` reads a plain answer string or the `answer` field
of a structured RAG output (`{"answer": "...", "contexts": [...]}`) and scores
`1.0` when the answer contains a citation marker. By default any bracketed
marker such as `[1]` or `[doc-1]` counts; pass `pattern` to require a custom
citation format such as `pattern=r"\(\d+\)"` for markers like `(1)`. This is a
deterministic format check, not proof that the citation is correct or that the
cited source supports the answer. Non-text output or a missing `answer` produces
a `0.0` score with failure metadata instead of failing the experiment.

## Event Loading

`load_events()` validates JSONL records against the current event schema and
raises `ValueError` for malformed rows, unsupported event types, invalid
timestamps, or unsupported schema versions.

To write traces somewhere else:

```python
configure(trace_path="tmp/bir-traces.jsonl")
```

## Development

Run the SDK unit tests from this directory:

```bash
PYTHONPATH=src ../../.venv/bin/python -m unittest discover -s tests
```

Or install the package with test dependencies and run pytest:

```bash
python3 -m pip install -e ".[dev]"
pytest
```

Run repository type checking from the repository root:

```bash
./.venv/bin/pyright
```

Run the release verification script from the repository root before publishing:

```bash
./.venv/bin/python scripts/verify_release.py
```

The script builds a temporary SDK wheel, installs it in a fresh temporary
virtual environment, and smoke-tests local tracing and retrieval without writing
build artifacts into the repository.

Release planning lives in `CHANGELOG.md` and `docs/SDK_RELEASE_CHECKLIST.md`.

## License

Bir is licensed under the [Apache License 2.0](LICENSE).
