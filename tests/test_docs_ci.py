"""Repository contract checks for the isolated documentation CI gate."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docs-deploy.yml"


class DocumentationCIContractTests(unittest.TestCase):
    """Keep documentation validation isolated from runtime and SDK matrix jobs."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = CI_WORKFLOW.read_text(encoding="utf-8")
        cls.pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    def test_docs_extra_is_optional_and_runtime_dependencies_stay_empty(self) -> None:
        self.assertRegex(self.pyproject, r"(?m)^dependencies = \[\]$")
        self.assertRegex(self.pyproject, r'(?m)^docs = \[[^\]]*"mkdocs[^"]*"[^\]]*\]$')

    def test_strict_docs_build_runs_exactly_once_from_docs_extra(self) -> None:
        self.assertEqual(self.workflow.count('run: python -m pip install -e ".[docs]"'), 1)
        self.assertEqual(self.workflow.count("run: mkdocs build --strict"), 1)

        docs_job = self._job("docs")
        self.assertIn('run: python -m pip install -e ".[docs]"', docs_job)
        self.assertIn("run: mkdocs build --strict", docs_job)
        self.assertNotIn("matrix:", docs_job)
        self.assertNotIn(".[dev]", docs_job)
        self.assertNotRegex(docs_job, r"pip install[^\n]*\bmkdocs\b")

    def test_workflow_runs_for_pull_requests_and_main_pushes(self) -> None:
        self.assertRegex(self.workflow, r"(?m)^  pull_request:$")
        self.assertRegex(self.workflow, r"(?ms)^  push:\n    branches:\n      - main$")

    def test_sdk_python_matrix_remains_unchanged(self) -> None:
        sdk_job = self._job("sdk")
        self.assertIn('python-version: ["3.10", "3.11", "3.12", "3.13"]', sdk_job)
        self.assertNotIn(".[docs]", sdk_job)
        self.assertNotIn("mkdocs", sdk_job)

    def test_generated_site_directory_is_ignored(self) -> None:
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("/site/", gitignore)

    def _job(self, name: str) -> str:
        match = re.search(
            rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)",
            self.workflow,
        )
        self.assertIsNotNone(match, f"CI job {name!r} is missing")
        return match.group(0)  # type: ignore[union-attr]


class DocumentationDeployWorkflowTests(unittest.TestCase):
    """The Pages deploy workflow publishes docs only after the strict build passes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    def test_triggers_on_main_push_and_manual_dispatch(self) -> None:
        self.assertRegex(self.workflow, r"(?ms)^  push:\n    branches:\n      - main$")
        self.assertRegex(self.workflow, r"(?m)^  workflow_dispatch:$")

    def test_grants_only_the_pages_deploy_permissions(self) -> None:
        # Pages deploys need pages:write plus the id-token used by deploy-pages.
        self.assertRegex(self.workflow, r"(?m)^  pages: write$")
        self.assertRegex(self.workflow, r"(?m)^  id-token: write$")

    def test_serializes_deploys_with_a_concurrency_group(self) -> None:
        self.assertRegex(self.workflow, r"(?m)^concurrency:$")
        self.assertRegex(self.workflow, r"(?m)^  group: pages$")

    def test_runs_strict_build_from_docs_extra_before_deploying(self) -> None:
        # The deploy job depends on a build job that runs the same strict build as
        # the PR gate, so a docs change failing --strict can never publish.
        self.assertIn('run: python -m pip install -e ".[docs]"', self.workflow)
        self.assertIn("run: mkdocs build --strict", self.workflow)
        self.assertRegex(self.workflow, r"(?m)^    needs: build$")

    def test_uploads_site_and_deploys_via_official_pages_actions(self) -> None:
        self.assertIn("uses: actions/upload-pages-artifact@", self.workflow)
        self.assertRegex(self.workflow, r"(?m)^          path: site$")
        self.assertIn("uses: actions/deploy-pages@", self.workflow)

    def test_does_not_weaken_the_ci_strict_build_gate(self) -> None:
        # The PR gate lives in ci.yml; the deploy workflow must not touch it.
        ci_workflow = CI_WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(ci_workflow.count("run: mkdocs build --strict"), 1)


if __name__ == "__main__":
    unittest.main()
