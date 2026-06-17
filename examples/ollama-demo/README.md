# Ollama Demo

Real local LLM tracing demo for the Bir SDK. Unlike `examples/openai-demo`,
this demo makes a real chat call to a locally running Ollama model and records
the retrieval, generation, token usage, and scores from the actual response.

## Prerequisites

```bash
brew install ollama
ollama serve
ollama pull llama3.2:1b
```

## Run

From this directory:

```bash
PYTHONPATH=../../src python3 demo.py
```

Send the trace to a running Bir server:

```bash
PYTHONPATH=../../src python3 demo.py --send
```

Useful flags:

- `--question "..."` to ask something else
- `--model llama3.2:1b` to switch Ollama models
- `--ollama-url http://127.0.0.1:11434` if Ollama runs elsewhere
- `--server-url http://127.0.0.1:8000` for the Bir ingestion server

Traces are written to `.bir/traces.jsonl` in this directory.
