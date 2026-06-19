# Quickstart

Install the SDK:

```bash
python -m pip install bir-sdk
```

Instrument a function and its LLM and retrieval work:

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

Trace, span, retrieval, generation, and score events are written as JSONL to:

```text
.bir/traces.jsonl
```

Payload capture remains off in this example. Calls such as `gen.set_output()`
make output available to the generation context, but it is written only when
output capture is enabled. See [Capture & Privacy](capture-privacy.md).

## Read traces

```python
from bir import load_traces

for trace in load_traces():
    print(trace.name, trace.status, trace.duration_ms)
    for event in trace.events:
        print(event.type, event.name)
```

## Use an explicit trace root

Use `trace()` when a context manager fits the workflow better than a decorator:

```python
from bir import generation, score, span, trace

with trace("answer_question", metadata={"kind": "manual"}):
    with span("draft_answer"):
        with generation("local.llm", model="demo-model") as gen:
            response = "ok"
            gen.set_output(response)
    score("helpfulness", 0.82)
```

Next, see the full [Core API](core-api.md), add an
[integration](integrations.md), or create a local
[evaluation experiment](evals-experiments.md).
