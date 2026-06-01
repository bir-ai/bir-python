# Evaluator Implementation Guide

This guide is the working implementation brief for agents extending Bir's
evaluation layer. The project is not currently optimizing for a public package
release. Treat this document as the source of truth for finishing evaluator
work through small, tested, local-first slices.

## Current State

The first evaluator slice exists in `packages/python-sdk/src/bir/evals.py`.

Implemented SDK pieces:

- `EvalResult`
- `DeterministicEvaluator`
- `exact_match()`
- `contains()`
- `regex_match()`
- `json_valid()`
- `Dataset`
- `DatasetExample`
- `Dataset.from_jsonl()`
- `Dataset.to_jsonl()`
- `run_experiment()`
- JSONL experiment result writing
- JSON experiment summary writing
- `load_experiment()`
- `load_experiment_summary()`
- `list_experiments()`
- aggregate score calculation

Current tests live in `packages/python-sdk/tests/test_evals.py`.

Important current constraints:

- Evaluators are local and deterministic.
- Evaluators must not require an LLM provider.
- Evaluators must not require the FastAPI server.
- Evaluators must preserve redaction and safe JSON serialization.
- Experiment storage is JSONL-first.
- Async support is not required until the sync API stabilizes.

## Product Goal

The evaluator layer should let a Python developer run a task over a small local
dataset, compute deterministic scores, inspect failures, and later compare
experiments in the dashboard.

Target developer experience:

```python
from bir.evals import Dataset, contains, exact_match, run_experiment

dataset = Dataset.from_jsonl("questions.jsonl")

result = run_experiment(
    "prompt-v2",
    dataset=dataset,
    task=answer_question,
    evaluators=[
        contains(),
        exact_match("Bir is an LLM observability SDK."),
    ],
)

print(result.aggregate_scores)
```

The evaluator layer should eventually support:

- deterministic correctness checks
- JSON and structured output checks
- latency and cost thresholds
- RAG context checks
- custom Python evaluators
- persisted experiment summaries
- dashboard experiment list and detail views
- comparison between experiments

Do not jump directly to LLM-as-judge. Provider-backed evaluators introduce
credentials, cost, latency, prompt management, retries, and safety issues. They
should come after deterministic local evaluation is mature.

## Non-Goals

Do not add these while completing the evaluator layer:

- hosted evaluation services
- auth, teams, organizations, or RBAC
- queues or distributed workers
- new databases before JSONL becomes limiting
- provider-specific pricing tables
- vector database integrations
- broad plugin systems
- LLM-as-judge by default
- automatic telemetry to third parties

## Core Concepts

### Dataset

A dataset is a local JSONL file containing examples. The minimum row shape is:

```json
{"id":"q1","input":{"question":"What is Bir?"},"expected":"An observability SDK"}
```

Recommended row fields:

- `id`: required stable string identifier
- `input`: required task input; object inputs are passed as keyword arguments
- `expected`: optional expected output or assertion target
- `metadata`: optional JSON object for tags, split, difficulty, source, etc.

Dataset rules:

- Validate that every row is a JSON object.
- Require non-empty string `id`.
- Require `input`.
- Keep `expected` optional.
- Keep `metadata` optional and object-shaped.
- Preserve raw task input in memory so the task receives the real value.
- Redact persisted dataset and experiment output where appropriate.

### Evaluator

An evaluator computes an `EvalResult` from task output and expected data.

Current internal shape:

```python
EvalResult(
    name="exact_match",
    value=1.0,
    metadata={"expected": "Paris"},
)
```

Evaluator rules:

- Return finite numeric scores.
- Use `1.0` for pass and `0.0` for fail for binary checks.
- Include compact metadata that helps debug failures.
- Redact secret-like values in persisted metadata.
- Raise clear `TypeError` or `ValueError` for invalid evaluator configuration.
- Avoid hidden network calls.
- Avoid dependency-heavy implementations.

### Experiment

An experiment runs a task over a dataset and writes one JSONL result row per
example.

Current result row includes:

- `experiment_id`
- `experiment_name`
- per-example result `id`
- `example_id`
- `input`
- `expected`
- `output`
- `scores`
- `start_time`
- `end_time`
- `duration_ms`
- `status`
- `error`

Experiment rules:

