"""Shared mutable state under real thread parallelism.

The SDK holds two pieces of state that threads share: the module-level
configuration, and the trace file. Writes to the file are serialized by an
explicit lock and already covered by the concurrent-write tests in
``test_sdk.py``. The configuration is not locked at all — ``configure()``
rebinds one immutable dataclass, so a recorder reads either the old object or
the new one — and nothing exercised that while recording was in flight.

On a GIL build these tests mostly prove the uninteresting half: nothing is
corrupted, nothing is lost, and no thread reads another's trace. They are
written for the free-threaded build, where the interpreter no longer serializes
the bytecode and a torn read becomes a real possibility. Running them there is
what makes the supported-Python claim on the stability page precise, so each one
reports which build it ran on when it fails.

What they do not do is manufacture a rare interleaving on demand. A single
unlucky scheduling of ``configure()`` between two reads is not reproducible by
adding threads, so the config is bound once per operation by construction rather
than defended by a probabilistic test.
"""

from __future__ import annotations

import os
import sys
import sysconfig
import tempfile
import threading
import time
import unittest
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import bir
from bir._sdk import _reset_config_for_tests

CAPTURED_INPUT = {"prompt": "hello"}
CAPTURED_OUTPUT = "hi"


def build_description() -> str:
    """Describe the interpreter, naming whether the GIL is disabled."""

    free_threaded = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
    gil_enabled = getattr(sys, "_is_gil_enabled", None)
    running_without_gil = free_threaded and gil_enabled is not None and not gil_enabled()
    build = "free-threaded" if free_threaded else "GIL"
    return f"{platform_version()} ({build} build, GIL {'off' if running_without_gil else 'on'} at runtime)"


def platform_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


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


class ConfigurationRaceTests(unittest.TestCase):
    """``configure()`` during recording is seen whole or not at all."""

    def test_capture_settings_flip_without_tearing_an_event(self) -> None:
        with temporary_workdir():
            recorded = 24
            stop = threading.Event()

            def flip_capture() -> None:
                # Rebind the whole config repeatedly while events are being
                # written. Each rebind swaps one immutable object for another,
                # so a recorder can only ever read a complete one. The yield
                # keeps the flipper from starving the recorders it is racing.
                enabled = False
                while not stop.is_set():
                    enabled = not enabled
                    bir.configure(capture_inputs=enabled, capture_outputs=enabled)
                    time.sleep(0)

            def record(index: int) -> None:
                with bir.trace(f"request-{index}"):
                    with bir.generation("llm", model="gpt-4o-mini", input=CAPTURED_INPUT) as generation:
                        generation.set_output(CAPTURED_OUTPUT)

            flipper = threading.Thread(target=flip_capture, daemon=True)
            flipper.start()
            try:
                with ThreadPoolExecutor(max_workers=8) as executor:
                    list(executor.map(record, range(recorded)))
            finally:
                stop.set()
                flipper.join(timeout=5)
            self.assertFalse(flipper.is_alive(), "the flipper thread outlived its test")

            events = bir.load_events()
            generations = [event for event in events if event.type == "generation"]
            self.assertEqual(len(generations), recorded, f"on {build_description()}")

            for event in generations:
                # Whichever configuration an event saw, it saw one of them: the
                # payload is either the recorded value or absent, never partial.
                self.assertIn(event.input, (None, CAPTURED_INPUT), f"on {build_description()}")
                self.assertIn(event.output, (None, CAPTURED_OUTPUT), f"on {build_description()}")
                # The flipper only ever sets both settings to the same value, so
                # an event capturing one but not the other read a half-applied
                # configuration. On a GIL build a single unlucky interleaving is
                # too rare for this to catch reliably — it catches a systematic
                # break, such as capture settings being applied one field at a
                # time. On a free-threaded build the window is real, which is
                # why this suite runs there.
                self.assertEqual(
                    event.input is not None,
                    event.output is not None,
                    f"event saw a half-applied configuration on {build_description()}",
                )
                # Fields that do not depend on capture are always recorded.
                self.assertEqual(event.model, "gpt-4o-mini", f"on {build_description()}")
                self.assertEqual(event.status, "success", f"on {build_description()}")

    def test_every_thread_keeps_its_own_trace(self) -> None:
        with temporary_workdir():
            workers = 8
            per_worker = 8

            def record(worker: int) -> None:
                for index in range(per_worker):
                    with bir.trace(f"worker-{worker}"):
                        with bir.span(f"step-{index}"):
                            pass

            with ThreadPoolExecutor(max_workers=workers) as executor:
                list(executor.map(record, range(workers)))

            events = bir.load_events()
            spans = [event for event in events if event.type == "span"]
            roots = {event.id: event for event in events if event.type == "trace"}

            self.assertEqual(len(roots), workers * per_worker, f"on {build_description()}")
            self.assertEqual(len(spans), workers * per_worker, f"on {build_description()}")
            for span in spans:
                # Trace context lives in context variables, which are per-thread.
                # A span parented outside its own trace would mean one thread
                # read another's active trace.
                parent_id = span.parent_id
                self.assertIsNotNone(parent_id, f"on {build_description()}")
                parent = roots.get(parent_id or "")
                self.assertIsNotNone(parent, f"on {build_description()}")
                assert parent is not None
                self.assertEqual(parent.trace_id, span.trace_id, f"on {build_description()}")

    def test_reconfiguring_the_trace_path_never_loses_a_started_trace(self) -> None:
        with temporary_workdir() as workdir:
            first = workdir / "first.jsonl"
            second = workdir / "second.jsonl"
            bir.configure(trace_path=first)
            stop = threading.Event()

            def flip_path() -> None:
                target = second
                while not stop.is_set():
                    bir.configure(trace_path=target)
                    target = first if target is second else second
                    time.sleep(0)

            def record(index: int) -> None:
                with bir.trace(f"request-{index}"):
                    pass

            flipper = threading.Thread(target=flip_path, daemon=True)
            flipper.start()
            try:
                with ThreadPoolExecutor(max_workers=8) as executor:
                    list(executor.map(record, range(24)))
            finally:
                stop.set()
                flipper.join(timeout=5)
            self.assertFalse(flipper.is_alive(), "the flipper thread outlived its test")

            # Every event landed in one of the two files and every line is
            # readable: a torn path would have produced a third file or a
            # truncated line.
            total = len(bir.load_events(first)) + len(bir.load_events(second))
            self.assertEqual(total, 24, f"on {build_description()}")
            written = {path.name for path in workdir.iterdir() if path.suffix == ".jsonl"}
            self.assertLessEqual(written, {"first.jsonl", "second.jsonl"}, f"on {build_description()}")


class BuildIdentificationTests(unittest.TestCase):
    """The suite can say which build it ran on, so CI logs are unambiguous."""

    def test_the_build_is_described_consistently(self) -> None:
        description = build_description()

        self.assertIn(platform_version(), description)
        self.assertIn("build", description)
        free_threaded = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
        self.assertIn("free-threaded" if free_threaded else "GIL", description)

    def test_a_free_threaded_build_reports_its_runtime_gil_state(self) -> None:
        if not sysconfig.get_config_var("Py_GIL_DISABLED"):
            self.skipTest("not a free-threaded build")

        # A free-threaded interpreter can still re-enable the GIL at runtime
        # (an extension may ask for it), so the claim has to name both.
        self.assertTrue(hasattr(sys, "_is_gil_enabled"))
        self.assertIn("GIL o", build_description())


if __name__ == "__main__":
    unittest.main()
