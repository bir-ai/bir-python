# Evals & Experiments

Bir includes deterministic local evaluation tools for regression checks. They
require neither a server nor an LLM judge.

## Run an experiment

```python
from bir.evals import (
    Dataset,
    DatasetExample,
    contains,
    exact_match,
    latency_under,
    run_experiment,
)

dataset = Dataset(
    [
        DatasetExample(
            id="q1",
            input={"question": "What is Bir?"},
            expected="An observability SDK",
        )
    ]
)

def answer_question(question: str) -> str:
    return "Bir is an observability SDK."

result = run_experiment(
    "quickstart",
    dataset=dataset,
    task=answer_question,
    evaluators=[
        contains(),
        exact_match("Bir is an observability SDK."),
        latency_under(1000),
    ],
)

print(result.aggregate_scores)
```

Results are written to `.bir/experiments/*.jsonl`, one row per example. A
sibling `.summary.json` stores the experiment status, counts, aggregate scores,
and result path.

## Run experiments concurrently with a thread pool

For I/O-bound synchronous tasks — such as network LLM calls behind a sync
client — pass `max_workers=N` to run up to `N` examples at once inside a
`concurrent.futures.ThreadPoolExecutor`:

```python
result = run_experiment(
    "quickstart",
    dataset=dataset,
    task=answer_question,
    evaluators=[contains("observability")],
    max_workers=8,
)
```

Results, JSONL rows, and summary aggregates are always written in dataset
order regardless of which examples finish first. All other semantics —
`raise_on_error`, `record_traces` trace isolation, redaction, and the
persisted schema — match the sequential path. The default is `max_workers=1`,
which runs examples one at a time and is byte-for-byte identical to the
previous behavior.

## Run an async experiment

`run_experiment_async()` is the asynchronous counterpart to `run_experiment()`.
Use it when your task is a coroutine — for example an async provider client — so
you do not have to wrap it in an event-loop adapter. It accepts coroutine
functions, plain sync callables, and sync callables that return an awaitable; a
returned value is awaited only when it is awaitable.

```python
import asyncio

from bir.evals import Dataset, DatasetExample, contains, run_experiment_async

dataset = Dataset([DatasetExample(id="q1", input={"question": "What is Bir?"})])

async def answer_question(question: str) -> str:
    # await your async model client here
    return "Bir is an observability SDK."

result = asyncio.run(
    run_experiment_async(
        "quickstart-async",
        dataset=dataset,
        task=answer_question,
        evaluators=[contains("observability")],
        max_concurrency=8,
    )
)

print(result.aggregate_scores)
```

Up to `max_concurrency` examples (a positive integer, default `1`) run at once,
but the returned results, the persisted JSONL rows, and the summary aggregates
always follow dataset order regardless of completion order. Evaluator execution,
task input binding, redaction, `raise_on_error`, `record_traces`, and the
persisted JSONL/summary schema are identical to `run_experiment()`.

Each example runs in its own asyncio task, so `record_traces=True` produces an
isolated trace tree per example even when they run concurrently. If the
surrounding coroutine is cancelled, the in-flight example tasks are cancelled and
awaited and `CancelledError` propagates without writing a summary.

## Store datasets as JSONL

```json
{"id":"q1","input":{"question":"What is Bir?"},"expected":"An observability SDK"}
```

```python
from bir.evals import Dataset

dataset = Dataset.from_jsonl("questions.jsonl")
dataset.to_jsonl("copy.jsonl")
```

`Dataset.to_jsonl()` redacts common secret-like values by default. To preserve
raw payloads intentionally, pass `redact=False`.

## Evaluator catalog

Available deterministic evaluators are:

| Evaluator | Check |
| --- | --- |
| `exact_match()` | Output equals an expected value. |
| `contains()` | Text contains the expected value. |
| `similarity_above()` | Fuzzy text similarity ratio to the expected value meets a threshold. |
| `regex_match()` | Text matches a regular expression. |
| `json_valid()` | Output is valid JSON or JSON-like data. |
| `field_equals()` | A dot/index path equals the expected value. |
| `field_contains()` | A field contains expected text. |
| `latency_under()` | Measured task duration stays below a limit. |
| `cost_under()` | An explicit output cost stays below a limit. |
| `numeric_between()` | Numeric output or field stays in a range. |
| `retrieved_context_contains()` | Retrieved contexts include expected text. |
| `answer_context_overlap()` | Answer/context token overlap reaches a ratio. |
| `answer_contains_citation()` | An answer contains a citation marker. |
| `custom_evaluator()` | A local callable implements a task-specific check. |

### Fuzzy text matching

