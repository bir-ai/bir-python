"""What a user is told when an experiment file cannot be trusted.

Experiment files are written by the SDK but read back from wherever a user kept
them — a CI artifact, a copied directory, a file edited by hand to fix a name.
Every field is validated on the way in, and each rejection carries the path, the
line, and the field, because that is what turns "invalid experiment" into
something a person can fix.

These tests assert those messages and the transport failures beside them, rather
than that a validation line ran.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bir._sdk import _reset_config_for_tests
from bir.evals import (
    Dataset,
    DatasetExample,
    exact_match,
    load_experiment,
    load_experiment_summary,
    run_experiment,
    send_experiment,
)

SERVER = "http://server.test"
ENDPOINT = f"{SERVER}/v1/experiments"


@contextmanager
def temporary_workdir() -> Iterator[Path]:
    previous = Path.cwd()
    with tempfile.TemporaryDirectory() as directory:
        workdir = Path(directory)
        os.chdir(workdir)
        try:
            yield workdir
        finally:
            os.chdir(previous)
            _reset_config_for_tests()


def result_row(**overrides: Any) -> dict[str, Any]:
    """Build a valid experiment result row, before any field is broken."""

    row = {
        "experiment_id": "experiment-1",
        "experiment_name": "faq",
        "id": "result-1",
        "example_id": "example-1",
        "input": {"question": "hello"},
        "expected": "hi",
        "output": "hi",
        "scores": [{"name": "exact_match", "value": 1.0, "metadata": {}}],
        "start_time": "2026-08-01T10:00:00+00:00",
        "end_time": "2026-08-01T10:00:01+00:00",
        "status": "success",
        "error": None,
        "trace_id": "trace-1",
    }
    row.update(overrides)
    return row


def summary_payload(**overrides: Any) -> dict[str, Any]:
    """Build a valid experiment summary, before any field is broken."""

    payload = {
        "schema_version": "1.0",
        "experiment_id": "experiment-1",
        "name": "faq",
        "start_time": "2026-08-01T10:00:00+00:00",
        "end_time": "2026-08-01T10:00:01+00:00",
        "status": "success",
        "example_count": 1,
        "error_count": 0,
        "aggregate_scores": {"exact_match": 1.0},
        "result_path": "faq.jsonl",
    }
    payload.update(overrides)
    return payload


def write_lines(path: Path, *lines: str) -> Path:
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


class ExperimentResultValidationTests(unittest.TestCase):
    """A result file is refused with the path, line, and field named."""

    def load(self, *lines: str) -> str:
        """Load the given lines and return the rejection message."""

        with temporary_workdir() as workdir:
            path = write_lines(workdir / "faq.jsonl", *lines)
            with self.assertRaises(ValueError) as raised:
                load_experiment(path)
            return str(raised.exception)

    def test_a_valid_file_loads(self) -> None:
        with temporary_workdir() as workdir:
            path = write_lines(workdir / "faq.jsonl", json.dumps(result_row()))

            experiment = load_experiment(path)

            self.assertEqual(experiment.id, "experiment-1")
            self.assertEqual(len(experiment.results), 1)
            self.assertEqual(experiment.results[0].scores[0].name, "exact_match")

    def test_an_unreadable_line_names_its_number(self) -> None:
        message = self.load(json.dumps(result_row()), "{not json")

        self.assertIn("Invalid JSON", message)
        self.assertIn("line 2", message)

    def test_a_non_object_line_is_refused(self) -> None:
        self.assertIn("must contain a JSON object", self.load("[1, 2, 3]"))

    def test_rows_from_two_experiments_are_refused(self) -> None:
        # A file holding two experiments would silently merge their results, so
        # it is rejected rather than half-read.
        message = self.load(
            json.dumps(result_row()),
            json.dumps(result_row(experiment_id="experiment-2")),
        )

        self.assertIn("multiple experiment IDs", message)

    def test_rows_disagreeing_on_the_name_are_refused(self) -> None:
        message = self.load(
            json.dumps(result_row()),
            json.dumps(result_row(experiment_name="other")),
        )

        self.assertIn("multiple experiment names", message)

    def test_a_missing_or_blank_required_string_names_the_field(self) -> None:
        for field_name in ("experiment_id", "experiment_name", "id", "example_id", "start_time", "end_time"):
            for broken in (None, "", 42):
                with self.subTest(field=field_name, value=broken):
                    message = self.load(json.dumps(result_row(**{field_name: broken})))
                    self.assertIn(f"field '{field_name}' must be a non-empty string", message)

    def test_an_unknown_status_is_refused(self) -> None:
        self.assertIn("must be success or error", self.load(json.dumps(result_row(status="maybe"))))

    def test_scores_must_be_a_list(self) -> None:
        self.assertIn("field 'scores' must be a list", self.load(json.dumps(result_row(scores={"a": 1}))))

    def test_an_error_field_must_be_a_string_or_null(self) -> None:
        self.assertIn("must be a string or null", self.load(json.dumps(result_row(error=500))))

    def test_a_trace_id_must_be_a_non_empty_string_or_null(self) -> None:
        for broken in ("", 7):
            with self.subTest(value=broken):
                message = self.load(json.dumps(result_row(trace_id=broken)))
                self.assertIn("field 'trace_id' must be a non-empty string or null", message)

    def test_the_payload_fields_must_be_present_even_when_null(self) -> None:
        # ``None`` is a legitimate recorded value, so presence is what is
        # checked; a missing key means the row was not written by Bir.
        for field_name in ("input", "expected", "output"):
            with self.subTest(field=field_name):
                row = result_row()
                del row[field_name]
                self.assertIn(f"missing required field '{field_name}'", self.load(json.dumps(row)))

    def test_a_score_entry_must_be_an_object(self) -> None:
        self.assertIn("score entries must be objects", self.load(json.dumps(result_row(scores=["exact_match"]))))

    def test_a_score_name_must_be_a_non_empty_string(self) -> None:
        for broken in (None, "", 1):
            with self.subTest(value=broken):
                row = result_row(scores=[{"name": broken, "value": 1.0}])
                self.assertIn("score field 'name' must be a non-empty string", self.load(json.dumps(row)))

    def test_a_score_must_carry_a_value(self) -> None:
        row = result_row(scores=[{"name": "exact_match"}])

        self.assertIn("score is missing required field 'value'", self.load(json.dumps(row)))

    def test_score_metadata_must_be_an_object(self) -> None:
        row = result_row(scores=[{"name": "exact_match", "value": 1.0, "metadata": [1]}])

        self.assertIn("score field 'metadata' must be an object", self.load(json.dumps(row)))

    def test_an_empty_file_without_a_summary_is_refused(self) -> None:
        self.assertIn("does not contain result rows", self.load(""))


class ExperimentSummaryValidationTests(unittest.TestCase):
    """A summary file is refused with the path and field named."""

    def load(self, text: str) -> str:
        with temporary_workdir() as workdir:
            path = workdir / "faq.summary.json"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(ValueError) as raised:
                load_experiment_summary(path)
            return str(raised.exception)

    def test_a_valid_summary_loads(self) -> None:
        with temporary_workdir() as workdir:
            path = workdir / "faq.summary.json"
            path.write_text(json.dumps(summary_payload()), encoding="utf-8")

            summary = load_experiment_summary(path)

            self.assertEqual(summary.experiment_id, "experiment-1")
            self.assertEqual(summary.aggregate_scores, {"exact_match": 1.0})

    def test_unreadable_json_is_refused(self) -> None:
        self.assertIn("Invalid JSON in experiment summary", self.load("{not json"))

    def test_a_non_object_summary_is_refused(self) -> None:
        self.assertIn("must contain a JSON object", self.load("[1, 2, 3]"))

    def test_aggregate_scores_must_be_an_object(self) -> None:
        message = self.load(json.dumps(summary_payload(aggregate_scores=[1, 2])))

        self.assertIn("field 'aggregate_scores' must be an object", message)

    def test_a_missing_or_blank_required_string_names_the_field(self) -> None:
        for field_name in (
            "schema_version",
            "experiment_id",
            "name",
            "start_time",
            "end_time",
            "status",
            "result_path",
        ):
            for broken in (None, "", 3):
                with self.subTest(field=field_name, value=broken):
                    message = self.load(json.dumps(summary_payload(**{field_name: broken})))
                    self.assertIn(f"field '{field_name}' must be a non-empty string", message)

    def test_counts_must_be_non_negative_integers(self) -> None:
        for field_name in ("example_count", "error_count"):
            # ``True`` is an int in Python; a count that arrived as a boolean
            # means the writer was not Bir, so it is refused with the rest.
            for broken in (-1, "1", True, None):
                with self.subTest(field=field_name, value=broken):
                    message = self.load(json.dumps(summary_payload(**{field_name: broken})))
                    self.assertIn(f"field '{field_name}' must be a non-negative integer", message)


class DefaultExperimentPathTests(unittest.TestCase):
    """An experiment written without a path lands somewhere predictable."""

    def test_it_is_written_under_the_default_directory(self) -> None:
        with temporary_workdir() as workdir:
            result = run_experiment(
                "faq answers",
                dataset=Dataset([DatasetExample(id="1", input={"q": "a"}, expected="a")]),
                task=lambda q: "a",
                evaluators=[exact_match()],
            )

            written = Path(result.path or "")
            self.assertTrue(written.is_file())
            self.assertEqual(written.parent, Path(".bir") / "experiments")
            # Spaces become separators so the name is usable as a filename.
            self.assertTrue(written.name.startswith("faq-answers-"))
            self.assertEqual((workdir / written).resolve().parent, (workdir / ".bir" / "experiments").resolve())

    def test_a_name_cannot_escape_the_directory(self) -> None:
        with temporary_workdir() as workdir:
            # Anything outside the safe character set collapses to a separator,
            # so a name carrying path syntax cannot write outside the store.
            result = run_experiment(
                "../../etc/passwd",
                dataset=Dataset([DatasetExample(id="1", input={"q": "a"}, expected="a")]),
                task=lambda q: "a",
                evaluators=[exact_match()],
            )

            written = (workdir / Path(result.path or "")).resolve()
            self.assertEqual(written.parent, (workdir / ".bir" / "experiments").resolve())
            self.assertNotIn("/", written.name)

    def test_a_name_of_only_unsafe_characters_still_writes(self) -> None:
        with temporary_workdir():
            result = run_experiment(
                "///",
                dataset=Dataset([DatasetExample(id="1", input={"q": "a"}, expected="a")]),
                task=lambda q: "a",
                evaluators=[exact_match()],
            )

            # Nothing usable survives sanitizing, so a stand-in name is used
            # rather than writing a file whose name is just an id.
            self.assertTrue(Path(result.path or "").name.startswith("experiment-"))


class FakeResponse:
    def __init__(self, body: str, *, status: int = 200) -> None:
        self.status = status
        self._body = body.encode("utf-8")

    def read(self, amt: int | None = None) -> bytes:
        # http.client.HTTPResponse.read takes an optional byte count, and
        # the transport passes one to bound a success response.
        return self._body if amt is None else self._body[:amt]

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def http_error(code: int, body: str = "denied") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url=ENDPOINT,
        code=code,
        msg="error",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body.encode("utf-8")),
    )


class SendExperimentErrorTests(unittest.TestCase):
    """Sending an experiment fails the way sending events does."""

    @contextmanager
    def serving(self, make_answer: Callable[[], Any]) -> Iterator[list[str]]:
        """Answer every request with a freshly built response, counting them."""

        attempts: list[str] = []

        def respond(request: Any, timeout: float = 0.0) -> Any:
            attempts.append(request.full_url)
            answer = make_answer()
            if isinstance(answer, BaseException):
                raise answer
            return answer

        with patch("bir._sending._opener.open", side_effect=respond), patch("bir._sending.time.sleep"):
            yield attempts

    def send(self, workdir: Path) -> Any:
        path = write_lines(workdir / "faq.jsonl", json.dumps(result_row()))
        (workdir / "faq.summary.json").write_text(json.dumps(summary_payload()), encoding="utf-8")
        return send_experiment(path, SERVER, retries=1)

    def test_a_server_error_is_retried_then_reported(self) -> None:
        with temporary_workdir() as workdir:
            with self.serving(lambda: http_error(503, "overloaded")) as attempts:
                with self.assertRaises(RuntimeError) as raised:
                    self.send(workdir)

            self.assertEqual(len(attempts), 2)
            self.assertIn("rejected experiment with HTTP 503", str(raised.exception))
            self.assertIn("overloaded", str(raised.exception))

    def test_a_rejected_experiment_is_not_retried(self) -> None:
        with temporary_workdir() as workdir:
            with self.serving(lambda: http_error(409, "already exists")) as attempts:
                with self.assertRaises(RuntimeError) as raised:
                    self.send(workdir)

            self.assertEqual(len(attempts), 1)
            self.assertIn("HTTP 409", str(raised.exception))

    def test_a_network_error_is_retried_and_names_the_endpoint(self) -> None:
        with temporary_workdir() as workdir:
            with self.serving(lambda: urllib.error.URLError("connection refused")) as attempts:
                with self.assertRaises(RuntimeError) as raised:
                    self.send(workdir)

            self.assertEqual(len(attempts), 2)
            self.assertIn(ENDPOINT, str(raised.exception))
            self.assertIn("connection refused", str(raised.exception))

    def test_a_read_timeout_is_retried(self) -> None:
        with temporary_workdir() as workdir:
            with self.serving(lambda: TimeoutError("timed out")) as attempts:
                with self.assertRaises(RuntimeError) as raised:
                    self.send(workdir)

            self.assertEqual(len(attempts), 2)
            self.assertIn("could not send experiment", str(raised.exception))

    def test_a_non_2xx_success_response_is_refused(self) -> None:
        with temporary_workdir() as workdir:
            with self.serving(lambda: FakeResponse("moved", status=302)):
                with self.assertRaises(RuntimeError) as raised:
                    self.send(workdir)

            self.assertIn("rejected experiment with HTTP 302", str(raised.exception))

    def test_an_unreadable_response_is_refused(self) -> None:
        for name, body, expected in (
            ("not json", "OK", "invalid experiment response JSON"),
            ("not an object", "[1]", "invalid experiment response"),
            ("accepted missing", json.dumps({"id": "x"}), "'accepted' must be an integer"),
            ("accepted is a bool", json.dumps({"accepted": True, "id": "x"}), "'accepted' must be an integer"),
            ("id missing", json.dumps({"accepted": 1}), "'id' must be a non-empty string"),
            ("id is blank", json.dumps({"accepted": 1, "id": ""}), "'id' must be a non-empty string"),
        ):
            with self.subTest(case=name), temporary_workdir() as workdir:
                with self.serving(lambda body=body: FakeResponse(body)):
                    with self.assertRaises(RuntimeError) as raised:
                        self.send(workdir)
                self.assertIn(expected, str(raised.exception))

    def test_an_empty_server_url_is_refused_before_any_request(self) -> None:
        with temporary_workdir() as workdir:
            path = write_lines(workdir / "faq.jsonl", json.dumps(result_row()))
            (workdir / "faq.summary.json").write_text(json.dumps(summary_payload()), encoding="utf-8")

            def fail(*_args: Any, **_kwargs: Any) -> None:
                raise AssertionError("no request should be made for an empty server URL")

            with patch("bir._sending._opener.open", side_effect=fail):
                for server_url in ("", "/", "///"):
                    with self.subTest(server_url=server_url):
                        with self.assertRaises(ValueError) as raised:
                            send_experiment(path, server_url)
                        self.assertIn("must not be empty", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
