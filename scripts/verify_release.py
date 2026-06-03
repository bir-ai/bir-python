from __future__ import annotations

import os
import re
import base64
import hashlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import venv
import zipfile
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[1]


def main() -> int:
    version = package_version()
    with tempfile.TemporaryDirectory(prefix="bir-sdk-release-") as temp_dir_text:
        temp_dir = Path(temp_dir_text)
        wheelhouse = temp_dir / "wheelhouse"
        smoke_dir = temp_dir / "smoke"
        smoke_env = temp_dir / "venv"

        wheelhouse.mkdir()
        smoke_dir.mkdir()

        run_sdk_tests()
        run_pyright()
        wheel = build_wheel(wheelhouse, version)
        inspect_wheel(wheel)
        run_install_smoke_test(smoke_env, smoke_dir, wheel)

    print("Bir SDK release verification passed.")
    return 0


def run_sdk_tests() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=PACKAGE_ROOT,
        env=env,
        label="SDK unit tests",
    )


def run_pyright() -> None:
    pyright = REPO_ROOT / ".venv" / "bin" / "pyright"
    if not pyright.exists():
        resolved = shutil.which("pyright")
        if resolved is None:
            raise RuntimeError("pyright is required for release verification but was not found")
        pyright = Path(resolved)
    run([str(pyright)], cwd=REPO_ROOT, label="pyright")


def build_wheel(wheelhouse: Path, version: str) -> Path:
    print("==> wheel build", flush=True)
    wheel = wheelhouse / f"bir-{version}-py3-none-any.whl"
    dist_info = f"bir-{version}.dist-info"
    records: list[tuple[str, str, int]] = []

    def write_file(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
        archive.writestr(name, data)
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode("ascii").rstrip("=")
        records.append((name, f"sha256={digest}", len(data)))

    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sorted((PACKAGE_ROOT / "src" / "bir").glob("*.py")):
            write_file(archive, f"bir/{source.name}", source.read_bytes())

        write_file(archive, f"{dist_info}/METADATA", metadata(version).encode("utf-8"))
        write_file(
            archive,
            f"{dist_info}/WHEEL",
            textwrap.dedent(
                """\
                Wheel-Version: 1.0
                Generator: bir verify_release.py
                Root-Is-Purelib: true
                Tag: py3-none-any
                """
            ).encode("utf-8"),
        )

        record_lines = [f"{name},{digest},{size}" for name, digest, size in records]
        record_lines.append(f"{dist_info}/RECORD,,")
        archive.writestr(f"{dist_info}/RECORD", "\n".join(record_lines) + "\n")

    return wheel


def metadata(version: str) -> str:
    pyproject = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    readme = (PACKAGE_ROOT / "README.md").read_text(encoding="utf-8")
    description = required_string(pyproject, "description")
    requires_python = required_string(pyproject, "requires-python")
    headers = "\n".join(
        [
            "Metadata-Version: 2.3",
            "Name: bir",
            f"Version: {version}",
            f"Summary: {description}",
            f"Requires-Python: {requires_python}",
            "License: FSL-1.1-ALv2",
            "Description-Content-Type: text/markdown",
        ]
    )
    return f"{headers}\n\n{readme}\n"


def inspect_wheel(wheel: Path) -> None:
    forbidden_parts = {
        ".bir",
        ".env",
        ".pytest_cache",
        "__pycache__",
        "build",
        "dist",
    }
    required_files = {
        "bir/__init__.py",
        "bir/_sdk.py",
    }

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())

    missing = required_files.difference(names)
    if missing:
        raise RuntimeError(f"wheel is missing expected SDK files: {sorted(missing)}")

    for name in names:
        parts = set(Path(name).parts)
        if forbidden_parts.intersection(parts):
            raise RuntimeError(f"wheel contains forbidden local/generated path: {name}")


def run_install_smoke_test(smoke_env: Path, smoke_dir: Path, wheel: Path) -> None:
    venv.EnvBuilder(with_pip=True).create(smoke_env)
    smoke_python = smoke_env / "bin" / "python"
    install_env = os.environ.copy()
    install_env["PIP_NO_CACHE_DIR"] = "1"
    run(
        [str(smoke_python), "-m", "pip", "install", "--no-index", str(wheel)],
        cwd=smoke_dir,
        env=install_env,
        label="fresh venv wheel install",
    )

    smoke_test = smoke_dir / "smoke_test.py"
    smoke_test.write_text(
        textwrap.dedent(
            """
            from bir import configure, generation, load_traces, observe, prompt, retrieval, score, span
            from bir.evals import Dataset, DatasetExample, contains, exact_match, run_experiment

            configure(capture_inputs=True, capture_outputs=True)

            @observe()
            def answer(question: str) -> str:
                with span("retrieve_context"):
                    with retrieval("vector_search", query=question) as result:
                        result.add_document(id="doc-1", rank=1, score=0.82, text="local context")
                answer_prompt = prompt(
                    "answer_question",
                    version="v1",
                    template="Answer {question}",
                    variables={"question": question},
                )
                with generation("local.llm", model="demo-model", prompt=answer_prompt) as gen:
                    gen.set_output("ok")
                    gen.set_usage(input_tokens=1, output_tokens=2)
                    gen.set_cost(input_cost=0.000001, output_cost=0.000002)
                score("helpfulness", 0.9)
                return "ok"

            assert answer("hello") == "ok"
            trace = load_traces()[0]
            events = trace.events
            assert [event.type for event in events] == ["trace", "span", "tool_call", "generation", "score"]

            retrieval_event = next(event for event in events if event.name == "vector_search")
            assert retrieval_event.metadata["kind"] == "retrieval"
            assert retrieval_event.input == {"query": "hello"}
            assert retrieval_event.output == {
                "documents": [
                    {"id": "doc-1", "rank": 1, "score": 0.82, "text": "local context"}
                ]
            }

            generation_event = next(event for event in events if event.type == "generation")
            assert generation_event.metadata["prompt"]["name"] == "answer_question"
            assert generation_event.metadata["prompt"]["version"] == "v1"
            assert "template_sha256" in generation_event.metadata["prompt"]
            assert "rendered" not in generation_event.metadata["prompt"]
            assert generation_event.model == "demo-model"
            assert generation_event.usage == {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}
            assert generation_event.cost == {
                "input_cost": 0.000001,
                "output_cost": 0.000002,
                "total_cost": 0.000003,
            }
            assert generation_event.currency == "USD"

            dataset = Dataset([DatasetExample(id="q1", input={"question": "hello"}, expected="ok")])
            experiment = run_experiment(
                "smoke",
                dataset=dataset,
                task=answer,
                evaluators=[exact_match(), contains("o")],
            )
            assert experiment.status == "success"
            assert experiment.aggregate_scores == {"contains": 1.0, "exact_match": 1.0}
            """
        ),
        encoding="utf-8",
    )
    run([str(smoke_python), str(smoke_test)], cwd=smoke_dir, label="fresh venv smoke test")


def package_version() -> str:
    pyproject = PACKAGE_ROOT / "pyproject.toml"
    return required_string(pyproject.read_text(encoding="utf-8"), "version")


def required_string(text: str, key: str) -> str:
    match = re.search(rf'^{re.escape(key)}\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match is None:
        raise RuntimeError(f"could not find {key} in pyproject.toml")
    return match.group(1)


def run(
    command: list[str],
    *,
    cwd: Path,
    label: str,
    env: dict[str, str] | None = None,
) -> None:
    print(f"==> {label}", flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
