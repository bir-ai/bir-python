"""Whether a value that *identifies* a record is required to be a string.

Every other kind of validation on these entry points was already pinned
somewhere: ``test_sdk.py`` covers empty names and non-finite scores,
``test_evals.py`` covers evaluator arguments, and
``test_experiment_loading_errors.py`` covers what the loaders refuse to *read*.
Nothing asked the writer's half of that last question -- whether an identity a
caller passes is a string before it is written into a ``schema_version = "1.0"``
file -- and the answer was that six entry points did not check, because each
checked emptiness by hand instead of calling the validator every other name uses.

Two consequences, and they are different from each other. ``prompt(name=3)``
wrote ``{"name": 3}`` and loaded back fine, so a consumer reading
``metadata.prompt.name`` got whichever JSON type the application happened to
pass. The other five wrote into fields their own loaders type-check, so the SDK
produced files it then refused to read: ``generation(model=3)`` cost the whole
trace file, and a non-string evaluator name, example id, or experiment name cost
the whole experiment file.

The empty-string errors these entry points already raised are unchanged, which
is the point of routing them through one validator rather than adding a second
check beside the first. ``generation(model="")`` is the one exception and is
pinned below as a change: the constructor accepted it while ``set_model("")``
refused it, and both now refuse it.

The second class pins what the sweep covered and found already guarded. It is
here so that "the rest of the public surface was driven" is a test rather than a
sentence in a changelog, and so that a new entry point that takes an identity
has an obvious place to be added to one column or the other.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import bir
from bir import evals
from bir._sdk import _reset_config_for_tests
from bir.integrations.openai import trace_chat_completion

# The types a `str`-hinted parameter can actually receive from an application
# that is not type-checked. `bool` is here because it is an `int` subclass and
# JSON writes it as `true`, and `bytes` because `str(b"x")` is `"b'x'"` -- a
# silent, wrong-looking string rather than an error.
#
# `None` is not in this table because it is a legitimate value on most of these
# parameters (an unset version, a generation with no model, a `configure` field
# left alone), so it is driven separately against the identities that are
# required.
NON_STRINGS: tuple[tuple[str, Any], ...] = (
    ("int", 3),
    ("float", 3.5),
    ("bool", True),
    ("dict", {"a": 1}),
    ("list", ["a"]),
    ("bytes", b"gpt-4o"),
)


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


def _run_experiment(**kwargs: Any) -> evals.ExperimentResult:
    kwargs.setdefault("name", "experiment")
    kwargs.setdefault("dataset", evals.Dataset([evals.DatasetExample(id="e1", input="x", expected="x")]))
    kwargs.setdefault("task", lambda value: value)
    kwargs.setdefault("evaluators", [evals.exact_match()])
    return evals.run_experiment(**kwargs)


def _run_experiment_async(**kwargs: Any) -> evals.ExperimentResult:
    async def task(value: Any) -> Any:
        return value

    kwargs.setdefault("name", "experiment")
    kwargs.setdefault("dataset", evals.Dataset([evals.DatasetExample(id="e1", input="x", expected="x")]))
    kwargs.setdefault("task", task)
    kwargs.setdefault("evaluators", [evals.exact_match()])
    return asyncio.run(evals.run_experiment_async(**kwargs))


# Each entry drives one identity that is written into a recorded file, with the
# field it lands in named beside it, the message the raise has to produce, and
# whether `None` means "not supplied" rather than a bad value.
WRITTEN_IDENTITIES: tuple[tuple[str, Callable[[Any], Any], str, bool], ...] = (
    ("prompt(name=...) -> metadata.prompt.name", lambda value: bir.prompt(value), "prompt name", True),
    (
        "prompt(version=...) -> metadata.prompt.version",
        lambda value: bir.prompt("p", version=value),
        "prompt version",
        False,
    ),
    ("generation(model=...) -> model", lambda value: bir.generation("g", model=value), "model", False),
    (
        "DatasetExample(id=...) -> example_id",
        lambda value: evals.DatasetExample(id=value, input="x"),
        "dataset example id",
        True,
    ),
    (
        "EvalResult(name=...) -> scores[].name",
        lambda value: evals.EvalResult(name=value, value=1.0),
        "eval result name",
        True,
    ),
    (
        "exact_match(name=...) -> scores[].name",
        lambda value: evals.exact_match(name=value),
        "evaluator name",
        True,
    ),
    (
        "custom_evaluator(name=...) -> scores[].name",
        lambda value: evals.custom_evaluator(value, lambda output, expected: True),
        "evaluator name",
        True,
    ),
    (
        "run_experiment(name=...) -> experiment_name",
        lambda value: _run_experiment(name=value),
        "experiment name",
        True,
    ),
    (
        "run_experiment_async(name=...) -> experiment_name",
        lambda value: _run_experiment_async(name=value),
        "experiment name",
        True,
    ),
)

# The same entry points on the empty string. Every one of these raised before the
# change and has to keep raising with the same message, since collapsing two
# checks into one validator is only safe if the error it already produced
# survives.
EMPTY_IDENTITIES: tuple[tuple[str, Callable[[], Any], str], ...] = (
    ("prompt('')", lambda: bir.prompt(""), "prompt name must not be empty"),
    ("prompt('p', version='')", lambda: bir.prompt("p", version=""), "prompt version must not be empty"),
    ("DatasetExample(id='')", lambda: evals.DatasetExample(id="", input="x"), "dataset example id must not be empty"),
    ("EvalResult(name='')", lambda: evals.EvalResult(name="", value=1.0), "eval result name must not be empty"),
    ("exact_match(name='')", lambda: evals.exact_match(name=""), "evaluator name must not be empty"),
    ("run_experiment(name='')", lambda: _run_experiment(name=""), "experiment name must not be empty"),
    (
        "run_experiment_async(name='')",
        lambda: _run_experiment_async(name=""),
        "experiment name must not be empty",
    ),
)


class IdentityFieldTypeTests(unittest.TestCase):
    """The six entry points that used to write a non-string identity to a file."""

    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_written_identities_reject_every_non_string(self) -> None:
        for label, call, field, _ in WRITTEN_IDENTITIES:
            for type_name, value in NON_STRINGS:
                with self.subTest(surface=label, value=type_name), temporary_workdir():
                    with self.assertRaises(TypeError) as raised:
                        call(value)
                    self.assertIn(f"{field} must be a string", str(raised.exception))

    def test_required_identities_reject_none(self) -> None:
        # `None` is the value most likely to arrive from an unset variable, and
        # it used to produce a misleading "must not be empty" on these, since
        # `not None` is true. Only the required identities are driven: an unset
        # `prompt(version=...)` or `generation(model=...)` is legitimately
        # `None`, which is what the last column of the table records.
        for label, call, field, required in WRITTEN_IDENTITIES:
            if not required:
                continue
            with self.subTest(surface=label), temporary_workdir():
                with self.assertRaises(TypeError) as raised:
                    call(None)
                self.assertIn(f"{field} must be a string", str(raised.exception))

    def test_the_optional_identities_still_accept_none(self) -> None:
        with temporary_workdir() as workdir:
            with bir.trace("t"):
                with bir.generation("g", model=None, prompt=bir.prompt("answer", version=None)):
                    pass

            events = bir.load_events(str(workdir / ".bir" / "traces.jsonl"))
            generation = next(event for event in events if event.type == "generation")
            self.assertIsNone(generation.model)
            self.assertEqual((generation.metadata or {})["prompt"], {"name": "answer"})

    def test_empty_identities_still_raise_what_they_always_raised(self) -> None:
        for label, call, message in EMPTY_IDENTITIES:
            with self.subTest(surface=label), temporary_workdir():
                with self.assertRaisesRegex(ValueError, message):
                    call()

    def test_generation_constructor_now_matches_set_model_on_the_empty_string(self) -> None:
        # The one behavior change beyond the type check. `set_model("")` refused
        # an empty model from the first release; the constructor recorded it, so
        # the same value was accepted or rejected depending on which of the two
        # the caller reached for.
        with temporary_workdir():
            with self.assertRaisesRegex(ValueError, "model must not be empty"):
                bir.generation("g", model="")

    def test_a_rejected_identity_leaves_the_trace_store_readable(self) -> None:
        # What the raise buys. The call is refused before anything is written,
        # so the events recorded around it still load; previously the bad model
        # was written and `load_events` then refused the file it was in --
        # including every other trace that file held.
        with temporary_workdir() as workdir:
            with bir.trace("t"):
                with bir.generation("g", model="gpt-4o"):
                    pass
                with self.assertRaises(TypeError):
                    bir.generation("g", model=3)  # type: ignore[arg-type]

            events = bir.load_events(str(workdir / ".bir" / "traces.jsonl"))
            models = [event.model for event in events if event.type == "generation"]
            self.assertEqual(models, ["gpt-4o"])

    def test_an_experiment_with_a_bad_identity_writes_no_file_at_all(self) -> None:
        # Every evaluation identity is refused where it is built or where the run
        # validates its arguments, which is before the first row is written --
        # so the outcome is no file, rather than a file whose rows
        # `load_experiment` refuses one at a time.
        with temporary_workdir() as workdir:
            path = workdir / "experiment.jsonl"

            with self.subTest(identity="evaluator name"):
                with self.assertRaises(TypeError):
                    _run_experiment(evaluators=[evals.exact_match(name=3)], path=str(path))  # type: ignore[arg-type]
                self.assertFalse(path.exists())

            with self.subTest(identity="example id"):
                with self.assertRaises(TypeError):
                    bad_example = evals.DatasetExample(id=3, input="x")  # type: ignore[arg-type]
                    _run_experiment(dataset=evals.Dataset([bad_example]), path=str(path))
                self.assertFalse(path.exists())

            with self.subTest(identity="experiment name"):
                with self.assertRaises(TypeError):
                    _run_experiment(name=3, path=str(path))  # type: ignore[arg-type]
                self.assertFalse(path.exists())

            _run_experiment(name="ok", path=str(path))
            self.assertEqual(len(evals.load_experiment(path).results), 1)

    def test_string_identities_still_record_and_load_back(self) -> None:
        # The other half: nothing about the ordinary path moved. A prompt name
        # and version, a model, an evaluator name, an example id and an
        # experiment name all still reach the file and read back as given.
        with temporary_workdir() as workdir:
            with bir.trace("t"):
                with bir.generation("g", model="gpt-4o", prompt=bir.prompt("answer", version="3")):
                    pass

            events = bir.load_events(str(workdir / ".bir" / "traces.jsonl"))
            generation = next(event for event in events if event.type == "generation")
            self.assertEqual(generation.model, "gpt-4o")
            self.assertEqual((generation.metadata or {})["prompt"], {"name": "answer", "version": "3"})

            experiment_path = workdir / "experiment.jsonl"
            _run_experiment(
                name="nightly",
                dataset=evals.Dataset([evals.DatasetExample(id="e1", input="x", expected="x")]),
                evaluators=[evals.exact_match(name="exact")],
                path=str(experiment_path),
            )
            loaded = evals.load_experiment(experiment_path)
            self.assertEqual(loaded.name, "nightly")
            self.assertEqual(loaded.results[0].example_id, "e1")
            self.assertEqual([score.name for score in loaded.results[0].scores], ["exact"])

    def test_a_provider_response_cannot_break_a_traced_call_with_a_bad_model(self) -> None:
        # `generation(model=...)` now raises, and the integrations pass a model
        # they read off a provider request or response into exactly that
        # argument. They funnel it through `_string_or_none` first, so a provider
        # that answers with a non-string records no model rather than raising
        # inside the call being traced. This is the guardrail the new raise could
        # plausibly have broken, so it is pinned on a bridge rather than argued.
        with temporary_workdir() as workdir:

            def fake_create(**kwargs: Any) -> object:
                return object()

            with bir.trace("chat"):
                trace_chat_completion(fake_create, model=3)

            events = bir.load_events(str(workdir / ".bir" / "traces.jsonl"))
            generation = next(event for event in events if event.type == "generation")
            self.assertEqual(generation.status, "success")
            self.assertIsNone(generation.model)


class IdentitySweepTests(unittest.TestCase):
    """What the sweep of the rest of the public surface found already guarded.

    Driven with the same values as the class above, across `bir.__all__`,
    `bir.evals`, `bir.logging` and `bir.testing`. These entry points reached
    `_validate_event_name` (or an equivalent check) before this change and still
    do; they are pinned because "the rest of the surface was checked" is only
    worth anything if a regression in one of them fails a test.
    """

    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_event_names_are_guarded(self) -> None:
        surfaces: tuple[tuple[str, Callable[[Any], Any]], ...] = (
            ("observe", lambda value: bir.observe(name=value)),
            ("span", lambda value: bir.span(value)),
            ("generation", lambda value: bir.generation(value)),
            ("tool_call", lambda value: bir.tool_call(value)),
            ("retrieval", lambda value: bir.retrieval(value, query="q")),
            ("trace", lambda value: _enter_trace(value)),
            ("score", lambda value: _record_score(value)),
        )
        for label, call in surfaces:
            for type_name, value in NON_STRINGS:
                with self.subTest(surface=label, value=type_name), temporary_workdir():
                    with self.assertRaisesRegex(TypeError, "must be a string"):
                        call(value)

    def test_configured_service_constants_are_guarded(self) -> None:
        for field in ("service_name", "environment", "source"):
            for type_name, value in NON_STRINGS:
                with self.subTest(field=field, value=type_name), temporary_workdir():
                    with self.assertRaisesRegex(TypeError, f"bir {field} must be a string"):
                        bir.configure(**{field: value})

    def test_the_generation_setters_are_guarded(self) -> None:
        surfaces: tuple[tuple[str, Callable[[Any], Any]], ...] = (
            ("set_model", lambda value: bir.generation("g").set_model(value)),
            ("set_cost(currency=...)", lambda value: bir.generation("g").set_cost(total_cost=1.0, currency=value)),
        )
        for label, call in surfaces:
            for type_name, value in NON_STRINGS:
                with self.subTest(surface=label, value=type_name), temporary_workdir():
                    with self.assertRaisesRegex(TypeError, "must be a string"):
                        call(value)

    def test_prompt_content_arguments_are_guarded(self) -> None:
        for type_name, value in NON_STRINGS:
            with self.subTest(value=type_name):
                with self.assertRaisesRegex(TypeError, "prompt template must be a string"):
                    bir.prompt("p", template=value)
                with self.assertRaisesRegex(TypeError, "rendered prompt must be a string"):
                    bir.prompt("p", rendered=value)

    def test_an_evaluators_expected_value_is_checked_when_the_example_is_known(self) -> None:
        # These are not identities and are validated at a different moment on
        # purpose: `expected` defaults to the example's own expected value, so
        # what it is cannot be known until an example is being scored. The check
        # exists, it just fires from `evaluate` rather than from the builder.
        surfaces: tuple[tuple[str, evals.DeterministicEvaluator], ...] = (
            ("contains", evals.contains(3)),  # type: ignore[arg-type]
            ("similarity_above", evals.similarity_above(0.5, 3)),  # type: ignore[arg-type]
        )
        for label, evaluator in surfaces:
            with self.subTest(evaluator=label):
                with self.assertRaisesRegex(TypeError, "expected value must be a string"):
                    evaluator.evaluate("x", expected=None)

        with self.assertRaisesRegex(TypeError, "expected value must be a string"):
            evals.retrieved_context_contains(3)  # type: ignore[arg-type]

    def test_the_evaluation_choice_arguments_are_guarded(self) -> None:
        # Not identities either -- each is one of a fixed set of words -- so a
        # non-string fails the membership test rather than a type check, which
        # is the same refusal by a different route.
        with temporary_workdir() as workdir:
            result = _run_experiment(path=str(workdir / "experiment.jsonl"))
            with self.assertRaisesRegex(ValueError, "format must be one of"):
                evals.render_experiment_report(result, format=3)  # type: ignore[arg-type]
            with self.assertRaisesRegex(ValueError, "missing_score must be one of"):
                evals.compare_experiments(result, result, missing_score=3)  # type: ignore[arg-type]
            with self.assertRaisesRegex(ValueError, "failed_examples must be one of"):
                evals.compare_experiments(result, result, failed_examples=3)  # type: ignore[arg-type]

    def test_the_read_back_dataclasses_are_guarded_by_their_loaders(self) -> None:
        # `TraceEvent`, `LoadedTrace` and the experiment result types are
        # outputs: the loaders build them, and those loaders type-check every
        # field they read -- which is exactly why the writer-side gap above
        # produced files the SDK could not read. Nothing writes a hand-built one
        # back out, so they are checked where they are read.
        with temporary_workdir() as workdir:
            with bir.trace("t"):
                with bir.generation("g", model="gpt-4o"):
                    pass

            # Patch the recorded model to what the writer can no longer produce,
            # so the reader's half of the contract is pinned against a real
            # event rather than a hand-written one that could drift from it.
            trace_path = workdir / ".bir" / "traces.jsonl"
            lines = trace_path.read_text(encoding="utf-8").splitlines()
            patched = []
            for line in lines:
                event = json.loads(line)
                if event["type"] == "generation":
                    event["model"] = 3
                patched.append(json.dumps(event))
            trace_path.write_text("\n".join(patched) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "field 'model' must be a string"):
                bir.load_events(str(trace_path))


def _enter_trace(name: Any) -> None:
    with bir.trace(name):
        pass


def _record_score(name: Any) -> None:
    with bir.trace("t"):
        bir.score(name, 1.0)


if __name__ == "__main__":
    unittest.main()
