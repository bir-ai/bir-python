"""Reading a trace store an interrupted write damaged.

An event is appended with one buffered ``write()``, so a killed process, an OOM
kill, or a full disk can leave a truncated final line. Every reader validates
line by line and refuses the whole store on the first bad one, which turns one
damaged line into total loss of everything recorded before it.

These tests pin both halves of the answer: the strict default that a program
building on ``load_events`` / ``load_traces`` relies on, and the opt-in CLI read
that shows what survived and says what it could not read. The commands that
write — ``send`` and ``prune`` — must stay strict, because skipping a line there
would drop or duplicate recorded data rather than hide it from a table.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import bir
from bir import cli
from bir._sdk import _reset_config_for_tests

TRACE_PATH = Path(".bir/traces.jsonl")


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


def run_cli(*argv: str) -> tuple[int, str, str]:
    """Run ``cli.main`` with captured stdout/stderr, returning (code, out, err)."""

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def record_traces(count: int) -> None:
    """Record ``count`` traces, each carrying one generation."""

    for index in range(count):
        with bir.trace(f"request-{index}"):
            with bir.generation("llm", model="gpt-4o-mini") as generation:
                generation.set_usage(input_tokens=11, output_tokens=4)


def truncate_last_line(keep: int = 60) -> str:
    """Cut the store's last line short, as an interrupted write would.

    Returns the surviving prefix so a test can show it never became an event.
    """

    lines = TRACE_PATH.read_bytes().split(b"\n")
    # The file ends with a newline, so the last written line is second to last.
    damaged = lines[-2][:keep]
    lines[-2] = damaged
    TRACE_PATH.write_bytes(b"\n".join(lines[:-1]))
    return damaged.decode("utf-8")


def append_line(text: str) -> None:
    """Append a raw line to the store, however malformed."""

    with TRACE_PATH.open("a", encoding="utf-8") as trace_file:
        trace_file.write(f"{text}\n")


class StrictReadingTests(unittest.TestCase):
    """The default refuses a store it cannot read completely."""

    def test_a_truncated_line_is_not_valid_json_and_stops_every_loader(self) -> None:
        with temporary_workdir():
            record_traces(3)
            damaged = truncate_last_line()

            with self.assertRaises(json.JSONDecodeError):
                json.loads(damaged)

            for loader in (bir.load_events, bir.load_traces):
                with self.subTest(loader=loader.__name__):
                    with self.assertRaises(ValueError) as raised:
                        loader()
                    self.assertIn("Invalid JSON in trace file", str(raised.exception))

    def test_the_events_before_the_damage_are_still_on_disk(self) -> None:
        with temporary_workdir():
            record_traces(3)
            truncate_last_line()

            readable = 0
            for line in TRACE_PATH.read_text(encoding="utf-8").splitlines():
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    continue
                readable += 1

            # Nothing is corrupted: five of six events are intact and only
            # unreachable because the reader refuses the file as a whole.
            self.assertEqual(readable, 5)

    def test_display_commands_refuse_by_default(self) -> None:
        with temporary_workdir():
            record_traces(3)
            truncate_last_line()

            for command in (["traces"], ["stats"], ["show", "any-id"]):
                with self.subTest(command=command[0]):
                    code, _out, err = run_cli(*command)
                    self.assertEqual(code, 1)
                    self.assertIn("Invalid JSON in trace file", err)

    def test_writing_commands_stay_strict(self) -> None:
        with temporary_workdir():
            record_traces(3)
            truncate_last_line()

            # Skipping a line here would delete or re-send data the user still
            # has, so these have no opt-out and must keep refusing.
            code, _out, err = run_cli("prune", "--keep-last", "1", "--dry-run")

            self.assertEqual(code, 1)
            self.assertIn("Invalid JSON in trace file", err)

    def test_writing_commands_do_not_accept_the_skip_flag(self) -> None:
        with temporary_workdir():
            record_traces(1)

            for command in (["send"], ["prune", "--keep-last", "1"]):
                with self.subTest(command=command[0]):
                    with self.assertRaises(SystemExit):
                        run_cli(*command, "--skip-invalid")


class SkipInvalidTests(unittest.TestCase):
    """``--skip-invalid`` reads what survived and says what it skipped."""

    def test_traces_shows_the_surviving_traces(self) -> None:
        with temporary_workdir():
            record_traces(3)
            truncate_last_line()

            code, out, err = run_cli("traces", "--skip-invalid")

            self.assertEqual(code, 0)
            self.assertIn("request-0", out)
            self.assertIn("request-1", out)
            self.assertIn("skipped 1 unreadable line", err)
            self.assertIn("Invalid JSON in trace file", err)

    def test_stats_reports_one_damaged_line_once(self) -> None:
        with temporary_workdir():
            record_traces(3)
            truncate_last_line()

            code, out, err = run_cli("stats", "--skip-invalid")

            self.assertEqual(code, 0)
            self.assertIn("traces", out)
            # ``stats`` reads the store twice; one damaged line is still one.
            self.assertIn("skipped 1 unreadable line;", err)
            self.assertNotIn("skipped 2", err)

    def test_show_finds_a_trace_that_survived(self) -> None:
        with temporary_workdir():
            record_traces(3)
            truncate_last_line()
            trace_id = run_cli("traces", "--skip-invalid", "--json")[1]
            first = json.loads(trace_id)[0]["id"]

            code, out, err = run_cli("show", first, "--skip-invalid")

            self.assertEqual(code, 0)
            self.assertIn("request-", out)
            self.assertIn("skipped 1 unreadable line", err)

    def test_json_output_stays_parseable(self) -> None:
        with temporary_workdir():
            record_traces(3)
            truncate_last_line()

            code, out, err = run_cli("traces", "--skip-invalid", "--json")

            self.assertEqual(code, 0)
            # The report goes to stderr precisely so this still parses.
            self.assertEqual(len(json.loads(out)), 2)
            self.assertIn("skipped 1 unreadable line", err)

    def test_several_damaged_lines_are_counted_and_pluralized(self) -> None:
        with temporary_workdir():
            record_traces(2)
            append_line("{not json at all")
            append_line("[1, 2, 3]")
            append_line('{"id": "missing-everything-else"}')

            code, out, err = run_cli("traces", "--skip-invalid")

            self.assertEqual(code, 0)
            self.assertIn("request-0", out)
            self.assertIn("skipped 3 unreadable lines", err)

    def test_a_healthy_store_reports_nothing(self) -> None:
        with temporary_workdir():
            record_traces(2)

            code, out, err = run_cli("traces", "--skip-invalid")

            self.assertEqual(code, 0)
            self.assertIn("request-0", out)
            # Nothing was skipped, so the user is told nothing.
            self.assertNotIn("skipped", err)

    def test_the_flag_changes_nothing_else_about_the_output(self) -> None:
        with temporary_workdir():
            record_traces(2)

            strict = run_cli("traces", "--json")
            lenient = run_cli("traces", "--json", "--skip-invalid")

            self.assertEqual(strict, lenient)


class LenientLoaderTests(unittest.TestCase):
    """The internal loaders hand every unreadable line to the caller."""

    def test_every_damaged_line_is_reported_not_just_the_first(self) -> None:
        from bir._sdk import _load_events_skipping_invalid

        with temporary_workdir():
            record_traces(1)
            append_line("{broken one")
            append_line("{broken two")

            errors: list[ValueError] = []
            events = _load_events_skipping_invalid(None, include_rotated=False, on_invalid=errors.append)

            self.assertEqual(len(events), 2)
            self.assertEqual(len(errors), 2)
            for error in errors:
                self.assertIsInstance(error, ValueError)

    def test_a_line_failing_validation_is_skipped_like_bad_json(self) -> None:
        from bir._sdk import _load_traces_skipping_invalid

        with temporary_workdir():
            record_traces(1)
            # Valid JSON, valid object, but not a valid event: the reader's
            # field validation must be skippable too, not only the JSON parse.
            append_line(json.dumps({"schema_version": "1.0", "id": "x"}))

            errors: list[ValueError] = []
            traces = _load_traces_skipping_invalid(None, include_rotated=False, on_invalid=errors.append)

            self.assertEqual(len(traces), 1)
            self.assertEqual(len(errors), 1)
            self.assertIn("missing required field", str(errors[0]))


if __name__ == "__main__":
    unittest.main()


class ExportOtelSkipInvalidTests(unittest.TestCase):
    """``export-otel`` reads past a damaged line like the other display commands.

    It streams the store in two passes now, so a damaged line is met once per
    pass; the report is de-duplicated, so it is still said once.
    """

    def setUp(self) -> None:
        _reset_config_for_tests()

    def test_it_reports_the_damaged_line_once_and_exports_the_rest(self) -> None:
        with temporary_workdir():
            record_traces(3)
            truncate_last_line()

            exported: list[object] = []

            def fake_export(traces, **kwargs):
                on_invalid = kwargs.get("on_invalid")
                # Drive both passes the way the real exporter does.
                from bir.integrations.otel import _TraceReader

                reader = _TraceReader(traces, include_rotated=False, on_invalid=on_invalid)
                list(reader.roots())
                exported.extend(reader.traces())
                from bir.integrations.otel import _ExportCounts

                return _ExportCounts(traces=len(exported), spans=len(exported))

            with mock.patch("bir.integrations.otel._export_traces", fake_export):
                code, _out, err = run_cli(
                    "export-otel",
                    "--path",
                    str(TRACE_PATH),
                    "--endpoint",
                    "http://collector.test/v1/traces",
                    "--skip-invalid",
                )

            self.assertEqual(code, 0)
            self.assertEqual(err.count("skipped"), 1, err)
            self.assertIn("skipped 1 unreadable line", err)
            self.assertTrue(exported)
