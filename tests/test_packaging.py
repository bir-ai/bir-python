"""Packaging checks for the PEP 561 ``py.typed`` marker.

The Bir SDK is fully type annotated, so it ships an empty ``py.typed`` marker
(PEP 561) that tells downstream type checkers to trust the inline types. These
tests guard two things: the marker is locatable from the imported package, and
``scripts/verify_release.py`` both ships the marker in its hand-built wheel and
fails if a wheel is missing it.
"""

from __future__ import annotations

import importlib.resources
import importlib.util
import base64
import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import ModuleType

import bir

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_verify_release() -> ModuleType:
    """Load ``scripts/verify_release.py`` by path (``scripts/`` is not a package)."""

    script_path = REPO_ROOT / "scripts" / "verify_release.py"
    spec = importlib.util.spec_from_file_location("bir_verify_release", script_path)
    assert spec is not None and spec.loader is not None, f"cannot load {script_path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PyTypedMarkerTests(unittest.TestCase):
    """The imported package exposes a locatable ``py.typed`` marker."""

    def test_marker_is_locatable_from_package(self) -> None:
        marker = importlib.resources.files("bir").joinpath("py.typed")
        self.assertTrue(marker.is_file())


class VerifyReleaseMarkerTests(unittest.TestCase):
    """``verify_release`` ships and enforces the ``py.typed`` marker."""

    def setUp(self) -> None:
        self.verify_release = _load_verify_release()
        tmp = tempfile.TemporaryDirectory(prefix="bir-pytyped-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)

    def test_built_wheel_ships_marker_and_inspect_passes(self) -> None:
        version = self.verify_release.package_version()
        wheel = self.verify_release.build_wheel(self.tmp_path, version)

        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            record = archive.read(f"bir_sdk-{version}.dist-info/RECORD").decode("utf-8")

        self.assertIn("bir/py.typed", names)
        # The marker is also accounted for in RECORD (with a hash and size).
        self.assertIn("bir/py.typed", record)

        # Should not raise: the wheel carries every required file.
        self.verify_release.inspect_wheel(wheel)

    def test_built_wheel_ships_complete_package_tree_with_valid_record(self) -> None:
        version = self.verify_release.package_version()
        wheel = self.verify_release.build_wheel(self.tmp_path, version)
        expected_python = {
            (Path("bir") / path.relative_to(REPO_ROOT / "src" / "bir")).as_posix()
            for path in self.verify_release.package_python_files(REPO_ROOT / "src" / "bir")
        }

        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            record_text = archive.read(f"bir_sdk-{version}.dist-info/RECORD").decode("utf-8")

        self.assertTrue(expected_python.issubset(names))
        self.assertIn("bir/integrations/openai.py", names)
        self.assertIn("bir/integrations/vertexai.py", names)
        self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in names))
        self.assertFalse(any(name.startswith(("tests/", "docs/", ".bir/")) for name in names))
        for name in expected_python | {"bir/py.typed"}:
            data = (REPO_ROOT / "src" / name).read_bytes()
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode("ascii").rstrip("=")
            self.assertIn(f"{name},sha256={digest},{len(data)}", record_text)

    def test_inspect_wheel_rejects_missing_marker(self) -> None:
        version = self.verify_release.package_version()
        built = self.verify_release.build_wheel(self.tmp_path, version)
        wheel = self.tmp_path / "missing-marker.whl"
        self._copy_wheel_without(built, wheel, "bir/py.typed")

        with self.assertRaisesRegex(RuntimeError, "missing expected SDK files"):
            self.verify_release.inspect_wheel(wheel)

    def test_build_is_deterministic(self) -> None:
        version = self.verify_release.package_version()
        first_dir = self.tmp_path / "first"
        second_dir = self.tmp_path / "second"
        first_dir.mkdir()
        second_dir.mkdir()

        first = self.verify_release.build_wheel(first_dir, version)
        second = self.verify_release.build_wheel(second_dir, version)

        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_package_file_discovery_excludes_non_package_directories(self) -> None:
        package = self.tmp_path / "sample"
        subpackage = package / "included"
        cache = package / "__pycache__"
        non_package = package / "generated"
        subpackage.mkdir(parents=True)
        cache.mkdir()
        non_package.mkdir()
        for path in (
            package / "__init__.py",
            package / "module.py",
            subpackage / "__init__.py",
            subpackage / "nested.py",
            cache / "cached.py",
            non_package / "artifact.py",
        ):
            path.touch()

        discovered = {
            path.relative_to(package).as_posix()
            for path in self.verify_release.package_python_files(package)
        }

        self.assertEqual(
            discovered,
            {"__init__.py", "module.py", "included/__init__.py", "included/nested.py"},
        )

    def test_inspect_wheel_rejects_missing_integrations_subpackage(self) -> None:
        version = self.verify_release.package_version()
        built = self.verify_release.build_wheel(self.tmp_path, version)
        wheel = self.tmp_path / "missing-integrations.whl"
        self._copy_wheel_without(built, wheel, "bir/integrations/")

        with self.assertRaisesRegex(RuntimeError, "missing expected SDK files"):
            self.verify_release.inspect_wheel(wheel)

    def test_inspect_wheel_rejects_missing_required_package_module(self) -> None:
        version = self.verify_release.package_version()
        built = self.verify_release.build_wheel(self.tmp_path, version)
        wheel = self.tmp_path / "missing-evals.whl"
        self._copy_wheel_without(built, wheel, "bir/evals.py")

        with self.assertRaisesRegex(RuntimeError, "missing expected SDK files"):
            self.verify_release.inspect_wheel(wheel)

    @staticmethod
    def _copy_wheel_without(source: Path, destination: Path, prefix: str) -> None:
        """Copy an archive while removing members and their RECORD rows."""

        with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, "w") as changed:
            for info in original.infolist():
                data = original.read(info.filename)
                if info.filename.startswith(prefix):
                    continue
                if info.filename.endswith(".dist-info/RECORD"):
                    lines = data.decode("utf-8").splitlines()
                    data = ("\n".join(line for line in lines if not line.startswith(prefix)) + "\n").encode()
                changed.writestr(info, data)

    def test_built_wheel_resolves_as_bir_sdk_distribution(self) -> None:
        version = self.verify_release.package_version()
        wheel = self.verify_release.build_wheel(self.tmp_path, version)

        # The wheel filename and *.dist-info use the normalized distribution
        # name so pip/importlib resolve the dist as "bir-sdk", not "bir".
        self.assertEqual(wheel.name, f"bir_sdk-{version}-py3-none-any.whl")

        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            metadata = archive.read(f"bir_sdk-{version}.dist-info/METADATA").decode("utf-8")

        self.assertIn(f"bir_sdk-{version}.dist-info/METADATA", names)
        self.assertIn("Name: bir-sdk", metadata.splitlines())
        # The import package is still shipped as "bir/", unchanged.
        self.assertIn("bir/__init__.py", names)

    def test_optional_extras_are_declared_in_wheel_metadata(self) -> None:
        version = self.verify_release.package_version()
        wheel = self.verify_release.build_wheel(self.tmp_path, version)

        with zipfile.ZipFile(wheel) as archive:
            metadata = archive.read(f"bir_sdk-{version}.dist-info/METADATA").decode("utf-8")

        lines = metadata.splitlines()
        self.assertIn("Provides-Extra: dev", lines)
        self.assertIn('Requires-Dist: pytest>=8.0; extra == "dev"', lines)
        self.assertIn("Provides-Extra: docs", lines)
        self.assertIn('Requires-Dist: mkdocs>=1.5; extra == "docs"', lines)
        self.assertIn("Provides-Extra: otel", lines)
        self.assertIn('Requires-Dist: opentelemetry-sdk>=1.20; extra == "otel"', lines)
        self.assertIn(
            'Requires-Dist: opentelemetry-exporter-otlp-proto-http>=1.20; extra == "otel"',
            lines,
        )

    def test_wheel_metadata_advertises_typed_classifier(self) -> None:
        version = self.verify_release.package_version()
        wheel = self.verify_release.build_wheel(self.tmp_path, version)

        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            metadata = archive.read(f"bir_sdk-{version}.dist-info/METADATA").decode("utf-8")

        # The distribution ships inline types (the PEP 561 marker below), so its
        # metadata must carry the matching trove classifier for PyPI/tooling.
        self.assertIn("Classifier: Typing :: Typed", metadata.splitlines())
        self.assertIn("bir/py.typed", names)

    def test_pyproject_classifiers_include_typed_and_stay_sorted(self) -> None:
        declared = self.verify_release.classifiers()

        self.assertIn("Typing :: Typed", declared)
        self.assertEqual(declared, sorted(declared))

    def test_wheel_metadata_has_no_unconditional_runtime_dependencies(self) -> None:
        version = self.verify_release.package_version()
        wheel = self.verify_release.build_wheel(self.tmp_path, version)

        with zipfile.ZipFile(wheel) as archive:
            metadata = archive.read(f"bir_sdk-{version}.dist-info/METADATA").decode("utf-8")

        unconditional_requirements = [
            line
            for line in metadata.splitlines()
            if line.startswith("Requires-Dist: ") and "; extra ==" not in line
        ]
        self.assertEqual(unconditional_requirements, [])


