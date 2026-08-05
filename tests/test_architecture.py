"""Architectural compatibility checks for Bir's internal module boundaries.

These tests intentionally exercise only public behavior and historical public
object identities. Internal helpers may move freely as long as these observable
contracts remain unchanged.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import pickle
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

import bir
import bir._sdk as sdk
import bir.cli as cli
import bir.evals as evals
import bir.integrations as integrations

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

BIR_ALL = [
    "__version__",
    "TraceEvent",
    "LoadedTrace",
    "SendEventsResult",
    "PromptRecord",
    "configure",
    "load_events",
    "load_traces",
    "send_events",
    "observe",
    "trace",
    "prompt",
    "span",
    "generation",
    "tool_call",
    "retrieval",
    "score",
    "get_current_trace_id",
    "get_current_span_id",
]

EVALS_ALL = [
    "Dataset",
    "DatasetExample",
    "DeterministicEvaluator",
    "EvaluationContext",
    "EvalResult",
    "ExperimentDiff",
    "ExperimentExampleResult",
    "ExperimentResult",
    "ExperimentSummary",
    "SendExperimentResult",
    "answer_contains_citation",
    "answer_context_overlap",
    "contains",
    "compare_experiments",
    "cost_under",
    "custom_evaluator",
    "exact_match",
    "field_contains",
    "field_equals",
    "json_valid",
    "latency_under",
    "list_experiments",
    "load_experiment",
    "load_experiment_summary",
    "numeric_between",
    "regex_match",
    "render_experiment_report",
    "retrieved_context_contains",
    "run_experiment",
    "run_experiment_async",
    "send_experiment",
    "similarity_above",
]

INTEGRATIONS_ALL = [
    "cohere",
    "export_traces_to_otlp",
    "trace_lm",
    "trace_lm_async",
    "trace_create",
    "trace_create_async",
    "trace_messages",
    "trace_messages_async",
    "trace_converse",
    "trace_converse_async",
    "trace_converse_stream",
    "trace_converse_stream_async",
    "trace_generate_content",
    "trace_generate_content_async",
    "trace_vertex_generate_content",
    "trace_vertex_generate_content_async",
    "BirHaystackTracer",
    "BirCallbackHandler",
    "BirLlamaIndexHandler",
    "BirAgentsTracingProcessor",
    "BirPydanticAIHandler",
    "BirCrewAIHandler",
    "BirAutoGenHandler",
    "trace_completion",
    "trace_completion_async",
    "trace_chat",
    "trace_chat_async",
    "trace_ollama_chat",
    "trace_ollama_chat_async",
    "trace_ollama_generate",
    "trace_ollama_generate_async",
    "trace_chat_completion",
    "trace_chat_completion_async",
    "trace_response",
    "trace_response_async",
]

OPTIONAL_PROVIDER_ROOTS = (
    "ag2",
    "agents",
    "anthropic",
    "autogen",
    "boto3",
    "botocore",
    "cohere",
    "crewai",
    "dspy",
    "google",
    "haystack",
    "instructor",
    "langchain",
    "langchain_core",
    "litellm",
    "llama_index",
    "llamaindex",
    "mistral",
    "mistralai",
    "ollama",
    "openai",
    "opentelemetry",
    "pydantic_ai",
    "vertexai",
)


def _subprocess_env() -> dict[str, str]:
    """Return an isolated environment that imports the working-tree package."""

    env = dict(os.environ)
    for name in tuple(env):
        if name.startswith("BIR_"):
            del env[name]
    existing_pythonpath = env.get("PYTHONPATH")
    pythonpath = [str(SRC_ROOT)]
    if existing_pythonpath:
        pythonpath.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_python(code: str) -> subprocess.CompletedProcess[str]:
    """Run a fresh interpreter against the working tree."""

    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _console_script() -> Path:
    """Locate the installed ``bir`` launcher for the active interpreter."""

    executable_name = "bir.exe" if os.name == "nt" else "bir"
    candidates = [
        Path(sys.executable).parent / executable_name,
        Path(sysconfig.get_path("scripts")) / executable_name,
    ]
    discovered = shutil.which("bir")
    if discovered is not None:
        candidates.append(Path(discovered))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise AssertionError(f"installed bir console script was not found; checked {candidates!r}")


class PublicSurfaceTests(unittest.TestCase):
    """Public exports and historical object locations remain stable."""

    def test_exact_public_exports(self) -> None:
        self.assertEqual(bir.__all__, BIR_ALL)
        self.assertEqual(evals.__all__, EVALS_ALL)
        # The flat integration re-exports include aliases that exist only here
        # (``trace_vertex_generate_content``), so renaming one would go unnoticed
        # by the per-module checks in the stability suite.
        self.assertEqual(integrations.__all__, INTEGRATIONS_ALL)

    def test_top_level_sdk_re_exports_keep_identity(self) -> None:
        for name in BIR_ALL:
            if name == "__version__":
                continue
            with self.subTest(name=name):
                self.assertIs(getattr(bir, name), getattr(sdk, name))

    def test_public_callable_module_identities_are_compatible(self) -> None:
        for name in BIR_ALL:
            if name == "__version__":
                continue
            with self.subTest(module="bir", name=name):
                self.assertEqual(getattr(bir, name).__module__, "bir._sdk")

        for name in EVALS_ALL:
            with self.subTest(module="bir.evals", name=name):
                self.assertEqual(getattr(evals, name).__module__, "bir.evals")

    def test_public_class_member_module_identities_are_compatible(self) -> None:
        for module, names, expected_module in (
            (sdk, BIR_ALL, "bir._sdk"),
            (evals, EVALS_ALL, "bir.evals"),
        ):
            for name in names:
                if name == "__version__":
                    continue
                value = getattr(module, name)
                if not isinstance(value, type):
                    continue
                for member_name, raw_member in value.__dict__.items():
                    member = (
                        raw_member.__func__
                        if isinstance(raw_member, (classmethod, staticmethod))
                        else raw_member.fget
                        if isinstance(raw_member, property)
                        else raw_member
                    )
                    member_module = getattr(member, "__module__", None)
                    if member_module is None or member_module == "dataclasses":
                        continue
                    with self.subTest(class_name=name, member=member_name):
                        self.assertEqual(member_module, expected_module)

    def test_module_and_console_entry_points_share_cli_main(self) -> None:
        import bir.__main__ as module_entry

        distribution = importlib.metadata.distribution("bir-sdk")
        entry_points = [
            entry_point
            for entry_point in distribution.entry_points
            if entry_point.group == "console_scripts" and entry_point.name == "bir"
        ]

        self.assertEqual([entry_point.value for entry_point in entry_points], ["bir.cli:main"])
        self.assertIs(entry_points[0].load(), cli.main)
        self.assertIs(module_entry.main, cli.main)


class PublicPickleCompatibilityTests(unittest.TestCase):
    """Representative public dataclasses remain importable by their pickle names."""

    def assert_pickle_round_trip(self, value: Any) -> None:
        restored = pickle.loads(pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL))
        self.assertIs(type(restored), type(value))
        self.assertEqual(restored, value)

    def test_sdk_dataclasses_round_trip(self) -> None:
        event = sdk.TraceEvent(
            id="event-1",
            trace_id="trace-1",
            parent_id=None,
            name="request",
            type="trace",
            start_time="2026-08-01T10:00:00+00:00",
            end_time="2026-08-01T10:00:01+00:00",
            status="success",
            metadata={"service": {"name": "checkout"}},
            input={"request": "hello"},
            output={"response": "world"},
            error=None,
            raw={"schema_version": "1.0", "id": "event-1"},
        )
        values = [
            event,
            sdk.LoadedTrace(
                id="trace-1",
                name="request",
                start_time=event.start_time,
                end_time=event.end_time,
                status="success",
                events=[event],
                root=event,
            ),
            sdk.SendEventsResult(accepted=1, event_ids=["event-1"], attempted=1),
            sdk.PromptRecord(
                name="support-answer",
                version="v1",
                template="Answer {question}",
                variables={"question": "hello"},
                rendered=None,
                metadata={"team": "support"},
                capture_template=False,
                capture_variables=True,
                capture_rendered=False,
            ),
        ]

        for value in values:
            with self.subTest(class_name=type(value).__name__):
                self.assert_pickle_round_trip(value)

    def test_eval_dataclasses_round_trip(self) -> None:
        example = evals.DatasetExample(
            id="example-1",
            input={"question": "hello"},
            expected="world",
            metadata={"split": "test"},
        )
        score = evals.EvalResult(name="exact_match", value=1.0, metadata={"expected": "world"})
        example_result = evals.ExperimentExampleResult(
            id="result-1",
            example_id=example.id,
            input=example.input,
            expected=example.expected,
            output="world",
            scores=[score],
            start_time="2026-08-01T10:00:00+00:00",
            end_time="2026-08-01T10:00:01+00:00",
            status="success",
            error=None,
            trace_id="trace-1",
        )
        experiment = evals.ExperimentResult(
            id="experiment-1",
            name="support",
            start_time="2026-08-01T10:00:00+00:00",
            end_time="2026-08-01T10:00:01+00:00",
            status="success",
            results=[example_result],
            path="experiments/experiment-1.json",
        )
        values = [
            example,
            evals.Dataset([example]),
            score,
            evals.EvaluationContext(example=example, output="world", duration_ms=1000, metadata={"attempt": 1}),
            example_result,
            experiment,
            evals.ExperimentDiff(
                deltas={"exact_match": 0.0},
                regressed=frozenset(),
                improved=frozenset(),
                unchanged=frozenset({"exact_match"}),
                baseline_only=frozenset(),
                candidate_only=frozenset(),
                tolerance=0.0,
            ),
            evals.ExperimentSummary(
                schema_version="1.0",
                experiment_id=experiment.id,
                name=experiment.name,
                start_time=experiment.start_time,
                end_time=experiment.end_time,
                status=experiment.status,
                example_count=1,
                error_count=0,
                aggregate_scores={"exact_match": 1.0},
                result_path=experiment.path or "experiments/experiment-1.json",
            ),
            evals.SendExperimentResult(accepted=1, experiment_id=experiment.id),
        ]

        for value in values:
            with self.subTest(class_name=type(value).__name__):
                self.assert_pickle_round_trip(value)


class FreshImportTests(unittest.TestCase):
    """Imports complete in a fresh interpreter without optional providers."""

    def test_public_and_integration_modules_import_without_provider_sdks(self) -> None:
        code = textwrap.dedent(
            f"""
            import importlib
            import importlib.util
            import json
            import pkgutil
            import sys

            before = set(sys.modules)
            integration_spec = importlib.util.find_spec("bir.integrations")
            assert integration_spec is not None
            assert integration_spec.submodule_search_locations is not None
            integration_modules = sorted(
                module.name
                for module in pkgutil.iter_modules(
                    integration_spec.submodule_search_locations,
                    "bir.integrations.",
                )
            )
            assert integration_modules

            requested = ["bir", "bir.evals", "bir.cli", "bir.integrations", *integration_modules]
            for module_name in requested:
                module = importlib.import_module(module_name)
                assert module.__name__ == module_name
                assert module.__spec__ is not None
                assert not getattr(module.__spec__, "_initializing", False)

            introduced = set(sys.modules) - before
            provider_roots = {OPTIONAL_PROVIDER_ROOTS!r}
            imported_providers = sorted(
                root
                for root in provider_roots
                if any(name == root or name.startswith(root + ".") for name in introduced)
            )
            assert not imported_providers, imported_providers
            print(json.dumps(integration_modules))
            """
        )

        completed = _run_python(code)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        imported = json.loads(completed.stdout)
        self.assertTrue(imported)
        self.assertTrue(all(module_name.startswith("bir.integrations.") for module_name in imported))


class CliDispatchCompatibilityTests(unittest.TestCase):
    """The module and installed console launchers dispatch identically."""

    def test_module_and_console_script_outputs_match(self) -> None:
        console_script = _console_script()
        env = _subprocess_env()
        with tempfile.TemporaryDirectory(prefix="bir-cli-architecture-") as tmp_dir:
            env["BIR_TRACE_PATH"] = str(Path(tmp_dir) / "traces.jsonl")
            for arguments in ([], ["--help"], ["--version"]):
                with self.subTest(arguments=arguments):
                    module_result = subprocess.run(
                        [sys.executable, "-m", "bir", *arguments],
                        cwd=tmp_dir,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                    console_result = subprocess.run(
                        [str(console_script), *arguments],
                        cwd=tmp_dir,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )

                    self.assertEqual(module_result.returncode, console_result.returncode)
                    self.assertEqual(module_result.stdout, console_result.stdout)
                    self.assertEqual(module_result.stderr, console_result.stderr)


if __name__ == "__main__":
    unittest.main()