```python
from bir.evals import similarity_above

evaluators = [similarity_above(0.8, "Bir is an observability SDK.")]
```

`similarity_above()` sits between `exact_match()` and `contains()`: it scores
`1.0` when the normalized `difflib.SequenceMatcher` ratio between the output text
and the expected text is at or above `threshold` (inclusive), and `0.0`
otherwise. This is a deterministic, standard-library-only check that tolerates
typos, reordering, and minor wording differences without an embedding model or
new dependency. Like `contains()`, it accepts a per-example expected value when
the `expected` argument is omitted, and `case_sensitive=False` lowercases both
sides before comparing. The achieved `ratio` and `threshold` are recorded in the
score metadata so failures are inspectable. It is a surface-form heuristic, not
semantic similarity: paraphrases that share few characters can still score `0.0`.

### Structured output

```python
from bir.evals import field_contains, field_equals, numeric_between

evaluators = [
    field_contains("answer", "observability"),
    field_equals("citations[0].id", "doc-1"),
    numeric_between(min_value=0.7, max_value=1.0, field="confidence"),
]
```

Field paths support dot paths and list indexes. Missing paths produce a `0.0`
score with failure metadata instead of stopping the experiment.

### Latency and explicit cost

```python
from bir.evals import cost_under, latency_under, numeric_between

evaluators = [
    latency_under(1000),
    cost_under(0.05),
    numeric_between(min_value=0.0, max_value=1.0),
]
```

`latency_under()` uses task duration measured by `run_experiment()`.
`cost_under()` reads `{"total_cost": 0.01}` or
`{"cost": {"total_cost": 0.01}}`. Bir never calculates provider pricing.

### RAG heuristics

```python
from bir.evals import (
    answer_contains_citation,
    answer_context_overlap,
    retrieved_context_contains,
)

evaluators = [
    retrieved_context_contains("observability"),
    answer_context_overlap(0.5),
    answer_contains_citation(),
]
```

These evaluators expect output shaped like
`{"answer": "...", "contexts": ["doc text", "..."]}`. They are
deterministic heuristics, not proof of retrieval quality, faithfulness, or
citation correctness. Missing inputs produce a `0.0` score with failure
metadata.

### Custom evaluators

```python
from bir.evals import EvalResult, custom_evaluator

has_citation = custom_evaluator(
    "has_citation",
    lambda output, expected: "[1]" in str(output),
)

debuggable = custom_evaluator(
    "debuggable",
    lambda output, expected: EvalResult(
        name="debuggable",
        value=1.0,
        metadata={"expected": expected},
    ),
)
```

Custom evaluator callables may return `bool`, `int`, `float`, or `EvalResult`.
Their exceptions surface normally.

## Load and list results

```python
from bir.evals import list_experiments, load_experiment

loaded = load_experiment(result.path)
summaries = list_experiments()
```

From the command line, `bir experiments` lists every experiment under
`.bir/experiments/`, and `bir experiment-show <experiment-id>` prints one
experiment's summary (evaluator aggregate means) and a per-example table of id,
status, and scores:

```bash
bir experiments                          # list experiments and aggregate scores
bir experiment-show <experiment-id>      # one experiment's summary and results
bir experiment-show <experiment-id> --json   # nested object for scripts
```

Both commands accept `--dir` to read an experiments directory other than the
default `.bir/experiments`. `bir experiment-show --json` emits a deterministic
object with the summary fields and a `results` list of per-example `example_id`,
`status`, `scores`, and `error`; an unknown id prints nothing and exits non-zero.

## Share a report

`bir experiment-report <experiment-id>` renders one experiment to a
self-contained, stdlib-only file — the summary, the per-evaluator aggregate
means, and the per-example table of statuses and scores — so you can share or
archive a result without standing up the server or dashboard:

```bash
bir experiment-report <experiment-id>                       # HTML to stdout
bir experiment-report <experiment-id> --format markdown     # Markdown to stdout
bir experiment-report <experiment-id> --output report.html  # write to a file
```

The default `html` format is a complete standalone document with inline styles
and no external assets; `--format markdown` emits the same sections as a Markdown
document. Like `experiment-show` it accepts `--dir` and exits non-zero (printing
nothing to stdout) for an unknown id. Output is deterministic — evaluators are
ordered by name and examples follow dataset order — and every experiment-derived
string is escaped for the chosen format, so already-redacted example text cannot
inject markup. The same rendering is available in Python:

```python
from bir.evals import load_experiment, render_experiment_report

report = render_experiment_report(load_experiment(result.path), format="html")
```

## Compare experiments

Compare aggregate evaluator means against a persisted baseline:

