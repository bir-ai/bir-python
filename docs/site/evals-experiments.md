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

## Compare experiments

Compare aggregate evaluator means against a persisted baseline:

```python
from bir.evals import compare_experiments

diff = compare_experiments("baseline.jsonl", "candidate.jsonl", tolerance=0.01)
print(diff.to_dict())
if diff.has_regressions:
    raise SystemExit(1)
```

Evaluators present in only one run are reported separately. The CLI exposes the
same CI gate and exits `1` only when a shared evaluator drops by more than the
tolerance:

```console
bir eval-gate baseline.jsonl candidate.jsonl --tolerance 0.01
```

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
