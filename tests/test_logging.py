"""Tests for the public ``bir.logging`` trace-id logging filter."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import threading
import unittest
import warnings
from contextlib import redirect_stderr
from pathlib import Path

import bir
from bir import _sdk
from bir.logging import (
    SPAN_ID_FIELD,
    TRACE_ID_FIELD,
    BirTraceIdFilter,
    install_trace_id_filter,
)


class _IsolatedTraceWritesTestCase(unittest.TestCase):
    """Run each test in a temp working directory with default SDK config.

    ``bir.trace`` writes a JSONL event on exit; redirecting the working directory
    keeps those writes out of the repo's ``.bir/`` while leaving the task-local id
    context (all this module asserts on) untouched.
    """

    def setUp(self) -> None:
        super().setUp()
        _sdk._reset_config_for_tests()
        self._previous_cwd = Path.cwd()
        self._temp_dir = tempfile.TemporaryDirectory(prefix="bir-logging-test-")
        os.chdir(self._temp_dir.name)

    def tearDown(self) -> None:
        os.chdir(self._previous_cwd)
        self._temp_dir.cleanup()
        _sdk._reset_config_for_tests()
        super().tearDown()


def _stamped_ids(record: logging.LogRecord) -> tuple[str | None, str | None]:
    """Read the ids the filter writes (dynamic ``LogRecord`` attributes)."""

    return getattr(record, TRACE_ID_FIELD), getattr(record, SPAN_ID_FIELD)


def _make_record() -> logging.LogRecord:
    """A bare ``LogRecord`` to push through a filter in isolation."""

    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )


class BirTraceIdFilterTests(_IsolatedTraceWritesTestCase):
    """The filter stamps the active ids onto a record and never drops it."""

    def test_field_name_constants_match_record_attributes(self) -> None:
        self.assertEqual(TRACE_ID_FIELD, "bir_trace_id")
        self.assertEqual(SPAN_ID_FIELD, "bir_span_id")

    def test_stamps_active_ids_inside_a_trace(self) -> None:
        record = _make_record()
        filter_ = BirTraceIdFilter()
        trace_id: str | None = None
        span_id: str | None = None
        kept = False

        with bir.trace("t"):
            trace_id = bir.get_current_trace_id()
            span_id = bir.get_current_span_id()
            kept = filter_.filter(record)

        self.assertTrue(kept)
        self.assertIsNotNone(trace_id)
        self.assertEqual(_stamped_ids(record), (trace_id, span_id))

    def test_span_id_tracks_innermost_span(self) -> None:
        record = _make_record()
        filter_ = BirTraceIdFilter()
        trace_id: str | None = None
        span_id: str | None = None

        with bir.trace("t"):
            with bir.span("child"):
                trace_id = bir.get_current_trace_id()
                span_id = bir.get_current_span_id()
                filter_.filter(record)

        stamped_trace_id, stamped_span_id = _stamped_ids(record)
        self.assertEqual(stamped_trace_id, trace_id)
        self.assertEqual(stamped_span_id, span_id)
        # The innermost span id differs from the trace root id.
        self.assertNotEqual(stamped_span_id, stamped_trace_id)

    def test_outside_a_trace_ids_are_none_and_nothing_raises(self) -> None:
        record = _make_record()
        filter_ = BirTraceIdFilter()

        kept = filter_.filter(record)

        self.assertTrue(kept)
        self.assertEqual(_stamped_ids(record), (None, None))

    def test_filter_renders_through_a_formatter(self) -> None:
        record = _make_record()
        filter_ = BirTraceIdFilter()
        formatter = logging.Formatter("trace=%(bir_trace_id)s span=%(bir_span_id)s")
        trace_id: str | None = None
        span_id: str | None = None

        with bir.trace("t"):
            trace_id = bir.get_current_trace_id()
            span_id = bir.get_current_span_id()
            filter_.filter(record)

        self.assertEqual(formatter.format(record), f"trace={trace_id} span={span_id}")


class InstallTraceIdFilterTests(_IsolatedTraceWritesTestCase):
    """End-to-end: attach the filter to a logger and capture stamped records."""

    def setUp(self) -> None:
        super().setUp()
        self.logger = logging.getLogger(f"bir.test.{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.handler = _MemoryHandler()
        self.logger.addHandler(self.handler)
        self.addCleanup(self.logger.removeHandler, self.handler)

    def test_install_returns_filter_attached_to_target(self) -> None:
        installed = install_trace_id_filter(self.logger)
        self.addCleanup(self.logger.removeFilter, installed)

        self.assertIsInstance(installed, BirTraceIdFilter)
        self.assertIn(installed, self.logger.filters)

    def test_install_defaults_to_the_root_logger_and_its_handlers(self) -> None:
        # Membership alone is what this used to assert, and it held while the
        # default stamped nothing an application emitted. It is kept only as a
        # shape check; what the default actually covers is asserted by emitting
        # records in PropagatedRecordTests.
        root = logging.getLogger()
        handler = logging.NullHandler()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)

        installed = install_trace_id_filter()
        self.addCleanup(handler.removeFilter, installed)
        self.addCleanup(root.removeFilter, installed)

        self.assertIn(installed, root.filters)
        self.assertIn(installed, handler.filters)

    def test_emitted_records_carry_ids_inside_and_outside_a_trace(self) -> None:
        installed = install_trace_id_filter(self.logger)
        self.addCleanup(self.logger.removeFilter, installed)

        trace_id: str | None = None
        span_id: str | None = None
        self.logger.info("outside")
        with bir.trace("t"):
            trace_id = bir.get_current_trace_id()
            span_id = bir.get_current_span_id()
            self.logger.info("inside")

        outside, inside = self.handler.records
        self.assertEqual(_stamped_ids(outside), (None, None))
        self.assertEqual(_stamped_ids(inside), (trace_id, span_id))

    def test_each_thread_observes_its_own_ids(self) -> None:
        installed = install_trace_id_filter(self.logger)
        self.addCleanup(self.logger.removeFilter, installed)
        seen: dict[str, str | None] = {}
        barrier = threading.Barrier(2)

        def worker(name: str) -> None:
            with bir.trace(name):
                barrier.wait()  # ensure both traces are open at once
                seen[name] = bir.get_current_trace_id()
                self.logger.info(name)

        threads = [threading.Thread(target=worker, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        by_message = {record.getMessage(): record for record in self.handler.records}
        self.assertEqual(_stamped_ids(by_message["a"])[0], seen["a"])
        self.assertEqual(_stamped_ids(by_message["b"])[0], seen["b"])
        # Concurrent traces produced distinct, non-bleeding ids.
        self.assertNotEqual(seen["a"], seen["b"])

    def test_each_asyncio_task_observes_its_own_ids(self) -> None:
        installed = install_trace_id_filter(self.logger)
        self.addCleanup(self.logger.removeFilter, installed)

        async def worker(name: str) -> str | None:
            with bir.trace(name):
                await asyncio.sleep(0)  # yield so the tasks interleave
                self.logger.info(name)
                return bir.get_current_trace_id()

        async def run() -> list[str | None]:
            return list(await asyncio.gather(worker("x"), worker("y")))

        x_id, y_id = asyncio.run(run())

        by_message = {record.getMessage(): record for record in self.handler.records}
        self.assertEqual(_stamped_ids(by_message["x"])[0], x_id)
        self.assertEqual(_stamped_ids(by_message["y"])[0], y_id)
        self.assertNotEqual(x_id, y_id)


class _MemoryHandler(logging.Handler):
    """Collect emitted records in memory for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


