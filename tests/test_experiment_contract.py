"""The SDK's experiment upload must match the shared contract fixture.

The SDK is the *producer* of experiment-result uploads and the Bir ingestion
server is the *consumer*: ``send_experiment`` POSTs a
``{"summary": ..., "results": [...]}`` body to ``/v1/experiments`` and the server
validates it. ``tests/fixtures/valid-experiment.json`` is a byte-for-byte copy of
the fixture the server locks as the canonical upload shape, so this test keeps the
producer from drifting away from the consumer's contract -- the experiment
counterpart of the event-schema and redaction-parity fixtures.

The upload legitimately omits two things the fixture carries, and the server
treats both as equivalent to what the SDK sends:

* the per-row ``experiment_id``/``experiment_name`` echo fields -- the SDK records
  these only in its on-disk JSONL artifact; the server re-derives them from the
  summary when it persists each row and ignores them on ingest.
* a ``trace_id`` of ``null`` -- the SDK drops the key when an example has no
  recorded trace and the server defaults the missing field back to ``null``.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bir.evals import send_experiment

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_EXPERIMENT_PATH = ROOT / "tests" / "fixtures" / "valid-experiment.json"

# Per-row metadata the SDK keeps only in its on-disk JSONL artifact; the upload
# omits it because the server re-derives it from the summary (see module docstring).
ECHO_FIELDS = ("experiment_id", "experiment_name")


def load_contract_experiment() -> dict[str, Any]:
    payload = json.loads(CONTRACT_EXPERIMENT_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("expected experiment fixture to be a JSON object")
    return payload


class FakeHttpResponse:
    """Minimal stand-in for the urllib response of a successful experiment POST."""

    status = 201

    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def capture_upload_payload(fixture: dict[str, Any]) -> dict[str, Any]:
    """Persist the fixture on disk and capture what ``send_experiment`` POSTs.

    The result rows are written verbatim -- including the ``experiment_id``/
    ``experiment_name`` echo fields -- because that is exactly the JSONL shape the
    SDK writes to disk. ``send_experiment`` then loads them back and serializes the
    upload body, which is the contract surface the server consumes.
    """

    with tempfile.TemporaryDirectory() as directory:
        result_path = Path(directory) / "contract-prompt-experiment-contract-1.jsonl"
        summary_path = result_path.with_suffix(".summary.json")
        with result_path.open("w", encoding="utf-8") as result_file:
            for row in fixture["results"]:
                result_file.write(json.dumps(row) + "\n")
        summary_path.write_text(json.dumps(fixture["summary"]), encoding="utf-8")

        response = FakeHttpResponse(b'{"accepted":1,"id":"experiment-contract-1"}')
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            send_experiment(result_path, "http://127.0.0.1:8000")

        request = urlopen.call_args.args[0]
        data = getattr(request, "data")
        if not isinstance(data, bytes):
            raise TypeError("expected request data to be bytes")
        payload = json.loads(data.decode("utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("expected upload body to be a JSON object")
        return payload


class ExperimentUploadContractTest(unittest.TestCase):
    """``send_experiment`` must serialize the shared fixture's exact upload shape."""

    def setUp(self) -> None:
        self.fixture = load_contract_experiment()
        self.payload = capture_upload_payload(self.fixture)

    def test_upload_envelope_matches_fixture(self) -> None:
        self.assertEqual(set(self.fixture), {"summary", "results"})
        self.assertEqual(set(self.payload), {"summary", "results"})
        self.assertEqual(len(self.payload["results"]), len(self.fixture["results"]))

    def test_summary_payload_matches_fixture_exactly(self) -> None:
        # The summary has no producer/consumer divergence, so it round-trips field
        # for field: schema_version, status, counts, aggregate_scores, result_path.
        self.assertEqual(self.payload["summary"], self.fixture["summary"])

    def test_result_rows_conform_to_fixture_shape(self) -> None:
        for index, (uploaded, expected_row) in enumerate(
            zip(self.payload["results"], self.fixture["results"])
        ):
            with self.subTest(result=index):
                # No on-disk echo fields leak into the upload ...
                for echo_field in ECHO_FIELDS:
                    self.assertNotIn(echo_field, uploaded)
                # ... and every remaining contract field matches the fixture exactly,
                # with a null trace_id dropped rather than sent.
                expected = {key: value for key, value in expected_row.items() if key not in ECHO_FIELDS}
                if expected_row.get("trace_id") is None:
                    expected.pop("trace_id", None)
                self.assertEqual(uploaded, expected)

    def test_score_rows_match_fixture_shape(self) -> None:
        for index, (uploaded, expected_row) in enumerate(
            zip(self.payload["results"], self.fixture["results"])
        ):
            with self.subTest(result=index):
                self.assertEqual(len(uploaded["scores"]), len(expected_row["scores"]))
                for score in uploaded["scores"]:
                    self.assertEqual(set(score), {"name", "value", "metadata"})
                    self.assertIsInstance(score["name"], str)
                    self.assertIsInstance(score["value"], (int, float))
                    self.assertNotIsInstance(score["value"], bool)
                    self.assertIsInstance(score["metadata"], dict)


if __name__ == "__main__":
    unittest.main()
