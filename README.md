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

## Tracing generators and streaming

`@observe()` also traces generator and async-generator functions across their
full iteration, not just their creation. The wrapper stays lazy — the body does
not run and nothing is written until the first iteration — and the trace stays
open from the first `next()`/`await __anext__()` through exhaustion, so spans and
generations created in the body (for example while consuming a streamed LLM
response) attach to it:

```python
from bir import generation, observe


@observe()
def stream_answer(question: str):
    with generation("local.llm", model="demo-model") as gen:
        chunks = []
        for token in ("Ans", "wer", "!"):
            chunks.append(token)
            yield token
        gen.set_output("".join(chunks))
```

The trace is finalized when the generator is exhausted (a successful trace),
raises (a redacted error, re-raised unchanged to the consumer), or is closed or
cancelled early. An early `close()`/`aclose()` or a cancellation is recorded as a
successful trace whose `metadata.generator.outcome` is `"closed"`, and it resets
all trace context so nothing leaks into later work. `send`/`throw`/`close` (and
`asend`/`athrow`/`aclose`) and the body's `finally` blocks all behave exactly as
they would without the decorator, and concurrent async generators running in
separate tasks stay isolated. Yielded values are never buffered; with output
capture enabled only a bounded yielded-item count is recorded under
`metadata.generator.items`.

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

## Evaluations and experiments

Bir ships deterministic, local-first evaluators and an experiment runner that
scores a task over a dataset and persists per-example results and a summary under
`.bir/experiments/`. Use `run_experiment()` for synchronous tasks, or
`run_experiment_async()` when your task is a coroutine such as an async provider
client:

```python
import asyncio

from bir.evals import Dataset, DatasetExample, contains, run_experiment_async


async def answer(question: str) -> str:
    ...  # await your async model client


result = asyncio.run(
    run_experiment_async(
        "prompt-v1",
        dataset=Dataset([DatasetExample(id="q1", input={"question": "Hi"})]),
        task=answer,
        evaluators=[contains("Hi")],
        max_concurrency=8,
    )
)
```

`run_experiment_async()` runs up to `max_concurrency` examples concurrently while
keeping results, JSONL rows, and summary aggregates in dataset order. It accepts
async tasks, plain sync callables, and sync callables that return an awaitable,
and otherwise matches `run_experiment()`. See
[local evals and experiments](docs/site/evals-experiments.md).

Compare a candidate against a baseline and gate CI on regressions with
`compare_experiments()` or `bir eval-gate`. A global `tolerance` bounds how far a
shared evaluator may drop, `score_tolerances` (and repeatable
`--score-tolerance NAME=VALUE`) override that per evaluator, and `missing_score`
(`--missing-score {ignore,regress}`) decides whether an evaluator dropped from
the candidate fails the gate:

```bash
bir eval-gate baseline.jsonl candidate.jsonl \
  --tolerance 0.01 --score-tolerance latency_under=0.05 --missing-score regress
```

The command exits `1` exactly when the policy reports a regression and prints a
machine-readable diff with `effective_tolerances`, `missing_score`, and
`regression_reasons`.

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
