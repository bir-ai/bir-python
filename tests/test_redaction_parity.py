from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from bir._sdk import _redact_secret_text, _safe_capture

ROOT = Path(__file__).resolve().parents[3]
REDACTION_CASES_PATH = ROOT / "tests" / "fixtures" / "redaction-cases.json"


def load_redaction_cases() -> list[dict[str, Any]]:
    cases = json.loads(REDACTION_CASES_PATH.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("redaction fixture must be a non-empty list of cases")
    return cases


class RedactionFixtureParityTest(unittest.TestCase):
    """The SDK redactor must match the shared fixture the server also verifies.

    The SDK and server intentionally keep separate copies of the redaction
    logic (the SDK ships zero dependencies and cannot import server code), so
    this fixture is the contract that keeps them from drifting apart.
    """

    def test_fixture_cases_match_expected(self) -> None:
        for case in load_redaction_cases():
            value = case["input"]
            expected = case["expected"]
            with self.subTest(case=case["name"]):
                if isinstance(value, str):
                    self.assertEqual(_redact_secret_text(value), expected)
                else:
                    self.assertEqual(_safe_capture(value), expected)


if __name__ == "__main__":
    unittest.main()
