"""CI smoke tests for the offline example demos in ``examples/``.

These tests guard the demos against silent breakage when the Bir SDK's public
API changes. The example directories use hyphenated names and ship no
``__init__.py``, so they are not importable packages; each demo module is loaded
directly from its file path with ``importlib.util.spec_from_file_location``.

Only the fully offline demos are exercised here:

* ``examples/openai-demo/demo.py`` is simulated end to end (no network).
* ``examples/langchain-demo/demo.py`` drives the dependency-free
  ``BirCallbackHandler`` through a LangChain-shaped callback lifecycle.
* ``examples/eval-demo/demo.py`` runs a deterministic local experiment over a
  small JSONL dataset (no network).

``examples/ollama-demo/demo.py`` is intentionally excluded: it makes real HTTP
calls to a live Ollama model server on 127.0.0.1:11434, which is not available
in CI.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

import bir
from bir import evals

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture(autouse=True)
def reset_sdk_state() -> Iterator[None]:
    """Keep each test hermetic by resetting global SDK config afterwards."""

    try:
        yield
    finally:
        bir._sdk._reset_config_for_tests()


def _load_demo(example_dir: str, module_name: str) -> ModuleType:
    """Load an example ``demo.py`` by file path (examples are not packages)."""

    demo_path = EXAMPLES_DIR / example_dir / "demo.py"
    spec = importlib.util.spec_from_file_location(module_name, demo_path)
    assert spec is not None and spec.loader is not None, f"cannot load {demo_path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_openai_demo_records_offline_trace(tmp_path: Path) -> None:
    """The simulated OpenAI demo records one trace with its real event types."""

    module = _load_demo("openai-demo", "bir_example_openai_demo")
    trace_path = tmp_path / "traces.jsonl"
    bir.configure(trace_path=trace_path, capture_inputs=True, capture_outputs=True)

    # Call the traced entry point directly; main() would parse pytest's argv.
    module.answer_question("How does Bir help with LLM observability?")

    traces = bir.load_traces(trace_path)
    assert len(traces) == 1
    recorded = traces[0]

    roots = [event for event in recorded.events if event.type == "trace"]
    assert len(roots) == 1
    assert recorded.root.type == "trace"

    # retrieve_context/draft_answer spans, a retrieval tool_call, an LLM
    # generation, and a helpfulness score.
    event_types = {event.type for event in recorded.events}
    assert event_types == {"trace", "span", "generation", "tool_call", "score"}


def test_langchain_demo_records_offline_trace(tmp_path: Path) -> None:
    """The dependency-free LangChain callback demo records one trace."""

    module = _load_demo("langchain-demo", "bir_example_langchain_demo")
    trace_path = tmp_path / "traces.jsonl"
    bir.configure(trace_path=trace_path, capture_inputs=True, capture_outputs=True)

    # Drive the callback lifecycle directly; main() would parse pytest's argv.
    module.run_callback_lifecycle("How does Bir work with LangChain?")

    traces = bir.load_traces(trace_path)
    assert len(traces) == 1
    recorded = traces[0]

    roots = [event for event in recorded.events if event.type == "trace"]
    assert len(roots) == 1
    assert recorded.root.type == "trace"

    # The chain root, retriever and tool tool_calls, and an LLM generation.
    event_types = {event.type for event in recorded.events}
    assert event_types == {"trace", "generation", "tool_call"}

    # The retriever callback specifically records a retrieval-kind tool_call.
    assert any(
        event.type == "tool_call" and event.metadata.get("kind") == "retrieval"
        for event in recorded.events
    )


def test_eval_demo_runs_offline_experiment(tmp_path: Path) -> None:
    """The eval demo runs a deterministic experiment and persists scored results."""

    module = _load_demo("eval-demo", "bir_example_eval_demo")
    experiment_path = tmp_path / "faq-eval.jsonl"

    # Use the shipped dataset; redirect output into tmp_path to stay hermetic.
    result = module.run_eval(experiment_path=experiment_path)

    # Every dataset example runs without error.
    assert result.status == "success"
    assert len(result.results) == 5
    assert all(example_result.status == "success" for example_result in result.results)

    # Both built-in evaluators score every example; on the shipped dataset the
    # task is exactly right 3/5 times but mentions the product 4/5 times.
    assert set(result.aggregate_scores) == {"exact_match", "contains"}
    assert result.aggregate_scores["exact_match"] == pytest.approx(0.6)
    assert result.aggregate_scores["contains"] == pytest.approx(0.8)

    # Results and the aggregate summary persist and reload cleanly.
    reloaded = evals.load_experiment(experiment_path)
    assert reloaded.id == result.id
    assert len(reloaded.results) == 5

    summary = evals.load_experiment_summary(experiment_path.with_suffix(".summary.json"))
    assert summary.example_count == 5
    assert summary.error_count == 0
    assert summary.aggregate_scores["exact_match"] == pytest.approx(0.6)
