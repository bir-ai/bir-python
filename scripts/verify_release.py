"""Run local packaging quality checks for the Bir Python SDK."""

from __future__ import annotations

import os
import re
import base64
import csv
import gzip
import hashlib
import importlib.util
import io
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import venv
import zipfile
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
# The SDK package now lives at the repository root, so the package root and the
# repository root are the same directory.
REPO_ROOT = PACKAGE_ROOT
PACKAGE_SOURCE = PACKAGE_ROOT / "src" / "bir"
REQUIRED_PACKAGE_FILES = {
    "bir/__init__.py",
    "bir/__main__.py",
    "bir/_sdk.py",
    "bir/cli.py",
    "bir/evals.py",
    "bir/integrations/__init__.py",
    "bir/integrations/_common.py",
    "bir/integrations/anthropic.py",
    "bir/integrations/autogen.py",
    "bir/integrations/bedrock.py",
    "bir/integrations/cohere.py",
    "bir/integrations/crewai.py",
    "bir/integrations/dspy.py",
    "bir/integrations/google.py",
    "bir/integrations/haystack.py",
    "bir/integrations/instructor.py",
    "bir/integrations/langchain.py",
    "bir/integrations/litellm.py",
    "bir/integrations/llamaindex.py",
    "bir/integrations/mistral.py",
    "bir/integrations/ollama.py",
    "bir/integrations/openai.py",
    "bir/integrations/openai_agents.py",
    "bir/integrations/otel.py",
    "bir/integrations/pydantic_ai.py",
    "bir/integrations/vertexai.py",
    "bir/logging.py",
    "bir/py.typed",
    "bir/testing.py",
}

# Path components that must never appear inside a published distribution: local
# trace output, dev environments, caches, and generated build trees. Shared by
# the wheel and sdist inspectors so both reject the same local/generated leaks.
FORBIDDEN_PATH_PARTS = {
    ".bir",
    ".env",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "site",
}


def main() -> int:
    """Run the full SDK verification workflow."""

    version = package_version()
    with tempfile.TemporaryDirectory(prefix="bir-sdk-release-") as temp_dir_text:
        temp_dir = Path(temp_dir_text)
        wheelhouse = temp_dir / "wheelhouse"
        smoke_dir = temp_dir / "smoke"
        smoke_env = temp_dir / "venv"
        sdist_dir = temp_dir / "sdist"
        sdist_smoke_dir = temp_dir / "sdist-smoke"
        sdist_env = temp_dir / "sdist-venv"
        backend_dir = temp_dir / "build-backend"

        wheelhouse.mkdir()
        smoke_dir.mkdir()
        sdist_dir.mkdir()
        sdist_smoke_dir.mkdir()

        run_sdk_tests()
        run_pyright()

        wheel = build_wheel(wheelhouse, version)
        inspect_wheel(wheel)
        run_install_smoke_test(smoke_env, smoke_dir, wheel, version)

        sdist = build_sdist(sdist_dir, version)
        inspect_sdist(sdist, version)
        run_sdist_install_smoke_test(sdist_env, sdist_smoke_dir, backend_dir, sdist, version)

    print("Bir SDK release verification passed.")
    return 0


def run_sdk_tests() -> None:
    """Run the SDK unit test suite with src on PYTHONPATH."""

    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=PACKAGE_ROOT,
        env=env,
        label="SDK unit tests",
    )


def run_pyright() -> None:
    """Run pyright from the repo virtual environment or PATH."""

    pyright = REPO_ROOT / ".venv" / "bin" / "pyright"
    if not pyright.exists():
        resolved = shutil.which("pyright")
        if resolved is None:
            raise RuntimeError("pyright is required for release verification but was not found")
        pyright = Path(resolved)
    run([str(pyright)], cwd=REPO_ROOT, label="pyright")


