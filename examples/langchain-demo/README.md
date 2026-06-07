# LangChain Demo

This example shows Bir's LangChain callback integration without requiring
LangChain or provider API keys. The demo manually drives the same callback
methods LangChain calls during a simple RAG workflow, so it records trace,
retrieval, tool, and generation events locally.

## Use With LangChain

In a real LangChain app, pass `BirCallbackHandler` in the runnable config:

```python
from bir import configure
from bir.integrations.langchain import BirCallbackHandler

configure(capture_inputs=True, capture_outputs=True)

result = chain.invoke(
    {"question": "What is Bir?"},
    config={"callbacks": [BirCallbackHandler()]},
)
```

The handler is dependency-free. It does not install or import LangChain; it
implements the callback methods LangChain calls when LangChain is already part
of your application.

## Run The Local Demo

From this directory:

```bash
PYTHONPATH=../../packages/python-sdk/src python3 demo.py
```

The demo writes events to:

```text
.bir/traces.jsonl
```

To send the events to a running Bir server:

```bash
PYTHONPATH=../../packages/python-sdk/src python3 demo.py --send
```

Open the dashboard and refresh the trace list to inspect the LangChain-shaped
workflow.
