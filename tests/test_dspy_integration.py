from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from bir import configure, load_traces, trace
from bir._sdk import _reset_config_for_tests
from bir.integrations.dspy import trace_lm, trace_lm_async


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


class FakeResponse:
    """Mimics the LiteLLM-style response DSPy's ``LM.forward`` returns."""

    def __init__(self, *, model: str | None = None, usage: object | None = None) -> None:
        self.model = model
        self.usage = usage

    def model_dump(self) -> dict[str, object]:
        return {"model": self.model}


class FakeLM:
    """Mimics a ``dspy.LM`` whose ``forward`` returns a LiteLLM-style response."""

    def __init__(self, *, model: str, response: FakeResponse) -> None:
        self.model = model
        self._response = response
        self.received: dict[str, object] = {}

    def forward(self, *args: object, **kwargs: object) -> FakeResponse:
        self.received = dict(kwargs)
        return self._response

    async def aforward(self, *args: object, **kwargs: object) -> FakeResponse:
        self.received = dict(kwargs)
        return self._response


class DSPyIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        _reset_config_for_tests()

    def test_records_generation_with_model_and_usage(self) -> None:
        usage = FakeUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        response = FakeResponse(model="gpt-4o-mini-response", usage=usage)
        lm = FakeLM(model="gpt-4o-mini", response=response)

        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            result: object = None
            with trace("t"):
                result = trace_lm(lm.forward, messages=[{"role": "user", "content": "hi"}])

            self.assertIs(result, response)
            events = load_traces()[0].events
            gen = next(e for e in events if e.type == "generation")
            self.assertEqual(gen.name, "dspy.lm")
            # Request model comes from the bound LM, refined by the response model.
            self.assertEqual(gen.model, "gpt-4o-mini-response")
            self.assertEqual(gen.usage, {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
            self.assertEqual(gen.output, {"model": "gpt-4o-mini-response"})

    def test_request_model_from_bound_lm_when_response_omits_it(self) -> None:
        response = FakeResponse(model=None, usage=None)
        lm = FakeLM(model="gpt-4o-mini", response=response)

        with temporary_workdir():
            configure()
            with trace("t"):
                trace_lm(lm.forward, messages=[{"role": "user", "content": "hi"}])

            events = load_traces()[0].events
            gen = next(e for e in events if e.type == "generation")
            self.assertEqual(gen.model, "gpt-4o-mini")

    def test_missing_usage_records_generation_without_usage(self) -> None:
        response = FakeResponse(model="gpt-4o-mini", usage=None)
        lm = FakeLM(model="gpt-4o-mini", response=response)

        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            with trace("t"):
                trace_lm(lm.forward, messages=[{"role": "user", "content": "hi"}])

            events = load_traces()[0].events
            gen = next(e for e in events if e.type == "generation")
            self.assertIsNone(gen.usage)

    def test_capture_opt_in_records_output(self) -> None:
        response = FakeResponse(model="gpt-4o-mini")
        lm = FakeLM(model="gpt-4o-mini", response=response)

        with temporary_workdir():
            configure(capture_inputs=False, capture_outputs=False)
            with trace("t"):
                trace_lm(
                    lm.forward,
                    messages=[{"role": "user", "content": "hi"}],
                    bir_capture_output=True,
                )

            events = load_traces()[0].events
            gen = next(e for e in events if e.type == "generation")
            self.assertEqual(gen.output, {"model": "gpt-4o-mini"})

    def test_capture_off_omits_output(self) -> None:
        response = FakeResponse(model="gpt-4o-mini")
        lm = FakeLM(model="gpt-4o-mini", response=response)

        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            with trace("t"):
                trace_lm(
                    lm.forward,
                    messages=[{"role": "user", "content": "hi"}],
                    bir_capture_output=False,
                )

            events = load_traces()[0].events
            gen = next(e for e in events if e.type == "generation")
            self.assertIsNone(gen.output)

    def test_bir_prefixed_kwargs_not_forwarded(self) -> None:
        response = FakeResponse(model="gpt-4o-mini")
        lm = FakeLM(model="gpt-4o-mini", response=response)

        with temporary_workdir():
            configure()
            with trace("t"):
                trace_lm(
                    lm.forward,
                    messages=[{"role": "user", "content": "hi"}],
                    bir_name="custom.name",
                    bir_metadata={"k": "v"},
                    bir_capture_input=True,
                    bir_capture_output=True,
                )

        self.assertIn("messages", lm.received)
        self.assertNotIn("bir_name", lm.received)
        self.assertNotIn("bir_metadata", lm.received)
        self.assertNotIn("bir_capture_input", lm.received)
        self.assertNotIn("bir_capture_output", lm.received)

    def test_custom_bir_name_and_metadata(self) -> None:
        response = FakeResponse(model="gpt-4o-mini")
        lm = FakeLM(model="gpt-4o-mini", response=response)

        with temporary_workdir():
            configure()
            with trace("t"):
                trace_lm(
                    lm.forward,
                    messages=[{"role": "user", "content": "hi"}],
                    bir_name="my.dspy",
                    bir_metadata={"env": "test"},
                )

            events = load_traces()[0].events
            gen = next(e for e in events if e.type == "generation")
            self.assertEqual(gen.name, "my.dspy")
            self.assertEqual(gen.metadata["env"], "test")
            self.assertEqual(gen.metadata["integration"], "dspy")

    def test_no_active_trace_raises(self) -> None:
        response = FakeResponse(model="gpt-4o-mini")
        lm = FakeLM(model="gpt-4o-mini", response=response)

        with temporary_workdir():
            configure()
            with self.assertRaises(RuntimeError):
                trace_lm(lm.forward, messages=[{"role": "user", "content": "hi"}])

    def test_redaction_not_weakened(self) -> None:
        """bir_capture_input=True must still redact secrets from the input."""
        import json

        secret = "sk-proj-supersecretkey1234"
        response = FakeResponse(model="gpt-4o-mini")
        lm = FakeLM(model="gpt-4o-mini", response=response)

        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=False)
            with trace("t"):
                trace_lm(
                    lm.forward,
                    messages=[{"role": "user", "content": "hi"}],
                    api_key=secret,
                )

            events = load_traces()[0].events
            gen = next(e for e in events if e.type == "generation")
            if gen.input is not None:
                serialized = json.dumps(gen.input)
                self.assertNotIn(secret, serialized)

    def test_async_records_generation_with_usage(self) -> None:
        usage = FakeUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5)
        response = FakeResponse(model="gpt-4o-mini", usage=usage)
        lm = FakeLM(model="gpt-4o-mini", response=response)

        async def run() -> object:
            async with trace("t"):  # type: ignore[attr-defined]
                return await trace_lm_async(
                    lm.aforward, messages=[{"role": "user", "content": "hi"}]
                )

        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            result = asyncio.run(run())
            self.assertIs(result, response)
            events = load_traces()[0].events
            gen = next(e for e in events if e.type == "generation")
            self.assertEqual(gen.usage, {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5})


if __name__ == "__main__":
    unittest.main()