- Keep result rows append/read friendly.
- Write results to `.bir/experiments/*.jsonl` by default.
- Allow custom `path`.
- Preserve `raise_on_error=True` default during development.
- Support `raise_on_error=False` for collecting failed examples.
- Never write raw secrets to experiment JSONL.

## Implementation Principles

Use these principles for every evaluator change:

- Prefer standard library.
- Keep public API small.
- Add one vertical slice at a time.
- Include SDK tests for all new behavior.
- Update docs when public behavior changes.
- Preserve sync-first design.
- Preserve JSONL-first storage.
- Preserve opt-in capture and redaction.
- Keep server/dashboard work separate unless the task explicitly includes it.
- Do not refactor unrelated SDK tracing behavior.

## Recommended Completion Plan

### Phase 1: Harden The Existing Evaluator Core

Goal: make the current `bir.evals` API stable enough for more evaluator types.

Tasks:

1. Audit current public names in `bir.evals.__all__`.
2. Ensure `EvalResult` rejects bool, non-number, NaN, and infinity values.
3. Ensure evaluator metadata is JSON-compatible and redacted.
4. Add tests for invalid evaluator configuration.
5. Add tests for empty datasets.
6. Add tests for duplicate dataset example IDs.
7. Add tests for non-object dataset rows.
8. Add tests for failed examples with `raise_on_error=True` and
   `raise_on_error=False`.
9. Add docs showing `Dataset.from_jsonl()` and custom output paths.

Acceptance checks:

```bash
cd packages/python-sdk
PYTHONPATH=src ../../.venv/bin/python -m unittest discover -s tests

cd ../..
./.venv/bin/pyright
```

### Phase 2: Add Threshold Evaluators

Goal: support common operational gates without needing dashboard work.

Add these evaluators:

```python
latency_under(max_ms: float, *, name: str = "latency_under")
cost_under(max_cost: float, *, field: str = "total_cost", name: str = "cost_under")
numeric_between(min_value: float | None = None, max_value: float | None = None, *, name: str = "numeric_between")
```

Design notes:

- `latency_under()` should evaluate experiment result duration when used in an
  experiment context. If the current evaluator interface cannot access
  duration, introduce a small `EvaluationContext` rather than overloading
  `output`.
- `cost_under()` should operate on explicit user-provided cost values only. Do
  not add provider pricing tables.
- `numeric_between()` can evaluate numeric outputs or named numeric fields in
  structured outputs.

Preferred context shape:

```python
@dataclass(frozen=True)
class EvaluationContext:
    example: DatasetExample
    output: Any
    duration_ms: float
    metadata: dict[str, Any]
```

Migration rule:

- Keep existing evaluators working.
- If context is introduced, adapt current evaluators internally without forcing
  user-facing API churn.

Tests:

- passing and failing latency thresholds
- invalid negative thresholds
- bool rejection for numeric thresholds
- missing cost fields
- non-numeric cost fields
- redaction in failure metadata

### Phase 3: Add Structured Output Evaluators

Goal: make evals useful for JSON/tool-output workflows.

Add evaluators:

```python
json_schema(schema: Mapping[str, Any], *, name: str = "json_schema")
field_equals(path: str, expected: Any, *, name: str = "field_equals")
field_contains(path: str, expected: str, *, case_sensitive: bool = True, name: str = "field_contains")
```

Implementation guidance:

- Prefer a tiny internal JSON-path subset over adding a dependency.
- Support dot paths and list indices only, for example:
  - `answer`
  - `usage.total_tokens`
  - `items[0].name`
- For `json_schema`, first consider whether a minimal internal validator is
  enough. If full JSON Schema is required, do not add a dependency without a
  short justification in docs or commit notes.
- Return useful metadata such as missing path, expected value, actual value, or
  validation message.

Tests:

- nested field success
- missing field failure
- list index success and out-of-range failure
- invalid path syntax
- schema success/failure if implemented

### Phase 4: Add Custom Evaluator Support

Goal: let users write small local evaluators without subclassing internals.

Target API:

```python
from bir.evals import custom_evaluator

has_citation = custom_evaluator(
    "has_citation",
    lambda output, expected: 1.0 if "[1]" in output else 0.0,
)
```

Better API if `EvaluationContext` exists:

```python
has_citation = custom_evaluator(
    "has_citation",
    lambda ctx: 1.0 if "[1]" in str(ctx.output) else 0.0,
)
```

Rules:

- Validate evaluator name.
- Accept return values as:
  - `int` or `float`
  - `bool`, converted to `1.0` or `0.0` only if explicitly documented
  - `EvalResult`
- Convert plain numeric returns into `EvalResult`.
- Redact metadata from returned `EvalResult`.
- Let exceptions surface in development mode unless experiment error handling is
  configured to continue.

Tests:

- numeric return
- `EvalResult` return
- bool behavior if supported
- exception propagation
- redaction in metadata

### Phase 5: Add RAG-Specific Deterministic Evaluators

Goal: support retrieval quality and faithfulness checks without LLM judges.

Add evaluators:

```python
retrieved_context_contains(expected: str, *, name: str = "retrieved_context_contains")
answer_contains_citation(*, name: str = "answer_contains_citation")
answer_context_overlap(min_ratio: float, *, name: str = "answer_context_overlap")
```

Input assumptions:

- RAG tasks may return a plain answer string.
- RAG tasks may return a structured dict such as:

```python
{
    "answer": "...",
    "contexts": ["doc text", "other doc text"],
    "citations": ["doc-1"],
}
```

Guidance:

- Keep these evaluators deterministic and clearly limited.
- Document that overlap is a heuristic, not proof of faithfulness.
- Do not add embeddings, vector stores, or LLM judges.
- Prefer explicit structured outputs in docs.

Tests:

- no context
- empty context
- answer with high overlap
- answer with low overlap
- citations present/missing
- redaction in context metadata

### Phase 6: Persist Experiment Summaries

Status: implemented in the SDK. Keep this section as the contract for future
server and dashboard work.

Goal: make dashboard and CLI inspection easier without scanning every row for
basic information.

Bir writes a summary artifact next to result JSONL:

```text
.bir/experiments/
  prompt-v2-<id>.jsonl
  prompt-v2-<id>.summary.json
```

Summary fields:

- `schema_version`
- `experiment_id`
- `name`
- `start_time`
- `end_time`
- `status`
- `example_count`
- `error_count`
- `aggregate_scores`
- `result_path`
- optional `metadata`

Rules:

- Keep summary JSON-compatible.
- Redact metadata.
- Do not break existing JSONL result writing.
- Loaders:

```python
load_experiment(path: str | Path) -> ExperimentResult
load_experiment_summary(path: str | Path) -> ExperimentSummary
list_experiments(directory: str | Path = ".bir/experiments") -> list[ExperimentSummary]
```

Tests:

- summary written after successful experiment
- summary written after continued errors
- loading missing file
- loading malformed JSON
- list ordering by start time descending

### Phase 7: Server Experiment Endpoints

Goal: expose local experiment artifacts through FastAPI for the dashboard.

Add endpoints only after SDK summary/loaders exist.

Suggested endpoints:

```text
GET /v1/experiments
GET /v1/experiments/{experiment_id}
```

Storage guidance:

- Read from local `.bir/experiments` JSONL/summary files.
- Allow path override with an environment variable:

```bash
BIR_EXPERIMENT_STORE=.bir/experiments
```

Validation:

- Use Pydantic schemas for summaries and detail responses.
- Redact values before returning if any loader path can bypass SDK redaction.
- Return 404 for missing experiment.
- Do not add auth.
- Do not add a database in this phase.

Tests:

- empty experiment list
- list summaries
- detail response with result rows
- missing experiment 404
- malformed experiment file handling

### Phase 8: Dashboard Experiment Views

Goal: make local evaluator work visible to users.

Initial screens:

- experiment list
- experiment detail
- aggregate score table
- per-example result table
- failed examples filter
- output/expected side-by-side panel

Dashboard rules:

- Keep the UI compact and debugging-oriented.
- Reuse existing trace dashboard visual language.
- Do not build a marketing page.
- Do not add charts until the table view is useful.
- Keep long payloads in scrollable `pre` blocks.
- Make failure scanning easy.

Suggested UI fields:

- experiment name
- status
- start time
- example count
- error count
- aggregate scores
- example id
- per-example status
- per-example scores
- duration
- error message
- input/expected/output payloads

Tests:

- TypeScript contract parser tests against fixture experiment JSON.
- Missing/invalid fields are ignored safely.
- Failed examples are surfaced.
- Aggregate scores render with stable formatting.

### Phase 9: Experiment Comparison

Goal: compare two experiment runs without adding a database.

Initial comparison behavior:

