# Bir Python SDK

Minimal local tracing SDK for Python LLM applications.

```python
from bir import configure, observe, score, span


configure(capture_inputs=True, capture_outputs=True)


@observe()
def answer_question(question: str) -> str:
    with span("retrieve_context"):
        context = "local context"

    response = f"{context}: {question}"
    score("helpfulness", 0.82)
    return response
```

Trace, span, and score events are written as JSONL to:

```text
.bir/traces.jsonl
```

Input and output capture is disabled by default. Enable it globally with `configure()`
or for a single function with `@observe(capture_inputs=True, capture_outputs=True)`.
Common secret-like fields such as `api_key`, `authorization`, `password`, `secret`,
and `token` are redacted before events are written.

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
