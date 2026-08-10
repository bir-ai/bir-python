"""Which *fields* redaction is pointed at, as opposed to which patterns it knows.

``test_redaction_parity.py`` and ``test_custom_redaction.py`` both ask the other
question: given a credential in a scanned field, is this format recognized. Those
say nothing about which fields are scanned in the first place, and the answer is
a deliberate split that nothing pinned: what the application passed as content is
scanned, and what identifies the record is written as given.

The split is not an oversight to be closed later. A name and a model are how a
record is found and read back -- ``bir traces --name``, the tree ``bir show``
prints, and the ``model_prices`` table that fills in a generation's cost all key
on them -- so replacing one with ``[redacted]`` would destroy the record without
un-leaking anything: the credential is already wherever it came from. Two of the
identity fields are filled in from a third party rather than by the developer (a
provider echoes ``model`` back, a framework bridge announces an event ``name``),
which is the case worth stating rather than the case for scanning them.

These run the real primitives against a real store and read the JSONL back, so
what is pinned is what lands on disk rather than what a helper returns. Adding a
public surface that takes a string means adding a row here, whichever column it
belongs in; changing which column it is in should be a decision someone makes on
purpose, and this is what makes them say so.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import bir
from bir import load_traces
from bir._sdk import _reset_config_for_tests

SECRET = "sk-live-abcdefghijklmnopqrstuvwxyz0123456789"


@contextmanager
def temporary_store() -> Iterator[Path]:
    previous = Path.cwd()
    with tempfile.TemporaryDirectory() as directory:
        workdir = Path(directory)
        os.chdir(workdir)
        try:
            yield workdir / "traces.jsonl"
        finally:
            os.chdir(previous)
            _reset_config_for_tests()


def _record(body: Callable[[], None], **configure_kwargs: object) -> str:
    """Run ``body`` against a fresh store and return everything it wrote."""

    with temporary_store() as trace_path:
        bir.configure(
            trace_path=str(trace_path),
            capture_inputs=True,
            capture_outputs=True,
            **configure_kwargs,  # type: ignore[arg-type]
        )
        body()
        raw = trace_path.read_text(encoding="utf-8")
    if not raw.strip():
        raise AssertionError("the surface wrote nothing; the case cannot say anything about redaction")
    return raw


def _trace_name() -> None:
    with bir.trace(name=SECRET):
        pass


def _trace_metadata_value() -> None:
    with bir.trace(name="t", metadata={"note": SECRET}):
        pass


def _trace_metadata_key() -> None:
    with bir.trace(name="t", metadata={SECRET: "v"}):
        pass


def _span_metadata() -> None:
    with bir.trace(name="t"), bir.span(name="s") as span:
        span.set_metadata({"note": SECRET})


def _generation_metadata() -> None:
    with bir.trace(name="t"), bir.generation(name="g", model="m", metadata={"note": SECRET}):
        pass


def _generation_input() -> None:
    with bir.trace(name="t"), bir.generation(name="g", model="m", input={"prompt": SECRET}):
        pass


def _generation_output() -> None:
    with bir.trace(name="t"), bir.generation(name="g", model="m") as generation:
        generation.set_output({"answer": SECRET})


def _generation_model_argument() -> None:
    with bir.trace(name="t"), bir.generation(name="g", model=SECRET):
        pass


def _generation_set_model() -> None:
    with bir.trace(name="t"), bir.generation(name="g") as generation:
        generation.set_model(SECRET)


def _tool_call_name() -> None:
    with bir.trace(name="t"), bir.tool_call(name=SECRET):
        pass


def _retrieval_name() -> None:
    with bir.trace(name="t"), bir.retrieval(name=SECRET, query="q"):
        pass


def _retrieval_query() -> None:
    with bir.trace(name="t"), bir.retrieval(name="r", query=SECRET):
        pass


def _retrieval_document() -> None:
    with bir.trace(name="t"), bir.retrieval(name="r", query="q") as retrieval:
        retrieval.add_document(text=SECRET)


def _score_name() -> None:
    with bir.trace(name="t"):
        bir.score(name=SECRET, value=1.0)


def _score_metadata() -> None:
    with bir.trace(name="t"):
        bir.score(name="s", value=1.0, metadata={"note": SECRET})


def _observed_input() -> None:
    @bir.observe()
    def echo(value: str) -> str:
        return "ok"

    echo(SECRET)


def _observed_output() -> None:
    @bir.observe()
    def produce() -> str:
        return SECRET

    produce()


def _raising_call() -> None:
    @bir.observe()
    def boom() -> None:
        raise RuntimeError(f"failed with {SECRET}")

    with suppress(RuntimeError):
        boom()


def _plain_trace() -> None:
    with bir.trace(name="t"):
        pass


SCANNED: tuple[tuple[str, Callable[[], None]], ...] = (
    ("trace(metadata={'note': secret})", _trace_metadata_value),
    ("trace(metadata={secret: 'v'})", _trace_metadata_key),
    ("span.set_metadata({'note': secret})", _span_metadata),
    ("generation(metadata={'note': secret})", _generation_metadata),
    ("generation(input={'prompt': secret})", _generation_input),
    ("generation.set_output({'answer': secret})", _generation_output),
    ("retrieval(query=secret)", _retrieval_query),
    ("retrieval.add_document(text=secret)", _retrieval_document),
    ("score(metadata={'note': secret})", _score_metadata),
    ("@observe() captured input", _observed_input),
    ("@observe() captured output", _observed_output),
    ("a traced call raises with the secret in its message", _raising_call),
)

RECORDED_AS_GIVEN: tuple[tuple[str, Callable[[], None]], ...] = (
    ("trace(name=secret)", _trace_name),
    ("tool_call(name=secret)", _tool_call_name),
    ("retrieval(name=secret)", _retrieval_name),
    ("score(name=secret)", _score_name),
    ("generation(model=secret)", _generation_model_argument),
    ("generation.set_model(secret)", _generation_set_model),
)

CONFIGURED_CONSTANTS: tuple[tuple[str, dict[str, object]], ...] = (
    ("configure(service_name=secret)", {"service_name": SECRET}),
    ("configure(environment=secret)", {"environment": SECRET}),
    ("configure(source=secret)", {"source": SECRET}),
)


class RedactionBoundaryTests(unittest.TestCase):
    """Content is scanned; identity is written as given."""

    def test_content_surfaces_are_scanned(self) -> None:
        for label, body in SCANNED:
            with self.subTest(surface=label):
                self.assertNotIn(SECRET, _record(body))

    def test_identity_surfaces_are_recorded_as_given(self) -> None:
        # Not a gap being tolerated: see the module docstring and the "Which
        # fields are scanned" table in docs/site/capture-privacy.md. Moving one
        # of these into the scanned set is a decision, and this is where it has
        # to be made.
        for label, body in RECORDED_AS_GIVEN:
            with self.subTest(surface=label):
                self.assertIn(SECRET, _record(body))

    def test_configured_service_constants_are_recorded_as_given(self) -> None:
        # Operator-set constants rather than anything an application passes at
        # runtime, so they are recorded exactly as configured.
        for label, configure_kwargs in CONFIGURED_CONSTANTS:
            with self.subTest(surface=label):
                self.assertIn(SECRET, _record(_plain_trace, **configure_kwargs))

    def test_the_documented_way_out_works(self) -> None:
        # The privacy page tells a reader who needs an identity value scanned to
        # put it in metadata as well and read it from there. Both halves of that
        # sentence have to hold at once, on the same event.
        with temporary_store() as trace_path:
            bir.configure(trace_path=str(trace_path))
            with bir.trace(name=SECRET, metadata={"tool": SECRET}):
                pass
            root = load_traces(str(trace_path))[0].root

            self.assertEqual(root.name, SECRET)
            self.assertEqual((root.metadata or {})["tool"], "[redacted]")


if __name__ == "__main__":
    unittest.main()
