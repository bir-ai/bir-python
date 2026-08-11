"""Reading a trace store an interrupted write damaged.

An event is appended with one buffered ``write()``, so a killed process, an OOM
kill, or a full disk can leave a truncated final line. Every reader validates
line by line and refuses the whole store on the first bad one, which turns one
damaged line into total loss of everything recorded before it.

These tests pin both halves of the answer: the strict default that a program
building on ``load_events`` / ``load_traces`` relies on, and the opt-in CLI read
that shows what survived and says what it could not read.

The commands that write have no such flag, because skipping a line there would
drop or duplicate recorded data rather than hide it from a table. ``prune`` has
one exception, pinned below: a final line the file never terminated. An event is
appended as one whole line ending in a newline, so a missing terminator proves
the bytes were never a record and no reader could ever read them — and refusing
them made the one command that reclaims space unusable on the store a full disk
produces. That opening is exactly one line wide. A line that was written whole
and cannot be parsed still refuses, wherever it sits, and ``send`` still refuses
either one.
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

    def test_send_stays_strict_about_the_interrupted_write(self) -> None:
        with temporary_workdir():
            record_traces(3)
            truncate_last_line()

            # Sending is not the repair path, and re-sending or dropping a record
            # is not something a transport may decide on its own, so this refuses
            # what prune now reads past. The remedy is to prune first.
            code, _out, err = run_cli("send")

            self.assertEqual(code, 1)
            self.assertIn("Invalid JSON in trace file", err)

    def test_writing_commands_do_not_accept_the_skip_flag(self) -> None:
        with temporary_workdir():
            record_traces(1)

            for command in (["send"], ["prune", "--keep-last", "1"]):
                with self.subTest(command=command[0]):
                    with self.assertRaises(SystemExit):
                        run_cli(*command, "--skip-invalid")


class PruneRepairsAnInterruptedWriteTests(unittest.TestCase):
    """``prune`` reads past a final line no write finished, and only that.

    This reverses a decision: prune used to refuse any store with a line it could
    not parse, which meant the store a full disk produces could not be shrunk at
    all, not even previewed with ``--dry-run``. The opening is bounded by the
    file's own bytes rather than by a flag — a line without a terminator — so the
    tests below pin both what it now reads past and what it still refuses.
    """

    def test_prune_completes_and_drops_the_unterminated_line(self) -> None:
        with temporary_workdir():
            record_traces(3)
            damaged = truncate_last_line()

            code, out, err = run_cli("prune", "--keep-last", "1", "--yes")

            self.assertEqual(code, 0)
            self.assertIn("removed=", out)
            self.assertIn("dropped an incomplete final line", err)
            self.assertIn(str(len(damaged.encode("utf-8"))), err)

            # The store is readable again, by the strict loaders and not just the
            # opt-in read, and the surviving trace kept every one of its events.
            traces = bir.load_traces()
            self.assertEqual(len(traces), 1)
            self.assertEqual(len(traces[0].events), 2)
            # As a line, not as a substring: the fragment is the head of an
            # event, and every event written in the same clock tick shares that
            # head, so a substring check passes or fails on the clock's
            # resolution rather than on the repair.
            self.assertNotIn(damaged, TRACE_PATH.read_text(encoding="utf-8").splitlines())
            self.assertTrue(TRACE_PATH.read_bytes().endswith(b"\n"))

    def test_a_dry_run_previews_the_drop_without_writing(self) -> None:
        with temporary_workdir():
            record_traces(3)
            truncate_last_line()
            before = TRACE_PATH.read_bytes()

            code, out, err = run_cli("prune", "--keep-last", "1", "--dry-run")

            self.assertEqual(code, 0)
            self.assertIn("dry run", out)
            self.assertIn("would drop an incomplete final line", err)
            self.assertEqual(TRACE_PATH.read_bytes(), before)

    def test_the_line_goes_even_when_the_selection_removes_nothing(self) -> None:
        with temporary_workdir():
            record_traces(3)
            truncate_last_line()

            # Keeping more traces than exist selects none for removal. The repair
            # must not depend on the selection filter happening to match.
            code, out, err = run_cli("prune", "--keep-last", "100", "--yes")

            self.assertEqual(code, 0)
            self.assertIn("removed=0", out)
            self.assertIn("dropped an incomplete final line", err)
            # Two, not three: the line the write never finished was the third
            # trace's root, so that trace has no root to be built from. Its child
            # event survives as an orphan, which is what the store held all along.
            self.assertEqual(len(bir.load_traces()), 2)

    def test_a_line_written_whole_still_refuses_wherever_it_is(self) -> None:
        with temporary_workdir():
            record_traces(3)
            append_line("{ half a line")
            at_end = TRACE_PATH.read_bytes()

            code, _out, err = run_cli("prune", "--keep-last", "1", "--yes")
            self.assertEqual(code, 1)
            self.assertIn("Invalid JSON in trace file", err)
            self.assertEqual(TRACE_PATH.read_bytes(), at_end)

            # The same line one row up, so the refusal is about the terminator
            # rather than about being last.
            lines = at_end.split(b"\n")
            TRACE_PATH.write_bytes(b"\n".join(lines[:2] + [b"{ half a line"] + lines[2:]))
            in_the_middle = TRACE_PATH.read_bytes()

            code, _out, err = run_cli("prune", "--keep-last", "1", "--yes")
            self.assertEqual(code, 1)
            self.assertIn("Invalid JSON in trace file", err)
            self.assertEqual(TRACE_PATH.read_bytes(), in_the_middle)

    def test_a_rotated_sibling_is_not_read_past(self) -> None:
        with temporary_workdir():
            record_traces(3)
            rotated = TRACE_PATH.with_name(TRACE_PATH.name + ".1")
            TRACE_PATH.replace(rotated)
            # A rotated file is never appended to, so a fragment there is not a
            # write in progress and prune has no reason to assume anything.
            with rotated.open("a", encoding="utf-8") as sibling:
                sibling.write('{"id":"frag')
            record_traces(1)
            before = rotated.read_bytes()

            code, _out, err = run_cli("prune", "--keep-last", "1", "--yes", "--include-rotated")

            self.assertEqual(code, 1)
            self.assertIn("Invalid JSON in trace file", err)
            self.assertEqual(rotated.read_bytes(), before)

    def test_a_healthy_store_reports_nothing_and_reclaims_the_same_bytes(self) -> None:
        with temporary_workdir():
            record_traces(3)

            code, out, err = run_cli("prune", "--keep-last", "1", "--yes", "--json")

            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertEqual(json.loads(out)["incomplete_tail_bytes"], 0)

    def test_the_json_result_carries_the_dropped_byte_count(self) -> None:
        with temporary_workdir():
            record_traces(3)
            damaged = truncate_last_line()

            code, out, err = run_cli("prune", "--keep-last", "1", "--yes", "--json")

            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(payload["incomplete_tail_bytes"], len(damaged.encode("utf-8")))
            # It is not an event, so it is not counted as one: the two removed
            # events are the one complete trace that --keep-last 1 dropped.
            self.assertEqual(payload["removed_events"], 2)
            self.assertGreater(payload["bytes_reclaimed"], payload["incomplete_tail_bytes"])
            # The report is on stderr, so --json still writes only JSON to stdout.
            self.assertIn("dropped an incomplete final line", err)


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
