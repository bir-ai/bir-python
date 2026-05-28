# Bir Python SDK

Minimal local tracing SDK for Python LLM applications.

```python
from bir import generation, load_traces, observe, score, send_events, span, tool_call


@observe()
def answer_question(question: str) -> str:
    with span("retrieve_context"):
        with tool_call("search_docs") as tool:
            documents = ["local context"]
            tool.set_output(documents)

    with generation("local.llm", model="demo-model") as gen:
        response = f"{documents[0]}: {question}"
        gen.set_output(response)
        gen.set_usage(input_tokens=12, output_tokens=24)

    score("helpfulness", 0.82)
    return response
```

Trace, span, tool call, generation, and score events are written as JSONL to:

```text
.bir/traces.jsonl
```

You can also read local traces back from the same file:

```python
for trace in load_traces():
    print(trace.name, trace.status, trace.duration_ms)
    for event in trace.events:
        print(event.type, event.name)
```

To send local events to a running Bir server:

```python
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

Input and output capture is disabled by default. Enable it globally with `configure()`
or for a single function with `@observe(capture_inputs=True, capture_outputs=True)`.
Common secret-like fields such as `api_key`, `authorization`, `password`, `secret`,
and `token` are redacted before events are written.
Secret-like values in captured error messages are also redacted.

```python
from bir import configure

configure(capture_inputs=True, capture_outputs=True)
```

Captured values are normalized to JSON-compatible data before writing. Non-finite
floats such as `NaN` and `Infinity` are stored as strings, and deeply nested
values are truncated. `score()` and generation token usage require finite numeric
values.

`load_events()` validates JSONL records against the current event schema and
raises `ValueError` for malformed rows, unsupported event types, invalid
timestamps, or unsupported schema versions.

To write traces somewhere else:

```python
configure(trace_path="tmp/bir-traces.jsonl")
```

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

Or install the package with test dependencies and run pytest:

```bash
python3 -m pip install -e ".[dev]"
pytest
```
