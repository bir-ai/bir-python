"""Capture failures must never reach the traced call.

Capturing a value runs code Bir does not own: a mapping's ``items()``, a
sequence's ``__iter__``, an exception's ``__str__``, a subclass's ``__len__``.
The objects that fail there are ordinary — a config client, a lazily-loading row
proxy, a result set whose second page is gone — and none of them should decide
whether the traced call succeeds or what it returns. Bookkeeping is not allowed
to become the failure.

These tests pin both halves. Whatever a captured value does, the call keeps its
own result and its own status; and what could be read is still recorded, with a
visible ``[uncapturable]`` marker for what could not, so a failed capture is
never mistaken for a value that was absent. A caller who passes a non-mapping
where a mapping belongs still gets a ``TypeError``: that is a mistake in the
call, not a value misbehaving while it is read.
"""

from __future__ import annotations

import functools
import json
import os
import tempfile
import unittest
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import bir
from bir import _capture, configure, generation, observe, retrieval, score, span, tool_call, trace
from bir._sdk import _UNCAPTURABLE, _reset_config_for_tests, _safe_capture, _safe_error
from bir.evals import Dataset, DatasetExample, EvalResult, custom_evaluator, run_experiment

TRACE_PATH = Path(".bir/traces.jsonl")
FAILURE = "backend unavailable"


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


