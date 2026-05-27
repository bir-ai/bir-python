# Bir Python SDK

Minimal local tracing SDK for Python LLM applications.

```python
from bir import observe, score, span


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
.llm_observe/traces.jsonl
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
