# Local Evaluation Demo

This demo runs a Bir experiment end to end without an LLM, an API key, or any
extra dependencies. It exercises the evaluation path the tracing demos do not:

1. load a dataset from JSONL
2. run a deterministic local task over every example
3. score each example with two built-in evaluators
4. write per-example results and an aggregate summary to `.bir/experiments/`
5. send the experiment to the FastAPI server and view it in the dashboard

The task is a tiny FAQ lookup over a fixed knowledge base in `demo.py`. It uses
two evaluators so they can disagree on purpose: `exact_match()` checks the
answer against each example's expected text, while `contains("Bir")` only checks
that the answer mentions the product. On the shipped dataset that yields an
`exact_match` mean of 0.60 and a `contains` mean of 0.80.

## Run The Demo

From this directory:

```bash
PYTHONPATH=../../packages/python-sdk/src python3 demo.py
```

The demo writes two files:

```text
.bir/experiments/faq-eval.jsonl          # one row per example, with scores
.bir/experiments/faq-eval.summary.json   # aggregate scores and run metadata
```

To inspect the persisted experiment from Python:

```bash
PYTHONPATH=../../packages/python-sdk/src python3 - <<'PY'
from bir.evals import load_experiment

experiment = load_experiment(".bir/experiments/faq-eval.jsonl")
print(experiment.name, experiment.status, experiment.aggregate_scores)
for result in experiment.results:
    scores = {score.name: score.value for score in result.scores}
    print(" ", result.example_id, result.status, scores)
PY
```

## Send To The Server

Install the server dependencies if `uvicorn` is not already available:

```bash
cd ../../apps/server
python3 -m pip install -e ".[dev]"
cd ../../examples/eval-demo
```

Start the server in another terminal:

```bash
cd ../../apps/server
uvicorn app.main:app --reload
```

Then send the recorded experiment:

```bash
PYTHONPATH=../../packages/python-sdk/src python3 demo.py --send
```

The server accepts the experiment at `/v1/experiments` and lists it at
`GET /v1/experiments`.

## View In The Dashboard

Install the web dependencies if they are not already available:

```bash
cd ../../apps/web
npm install
cd ../../examples/eval-demo
```

Start the dashboard in another terminal:

```bash
cd ../../apps/web
npm run dev
```

Open `http://localhost:3000` and use the experiments view to see the run and its
aggregate scores. By default the dashboard reads from `http://127.0.0.1:8000`.