class VerifyReleaseEntryPointTests(unittest.TestCase):
    """``verify_release`` ships the ``bir`` console script declared in pyproject."""

    def setUp(self) -> None:
        self.verify_release = _load_verify_release()
        tmp = tempfile.TemporaryDirectory(prefix="bir-entrypoint-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)

    def test_pyproject_declares_the_bir_console_script(self) -> None:
        self.assertEqual(self.verify_release.console_scripts(), {"bir": "bir.cli:main"})

    def test_built_wheel_ships_console_script_entry_point(self) -> None:
        version = self.verify_release.package_version()
        wheel = self.verify_release.build_wheel(self.tmp_path, version)

        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            entry_points = archive.read(f"bir_sdk-{version}.dist-info/entry_points.txt").decode("utf-8")
            record = archive.read(f"bir_sdk-{version}.dist-info/RECORD").decode("utf-8")

        self.assertIn(f"bir_sdk-{version}.dist-info/entry_points.txt", names)
        self.assertIn("[console_scripts]", entry_points)
        self.assertIn("bir = bir.cli:main", entry_points)
        # The entry-point file is accounted for in RECORD like every shipped file.
        self.assertIn(f"bir_sdk-{version}.dist-info/entry_points.txt", record)


class VersionSurfaceTests(unittest.TestCase):
    """``bir.__version__`` resolves the ``bir-sdk`` distribution version."""

    def test_version_is_non_empty_well_formed_string(self) -> None:
        self.assertIsInstance(bir.__version__, str)
        self.assertTrue(bir.__version__)
        self.assertRegex(bir.__version__, r"^\d+\.\d+\.\d+")

    def test_version_matches_pyproject_version(self) -> None:
        # Holds whether the dist is installed (``version("bir-sdk")``) or running
        # from source (the literal fallback): both must equal the pyproject
        # version, so a stale fallback or wrong dist name fails here.
        expected = _load_verify_release().package_version()
        self.assertEqual(bir.__version__, expected)


if __name__ == "__main__":
    unittest.main()
