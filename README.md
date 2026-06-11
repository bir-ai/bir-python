# Bir Python SDK

Minimal local tracing SDK for Python LLM applications.

Bir records traces, spans, generations, tool calls, and scores to local JSONL
without requiring a server. Start locally, then send events to the Bir FastAPI
server when you want to inspect them in the dashboard.

## Installation

After the first package release:

```bash
python -m pip install bir
```

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
print(result.accepted)
```

`send_events()` posts each local JSONL event to `/v1/events`. It uses the Python
standard library, reports the server's accepted event count, raises `RuntimeError`
when the server rejects an event or cannot be reached, and does not remove local
events after sending. Re-sending the same file is safe against the Bir server
because duplicate event IDs are treated as already ingested.
Complete traces are sent root-first so the server receives the trace event before
its spans, tool calls, generations, and scores.

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

Captured values are normalized to JSON-compatible data before writing. Non-finite
floats such as `NaN` and `Infinity` are stored as strings, and deeply nested
values are truncated. `score()` requires a finite numeric value. Generation token
usage and generation cost require non-negative finite numeric values.

Generation cost is user-provided. Bir records explicit cost values and defaults
the currency to `USD`; it does not calculate provider pricing automatically.

## Service Metadata

Use `configure()` to tag traces with the service and environment that produced
them. Both values are optional, must be non-empty strings, and are recorded on
trace root events under `metadata.service` so traces from different deployments
can be told apart later.

```python
from bir import configure

configure(service_name="rag-api", environment="production")
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
a small implicit trace root.

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

Experiment results are written to `.bir/experiments/*.jsonl` by default, with
one result row per example. Bir also writes a sibling `.summary.json` file with
the experiment id, status, example count, error count, aggregate scores, and
result path so local runs can be listed without scanning every result row.
Available deterministic evaluators are `exact_match()`, `contains()`,
`regex_match()`, `json_valid()`, `field_equals()`, `field_contains()`,
`latency_under()`, `cost_under()`, `numeric_between()`, and
`custom_evaluator()`.

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
./.venv/bin/python packages/python-sdk/scripts/verify_release.py
```

The script builds a temporary SDK wheel, installs it in a fresh temporary
virtual environment, and smoke-tests local tracing and retrieval without writing
build artifacts into the repository.

Release planning lives in `CHANGELOG.md` and `../../docs/SDK_RELEASE_CHECKLIST.md`.

## License

Bir is source-available under the Functional Source License 1.1 with Apache 2.0
as the future license (`FSL-1.1-ALv2`). FSL is not an OSI-approved open source
license.
