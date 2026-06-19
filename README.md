# Bir Python SDK

Minimal, zero-runtime-dependency, local-first tracing and evals for Python LLM
applications.

Bir records traces, spans, generations, tool calls, retrievals, and scores to
local JSONL without requiring a server. Start locally, evaluate deterministic
regressions, and send events to a Bir server when you want to inspect them in a
dashboard.

## Installation

```bash
python -m pip install bir-sdk
```

The distribution name is `bir-sdk`; the import name is `bir`. Runtime
installation has no third-party dependencies. Bir also ships inline type
annotations and a PEP 561 `py.typed` marker.

## Quickstart

```python
from bir import generation, observe, score


@observe()
def answer_question(question: str) -> str:
    with generation("local.llm", model="demo-model") as gen:
        response = f"Answer: {question}"
        gen.set_output(response)
        gen.set_usage(input_tokens=12, output_tokens=24)
    score("helpfulness", 0.82)
    return response
```

Events are written to `.bir/traces.jsonl` by default. Input and output capture
is disabled unless you explicitly enable it.

## Local persistence and concurrency

Trace appends and size-based rotation are serialized across threads and local
processes that write the same trace path. Opt-in sent-ID bookkeeping uses a
separate lock around sidecar merge and replacement, so concurrent workers and
`bir send` processes preserve the union of accepted IDs. The implementation is
stdlib only: it uses `flock` on POSIX and byte-range locking on Windows, with
stable hidden lock files beside the trace and sidecar files.

These locks are advisory, so every writer must use Bir's persistence path.
Cross-host coordination and filesystems that do not implement normal local
advisory-lock semantics are not supported; use one local trace path per host in
those deployments. Lock files may remain on disk and must not be deleted while
Bir processes are active.

By default `send_events()` and `bir send` upload only the active trace file. Pass
`include_rotated=True` (or `bir send --include-rotated`) to also upload retained
size-rotated files oldest-first, deduplicated by event ID, so rotation does not
strand unsent events. Both `send_events()`/`bir send` and
`send_experiment()`/`bir send-experiment` retry transient failures (network
errors, timeouts, and HTTP 5xx) with bounded exponential backoff via `retries`
and `backoff`, while HTTP 4xx and malformed inputs fail immediately. See
[server uploads](docs/site/sending.md).

## Documentation

The documentation site covers the [quickstart](docs/site/quickstart.md),
[core API](docs/site/core-api.md),
[capture and privacy](docs/site/capture-privacy.md),
[server uploads](docs/site/sending.md),
[optional integrations](docs/site/integrations.md), and
[local evals and experiments](docs/site/evals-experiments.md).

Build it locally with the isolated documentation extra:

```bash
python -m pip install -e ".[docs]"
mkdocs build --strict
```

CI installs the same `docs` extra and runs the strict build once on every pull
request and push to `main`, so invalid navigation, links reported by MkDocs,
and build warnings block the change.

For local SDK development, install `.[dev]` and see the
[release checklist](docs/SDK_RELEASE_CHECKLIST.md).

```bash
python -m pip install -e ".[dev]" pyright
pyright
python scripts/verify_release.py
```

Release verification builds the wheel without network access from the complete
`bir` package tree, checks its contents and RECORD hashes, then installs it into
a clean virtual environment. The installed-wheel smoke test imports `bir.evals`,
`bir.cli`, and every optional integration module without installing provider
SDKs.

The checked example tests use only standard-library test utilities, so Pyright's
release gate is hermetic whether tooling is installed in a repository `.venv` or
in CI's active interpreter. Pytest remains optional development tooling.

## License

Bir is licensed under the [Apache License 2.0](LICENSE).
