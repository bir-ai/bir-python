# Bir Python SDK

Bir is a minimal, local-first tracing and evaluation SDK for Python LLM
applications. It records traces, spans, generations, tool calls, retrievals,
and scores as local JSONL without requiring a server.

The SDK uses only the Python standard library at runtime. Provider and framework
integrations are thin wrappers that do not import their corresponding optional
packages.

## What Bir provides

- Sync and async tracing with `@observe()` and context managers.
- Generation, tool-call, retrieval, prompt-version, and score events.
- Opt-in payload capture with best-effort secret redaction.
- Trace sampling, service metadata, and size-based JSONL rotation.
- Deterministic datasets, evaluators, experiments, and regression gates.
- Local inspection and upload through the `bir` command.
- Optional wrappers for common LLM providers and frameworks.

Start with the [quickstart](quickstart.md), then use the [core API](core-api.md)
reference to instrument a real application.

## Installation

```bash
python -m pip install bir-sdk
```

The PyPI distribution is `bir-sdk`; the import package is `bir`:

```python
from bir import observe
```

Bir ships inline type annotations and a PEP 561 `py.typed` marker, so type
checkers use its types without a separate stub package.

## Local-first behavior

Events are written to `.bir/traces.jsonl` by default. Writes are serialized
within the SDK process, keeping the file line-delimited and parseable in
multi-threaded synchronous applications.

Input and output capture is disabled by default. See [Capture & Privacy](capture-privacy.md)
before enabling payload capture in a sensitive application.

## Build these docs

Documentation dependencies are isolated from runtime and test dependencies:

```bash
python -m pip install -e ".[docs]"
mkdocs build --strict
```

The generated site is written to the gitignored `site/` directory.
