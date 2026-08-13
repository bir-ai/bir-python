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

`aggregate_scores` is the mean of each evaluator over the examples that
evaluator scored. An example whose task raised is recorded with `status="error"`
and no scores at all, so it is absent from every mean rather than counted as a
zero. `result.example_count` and `result.error_count` are how many examples the
run held and how many of them failed; read them alongside the means, and see
[the failed-example policy](#failed-example-policy) for what the gate does with
them.

Each row is flushed as its example finishes, so a run that is stopped part-way
keeps the work it had already done. If the process is killed without unwinding —
`SIGTERM` from a pod eviction, `docker stop`, or a cancelled CI job — the rows
for the completed examples are on disk and `load_experiment()` reads them. The
`.summary.json` is written only when the run ends, so an interrupted experiment
has result rows and no summary; it will not appear in `list_experiments()` or
`bir experiments`, both of which list summaries. This matters most for the runs
it is most expensive to lose: an evaluation against a model API can be an hour
of paid calls.

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
awaited and `CancelledError` propagates without writing a summary; the leading
examples that had already finished keep their rows.

Because rows follow dataset order, an example that finishes while an earlier one
is still running is written only once that earlier one lands. What survives an
interruption is therefore the completed prefix of the dataset, not every example
that happened to finish.

## Bound each example with a timeout

Experiments often call real, network-backed model clients, where a single stuck
request can otherwise hang the whole run. Pass `timeout=<seconds>` (a positive,
finite number) to bound each example. It works on both runners:

```python
result = run_experiment(
    "quickstart",
    dataset=dataset,
    task=answer_question,
    evaluators=[contains("observability")],
    timeout=30,          # seconds per example
    raise_on_error=False,
)
```

An example whose task runs longer than `timeout` is recorded as an
`"error"`-status result with a `"task timed out after Ns"` message — the same
shape as any other failed example, so it counts toward the summary `error_count`,
honors `raise_on_error` (raising a `TimeoutError` when `True`), and keeps its
place in dataset order. The rest of the run continues.

`run_experiment()` enforces the limit by running each example on a worker thread
and waiting at most `timeout` seconds for it; the serial `max_workers=1` path
uses a dedicated single-worker executor per example so a timed-out worker never
blocks the next one. Because Python cannot force a thread to stop, a timed-out
synchronous task keeps running in the background until it returns on its own —
its result is simply discarded. `run_experiment_async()` wraps each example in
`asyncio.wait_for(...)`, so a timed-out example's task is cancelled and awaited
cleanly.

### What a run does with the tasks it cannot stop

Those still-running tasks are bounded. A serial run keeps at most sixteen of them
alive at once, and an example that finds none free waits for one only as long as
it was itself allowed — its own `timeout`. If none comes free in that time it is
recorded as an `"error"` too, saying `no worker was free within Ns; N task(s)
from earlier timed-out examples are still running and cannot be stopped`. That is
a different message from `task timed out after Ns` on purpose: the task did not
overrun, it never started.

The bound only ever matters when many examples time out at once, which means the
thing they call is not answering. A run whose examples occasionally overrun never
reaches it. Measured on 400 examples against a task that never returns, with a
5 ms timeout:

| | run time | peak threads |
| --- | --- | --- |
| unbounded, one worker per example | 2.70 s | 402 |
| bounded, waiting open-endedly for a slot | ~750 s | 18 |
| bounded, waiting only the example's timeout | 2.76 s | 18 |

The third row is what ships: waiting open-endedly bounds the threads but hands
back exactly what `timeout` exists to prevent.

### A concurrent run waits for a stuck worker, and says so

`max_workers > 1` bounds its threads by construction — the pool is the bound —
but a task that outran its timeout holds its slot until it returns, so a queued
example waits for one. That wait is open-ended, and deliberately so: bounding it
by the example's own timeout was measured, and two slow examples saturating a
two-worker pool made four of ten queued fast examples be recorded as failures
they would not have had, because a worker frees a moment after the bound
expires. Refusing an example that would have passed is worse than a slow run.

So a concurrent run can still take as long as its stuck tasks do — 60 examples
against a 20 s task with `max_workers=4` and a 5 ms timeout take about 280 s.
What it no longer does is look hung while it happens:

```
WARNING bir: bir experiment 'nightly' is waiting for a free worker: all 4 are
  still running tasks from examples that already timed out, and Python cannot
  stop a thread. The run continues as they return.
```

Said once per run, on the first example that waits longer than a whole timeout.
Bounding the run itself would need a budget for the run rather than for each
example, which is not something `run_experiment()` takes.

### Bound the run itself with `total_timeout`

`timeout` bounds an example. It does not bound the run: a task that outran it
keeps its worker until it returns, so a run against a backend that stopped
answering takes as long as the tasks do however small `timeout` is. Pass
`total_timeout=<seconds>` to bound the run:

```python
result = run_experiment(
    "nightly",
    dataset=dataset,
    task=answer_question,
    evaluators=[contains("observability")],
    timeout=30,           # seconds per example
    total_timeout=600,    # seconds for the whole run
    raise_on_error=False,
)
```

Measured on 60 examples against a task that never returns, with `max_workers=4`
and a 5 ms per-example timeout:

| | run time | rows |
| --- | --- | --- |
| `timeout` only | 280.07 s | 60 of 60 |
| `timeout` and `total_timeout=2` | 2.01 s | 4 of 60 |

When the limit passes the run starts no further examples and finalizes with the
ones that ran. The examples it did not reach are **absent** rather than recorded
as failures — they did not fail, and calling them errors would make the
experiment look worse than the code it measured. That is the same shape
`raise_on_error` already produces when it ends a run early: `example_count`
counts the rows the run produced, and they are the leading examples in dataset
order.

Stopping honors `raise_on_error`. With the default `True` it raises
`TimeoutError: experiment stopped after 600s with 143 of 500 example(s) run`, so
a truncated run cannot pass unnoticed; with `False` it returns what ran and says
so on the `bir` logger. `run_experiment_async()` takes the same argument and
means the same thing by it.

One limit is worth naming: a run can be stopped between examples, not inside
one. Python cannot interrupt a running task, so `total_timeout` stops the run
starting anything new — with `timeout` also set, that granularity is one
example's timeout; without it, an example that never returns is never
interrupted.

A run that returns with tasks still going says so once, on the `bir` logger:

```
WARNING bir: bir experiment 'nightly' returned with 16 task(s) from timed-out
  examples still running; Python cannot stop a thread, so they end when they
  return on their own. At most 16 run at once.
```

The default is `timeout=None` (unlimited), which is byte-for-byte identical to
the previous behavior — nothing is abandoned, so nothing is bounded or reported.

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

### Name each evaluator once per run

Every score is filed under its evaluator's name and nothing else: the aggregate
mean sums by name, the report prints one row per name, and `eval-gate` keys its
deltas by name. Two evaluators sharing a name would be averaged together into a
number no example was given, so a run refuses one before it writes anything:

```
ValueError: duplicate evaluator name 'field_equals': scores are aggregated by
name, so every evaluator in one run must have a distinct one. Pass name= to
override a factory's default.
```

Each factory above defaults `name` to its own, so the collision arrives from the
most ordinary pairing there is — two checks of the same kind. Every factory takes
a keyword-only `name` to tell them apart:

```python
evaluators=[
    field_equals("answer", name="answer_matches"),
    field_equals("citation.id", name="citation_matches"),
]
```

The check is on writing only. An experiment recorded before it existed still
loads and still compares; its two evaluators simply share one aggregate, as they
always did.

### Every identity must be a string

The four values that identify a row in an experiment file — the experiment's
`name`, a `DatasetExample`'s `id`, an evaluator's `name`, and the `name` on an
`EvalResult` a custom evaluator returns — must each be a non-empty string:

```
TypeError: experiment name must be a string
TypeError: dataset example id must be a string
TypeError: evaluator name must be a string
TypeError: eval result name must be a string
```

Only emptiness was checked before, so `DatasetExample(id=3, ...)` or
`exact_match(name=3)` was accepted and written into the JSONL — and
`load_experiment()` refuses to read a row whose identity is not a string, so the
run produced a file it could not read back. Pass `str(...)` for an id or name
your application keeps as a number.

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
inject markup.

A rendered report always encodes. `os.fsdecode` returns surrogate-escaped text
for a filename that is not valid UTF-8, so an `example_id` taken from a
filesystem walk holds code points no encoder accepts; those are escaped as
`\udcff` rather than dropped, so the odd id stays visible and the report can be
written at all.

`--output PATH` writes through a temporary sibling file and renames it into
place, so a write that cannot finish — a full disk, a volume that went away —
leaves the report that was there byte-identical instead of an empty file. A new
report is created with the umask's mode; re-rendering over an existing one keeps
the mode that file already had.

The same rendering is available in Python:

```python
from bir.evals import load_experiment, render_experiment_report

report = render_experiment_report(load_experiment(result.path), format="html")
```

## Compare experiments

Compare aggregate evaluator means, and how many examples failed, against a
persisted baseline:

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

### Failed-example policy

An aggregate score is a **mean over the examples an evaluator actually scored**.
An example whose task raised is scored by nobody, so it leaves that denominator
instead of lowering the mean — and a run that broke on half its dataset can
report a *higher* mean than one that answered every example badly:

```
baseline   20 examples,  0 failed   aggregate exact_match = 0.50
candidate  20 examples, 10 failed   aggregate exact_match = 1.00
```

Every aggregate delta in that comparison points the wrong way, so the diff also
carries the counts:

| Field | Meaning |
| --- | --- |
| `baseline_example_count` / `candidate_example_count` | Examples the run held |
| `baseline_error_count` / `candidate_error_count` | How many of them failed |
| `failed_example_regression` | Whether the candidate failed a larger *share* |

`failed_examples="regress"` is the **default**: the gate fails when
`failed_example_regression` is true. The comparison is a share rather than a
count, so runs over datasets of different sizes stay comparable, and it is exact
rather than floating-point, so 10 of 100 and 1 of 10 are equal and neither
regresses against the other.

Pass `"ignore"` to decide the gate on aggregate means alone, which is how it
behaved before `0.4.0`:

```python
diff = compare_experiments(
    "baseline.jsonl",
    "candidate.jsonl",
    failed_examples="ignore",
)
```

The counts and `failed_example_regression` are reported under either policy;
only the decision changes.

A run that scored *nothing* — an empty dataset, or every example failing — has no
shared evaluator, so there is no delta to fail on. The failed-example rule
catches the second case (a larger share failed) but not the first (a run of zero
examples has no share). `missing_score="regress"` is the policy for a candidate
that produced no scores at all.

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
reports a regression. `--score-tolerance NAME=VALUE` is repeatable, and
`--missing-score` and `--failed-examples` select the two policies:

```console
bir eval-gate baseline.jsonl candidate.jsonl \
  --tolerance 0.01 \
  --score-tolerance latency_under=0.05 \
  --missing-score regress \
  --failed-examples ignore
```

Repeating `--score-tolerance` for the same evaluator with the same value is
allowed; conflicting values, malformed `NAME=VALUE` assignments, and unknown
evaluator names are rejected with a clear error. The emitted JSON includes
`effective_tolerances`, `missing_score`, `regression_reasons`, the four example
and error counts, and `failed_example_regression`, so the gate decision is fully
machine-readable. Add `--per-example` to also emit `example_deltas` (the same
per-example detail as `per_example=True` above); without the flag the output
carries no per-example entries.

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
