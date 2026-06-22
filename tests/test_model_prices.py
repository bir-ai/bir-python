"""Tests for the opt-in ``configure(model_prices=...)`` cost auto-fill.

These cover the user-supplied, local-only price table that derives a
generation's ``input_cost``/``output_cost``/``total_cost`` from its token usage:
the happy path and currency handling, that an explicit ``set_cost(...)`` always
wins, the edge cases that leave cost unset, the configure-time validation/error
paths, and the replace/clear/reset config semantics. Bir bundles no prices, so a
pristine config derives nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from bir import configure, generation, load_events, observe
from bir._sdk import (
    _Config,
    _MAX_MODEL_PRICES,
    _reset_config_for_tests,
    _validate_model_prices,
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


def read_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def generation_events(workdir: Path) -> dict[str, dict[str, Any]]:
    events = read_events(workdir / ".bir" / "traces.jsonl")
    return {event["name"]: event for event in events if event["type"] == "generation"}


class ModelPriceCostFillTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_config_for_tests()

    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_derives_input_output_and_total_cost_in_default_usd(self) -> None:
        with temporary_workdir() as workdir:
            configure(model_prices={"gpt-4o-mini": {"input": 0.000005, "output": 0.000015}})

            @observe()
            def answer() -> None:
                with generation("chat", model="gpt-4o-mini") as gen:
                    gen.set_usage(input_tokens=10, output_tokens=20)

            answer()

            event = generation_events(workdir)["chat"]
            input_cost = 0.000005 * 10
            output_cost = 0.000015 * 20
            self.assertEqual(
                event["cost"],
                {
                    "input_cost": input_cost,
                    "output_cost": output_cost,
                    "total_cost": input_cost + output_cost,
                },
            )
            self.assertEqual(event["currency"], "USD")

    def test_uses_configured_currency(self) -> None:
        with temporary_workdir() as workdir:
            configure(model_prices={"gpt-4o-mini": {"input": 0.000005, "currency": "EUR"}})

            @observe()
            def answer() -> None:
                with generation("chat", model="gpt-4o-mini") as gen:
                    gen.set_usage(input_tokens=4, output_tokens=8)

            answer()

            event = generation_events(workdir)["chat"]
            # Only the input rate is priced, so there is an input_cost but no total.
            self.assertEqual(event["cost"], {"input_cost": 0.000005 * 4})
            self.assertEqual(event["currency"], "EUR")

    def test_only_output_rate_prices_output_without_total(self) -> None:
        with temporary_workdir() as workdir:
            configure(model_prices={"m": {"output": 0.0001}})

            @observe()
            def answer() -> None:
                with generation("chat", model="m") as gen:
                    gen.set_usage(input_tokens=5, output_tokens=3)

            answer()

            event = generation_events(workdir)["chat"]
            self.assertEqual(event["cost"], {"output_cost": 0.0001 * 3})

    def test_zero_rate_records_zero_cost(self) -> None:
        with temporary_workdir() as workdir:
            configure(model_prices={"free": {"input": 0, "output": 0}})

            @observe()
            def answer() -> None:
                with generation("chat", model="free") as gen:
                    gen.set_usage(input_tokens=100, output_tokens=200)

            answer()

            event = generation_events(workdir)["chat"]
            self.assertEqual(event["cost"], {"input_cost": 0, "output_cost": 0, "total_cost": 0})

    def test_derived_cost_survives_load_events(self) -> None:
        with temporary_workdir():
            configure(model_prices={"gpt-4o-mini": {"input": 0.000005, "output": 0.000015}})

            @observe()
            def answer() -> None:
                with generation("chat", model="gpt-4o-mini") as gen:
                    gen.set_usage(input_tokens=10, output_tokens=20)

            answer()

            event = next(e for e in load_events() if e.type == "generation")
            input_cost = 0.000005 * 10
            output_cost = 0.000015 * 20
            self.assertEqual(
                event.cost,
                {
                    "input_cost": input_cost,
                    "output_cost": output_cost,
                    "total_cost": input_cost + output_cost,
                },
            )
            self.assertEqual(event.currency, "USD")

    def test_async_generation_derives_cost(self) -> None:
        with temporary_workdir() as workdir:
            configure(model_prices={"gpt-4o-mini": {"input": 0.000005, "output": 0.000015}})

            @observe()
            async def answer() -> None:
                async with generation("chat", model="gpt-4o-mini") as gen:
                    await asyncio.sleep(0)
                    gen.set_usage(input_tokens=10, output_tokens=20)

            asyncio.run(answer())

            event = generation_events(workdir)["chat"]
            self.assertEqual(event["cost"]["total_cost"], 0.000005 * 10 + 0.000015 * 20)

    def test_explicit_set_cost_is_never_overwritten(self) -> None:
        with temporary_workdir() as workdir:
            configure(model_prices={"gpt-4o-mini": {"input": 0.000005, "output": 0.000015}})

            @observe()
            def answer() -> None:
                with generation("chat", model="gpt-4o-mini") as gen:
                    gen.set_usage(input_tokens=10, output_tokens=20)
                    gen.set_cost(total_cost=0.99, currency="GBP")

            answer()

            event = generation_events(workdir)["chat"]
            self.assertEqual(event["cost"], {"total_cost": 0.99})
            self.assertEqual(event["currency"], "GBP")

    def test_explicit_partial_set_cost_blocks_any_derivation(self) -> None:
        with temporary_workdir() as workdir:
            configure(model_prices={"gpt-4o-mini": {"input": 0.000005, "output": 0.000015}})

            @observe()
            def answer() -> None:
                with generation("chat", model="gpt-4o-mini") as gen:
                    gen.set_usage(input_tokens=10, output_tokens=20)
                    # A caller-set input-only cost must not be topped up with a
                    # derived output_cost or total.
                    gen.set_cost(input_cost=0.123)

            answer()

            event = generation_events(workdir)["chat"]
            self.assertEqual(event["cost"], {"input_cost": 0.123})
            self.assertEqual(event["currency"], "USD")

    def test_no_table_leaves_cost_unset(self) -> None:
        with temporary_workdir() as workdir:

            @observe()
            def answer() -> None:
                with generation("chat", model="gpt-4o-mini") as gen:
                    gen.set_usage(input_tokens=10, output_tokens=20)

            answer()

            event = generation_events(workdir)["chat"]
            self.assertNotIn("cost", event)
            self.assertNotIn("currency", event)

    def test_unknown_model_leaves_cost_unset(self) -> None:
        with temporary_workdir() as workdir:
            configure(model_prices={"gpt-4o-mini": {"input": 0.000005}})

            @observe()
            def answer() -> None:
                with generation("chat", model="some-other-model") as gen:
                    gen.set_usage(input_tokens=10, output_tokens=20)

            answer()

            event = generation_events(workdir)["chat"]
            self.assertNotIn("cost", event)

    def test_missing_usage_leaves_cost_unset(self) -> None:
        with temporary_workdir() as workdir:
            configure(model_prices={"gpt-4o-mini": {"input": 0.000005}})

            @observe()
            def answer() -> None:
                with generation("chat", model="gpt-4o-mini"):
                    pass

            answer()

            event = generation_events(workdir)["chat"]
            self.assertNotIn("cost", event)

    def test_usage_without_token_split_leaves_cost_unset(self) -> None:
        with temporary_workdir() as workdir:
            configure(model_prices={"gpt-4o-mini": {"input": 0.000005, "output": 0.000015}})

            @observe()
            def answer() -> None:
                with generation("chat", model="gpt-4o-mini") as gen:
                    gen.set_usage(total_tokens=30)

            answer()

            event = generation_events(workdir)["chat"]
            self.assertNotIn("cost", event)

    def test_generation_without_model_leaves_cost_unset(self) -> None:
        with temporary_workdir() as workdir:
            configure(model_prices={"gpt-4o-mini": {"input": 0.000005}})

            @observe()
            def answer() -> None:
                with generation("chat") as gen:
                    gen.set_usage(input_tokens=10, output_tokens=20)

            answer()

            event = generation_events(workdir)["chat"]
            self.assertNotIn("cost", event)

    def test_input_only_usage_with_full_rates_prices_input_only(self) -> None:
        with temporary_workdir() as workdir:
            configure(model_prices={"gpt-4o-mini": {"input": 0.000005, "output": 0.000015}})

            @observe()
            def answer() -> None:
                with generation("chat", model="gpt-4o-mini") as gen:
                    gen.set_usage(input_tokens=10)

            answer()

            event = generation_events(workdir)["chat"]
            # No output tokens, so only the input side is priced and there is no total.
            self.assertEqual(event["cost"], {"input_cost": 0.000005 * 10})


class ModelPriceValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_config_for_tests()

    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_invalid_tables_raise_at_configure_time(self) -> None:
        cases: list[tuple[str, Any, type[Exception]]] = [
            ("non-mapping table", ["gpt", {"input": 1}], TypeError),
            ("non-string model key", {1: {"input": 1}}, TypeError),
            ("empty model key", {"": {"input": 1}}, ValueError),
            ("rates not a mapping", {"m": [1, 2]}, TypeError),
            ("empty rates mapping", {"m": {}}, ValueError),
            ("unknown rate key", {"m": {"input": 1, "prompt": 2}}, ValueError),
            ("only unknown rate key", {"m": {"per_token": 2}}, ValueError),
            ("negative rate", {"m": {"input": -1}}, ValueError),
            ("infinite rate", {"m": {"input": float("inf")}}, ValueError),
            ("nan rate", {"m": {"output": float("nan")}}, ValueError),
            ("boolean rate", {"m": {"input": True}}, TypeError),
            ("string rate", {"m": {"input": "1e-6"}}, TypeError),
            ("empty currency", {"m": {"input": 1, "currency": ""}}, ValueError),
            ("non-string currency", {"m": {"input": 1, "currency": 5}}, TypeError),
            ("none currency", {"m": {"input": 1, "currency": None}}, TypeError),
            ("oversized table", {f"m{i}": {"input": 1} for i in range(_MAX_MODEL_PRICES + 1)}, ValueError),
        ]
        for label, value, expected in cases:
            with self.subTest(case=label):
                with self.assertRaises(expected):
                    configure(model_prices=cast(Any, value))

    def test_unknown_rate_key_error_names_the_model_and_key(self) -> None:
        with self.assertRaisesRegex(ValueError, r"model_prices\['m'\] has unknown rate keys: 'prompt'"):
            configure(model_prices={"m": {"input": 1, "prompt": 2}})

    def test_validated_table_is_name_sorted_and_normalized(self) -> None:
        table = _validate_model_prices(
            {"zzz": {"output": 2e-6}, "aaa": {"input": 1e-6, "currency": "EUR"}}
        )
        self.assertEqual([name for name, _ in table], ["aaa", "zzz"])
        self.assertEqual(table[0][1].input, 1e-6)
        self.assertEqual(table[0][1].output, None)
        self.assertEqual(table[0][1].currency, "EUR")
        self.assertEqual(table[1][1].output, 2e-6)
        self.assertEqual(table[1][1].currency, "USD")
        # The normalized table is immutable and hashable so the frozen config stays hashable.
        self.assertIsInstance(hash(table), int)


class ModelPriceConfigSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_config_for_tests()

    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_default_config_bundles_no_prices(self) -> None:
        self.assertEqual(_Config().model_prices, ())

    def test_passing_table_replaces_previous_table(self) -> None:
        with temporary_workdir() as workdir:
            configure(model_prices={"old": {"input": 0.001}})
            configure(model_prices={"new": {"input": 0.002}})

            @observe()
            def answer() -> None:
                with generation("old_call", model="old") as gen:
                    gen.set_usage(input_tokens=1, output_tokens=1)
                with generation("new_call", model="new") as gen:
                    gen.set_usage(input_tokens=1, output_tokens=1)

            answer()

            events = generation_events(workdir)
            # The replaced "old" entry no longer prices anything.
            self.assertNotIn("cost", events["old_call"])
            self.assertEqual(events["new_call"]["cost"], {"input_cost": 0.002})

    def test_empty_mapping_clears_table(self) -> None:
        with temporary_workdir() as workdir:
            configure(model_prices={"m": {"input": 0.001}})
            configure(model_prices={})

            @observe()
            def answer() -> None:
                with generation("chat", model="m") as gen:
                    gen.set_usage(input_tokens=1, output_tokens=1)

            answer()

            self.assertNotIn("cost", generation_events(workdir)["chat"])

    def test_omitting_argument_preserves_table(self) -> None:
        with temporary_workdir() as workdir:
            configure(model_prices={"m": {"input": 0.001}})
            # An unrelated configure() call must leave the price table in place.
            configure(service_name="rag-api")

            @observe()
            def answer() -> None:
                with generation("chat", model="m") as gen:
                    gen.set_usage(input_tokens=2, output_tokens=1)

            answer()

            self.assertEqual(generation_events(workdir)["chat"]["cost"], {"input_cost": 0.001 * 2})

    def test_reset_for_tests_clears_table(self) -> None:
        configure(model_prices={"m": {"input": 0.001}})
        _reset_config_for_tests()
        self.assertEqual(_Config().model_prices, ())
        with temporary_workdir() as workdir:

            @observe()
            def answer() -> None:
                with generation("chat", model="m") as gen:
                    gen.set_usage(input_tokens=1, output_tokens=1)

            answer()

            self.assertNotIn("cost", generation_events(workdir)["chat"])


if __name__ == "__main__":
    unittest.main()
