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
values are truncated. `score()`, generation token usage, and generation cost
require finite numeric values.

Generation cost is user-provided. Bir records explicit cost values and defaults
the currency to `USD`; it does not calculate provider pricing automatically.

## Retrieval

Use `retrieval()` to record RAG lookups with the existing `tool_call` event
contract. It sets `metadata.kind` to `retrieval`, stores the query at
`input.query` when input capture is enabled, and stores retrieved records at
`output.documents` when output capture is enabled.

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
payloads.

## Local Evals And Experiments

Bir includes a small deterministic evaluation layer for local regression checks.
It does not require a server or an LLM judge.

```python
from bir.evals import Dataset, DatasetExample, contains, exact_match, run_experiment


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
    evaluators=[contains(), exact_match("Bir is an observability SDK.")],
)

print(result.aggregate_scores)
```

Datasets can be stored as JSONL:

```json
{"id":"q1","input":{"question":"What is Bir?"},"expected":"An observability SDK"}
```

Load and run them locally:

```python
from bir.evals import Dataset, contains, run_experiment

dataset = Dataset.from_jsonl("questions.jsonl")
run_experiment("prompt-v1", dataset=dataset, task=answer_question, evaluators=[contains()])
```

Experiment results are written to `.bir/experiments/*.jsonl` by default, with
one result row per example. Available deterministic evaluators are
`exact_match()`, `contains()`, `regex_match()`, and `json_valid()`.

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