if __name__ == "__main__":
    unittest.main()


class PropagatedRecordTests(_IsolatedTraceWritesTestCase):
    """The arrangement the docs recommend: one install, application loggers stamped.

    A filter on a *logger* runs only for records that logger creates. Records from
    ``logging.getLogger("myapp")`` propagate to the root logger's handlers and
    never through the root logger's filters, so installing on the root logger left
    every application record unstamped — and because the documented format string
    asks for the attribute, ``logging`` dropped those lines and wrote a formatter
    error to stderr instead. These cases pin that a no-argument install covers the
    records an application actually emits.
    """

    def setUp(self) -> None:
        super().setUp()
        self.root = logging.getLogger()
        self.previous_handlers = self.root.handlers[:]
        self.previous_filters = self.root.filters[:]
        self.previous_level = self.root.level
        self.addCleanup(self._restore_root)
        self.root.handlers = []
        self.root.filters = []
        self.root.setLevel(logging.INFO)

    def _restore_root(self) -> None:
        self.root.handlers = self.previous_handlers
        self.root.filters = self.previous_filters
        self.root.setLevel(self.previous_level)

    def _root_handler(self) -> tuple[logging.Handler, io.StringIO]:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("[%(bir_trace_id)s|%(bir_span_id)s] %(message)s"))
        self.root.addHandler(handler)
        return handler, stream

    def _emit(self, logger: logging.Logger, message: str) -> str:
        """Emit a record, capturing anything ``logging`` writes to stderr.

        A record the formatter cannot render is dropped and reported on stderr, so
        the failure this module exists to prevent is invisible unless stderr is
        watched too.
        """

        errors = io.StringIO()
        with redirect_stderr(errors):
            logger.info(message)
        self.formatting_errors = errors.getvalue()
        return self.formatting_errors

    def test_a_no_argument_install_stamps_an_application_logger(self) -> None:
        _handler, stream = self._root_handler()
        install_trace_id_filter()

        trace_id: str | None = None
        with bir.trace("request"):
            trace_id = bir.get_current_trace_id()
            self._emit(logging.getLogger("myapp"), "hello")

        self.assertEqual(stream.getvalue().strip(), f"[{trace_id}|{trace_id}] hello")
        # The line has to survive, not merely carry the id: an unrendered record
        # is discarded rather than printed without it.
        self.assertEqual(self.formatting_errors, "")

    def test_a_deeply_nested_logger_is_stamped_too(self) -> None:
        _handler, stream = self._root_handler()
        install_trace_id_filter()

        trace_id: str | None = None
        with bir.trace("request"):
            trace_id = bir.get_current_trace_id()
            self._emit(logging.getLogger("myapp.db.pool"), "hello")

        self.assertIn(f"[{trace_id}|", stream.getvalue())

    def test_records_created_on_the_root_logger_are_still_stamped(self) -> None:
        _handler, stream = self._root_handler()
        install_trace_id_filter()

        trace_id: str | None = None
        with bir.trace("request"):
            trace_id = bir.get_current_trace_id()
            self._emit(self.root, "hello")

        self.assertIn(f"[{trace_id}|", stream.getvalue())

    def test_outside_a_trace_the_record_renders_with_none(self) -> None:
        _handler, stream = self._root_handler()
        install_trace_id_filter()

        self._emit(logging.getLogger("myapp"), "hello")

        self.assertEqual(stream.getvalue().strip(), "[None|None] hello")
        self.assertEqual(self.formatting_errors, "")

    def test_the_returned_filter_can_be_removed_from_every_target(self) -> None:
        handler, _stream = self._root_handler()

        installed = install_trace_id_filter()

        # One instance on each target, so the documented removal works on each.
        self.assertIn(installed, handler.filters)
        self.assertIn(installed, self.root.filters)
        handler.removeFilter(installed)
        self.root.removeFilter(installed)
        self.assertEqual(handler.filters, [])
        self.assertEqual(self.root.filters, [])

    def test_every_configured_handler_is_covered(self) -> None:
        _first, first_stream = self._root_handler()
        _second, second_stream = self._root_handler()

        install_trace_id_filter()
        trace_id: str | None = None
        with bir.trace("request"):
            trace_id = bir.get_current_trace_id()
            self._emit(logging.getLogger("myapp"), "hello")

        for stream in (first_stream, second_stream):
            self.assertIn(f"[{trace_id}|", stream.getvalue())

    def test_installing_before_any_handler_exists_warns(self) -> None:
        # This is the ordering the module used to document. It cannot work —
        # there is nothing to attach the stamp to — so it says so rather than
        # costing log lines in silence later.
        with self.assertWarnsRegex(RuntimeWarning, "no handlers on the root logger"):
            install_trace_id_filter()

    def test_a_configured_install_does_not_warn(self) -> None:
        self._root_handler()

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            install_trace_id_filter()

    def test_an_explicit_target_is_unchanged_and_never_warns(self) -> None:
        handler, stream = self._root_handler()
        handler.removeFilter  # the handler starts clean

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            installed = install_trace_id_filter(handler)

        self.assertIn(installed, handler.filters)
        self.assertEqual(self.root.filters, [])
        trace_id: str | None = None
        with bir.trace("request"):
            trace_id = bir.get_current_trace_id()
            self._emit(logging.getLogger("myapp"), "hello")
        self.assertIn(f"[{trace_id}|", stream.getvalue())


class DocumentedExampleTests(_IsolatedTraceWritesTestCase):
    """The module docstring's example, run as written."""

    def test_the_documented_recipe_stamps_an_application_log_line(self) -> None:
        root = logging.getLogger()
        previous = (root.handlers[:], root.filters[:], root.level)
        self.addCleanup(
            lambda: (
                setattr(root, "handlers", previous[0]),
                root.filters.clear(),
                root.filters.extend(previous[1]),
                root.setLevel(previous[2]),
            )
        )
        root.handlers = []
        root.filters = []

        stream = io.StringIO()
        logging.basicConfig(
            stream=stream,
            level=logging.INFO,
            format="%(levelname)s [trace=%(bir_trace_id)s span=%(bir_span_id)s] %(message)s",
            force=True,
        )
        install_trace_id_filter()

        errors = io.StringIO()
        trace_id: str | None = None
        with redirect_stderr(errors):
            with bir.trace("request"):
                trace_id = bir.get_current_trace_id()
                logging.getLogger("myapp").info("hello")

        self.assertIn(f"trace={trace_id}", stream.getvalue())
        self.assertEqual(errors.getvalue(), "")
