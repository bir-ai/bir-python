from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from bir import configure, load_events, load_traces, trace
from bir._sdk import _reset_config_for_tests
from bir.integrations.instructor import trace_create, trace_create_async


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


class FakeUsage:
    def __init__(
        self,
        *,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class FakeCompletion:
    """Mimics an OpenAI ChatCompletion as returned by Instructor's raw completion."""

    def __init__(
        self,
        *,
        model: str | None = None,
        usage: object | None = None,
    ) -> None:
        self.model = model
        self.usage = usage


class FakeParsedModel:
    """Mimics a Pydantic model returned by Instructor (direct shape)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.model = None
        self.usage = None

    def model_dump(self) -> dict[str, object]:
        return {"name": self.name}


class InstructorIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        _reset_config_for_tests()

    def test_direct_shape_returns_result_and_records_generation(self) -> None:
        parsed = FakeParsedModel("Alice")
        usage = FakeUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        parsed.usage = usage  # type: ignore[assignment]

        def fake_create(**kwargs: object) -> object:
            return parsed

        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            result: object = None
            with trace("t"):
                result = trace_create(fake_create, model="gpt-4o-mini", response_model=FakeParsedModel)

            self.assertIs(result, parsed)
            events = load_traces()[0].events
            gen = next(e for e in events if e.type == "generation")
            self.assertEqual(gen.name, "instructor.create")
            self.assertEqual(gen.model, "gpt-4o-mini")
            self.assertEqual(gen.usage, {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})

    def test_tuple_shape_returns_result_and_records_generation(self) -> None:
        parsed = FakeParsedModel("Bob")
        completion = FakeCompletion(
            model="gpt-4o-mini-response",
            usage=FakeUsage(prompt_tokens=8, completion_tokens=4, total_tokens=12),
        )

        def fake_create(**kwargs: object) -> object:
            return (parsed, completion)

        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            result: object = None
            with trace("t"):
                result = trace_create(fake_create, model="gpt-4o-mini", response_model=FakeParsedModel)

            self.assertEqual(result, (parsed, completion))
            events = load_traces()[0].events
            gen = next(e for e in events if e.type == "generation")
            self.assertEqual(gen.model, "gpt-4o-mini-response")
            self.assertEqual(gen.usage, {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12})

    def test_missing_usage_records_generation_without_usage(self) -> None:
        parsed = FakeParsedModel("Carol")

        def fake_create(**kwargs: object) -> object:
            return parsed

        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            with trace("t"):
                trace_create(fake_create, model="gpt-4o-mini", response_model=FakeParsedModel)

            events = load_traces()[0].events
            gen = next(e for e in events if e.type == "generation")
            self.assertIsNone(gen.usage)

    def test_capture_opt_in_records_output(self) -> None:
        parsed = FakeParsedModel("Dave")

        def fake_create(**kwargs: object) -> object:
            return parsed

        with temporary_workdir():
            configure(capture_inputs=False, capture_outputs=False)
            with trace("t"):
                trace_create(
                    fake_create,
                    model="gpt-4o-mini",
                    response_model=FakeParsedModel,
                    bir_capture_output=True,
                )

            events = load_traces()[0].events
            gen = next(e for e in events if e.type == "generation")
            self.assertEqual(gen.output, {"name": "Dave"})

    def test_capture_off_omits_output(self) -> None:
        parsed = FakeParsedModel("Eve")

        def fake_create(**kwargs: object) -> object:
            return parsed

        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            with trace("t"):
                trace_create(
                    fake_create,
                    model="gpt-4o-mini",
                    response_model=FakeParsedModel,
                    bir_capture_output=False,
                )

            events = load_traces()[0].events
            gen = next(e for e in events if e.type == "generation")
            self.assertIsNone(gen.output)

    def test_bir_prefixed_kwargs_not_forwarded(self) -> None:
        received: dict[str, object] = {}

        def fake_create(**kwargs: object) -> object:
            received.update(kwargs)
            return FakeParsedModel("Frank")

        with temporary_workdir():
            configure()
            with trace("t"):
                trace_create(
                    fake_create,
                    model="gpt-4o-mini",
                    response_model=FakeParsedModel,
                    bir_name="custom.name",
                    bir_metadata={"k": "v"},
                    bir_capture_input=True,
                    bir_capture_output=True,
                )

        self.assertNotIn("bir_name", received)
        self.assertNotIn("bir_metadata", received)
        self.assertNotIn("bir_capture_input", received)
        self.assertNotIn("bir_capture_output", received)

    def test_no_active_trace_raises(self) -> None:
        def fake_create(**kwargs: object) -> object:
            return FakeParsedModel("Grace")  # pragma: no cover

        with temporary_workdir():
            configure()
            with self.assertRaises(RuntimeError):
                trace_create(fake_create, model="gpt-4o-mini", response_model=FakeParsedModel)

    def test_async_direct_shape(self) -> None:
        parsed = FakeParsedModel("Hannah")
        usage = FakeUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5)
        parsed.usage = usage  # type: ignore[assignment]

        async def fake_create_async(**kwargs: object) -> object:
            return parsed

        async def run() -> object:
            async with trace("t"):  # type: ignore[attr-defined]
                return await trace_create_async(
                    fake_create_async, model="gpt-4o-mini", response_model=FakeParsedModel
                )

        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            result = asyncio.run(run())
            self.assertIs(result, parsed)
            events = load_traces()[0].events
            gen = next(e for e in events if e.type == "generation")
            self.assertEqual(gen.usage, {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5})

    def test_custom_bir_name_and_metadata(self) -> None:
        parsed = FakeParsedModel("Ivan")

        def fake_create(**kwargs: object) -> object:
            return parsed

        with temporary_workdir():
            configure()
            with trace("t"):
                trace_create(
                    fake_create,
                    model="gpt-4o-mini",
                    response_model=FakeParsedModel,
                    bir_name="my.instructor",
                    bir_metadata={"env": "test"},
                )

            events = load_traces()[0].events
            gen = next(e for e in events if e.type == "generation")
            self.assertEqual(gen.name, "my.instructor")
            self.assertEqual(gen.metadata["env"], "test")
            self.assertEqual(gen.metadata["integration"], "instructor")

    def test_redaction_not_weakened(self) -> None:
        """bir_capture_input=True must still redact secrets from the input."""
        import json

        secret = "sk-proj-supersecretkey1234"

        def fake_create(**kwargs: object) -> object:
            return FakeParsedModel("Jane")

        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=False)
            with trace("t"):
                trace_create(
                    fake_create,
                    model="gpt-4o-mini",
                    api_key=secret,
                    response_model=FakeParsedModel,
                )

            events = load_traces()[0].events
            gen = next(e for e in events if e.type == "generation")
            if gen.input is not None:
                serialized = json.dumps(gen.input)
                self.assertNotIn(secret, serialized)


if __name__ == "__main__":
    unittest.main()
