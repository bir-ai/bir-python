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
import gzip
import hashlib
import io
import tarfile
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


class VerifyReleaseSdistTests(unittest.TestCase):
    """``verify_release`` builds and validates the source distribution (sdist)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.verify_release = _load_verify_release()
        cls.version = cls.verify_release.package_version()
        cls.prefix = f"bir_sdk-{cls.version}"
        cls._tmp = tempfile.TemporaryDirectory(prefix="bir-sdist-test-")
        cls.tmp_path = Path(cls._tmp.name)
        # The deterministic assembler needs no build backend, so the content and
        # failure-path assertions stay hermetic and fast. A separate test covers
        # the real ``python -m build`` path.
        cls.assembled = cls.verify_release._assemble_sdist(cls.tmp_path, cls.version)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    @staticmethod
    def _names(sdist: Path) -> set[str]:
        with tarfile.open(sdist, "r:*") as archive:
            return set(archive.getnames())

    def test_assembled_sdist_ships_sources_license_and_marker(self) -> None:
        names = self._names(self.assembled)

        self.assertIn(f"{self.prefix}/src/bir/__init__.py", names)
        self.assertIn(f"{self.prefix}/src/bir/integrations/openai.py", names)
        self.assertIn(f"{self.prefix}/src/bir/py.typed", names)
        self.assertIn(f"{self.prefix}/pyproject.toml", names)
        self.assertIn(f"{self.prefix}/LICENSE", names)
        self.assertIn(f"{self.prefix}/README.md", names)
        self.assertIn(f"{self.prefix}/PKG-INFO", names)

        forbidden = {".bir", "build", "dist", "site", "__pycache__", ".venv"}
        self.assertFalse(
            any(forbidden.intersection(Path(name).parts) for name in names),
            msg="assembled sdist leaked a local/generated path",
        )
        # Should not raise: the assembled tarball carries every required file.
        self.verify_release.inspect_sdist(self.assembled, self.version)

    def test_assembled_sdist_is_deterministic(self) -> None:
        first_dir = self.tmp_path / "det-first"
        second_dir = self.tmp_path / "det-second"
        first_dir.mkdir()
        second_dir.mkdir()

        first = self.verify_release._assemble_sdist(first_dir, self.version)
        second = self.verify_release._assemble_sdist(second_dir, self.version)

        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_pkg_info_declares_distribution_name_and_version(self) -> None:
        with tarfile.open(self.assembled, "r:*") as archive:
            member = archive.extractfile(f"{self.prefix}/PKG-INFO")
            assert member is not None
            pkg_info = member.read().decode("utf-8").splitlines()

        self.assertIn("Name: bir-sdk", pkg_info)
        self.assertIn(f"Version: {self.version}", pkg_info)

    def test_real_build_sdist_passes_inspect(self) -> None:
        # Exercises the actual setuptools packaging config -- the path PyPI
        # mirrors and downstream package managers build from. ``build_sdist``
        # transparently falls back to the deterministic assembler when the build
        # backend is unavailable, so this still runs in a minimal environment.
        out = self.tmp_path / "real"
        out.mkdir()
        egg_info = REPO_ROOT / "src" / "bir_sdk.egg-info"
        preexisting = egg_info.exists()
        sdist = self.verify_release.build_sdist(out, self.version)

        # Should not raise.
        self.verify_release.inspect_sdist(sdist, self.version)
        names = self._names(sdist)
        self.assertIn(f"{self.prefix}/src/bir/py.typed", names)
        # A build we triggered must not leave its egg-info behind in the tree;
        # an egg-info that predates the build (e.g. from an editable install) is
        # left untouched and not our concern here.
        if not preexisting:
            self.assertFalse(
                egg_info.exists(),
                msg="build_sdist left an egg-info artifact in the source tree",
            )

    def test_inspect_sdist_rejects_missing_py_typed(self) -> None:
        broken = self.tmp_path / "no-pytyped.tar.gz"
        self._repack(self.assembled, broken, drop=f"{self.prefix}/src/bir/py.typed")

        with self.assertRaisesRegex(RuntimeError, "missing expected files"):
            self.verify_release.inspect_sdist(broken, self.version)

    def test_inspect_sdist_rejects_missing_license(self) -> None:
        broken = self.tmp_path / "no-license.tar.gz"
        self._repack(self.assembled, broken, drop=f"{self.prefix}/LICENSE")

        with self.assertRaisesRegex(RuntimeError, "missing expected files"):
            self.verify_release.inspect_sdist(broken, self.version)

    def test_inspect_sdist_rejects_local_trace_leak(self) -> None:
        broken = self.tmp_path / "bir-leak.tar.gz"
        self._repack(self.assembled, broken, add=(f"{self.prefix}/.bir/traces.jsonl", b"{}\n"))

        with self.assertRaisesRegex(RuntimeError, "forbidden local/generated path"):
            self.verify_release.inspect_sdist(broken, self.version)

    def test_inspect_sdist_rejects_build_tree_leak(self) -> None:
        broken = self.tmp_path / "build-leak.tar.gz"
        self._repack(self.assembled, broken, add=(f"{self.prefix}/build/lib/bir/__init__.py", b"\n"))

        with self.assertRaisesRegex(RuntimeError, "forbidden local/generated path"):
            self.verify_release.inspect_sdist(broken, self.version)

    def test_inspect_sdist_rejects_unexpected_top_level_directory(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "exactly one top-level directory"):
            self.verify_release.inspect_sdist(self.assembled, "9.9.9")

    @staticmethod
    def _repack(
        source: Path,
        destination: Path,
        *,
        drop: str | None = None,
        add: tuple[str, bytes] | None = None,
    ) -> None:
        """Rewrite a gzipped tarball while dropping or adding a single member."""

        with tarfile.open(source, "r:*") as original:
            members: list[tuple[tarfile.TarInfo, bytes | None]] = []
            for member in original.getmembers():
                extracted = original.extractfile(member)
                members.append((member, extracted.read() if extracted is not None else None))

        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as changed:
            for member, data in members:
                if drop is not None and member.name == drop:
                    continue
                changed.addfile(member, io.BytesIO(data) if data is not None else None)
            if add is not None:
                name, payload = add
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                changed.addfile(info, io.BytesIO(payload))

        with open(destination, "wb") as handle:
            with gzip.GzipFile(fileobj=handle, mode="wb", mtime=0) as gz:
                gz.write(buffer.getvalue())


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
