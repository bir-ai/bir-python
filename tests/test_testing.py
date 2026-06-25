"""Tests for the public ``bir.testing.capture_traces`` instrumentation helper."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import bir
from bir import _sdk
from bir._sdk import LoadedTrace, TraceEvent
from bir.testing import CapturedTraces, capture_traces


class CaptureTracesTests(unittest.TestCase):
    """Exercise capture, isolation, config restoration, and cleanup."""

    def setUp(self) -> None:
        _sdk._reset_config_for_tests()

    def tearDown(self) -> None:
        _sdk._reset_config_for_tests()

    def test_captures_events_written_inside_block(self) -> None:
        with capture_traces() as captured:
            self.assertIsInstance(captured, CapturedTraces)
            with bir.trace("inside"):
                bir.score("quality", 1.0)

        events = captured.events()
        self.assertTrue(all(isinstance(event, TraceEvent) for event in events))
        self.assertEqual({event.type for event in events}, {"trace", "score"})
        trace_event = next(event for event in events if event.type == "trace")
        self.assertEqual(trace_event.name, "inside")

    def test_traces_groups_events_by_trace(self) -> None:
        with capture_traces() as captured:
            with bir.trace("grouped"):
                with bir.span("child"):
                    pass

        traces = captured.traces()
        self.assertEqual(len(traces), 1)
        self.assertIsInstance(traces[0], LoadedTrace)
        self.assertEqual(traces[0].name, "grouped")
        self.assertEqual([event.type for event in traces[0].events], ["trace", "span"])

    def test_isolates_capture_from_outer_traces_and_restores_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outer_path = Path(directory) / "outer.jsonl"
            bir.configure(trace_path=outer_path)
            saved_config = _sdk._config

            with bir.trace("before"):
                pass

            with capture_traces() as captured:
                # The active path is redirected away from the user's path.
                self.assertNotEqual(_sdk._config.trace_path, outer_path)
                with bir.trace("inside"):
                    pass

            with bir.trace("after"):
                pass

            # The capture saw only what was written inside the block.
            self.assertEqual([trace.name for trace in captured.traces()], ["inside"])
            # The user's configured path saw only the outer traces, never "inside".
            self.assertEqual(
                {trace.name for trace in bir.load_traces(outer_path)},
                {"before", "after"},
            )
            # The exact prior configuration (including trace_path) is restored.
            self.assertIs(_sdk._config, saved_config)
            self.assertEqual(_sdk._config.trace_path, outer_path)

    def test_config_restored_after_exception(self) -> None:
        before_config = _sdk._config
        captured: CapturedTraces | None = None

        with self.assertRaises(RuntimeError):
            with capture_traces() as handle:
                captured = handle
                with bir.trace("inside"):
                    pass
                raise RuntimeError("boom")

        assert captured is not None  # the block was entered, so the handle is bound
        # The prior configuration is restored even though the body raised.
        self.assertIs(_sdk._config, before_config)
        # Events written before the exception are still readable from the snapshot.
        self.assertEqual([trace.name for trace in captured.traces()], ["inside"])
        # The temporary file was still cleaned up.
        self.assertFalse(captured.trace_path.exists())

    def test_capture_stays_opt_out_by_default(self) -> None:
        with capture_traces() as captured:
            with bir.trace("t"):
                with bir.generation("llm", input={"prompt": "hi"}) as gen:
                    gen.set_output("yo")

        generation_event = next(event for event in captured.events() if event.type == "generation")
        self.assertIsNone(generation_event.input)
        self.assertIsNone(generation_event.output)

    def test_capture_and_redaction_apply_when_enabled(self) -> None:
        bir.configure(capture_inputs=True, capture_outputs=True)

        with capture_traces() as captured:
            with bir.trace("t"):
                with bir.generation("llm", input={"api_key": "sk-supersecretvalue"}) as gen:
                    gen.set_output("done")

        generation_event = next(event for event in captured.events() if event.type == "generation")
        # Capture is honored (opt-in preserved) and redaction is applied exactly as
        # it would be for a real file write.
        self.assertEqual(generation_event.input, {"api_key": "[redacted]"})
        self.assertEqual(generation_event.output, "done")

    def test_events_readable_live_inside_block(self) -> None:
        with capture_traces() as captured:
            with bir.trace("first"):
                pass
            self.assertEqual({trace.name for trace in captured.traces()}, {"first"})
            with bir.trace("second"):
                pass
            self.assertEqual({trace.name for trace in captured.traces()}, {"first", "second"})

        # After the block the snapshot returns the same captured traces.
        self.assertEqual({trace.name for trace in captured.traces()}, {"first", "second"})

    def test_nested_capture_blocks_are_isolated(self) -> None:
        before_config = _sdk._config

        with capture_traces() as outer:
            with bir.trace("outer-1"):
                pass
            with capture_traces() as inner:
                with bir.trace("inner-1"):
                    pass
            # The inner block captured only its own trace.
            self.assertEqual([trace.name for trace in inner.traces()], ["inner-1"])
            with bir.trace("outer-2"):
                pass

        self.assertEqual({trace.name for trace in outer.traces()}, {"outer-1", "outer-2"})
        self.assertEqual([trace.name for trace in inner.traces()], ["inner-1"])
        self.assertIs(_sdk._config, before_config)

    def test_temp_directory_removed_on_exit(self) -> None:
        with capture_traces() as captured:
            with bir.trace("t"):
                pass
            trace_path = captured.trace_path
            self.assertTrue(trace_path.exists())

        self.assertFalse(trace_path.exists())
        self.assertFalse(trace_path.parent.exists())

    def test_does_not_write_to_default_path(self) -> None:
        # With the default config, capture must not touch the real ``.bir`` file in
        # the working directory; everything goes to the private temp file. Compare
        # against the pre-existing state so a stray ``.bir/traces.jsonl`` left in
        # the repo by other work does not make this assertion flaky.
        default_path = _sdk._config.trace_path
        existed_before = default_path.exists()
        before_bytes = default_path.read_bytes() if existed_before else None

        with capture_traces() as captured:
            with bir.trace("t"):
                pass

        self.assertEqual([trace.name for trace in captured.traces()], ["t"])
        self.assertEqual(default_path.exists(), existed_before)
        if existed_before:
            self.assertEqual(default_path.read_bytes(), before_bytes)


if __name__ == "__main__":
    unittest.main()
