# OpenAI-Style Local Demo

This demo records a realistic LLM workflow without requiring an OpenAI API key or
any extra dependencies. It uses an OpenAI-shaped generation event so you can test
the full Bir MVP loop locally:

1. decorate a Python function
2. record trace, span, tool call, generation, and score events
3. write local JSONL
4. send events to the FastAPI server
5. view them in the dashboard

The retrieval step is modeled with the current contract as a `tool_call` inside
the `retrieve_context` span. It sets `metadata.kind` to `retrieval`, records the
query in the tool input, and records matched documents in the tool output when
capture is enabled. This is the documented RAG shape until a dedicated
`retrieval()` SDK helper is added.

## Run The Demo

From this directory:

```bash
PYTHONPATH=../../packages/python-sdk/src python3 demo.py
```

The demo writes events to:

```text
.bir/traces.jsonl
```

To inspect the local trace from Python:

```bash
PYTHONPATH=../../packages/python-sdk/src python3 - <<'PY'
from bir import load_traces

for trace in load_traces(".bir/traces.jsonl"):
    print(trace.name, trace.status, len(trace.events))
    for event in trace.events:
        print(" ", event.type, event.name)
PY
```

## Send To The Server

Install the server dependencies if `uvicorn` is not already available:

```bash
cd ../../apps/server
python3 -m pip install -e ".[dev]"
cd ../../examples/openai-demo
```

Start the server in another terminal:

```bash
cd ../../apps/server
uvicorn app.main:app --reload
```

Then send the recorded events:

```bash
PYTHONPATH=../../packages/python-sdk/src python3 demo.py --send
```

The server stores accepted events in `.bir/server-events.jsonl` from the server
working directory.

## View In The Dashboard

Install the web dependencies if they are not already available:

```bash
cd ../../apps/web
npm install
cd ../../examples/openai-demo
```

Start the dashboard in another terminal:

```bash
cd ../../apps/web
npm run dev
```

Open `http://localhost:3000` and refresh the trace list. By default the
dashboard reads from `http://127.0.0.1:8000`.