def build_wheel(wheelhouse: Path, version: str) -> Path:
    """Build a minimal pure-Python wheel into the given wheelhouse."""

    print("==> wheel build", flush=True)
    # Use the normalized "bir-sdk" distribution name (bir_sdk) for the wheel
    # filename and *.dist-info so pip/importlib resolve the dist as "bir-sdk",
    # matching the published package. The import package stays "bir/".
    wheel = wheelhouse / f"bir_sdk-{version}-py3-none-any.whl"
    dist_info = f"bir_sdk-{version}.dist-info"
    records: list[tuple[str, str, int]] = []

    def write_file(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
        # Fixed timestamps and permissions keep repeated builds byte-for-byte
        # reproducible, in addition to the deterministic member ordering.
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        archive.writestr(info, data)
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode("ascii").rstrip("=")
        records.append((name, f"sha256={digest}", len(data)))

    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in package_python_files(PACKAGE_SOURCE):
            archive_name = (Path("bir") / source.relative_to(PACKAGE_SOURCE)).as_posix()
            write_file(archive, archive_name, source.read_bytes())

        # Ship the PEP 561 marker so downstream type checkers trust the inline
        # types. Carry the real marker bytes so RECORD reflects what we ship.
        py_typed = PACKAGE_ROOT / "src" / "bir" / "py.typed"
        write_file(archive, "bir/py.typed", py_typed.read_bytes())

        # Ship console_scripts so pip generates the ``bir`` command at install
        # time. Derived from pyproject's [project.scripts] so the two cannot drift.
        scripts = console_scripts()
        if scripts:
            write_file(archive, f"{dist_info}/entry_points.txt", entry_points_text(scripts).encode("utf-8"))

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
        write_record(archive, f"{dist_info}/RECORD", ("\n".join(record_lines) + "\n").encode("utf-8"))

    return wheel


def package_python_files(package_root: Path) -> list[Path]:
    """Return Python files from conventional package directories below root."""

    if not (package_root / "__init__.py").is_file():
        raise RuntimeError(f"package directory is missing __init__.py: {package_root}")

    package_dirs = [package_root]
    discovered: list[Path] = []
    while package_dirs:
        package_dir = package_dirs.pop(0)
        discovered.extend(path for path in package_dir.glob("*.py") if path.is_file())
        package_dirs.extend(
            child
            for child in sorted(package_dir.iterdir())
            if child.is_dir() and (child / "__init__.py").is_file()
        )
    return sorted(discovered, key=lambda path: path.relative_to(package_root).as_posix())


def write_record(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    """Write RECORD itself without adding a self-referential hash entry."""

    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data)


def metadata(version: str) -> str:
    """Render wheel metadata from pyproject.toml and the package README."""

    pyproject = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    readme = (PACKAGE_ROOT / "README.md").read_text(encoding="utf-8")
    description = required_string(pyproject, "description")
    requires_python = required_string(pyproject, "requires-python")
    headers = [
        "Metadata-Version: 2.4",
        "Name: bir-sdk",
        f"Version: {version}",
        f"Summary: {description}",
        f"Requires-Python: {requires_python}",
        "License-Expression: Apache-2.0",
        "Description-Content-Type: text/markdown",
    ]
    headers.extend(f"Classifier: {classifier}" for classifier in classifiers())
    for extra, requirements in optional_dependencies().items():
        headers.append(f"Provides-Extra: {extra}")
        headers.extend(
            f'Requires-Dist: {requirement}; extra == "{extra}"'
            for requirement in requirements
        )
    return "\n".join(headers) + f"\n\n{readme}\n"


def inspect_wheel(wheel: Path) -> None:
    """Validate that the wheel contains expected files and excludes local artifacts."""

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
        if len(record_names) != 1:
            raise RuntimeError("wheel must contain exactly one dist-info/RECORD")
        record_rows = list(csv.reader(io.StringIO(archive.read(record_names[0]).decode("utf-8"))))

        recorded = {row[0]: row[1:] for row in record_rows if len(row) == 3}
        if set(recorded) != names:
            raise RuntimeError("wheel RECORD entries do not match archive contents")
        for name in sorted(names - set(record_names)):
            digest, size = recorded[name]
            data = archive.read(name)
            expected_digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode("ascii").rstrip("=")
            if digest != f"sha256={expected_digest}" or size != str(len(data)):
                raise RuntimeError(f"wheel RECORD hash or size is invalid for: {name}")

    intended_python = {
        (Path("bir") / source.relative_to(PACKAGE_SOURCE)).as_posix()
        for source in package_python_files(PACKAGE_SOURCE)
    }
    missing = (REQUIRED_PACKAGE_FILES | intended_python).difference(names)
    if missing:
        raise RuntimeError(f"wheel is missing expected SDK files: {sorted(missing)}")

    for name in names:
        parts = set(Path(name).parts)
        if FORBIDDEN_PATH_PARTS.intersection(parts):
            raise RuntimeError(f"wheel contains forbidden local/generated path: {name}")


def run_install_smoke_test(smoke_env: Path, smoke_dir: Path, wheel: Path, version: str) -> None:
    """Install the wheel into a fresh venv and run a basic SDK smoke test."""

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

    run_smoke_imports(smoke_python, smoke_dir, version, distribution_label="wheel")
    run_console_scripts(smoke_env, smoke_dir)


def run_smoke_imports(
    python_exe: Path, work_dir: Path, version: str, *, distribution_label: str
) -> None:
    """Run the shared SDK import/behavior smoke test with the given interpreter.

    Reused by both the wheel and sdist install checks so the installed package is
    exercised identically however it was produced.
    """

    smoke_test = work_dir / "smoke_test.py"
    smoke_test.write_text(smoke_test_source(version, distribution_label), encoding="utf-8")
    run([str(python_exe), str(smoke_test)], cwd=work_dir, label=f"fresh venv {distribution_label} smoke test")


def run_console_scripts(env_dir: Path, work_dir: Path) -> None:
    """Exercise the installed ``bir`` console script, if one is declared."""

    # The console_scripts entry point must be installed by the real distribution
    # and be invokable as ``bir``. ``--version`` exercises the SDK import path and
    # ``traces``/``stats`` exercise subcommands (no local traces exist, so they
    # exit 0).
    if console_scripts():
        bir_script = env_dir / "bin" / "bir"
        run([str(bir_script), "--version"], cwd=work_dir, label="installed bir --version")
        run([str(bir_script), "traces"], cwd=work_dir, label="installed bir traces")
        run([str(bir_script), "stats"], cwd=work_dir, label="installed bir stats")


def smoke_test_source(version: str, distribution_label: str) -> str:
    """Return the SDK smoke-test program asserting the installed package works."""

    # Assert the installed distribution resolves as "bir-sdk" at the expected
    # version, so future drift between the dist name and __version__ fails here.
    version_check = textwrap.dedent(
        f"""
        from importlib.metadata import version as _distribution_version
        import importlib
        import pkgutil

        import bir
        import bir.cli
        import bir.evals
        import bir.integrations

        integration_modules = sorted(
            module.name
            for module in pkgutil.iter_modules(
                bir.integrations.__path__, bir.integrations.__name__ + "."
            )
        )
        assert integration_modules, "installed {distribution_label} has no integration modules"
        for module_name in integration_modules:
            importlib.import_module(module_name)

        installed_version = _distribution_version("bir-sdk")
        assert installed_version == {version!r}, (
            "installed bir-sdk version " + repr(installed_version)
            + " does not match the pyproject version " + {version!r}
        )
        assert bir.__version__ == {version!r}, (
            "bir.__version__ " + repr(bir.__version__)
            + " does not match the pyproject version " + {version!r}
        )
        """
    )
    return version_check + textwrap.dedent(
        """
            from bir import configure, generation, load_traces, observe, prompt, retrieval, score, span, trace
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
            recorded_trace = load_traces()[0]
            events = recorded_trace.events
            assert [event.type for event in events] == ["trace", "span", "tool_call", "generation", "score"]

            with trace("manual"):
                score("manual_score", 1.0)
            manual_trace = load_traces()[1]
            assert manual_trace.name == "manual"
            assert [event.type for event in manual_trace.events] == ["trace", "score"]

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
    )


def build_sdist(sdist_dir: Path, version: str) -> Path:
    """Build the source distribution (``.tar.gz``) into ``sdist_dir``.

    Prefer the real PEP 517 build (``python -m build --sdist --no-isolation``) so
    the gate exercises the *actual* packaging config that PyPI mirrors and many
    downstream package managers build from. ``--no-isolation`` keeps the build
    hermetic by reusing the already-installed setuptools backend instead of
    fetching one over the network. When that backend is unavailable (a minimal
    environment without the ``dev`` extra), fall back to assembling a
    deterministic tarball directly so its contents can still be inspected.
    """

    print("==> sdist build", flush=True)
    expected = sdist_dir / f"bir_sdk-{version}.tar.gz"
    if not _build_backend_available():
        return _assemble_sdist(sdist_dir, version)

    # setuptools writes ``src/bir_sdk.egg-info`` as a side effect of the in-place
    # build; remove it afterward if we are the ones who created it so the working
    # tree stays clean.
    egg_info = PACKAGE_ROOT / "src" / "bir_sdk.egg-info"
    preexisting = egg_info.exists()
    try:
        # Capture the verbose backend output and surface it only on failure so a
        # healthy build keeps the "==> sdist build" progress line uncluttered.
        completed = subprocess.run(
            [sys.executable, "-m", "build", "--sdist", "--no-isolation", "--outdir", str(sdist_dir)],
            cwd=PACKAGE_ROOT,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            sys.stdout.write(completed.stdout)
            sys.stderr.write(completed.stderr)
            raise RuntimeError("python -m build --sdist failed")
    finally:
        if not preexisting and egg_info.exists():
            shutil.rmtree(egg_info)

    if not expected.is_file():
        raise RuntimeError(f"sdist build did not produce {expected.name}")
    return expected


def _build_backend_available() -> bool:
    """Report whether ``python -m build`` can run offline with setuptools."""

    build_spec = importlib.util.find_spec("build")
    # ``build_spec.origin`` is ``None`` for a namespace package, which is what a
    # stray gitignored ``build/`` directory next to pyproject.toml resolves to.
    # Require a real installed ``build`` so a leftover directory does not look
    # like the frontend and break the in-place ``python -m build`` invocation.
    if build_spec is None or build_spec.origin is None:
        return False
    return importlib.util.find_spec("setuptools") is not None


def _assemble_sdist(sdist_dir: Path, version: str) -> Path:
    """Assemble a deterministic sdist tarball without the build backend.

    Mirrors the standard PEP 517 source layout (a single ``bir_sdk-<version>/``
    top-level directory holding ``pyproject.toml``, ``README.md``, ``LICENSE``,
    ``PKG-INFO``, and the ``src/bir`` tree) so ``inspect_sdist`` validates the
    same shape whether the tarball came from setuptools or from this fallback.
    """

    prefix = f"bir_sdk-{version}"
    members: list[tuple[str, bytes]] = [
        (f"{prefix}/pyproject.toml", (PACKAGE_ROOT / "pyproject.toml").read_bytes()),
        (f"{prefix}/README.md", (PACKAGE_ROOT / "README.md").read_bytes()),
        (f"{prefix}/LICENSE", (PACKAGE_ROOT / "LICENSE").read_bytes()),
        (f"{prefix}/PKG-INFO", metadata(version).encode("utf-8")),
        (f"{prefix}/src/bir/py.typed", (PACKAGE_SOURCE / "py.typed").read_bytes()),
    ]
    for source in package_python_files(PACKAGE_SOURCE):
        archive_name = (Path("bir") / source.relative_to(PACKAGE_SOURCE)).as_posix()
        members.append((f"{prefix}/src/{archive_name}", source.read_bytes()))
    members.sort()

    # Fixed member metadata plus a fixed gzip mtime keep repeated builds
    # byte-for-byte reproducible, matching the deterministic wheel build.
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))

    sdist = sdist_dir / f"{prefix}.tar.gz"
    with open(sdist, "wb") as handle:
        with gzip.GzipFile(filename="", fileobj=handle, mode="wb", mtime=0) as gz:
            gz.write(raw.getvalue())
    return sdist


def inspect_sdist(sdist: Path, version: str) -> None:
    """Validate that the sdist ships the sources and excludes local artifacts."""

    print("==> sdist inspect", flush=True)
    prefix = f"bir_sdk-{version}"
    with tarfile.open(sdist, "r:*") as archive:
        names = [member.name for member in archive.getmembers()]
        top_level = {name.split("/", 1)[0] for name in names if name}
        if top_level != {prefix}:
            raise RuntimeError(
                f"sdist must contain exactly one top-level directory {prefix!r}, found {sorted(top_level)}"
            )

        relative = set()
        for name in names:
            if name == prefix:
                continue
            relative.add(name[len(prefix) + 1 :])

        for rel in relative:
            if FORBIDDEN_PATH_PARTS.intersection(Path(rel).parts):
                raise RuntimeError(f"sdist contains forbidden local/generated path: {rel}")

        required = {"pyproject.toml", "LICENSE", "README.md", "PKG-INFO"}
        required |= {f"src/{name}" for name in REQUIRED_PACKAGE_FILES}
        required |= {
            f"src/{(Path('bir') / source.relative_to(PACKAGE_SOURCE)).as_posix()}"
            for source in package_python_files(PACKAGE_SOURCE)
        }
        missing = required.difference(relative)
        if missing:
            raise RuntimeError(f"sdist is missing expected files: {sorted(missing)}")

        pkg_info_member = archive.extractfile(f"{prefix}/PKG-INFO")
        if pkg_info_member is None:
            raise RuntimeError("sdist PKG-INFO is not a readable file")
        pkg_info = pkg_info_member.read().decode("utf-8").splitlines()

    if "Name: bir-sdk" not in pkg_info:
        raise RuntimeError("sdist PKG-INFO does not declare the bir-sdk distribution name")
    if f"Version: {version}" not in pkg_info:
        raise RuntimeError(f"sdist PKG-INFO version does not match the pyproject version {version}")


def run_sdist_install_smoke_test(
    sdist_env: Path, sdist_dir: Path, backend_dir: Path, sdist: Path, version: str
) -> None:
    """Install the sdist into a fresh venv offline and run the SDK smoke test."""

    print("==> sdist install", flush=True)
    backend = _stage_build_backend(backend_dir)

    venv.EnvBuilder(with_pip=True).create(sdist_env)
    sdist_python = sdist_env / "bin" / "python"
    install_env = os.environ.copy()
    install_env["PIP_NO_CACHE_DIR"] = "1"
    # Building the sdist into a wheel needs a PEP 517 backend. ``--no-index``
    # forbids fetching one, so provide the host's setuptools/wheel on PYTHONPATH
    # for the build only; the smoke test below runs with a clean environment and
    # imports the freshly installed distribution from the venv's site-packages.
    install_env["PYTHONPATH"] = str(backend)
    run(
        [
            str(sdist_python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-build-isolation",
            "--no-deps",
            str(sdist),
        ],
        cwd=sdist_dir,
        env=install_env,
        label="fresh venv sdist install",
    )

    run_smoke_imports(sdist_python, sdist_dir, version, distribution_label="sdist")
    run_console_scripts(sdist_env, sdist_dir)


def _stage_build_backend(backend_dir: Path) -> Path:
    """Stage a minimal setuptools/wheel build backend copied from the host.

    Copying only the backend packages (not the whole host environment) keeps the
    offline sdist build from importing the host's ``bir-sdk`` or other packages,
    so the install resolves the sources straight from the sdist.
    """

    spec = importlib.util.find_spec("setuptools")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError(
            "setuptools is required to verify the sdist install; install the dev extra "
            '(``pip install -e ".[dev]"``)'
        )

    host_site = Path(spec.submodule_search_locations[0]).parent
    backend_dir.mkdir(parents=True, exist_ok=True)
    for name in ("setuptools", "_distutils_hack", "pkg_resources", "wheel"):
        source = host_site / name
        if source.is_dir():
            shutil.copytree(source, backend_dir / name, dirs_exist_ok=True)
    precedence = host_site / "distutils-precedence.pth"
    if precedence.is_file():
        shutil.copy2(precedence, backend_dir / precedence.name)
    for info in list(host_site.glob("setuptools-*.dist-info")) + list(host_site.glob("wheel-*.dist-info")):
        shutil.copytree(info, backend_dir / info.name, dirs_exist_ok=True)
    return backend_dir


def package_version() -> str:
    """Read the SDK package version from pyproject.toml."""

    pyproject = PACKAGE_ROOT / "pyproject.toml"
    return required_string(pyproject.read_text(encoding="utf-8"), "version")


def console_scripts() -> dict[str, str]:
    """Parse the ``[project.scripts]`` table from pyproject.toml.

    Implemented without ``tomllib`` so it runs on Python 3.10, the minimum the
    SDK supports. The table is flat (``name = "module:callable"``), so a small
    line scan is enough.
    """

    text = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    scripts: dict[str, str] = {}
    in_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_section = line == "[project.scripts]"
            continue
        if in_section:
            match = re.match(r'^([A-Za-z0-9._-]+)\s*=\s*"([^"]+)"', line)
            if match:
                scripts[match.group(1)] = match.group(2)
    return scripts


def classifiers() -> list[str]:
    """Parse the ``classifiers`` array from the ``[project]`` table.

    Implemented without ``tomllib`` so it runs on Python 3.10, the minimum the
    SDK supports. The project keeps one quoted classifier per line inside a
    multi-line array, matching the rest of this module's line-scanning parsers.
    """

    text = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    values: list[str] = []
    in_project = False
    collecting = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_project = line == "[project]"
            collecting = False
            continue
        if not in_project:
            continue
        if not collecting:
            if re.match(r"^classifiers\s*=\s*\[\s*$", line):
                collecting = True
            continue
        if line.startswith("]"):
            collecting = False
            continue
        match = re.match(r'^"([^"]+)",?\s*$', line)
        if match:
            values.append(match.group(1))
    return values


def optional_dependencies() -> dict[str, list[str]]:
    """Parse ``[project.optional-dependencies]`` from pyproject.toml.

    The release builder intentionally avoids a TOML dependency so it can run on
    every supported Python. The project uses a narrow, conventional shape here:
    an inline quoted list or ``extra = [`` followed by one quoted requirement per
    line and a closing bracket.
    """

    text = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    extras: dict[str, list[str]] = {}
    current_extra: str | None = None
    in_section = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_section = line == "[project.optional-dependencies]"
            current_extra = None
            continue
        if not in_section:
            continue

        if current_extra is None:
            inline_match = re.match(r'^([A-Za-z0-9._-]+)\s*=\s*\[(.*)\]\s*$', line)
            if inline_match:
                extra = inline_match.group(1)
                raw_requirements = inline_match.group(2).strip()
                extras[extra] = _quoted_list_items(raw_requirements) if raw_requirements else []
                continue

            match = re.match(r'^([A-Za-z0-9._-]+)\s*=\s*\[\s*$', line)
            if match:
                current_extra = match.group(1)
                extras[current_extra] = []
            continue

        if line == "]":
            current_extra = None
            continue

        match = re.match(r'^"([^"]+)",?\s*$', line)
        if match:
            extras[current_extra].append(match.group(1))
            continue

        raise RuntimeError(f"unsupported optional dependency line in pyproject.toml: {raw_line}")

    if current_extra is not None:
        raise RuntimeError(f"unterminated optional dependency group in pyproject.toml: {current_extra}")
    return extras


def _quoted_list_items(text: str) -> list[str]:
    """Parse comma-separated quoted strings from one TOML array line."""

    items: list[str] = []
    remaining = text.strip()
    while remaining:
        match = re.match(r'^"([^"]+)"\s*(?:,\s*)?', remaining)
        if match is None:
            raise RuntimeError(f"unsupported inline optional dependency list: {text}")
        items.append(match.group(1))
        remaining = remaining[match.end() :].strip()
    return items


def entry_points_text(scripts: dict[str, str]) -> str:
    """Render a wheel ``entry_points.txt`` for the given console scripts."""

    lines = ["[console_scripts]"]
    lines.extend(f"{name} = {target}" for name, target in scripts.items())
    return "\n".join(lines) + "\n"


def required_string(text: str, key: str) -> str:
    """Extract a required quoted string from TOML-like text."""

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
    """Run a labeled subprocess command and fail on non-zero exit."""

    print(f"==> {label}", flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
