"""The published stability page must describe the package that ships.

``docs/site/stability.md`` is the SDK's compatibility promise: what is public,
what may change, and the checklist a Beta release is measured against. A promise
that drifts from the code is worse than none, so every inventory on that page is
compared against the running package here, in both directions — a name the page
omits fails just as loudly as a name the page invents.

The page is the source of truth for humans; ``__all__``, the CLI parser, and the
configuration reader are the source of truth for these tests.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import unittest
from pathlib import Path
from types import ModuleType

import bir
import bir.evals as evals
import bir.logging as bir_logging
import bir.testing as bir_testing
from bir._cli_parser import build_parser
from bir._storage import _SCHEMA_VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]
STABILITY_PAGE = REPO_ROOT / "docs" / "site" / "stability.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def load_verify_release() -> ModuleType:
    """Load ``scripts/verify_release.py``, which reads pyproject on Python 3.10.

    ``tomllib`` arrived in 3.11 and the SDK supports 3.10, so the release script
    parses pyproject by line scanning and the test suite reuses those readers
    rather than adding a parser of its own.
    """

    script_path = REPO_ROOT / "scripts" / "verify_release.py"
    spec = importlib.util.spec_from_file_location("bir_verify_release_stability", script_path)
    assert spec is not None and spec.loader is not None, f"cannot load {script_path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ``| `name` | ...`` — the first cell of a table row, which every inventory on
# the page uses for the thing being documented.
_ROW_NAME = re.compile(r"^\|\s*`([^`]+)`\s*\|")
# Every backticked token in a row, for the tables that list several per row.
_ROW_NAMES = re.compile(r"`([^`]+)`")


def page_text() -> str:
    return STABILITY_PAGE.read_text(encoding="utf-8")


def section_lines(title: str) -> list[str]:
    """Return the lines under a heading, up to the next heading of any level."""

    lines = page_text().splitlines()
    heading = re.compile(r"^#{2,6}\s+" + re.escape(title) + r"\s*$")
    for index, line in enumerate(lines):
        if heading.match(line):
            body: list[str] = []
            for candidate in lines[index + 1 :]:
                if candidate.startswith("#"):
                    break
                body.append(candidate)
            return body
    raise AssertionError(f"the stability page has no {title!r} section")


def documented_names(title: str) -> set[str]:
    """Return the first-column names of every table row in a section."""

    found = {match.group(1) for line in section_lines(title) if (match := _ROW_NAME.match(line))}
    if not found:
        raise AssertionError(f"the {title!r} section documents no names")
    return found


def cli_commands() -> set[str]:
    """Return the subcommands the CLI parser accepts."""

    handlers = {name: (lambda arguments: 0) for name in _HANDLER_NAMES}
    parser = build_parser(
        version="0.0.0",
        default_server="http://localhost:8000",
        default_experiment_dir="experiments",
        report_formats=("markdown",),
        missing_score_policies=("error",),
        failed_example_policies=("regress",),
        handlers=handlers,
    )
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise AssertionError("the CLI parser exposes no subcommands")


# The handler keys ``build_parser`` binds each subcommand to. Building the parser
# needs them all; the command names it exposes are what the page documents.
_HANDLER_NAMES = (
    "traces",
    "show",
    "stats",
    "tail",
    "experiments",
    "experiment_show",
    "experiment_report",
    "send",
    "send_experiment",
    "eval_gate",
    "export_otel",
    "prune",
    "config",
)


class PublicSurfaceDocumentationTests(unittest.TestCase):
    """Every exported name is documented, and every documented name exists."""

    def test_core_api_matches_the_page(self) -> None:
        self.assertEqual(documented_names("Core API"), set(bir.__all__))

    def test_evaluation_api_matches_the_page(self) -> None:
        self.assertEqual(documented_names("Evaluation API"), set(evals.__all__))

    def test_test_helpers_match_the_page(self) -> None:
        self.assertEqual(documented_names("Test helpers"), set(bir_testing.__all__))

    def test_logging_helpers_match_the_page(self) -> None:
        self.assertEqual(documented_names("Logging"), set(bir_logging.__all__))

    def test_integration_modules_and_entry_points_match_the_package(self) -> None:
        from test_integration_contract import BRIDGES, CONTRACTS, UNDECLARED_PROVIDER_ROOTS

        documented_modules = documented_names("Integrations")
        shipped_modules = (
            {f"bir.integrations.{contract.module}" for contract in CONTRACTS}
            | {f"bir.integrations.{bridge.module}" for bridge in BRIDGES}
            | {f"bir.integrations.{module}" for module in UNDECLARED_PROVIDER_ROOTS}
        )
        self.assertEqual(documented_modules, shipped_modules)

        # Each documented entry point must resolve on the module it is listed
        # under, so a renamed wrapper cannot leave a stale name on the page.
        for line in section_lines("Integrations"):
            match = _ROW_NAME.match(line)
            if match is None:
                continue
            module_name = match.group(1)
            module = __import__(module_name, fromlist=["__name__"])
            for entry_point in _ROW_NAMES.findall(line)[1:]:
                with self.subTest(module=module_name, entry_point=entry_point):
                    self.assertTrue(
                        hasattr(module, entry_point),
                        f"{module_name} does not export {entry_point}",
                    )

    def test_cli_commands_match_the_page(self) -> None:
        self.assertEqual(documented_names("CLI commands"), cli_commands())

    def test_environment_variables_match_the_configuration_reader(self) -> None:
        config_source = (REPO_ROOT / "src" / "bir" / "_config.py").read_text(encoding="utf-8")
        read_variables = set(re.findall(r'_env_value\("(BIR_[A-Z_]+)"\)', config_source))

        self.assertEqual(documented_names("Environment variables"), read_variables)


class CompatibilityPolicyTests(unittest.TestCase):
    """The stated policy matches the package metadata it describes."""

    def setUp(self) -> None:
        self.verify_release = load_verify_release()

    def test_documented_version_and_status_match_the_package(self) -> None:
        version = self.verify_release.package_version()
        status = next(item for item in self.verify_release.classifiers() if item.startswith("Development Status"))

        self.assertIn(f"`{version}`", page_text())
        self.assertIn(f"`{status}`", page_text())
        self.assertEqual(bir.__version__, version)

    def test_documented_python_range_matches_the_classifiers(self) -> None:
        versions = sorted(
            (
                item.rsplit(" :: ", 1)[-1]
                for item in self.verify_release.classifiers()
                if item.startswith("Programming Language :: Python :: 3.")
            ),
            key=lambda version: tuple(int(part) for part in version.split(".")),
        )
        self.assertTrue(versions)

        # The page states the range as endpoints; both must be the real ones.
        self.assertIn(f"**{versions[0]} through {versions[-1]}**", page_text())
        requires_python = self.verify_release.required_string(
            PYPROJECT.read_text(encoding="utf-8"),
            "requires-python",
        )
        self.assertEqual(requires_python, f">={versions[0]}")

    def test_documented_schema_version_matches_the_writer(self) -> None:
        self.assertIn(f'`schema_version = "{_SCHEMA_VERSION}"`', page_text())

    def test_runtime_dependency_promise_holds(self) -> None:
        # The page promises installing Bir cannot move an application's own
        # dependency versions, which only holds while this list stays empty.
        self.assertRegex(PYPROJECT.read_text(encoding="utf-8"), r"(?m)^dependencies = \[\]$")

    def test_beta_checklist_is_a_finite_list_of_checkable_items(self) -> None:
        items = [line for line in section_lines("Beta entry checklist") if line.startswith("- [")]

        self.assertTrue(items)
        for item in items:
            with self.subTest(item=item):
                # Only done or outstanding; a checklist with a third state is
                # back to being a judgment call.
                self.assertRegex(item, r"^- \[[ x]\] ")
        self.assertTrue(
            any(item.startswith("- [ ] ") for item in items),
            "a checklist with nothing outstanding means the SDK is ready for Beta; raise the classifier instead",
        )


if __name__ == "__main__":
    unittest.main()