```python
from bir.evals import compare_experiments

diff = compare_experiments("baseline.jsonl", "candidate.jsonl", tolerance=0.01)
print(diff.to_dict())
if diff.has_regressions:
    raise SystemExit(1)
```

A shared evaluator regresses when `candidate - baseline` drops by more than the
tolerance; a change exactly equal to the tolerance is treated as unchanged.
Evaluators present in only one run are reported separately.

### Per-evaluator tolerances

Different scores tolerate different movement. `score_tolerances` overrides the
global `tolerance` for individual evaluators while leaving the rest on the
global value:

```python
diff = compare_experiments(
    "baseline.jsonl",
    "candidate.jsonl",
    tolerance=0.01,
    score_tolerances={"latency_under": 0.05},
)
```

Each override value must be a non-negative, finite number, and each name must be
a *shared* evaluator present in both runs. A name that is not shared (a typo, or
an evaluator that only one run produced) raises a clear error rather than being
silently ignored. `diff.effective_tolerances` reports the tolerance actually
applied to each shared evaluator.

### Missing-score policy

By default, an evaluator that exists in the baseline but not the candidate is
reported under `baseline_only` without failing the gate (`missing_score="ignore"`).
Because a removed evaluator silently drops coverage, you can opt into treating it
as a regression:

```python
diff = compare_experiments(
    "baseline.jsonl",
    "candidate.jsonl",
    missing_score="regress",
)
```

Under `"regress"`, every baseline-only evaluator makes `diff.has_regressions`
true and appears in `diff.regression_reasons` with the reason `"baseline_only"`.
Delta-based regressions of shared evaluators use the reason
`"delta_below_tolerance"`. Evaluators that appear only in the candidate add
coverage and are never treated as regressions.

### Per-example detail

Aggregate means tell you *which* evaluator regressed, not *which examples* drove
it — and an unchanged mean can still hide one example dropping while another
improves. Pass `per_example=True` to also compute, for each shared evaluator, the
candidate-minus-baseline delta of every example scored in both runs:

```python
diff = compare_experiments(
    "baseline.jsonl",
    "candidate.jsonl",
    per_example=True,
)

for evaluator, deltas in diff.example_deltas.items():
    for example_id, delta in deltas.items():
        if delta < 0:
            print(f"{evaluator} dropped {delta:+.2f} on {example_id}")
```

`diff.example_deltas` is keyed by evaluator then example_id, both in sorted order.
Examples present in only one run (or not scored by the evaluator, such as an
errored example) are skipped. This is opt-in reporting detail only: it never
changes the aggregate comparison, `has_regressions`, or the gate exit code. When
`per_example=False` (the default) `example_deltas` is empty and is omitted from
`to_dict()`, so the aggregate-only output is unchanged.

### CLI gate

The CLI exposes the same gate and exits `1` exactly when the configured policy
reports a regression. `--score-tolerance NAME=VALUE` is repeatable and
`--missing-score` selects the policy:

```console
bir eval-gate baseline.jsonl candidate.jsonl \
  --tolerance 0.01 \
  --score-tolerance latency_under=0.05 \
  --missing-score regress
```

Repeating `--score-tolerance` for the same evaluator with the same value is
allowed; conflicting values, malformed `NAME=VALUE` assignments, and unknown
evaluator names are rejected with a clear error. The emitted JSON includes
`effective_tolerances`, `missing_score`, and `regression_reasons` so the gate
decision is fully machine-readable. Add `--per-example` to also emit
`example_deltas` (the same per-example detail as `per_example=True` above);
without the flag the output is unchanged.

## Link results to traces

```python
result = run_experiment(
    "prompt-v1",
    dataset=dataset,
    task=answer_question,
    evaluators=[contains()],
    record_traces=True,
)
```

This writes one trace per dataset example and records evaluator outputs as score
events.

## Upload an experiment

```python
from bir.evals import send_experiment

send_experiment(result.path, "http://127.0.0.1:8000")
```

The server can then display the experiment list and per-example details.

Like [`send_events()`](sending.md#retry-behavior), `send_experiment()` retries
transient failures — network errors, timeouts, and HTTP 5xx responses — with
exponential backoff. HTTP 4xx responses, a missing experiment or summary file,
and an invalid success body are permanent and raised immediately.

```python
send_experiment(
    result.path,
    "http://127.0.0.1:8000",
    retries=3,
    backoff=1.0,
    timeout=10.0,
)
```

The delay is `backoff * 2**attempt`. Defaults are two retries, a 0.5-second
backoff, and a 10-second timeout. A healthy send makes one request attempt. The
CLI exposes the same controls as `bir send-experiment --retries N --backoff
SECONDS`.
