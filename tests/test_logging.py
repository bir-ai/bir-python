"""Tests for the public ``bir.logging`` trace-id logging filter."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading
import unittest
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

    def test_install_defaults_to_root_logger(self) -> None:
        root = logging.getLogger()
        installed = install_trace_id_filter()
        try:
            self.assertIn(installed, root.filters)
        finally:
            root.removeFilter(installed)

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