- select baseline and candidate experiment summaries
- align rows by `example_id`
- show score deltas
- show changed pass/fail status
- show regressions first

Rules:

- Keep comparison local and file-based.
- Do not add statistical significance calculations yet.
- Do not add hosted state.

Tests:

- matching example IDs
- missing example in candidate
- new example in candidate
- improved score
- regressed score
- unchanged score

## Public API Guidelines

Prefer this namespace:

```python
from bir.evals import Dataset, DatasetExample, exact_match, run_experiment
```

Do not export evaluator helpers from `bir.__init__` yet. Keeping them under
`bir.evals` prevents the root SDK API from growing too quickly.

Naming rules:

- Use snake_case.
- Evaluator factories should read as predicates or metrics:
  - `exact_match`
  - `contains`
  - `latency_under`
  - `json_valid`
- Result names should default to the factory name.
- Allow `name=` override for custom score names.

Error behavior:

- Misconfigured evaluator: raise `TypeError` or `ValueError`.
- Task failure with `raise_on_error=True`: write the failed row, then re-raise.
- Task failure with `raise_on_error=False`: write failed row and continue.
- Evaluator failure: treat like task failure unless a future explicit option
  separates task failures from evaluator failures.

## Serialization Rules

Every persisted evaluator or experiment artifact must be JSON-compatible.

Allowed values:

- `None`
- `str`
- `bool`
- finite `int`
- finite `float`
- lists of allowed values
- dicts with string keys and allowed values

Rules:

- Use `json.dumps(..., allow_nan=False)`.
- Redact common secret-like keys and text patterns.
- Do not write raw exceptions with secrets.
- Include schema versions once experiment summaries are introduced.
- Keep JSONL row shapes append-friendly.

## Security And Privacy

The evaluator layer handles model inputs, outputs, expected answers, retrieved
context, and errors. These may contain sensitive data.

Required behavior:

- Do not log API keys.
- Do not hardcode secrets.
- Do not commit `.env` files.
- Redact common secret-like keys.
- Redact common secret-like text patterns.
- Keep capture explicit for verbose prompt/context fields.
- Do not send eval data to third-party services.

When adding LLM-as-judge later:

- Require explicit provider configuration.
- Keep provider credentials outside datasets and experiment files.
- Document cost and privacy implications.
- Add tests that no credentials are persisted.

## Test Matrix

For every evaluator feature, add tests for:

- pass case
- fail case
- invalid configuration
- non-finite numeric input where relevant
- bool rejection where numeric values are expected
- JSON serialization
- redaction of secret-like data
- experiment integration

For dataset changes, add tests for:

- valid JSONL load
- invalid JSON
- non-object row
- missing required field
- optional metadata
- writing JSONL
- redaction in persisted output

For server changes, add tests for:

- validation
- empty state
- malformed file handling
- 404 behavior
- response redaction

For dashboard changes, add tests for:

- parser normalization
- malformed payload handling
- aggregate score formatting
- failed result visibility
- stable ordering

## Standard Commands

SDK:

```bash
cd packages/python-sdk
PYTHONPATH=src ../../.venv/bin/python -m unittest discover -s tests
```

Backend:

```bash
cd apps/server
../../.venv/bin/python -m pytest
```

Python type checking:

```bash
./.venv/bin/pyright
```

Web:

```bash
cd apps/web
npm run lint
npm run typecheck
npm run test
```

Release-candidate quality check:

```bash
./.venv/bin/python packages/python-sdk/scripts/verify_release.py
```

Run only checks whose dependencies are already installed. Do not install new
dependencies unless the task explicitly requires it.

## Definition Of Done

An evaluator slice is complete when:

- the public API is documented
- SDK tests cover the evaluator behavior
- experiment integration is tested if applicable
- persisted outputs are redacted and JSON-compatible
- `pyright` passes for Python changes
- dashboard parser tests are added for dashboard changes
- server tests are added for server endpoint changes
- no unrelated release/publishing work is introduced

## Recommended Next Tasks

The next agents should prefer these small tasks:

1. Add `latency_under()` with an `EvaluationContext` if needed.
2. Add `field_equals()` for structured JSON/dict outputs.
3. Add `custom_evaluator()` for simple user-defined checks.
4. Add FastAPI experiment list/detail endpoints using SDK summaries.
5. Add dashboard experiment list/detail views.

Each task should stay commit-sized and should not start publishing work.