def read_events() -> list[dict[str, Any]]:
    return [json.loads(line) for line in TRACE_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


class UnreadableMapping(Mapping):
    """A mapping that cannot be read at all, like a client whose backend is down.

    Every way in fails, so the guard cannot be satisfied by falling back to a
    different accessor: ``keys()`` for a copy, ``items()`` for a walk,
    ``__getitem__`` and ``__iter__`` for everything else. ``__len__`` works, so
    the mapping is non-empty and truthy and nothing skips it as vacuous.
    """

    def keys(self) -> Any:
        raise ConnectionError(FAILURE)

    def items(self) -> Any:
        raise ConnectionError(FAILURE)

    def __getitem__(self, key: Any) -> Any:
        raise ConnectionError(FAILURE)

    def __iter__(self) -> Any:
        raise ConnectionError(FAILURE)

    def __len__(self) -> int:
        return 1


class HalfReadableMapping(Mapping):
    """A mapping whose walk fails after two entries, like a paged result set."""

    def keys(self) -> Any:
        raise ConnectionError(FAILURE)

    def items(self) -> Any:
        yield "first", 1
        yield "second", 2
        raise ConnectionError(FAILURE)

    def __getitem__(self, key: Any) -> Any:
        raise KeyError(key)

    def __iter__(self) -> Any:
        return iter(("first", "second"))

    def __len__(self) -> int:
        return 2


class UnreadableSequence(list):
    def __iter__(self) -> Any:
        raise ConnectionError(FAILURE)


class HalfReadableSequence(list):
    def __iter__(self) -> Iterator[Any]:
        yield "first"
        yield "second"
        raise ConnectionError(FAILURE)


class UnreadableError(Exception):
    def __str__(self) -> str:
        raise ConnectionError(FAILURE)


class CaptureFailureTests(unittest.TestCase):
    """A captured value that raises never changes the traced call's outcome."""

    def setUp(self) -> None:
        _reset_config_for_tests()

    def test_unreadable_argument_leaves_the_call_alone(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)

            @observe(name="handler")
            def handler(payload: Any) -> str:
                return "business result"

            # The body must run and its result must reach the caller, and the
            # recorded event must not blame the function for Bir's failure.
            self.assertEqual(handler({"rows": UnreadableMapping()}), "business result")

            event = read_events()[0]
            self.assertEqual(event["status"], "success")
            self.assertIsNone(event["error"])
            self.assertEqual(event["input"], {"payload": {"rows": {_UNCAPTURABLE: _UNCAPTURABLE}}})
            self.assertEqual(event["output"], "business result")

    def test_unreadable_return_value_still_reaches_the_caller(self) -> None:
        with temporary_workdir():
            configure(capture_outputs=True)
            result = {"rows": UnreadableMapping()}

            @observe(name="handler")
            def handler() -> dict[str, Any]:
                return result

            # The function already produced its value; capture runs afterwards
            # and must not be able to take it away.
            self.assertIs(handler(), result)

            event = read_events()[0]
            self.assertEqual(event["status"], "success")
            self.assertEqual(event["output"], {"rows": {_UNCAPTURABLE: _UNCAPTURABLE}})

    def test_unreadable_sequence_leaves_the_call_alone(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True)

            @observe(name="handler")
            def handler(payload: Any) -> str:
                return "ok"

            self.assertEqual(handler(UnreadableSequence([1, 2])), "ok")

            self.assertEqual(read_events()[0]["input"], {"payload": [_UNCAPTURABLE]})

    def test_a_walk_that_fails_part_way_keeps_what_it_read(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True)

            @observe(name="handler")
            def handler(mapping: Any, sequence: Any) -> str:
                return "ok"

            self.assertEqual(handler(HalfReadableMapping(), HalfReadableSequence()), "ok")

            # Entries read before the failure are kept, and the marker says the
            # rest is missing -- the shape an item limit already produces.
            self.assertEqual(
                read_events()[0]["input"],
                {
                    "mapping": {"first": 1, "second": 2, _UNCAPTURABLE: _UNCAPTURABLE},
                    "sequence": ["first", "second", _UNCAPTURABLE],
                },
            )

    def test_one_unreadable_entry_does_not_cost_its_siblings(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True)

            @observe(name="handler")
            def handler(payload: Any) -> str:
                return "ok"

            self.assertEqual(handler({"good": "kept", "bad": UnreadableMapping()}), "ok")

            self.assertEqual(
                read_events()[0]["input"],
                {"payload": {"good": "kept", "bad": {_UNCAPTURABLE: _UNCAPTURABLE}}},
            )

    def test_redaction_still_applies_around_an_unreadable_entry(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True)

            @observe(name="handler")
            def handler(payload: Any) -> str:
                return "ok"

            handler({"api_key": "sk-real-secret-value", "bad": UnreadableMapping()})

            raw = TRACE_PATH.read_text(encoding="utf-8")
            self.assertNotIn("sk-real-secret-value", raw)
            self.assertIn("[redacted]", raw)

    def test_capture_failure_does_not_replace_the_raised_exception(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True)

            @observe(name="handler")
            def handler() -> str:
                raise UnreadableError()

            # The caller must see its own exception, not the one Bir hit while
            # rendering the message for the event.
            with self.assertRaises(UnreadableError):
                handler()

            event = read_events()[0]
            self.assertEqual(event["status"], "error")
            self.assertEqual(event["error"], "<unrepresentable UnreadableError>")

    def test_a_signature_bir_cannot_bind_does_not_refuse_the_call(self) -> None:
        def widen(func: Any) -> Any:
            # functools.wraps sets __wrapped__, so inspect.signature reports the
            # narrow inner signature for a wrapper that accepts anything.
            @functools.wraps(func)
            def inner(*args: Any, **kwargs: Any) -> str:
                return f"got {args} {kwargs}"

            return inner

        with temporary_workdir():
            configure(capture_inputs=True)

            @observe(name="handler")
            @widen
            def handler(only: Any) -> Any:
                return only

            self.assertEqual(handler(1, 2, extra=3), "got (1, 2) {'extra': 3}")

            self.assertEqual(read_events()[0]["input"], {_UNCAPTURABLE: _UNCAPTURABLE})


class UnreadableMetadataTests(unittest.TestCase):
    """A ``metadata=`` mapping is copied before it is captured; that read is guarded too."""

    def setUp(self) -> None:
        _reset_config_for_tests()

    def metadata_of(self, name: str) -> Any:
        return next(event["metadata"] for event in read_events() if event["name"] == name)

    def test_trace_metadata(self) -> None:
        with temporary_workdir():
            with trace("request", metadata=UnreadableMapping()):
                pass
            self.assertEqual(self.metadata_of("request"), {_UNCAPTURABLE: _UNCAPTURABLE})

    def test_work_context_metadata(self) -> None:
        with temporary_workdir():
            with trace("request"):
                with generation("llm", metadata=UnreadableMapping()):
                    pass
                with tool_call("search", metadata=UnreadableMapping()):
                    pass
                with retrieval("docs", query="q", metadata=UnreadableMapping()):
                    pass
                score("quality", 1.0, metadata=UnreadableMapping())

            for name in ("llm", "search", "quality"):
                with self.subTest(event=name):
                    self.assertEqual(self.metadata_of(name), {_UNCAPTURABLE: _UNCAPTURABLE})
            # retrieval() adds its own kind marker alongside what it could read.
            self.assertEqual(
                self.metadata_of("docs"),
                {_UNCAPTURABLE: _UNCAPTURABLE, "kind": "retrieval"},
            )

    def test_set_metadata(self) -> None:
        with temporary_workdir():
            with trace("request"):
                with span("step") as step:
                    step.set_metadata(UnreadableMapping())
            self.assertEqual(self.metadata_of("step"), {_UNCAPTURABLE: _UNCAPTURABLE})

    def test_observe_metadata(self) -> None:
        with temporary_workdir():

            @observe(name="handler", metadata=UnreadableMapping())
            def handler() -> str:
                return "ok"

            self.assertEqual(handler(), "ok")
            self.assertEqual(self.metadata_of("handler"), {_UNCAPTURABLE: _UNCAPTURABLE})

    def test_prompt_metadata_and_variables(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True)
            record = bir.prompt(
                "greeting",
                template="hello",
                variables=UnreadableMapping(),
                metadata=UnreadableMapping(),
                capture_variables=True,
            )
            with trace("request"):
                with generation("llm", prompt=record):
                    pass

            recorded = self.metadata_of("llm")["prompt"]
            self.assertEqual(recorded["variables"], {_UNCAPTURABLE: _UNCAPTURABLE})
            self.assertEqual(recorded["metadata"], {_UNCAPTURABLE: _UNCAPTURABLE})

    def test_a_non_mapping_is_still_the_caller_s_mistake(self) -> None:
        # A value that misbehaves while it is read is Bir's problem to absorb; a
        # value of the wrong type is a bug at the call site and still surfaces.
        not_a_mapping = cast(Any, [1, 2])
        with temporary_workdir():
            for label, call in (
                ("trace", lambda: trace("request", metadata=not_a_mapping)),
                ("generation", lambda: generation("llm", metadata=not_a_mapping)),
                ("tool_call", lambda: tool_call("search", metadata=not_a_mapping)),
                ("retrieval", lambda: retrieval("docs", query="q", metadata=not_a_mapping)),
            ):
                with self.subTest(primitive=label):
                    with self.assertRaisesRegex(TypeError, "must be a mapping"):
                        call()


class CaptureHelperTests(unittest.TestCase):
    """The guarantee stated directly on the helpers the whole SDK captures through."""

    def setUp(self) -> None:
        _reset_config_for_tests()

    def test_safe_capture_never_raises(self) -> None:
        for label, value in (
            ("unreadable mapping", UnreadableMapping()),
            ("unreadable sequence", UnreadableSequence([1])),
            ("nested", {"outer": [UnreadableMapping()]}),
        ):
            with self.subTest(value=label):
                # No assertion about the shape here on purpose: the contract is
                # that this call returns, whatever it was handed.
                _safe_capture(value)

    def test_the_whole_value_guard_catches_what_the_walks_do_not(self) -> None:
        # The container guards cover the paths a misbehaving value actually
        # takes, so nothing ordinary reaches the outer one. It exists because
        # "capture never raises" has to hold for the paths nobody predicted --
        # an exotic __len__, an ABC whose isinstance hook fails -- so the failure
        # is injected rather than staged, and what is asserted is that the net is
        # wired at all.
        with patch.object(_capture, "_capture_value", side_effect=ConnectionError(FAILURE)):
            self.assertEqual(_safe_capture({"anything": 1}), _UNCAPTURABLE)

    def test_safe_error_never_raises(self) -> None:
        self.assertEqual(_safe_error(UnreadableError()), "<unrepresentable UnreadableError>")

    def test_marker_is_distinguishable_from_an_absent_value(self) -> None:
        self.assertIsNotNone(_UNCAPTURABLE)
        self.assertNotEqual(_safe_capture(UnreadableMapping()), {})
        self.assertNotEqual(_safe_capture(UnreadableSequence([1])), [])


class ExperimentCaptureFailureTests(unittest.TestCase):
    """An evaluator's metadata cannot abort the run that produced it."""

    def setUp(self) -> None:
        _reset_config_for_tests()

    def test_unreadable_evaluator_metadata_does_not_abort_the_run(self) -> None:
        with temporary_workdir():
            evaluator = custom_evaluator(
                "checked",
                # EvalResult accepts any Mapping at runtime and normalizes it;
                # the annotation names the normalized form.
                lambda output, expected: EvalResult(
                    name="checked",
                    value=1.0,
                    metadata=cast(Any, UnreadableMapping()),
                ),
            )
            result = run_experiment(
                "run",
                dataset=Dataset([DatasetExample(id="one", input="q", expected="a")]),
                task=lambda question: "a",
                evaluators=[evaluator],
                path="run.jsonl",
            )

            self.assertEqual(result.status, "success")
            self.assertEqual(result.aggregate_scores, {"checked": 1.0})

    def test_unreadable_task_output_does_not_abort_the_run(self) -> None:
        with temporary_workdir():
            result = run_experiment(
                "run",
                dataset=Dataset([DatasetExample(id="one", input="q", expected="a")]),
                task=lambda question: {"rows": UnreadableMapping()},
                evaluators=[custom_evaluator("always", lambda output, expected: EvalResult("always", 1.0))],
                path="run.jsonl",
            )

            self.assertEqual(result.status, "success")
            self.assertEqual(result.results[0].output, {"rows": {_UNCAPTURABLE: _UNCAPTURABLE}})


if __name__ == "__main__":
    unittest.main()
