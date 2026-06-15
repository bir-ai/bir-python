"""Deterministic local evaluation experiment for the Bir SDK.

Unlike the tracing demos, this example exercises the eval path: it loads a small
dataset, runs a fully local task (no network or LLM), scores each example with
two built-in evaluators, and persists the per-example results plus an aggregate
summary under ``.bir/experiments/``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from bir.evals import Dataset, ExperimentResult, contains, exact_match, run_experiment, send_experiment

HERE = Path(__file__).resolve().parent
DEFAULT_DATASET_PATH = HERE / "dataset.jsonl"
DEFAULT_EXPERIMENT_PATH = HERE / ".bir" / "experiments" / "faq-eval.jsonl"

# A tiny local "knowledge base" keyed by the topic word each answer covers.
KNOWLEDGE_BASE = {
    "trace": "A trace is one full LLM workflow execution recorded by Bir.",
    "span": "A span is a nested operation inside a Bir trace.",
    "generation": "A generation is a single LLM call recorded by Bir.",
    "score": "An eval score is a quality or safety metric attached to a Bir trace.",
}

FALLBACK_ANSWER = "I don't have a local answer for that question yet."


def answer_question(question: str) -> str:
    """Answer a question from the local knowledge base (no network or LLM)."""

    words = {word.strip(".,?!").lower() for word in question.split()}
    for topic, answer in KNOWLEDGE_BASE.items():
        if topic in words:
            return answer
    return FALLBACK_ANSWER


def run_eval(
    *,
    dataset_path: str | Path = DEFAULT_DATASET_PATH,
    experiment_path: str | Path = DEFAULT_EXPERIMENT_PATH,
) -> ExperimentResult:
    """Load the dataset and run the local task with two built-in evaluators."""

    dataset = Dataset.from_jsonl(dataset_path)
    return run_experiment(
        "faq-eval",
        dataset=dataset,
        task=answer_question,
        # exact_match uses each example's expected answer; contains checks that
        # the answer mentions the product, so the two scores can diverge.
        evaluators=[exact_match(), contains("Bir")],
        path=experiment_path,
    )


def main() -> None:
    """Run the experiment from the command line."""

    parser = argparse.ArgumentParser(description="Run a local Bir experiment over a small FAQ dataset.")
    parser.add_argument(
        "--dataset-path",
        default=str(DEFAULT_DATASET_PATH),
        help="JSONL dataset to evaluate.",
    )
    parser.add_argument(
        "--experiment-path",
        default=str(DEFAULT_EXPERIMENT_PATH),
        help="JSONL experiment output path.",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Send the experiment results to the Bir FastAPI server after writing them locally.",
    )
    parser.add_argument(
        "--server-url",
        default="http://127.0.0.1:8000",
        help="Bir server URL used with --send.",
    )
    args = parser.parse_args()

    result = run_eval(dataset_path=args.dataset_path, experiment_path=args.experiment_path)

    for example_result in result.results:
        scores = ", ".join(f"{score.name}={score.value:.2f}" for score in example_result.scores)
        print(f"{example_result.example_id}: {example_result.status} ({scores or 'no scores'})")

    print(f"wrote {len(result.results)} example results to {result.path}")
    print(f"experiment_id={result.id} status={result.status}")
    print("aggregate scores:")
    for name, value in result.aggregate_scores.items():
        print(f"  {name}: {value:.2f}")

    if args.send:
        send_result = send_experiment(result.path, args.server_url)
        print(f"sent experiment {send_result.experiment_id} ({send_result.accepted} rows) to {args.server_url}")


if __name__ == "__main__":
    main()
