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

    def test_inspect_wheel_rejects_missing_marker(self) -> None:
        wheel = self.tmp_path / "missing-marker.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("bir/__init__.py", b"")
            archive.writestr("bir/_sdk.py", b"")

        with self.assertRaises(RuntimeError):
            self.verify_release.inspect_wheel(wheel)

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
