"""Tests for the additive custom redaction rules configured via ``configure``.

These cover the user-supplied ``additional_secret_keys`` and
``additional_redaction_patterns`` options: that they widen redaction, that the
built-in rules can never be disabled or replaced by them, the replace/clear
semantics across ``configure`` calls, the validation/error paths, and that the
custom rules flow through every capture and persistence path (captured strings,
repr fallbacks, error text, prompt and score metadata, integration inputs and
outputs, and experiment JSONL/summary files).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from bir import configure, generation, load_events, observe, prompt, score, trace
from bir._sdk import (
    _is_secret_key,
    _redact_secret_text,
    _reset_config_for_tests,
    _safe_capture,
    _safe_error,
)
from bir.evals import DatasetExample, EvalResult, custom_evaluator, exact_match, run_experiment
from bir.integrations.openai import trace_chat_completion


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


class _Box:
    """An object with no JSON form, so capture falls back to its redacted repr."""

    def __init__(self, label: str) -> None:
        self._label = label

    def __repr__(self) -> str:
        return f"_Box({self._label})"


class AdditionalSecretKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_config_for_tests()

    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_additional_keys_redact_mapping_keys_case_insensitively(self) -> None:
        configure(additional_secret_keys=["ssn", "Badge"])
        captured = _safe_capture({"SSN": "123-45-6789", "badge": "A1", "name": "ok"})
        self.assertEqual(captured, {"SSN": "[redacted]", "badge": "[redacted]", "name": "ok"})

    def test_additional_keys_match_whole_name_not_substring(self) -> None:
        # Exact-match, unlike the built-in substring rules: a key that merely
        # contains the configured name is not redacted.
        configure(additional_secret_keys=["session"])
        self.assertTrue(_is_secret_key("session"))
        self.assertTrue(_is_secret_key("SESSION"))
        self.assertFalse(_is_secret_key("session_id"))
        self.assertFalse(_is_secret_key("my_session"))

    def test_additional_keys_treat_dash_and_underscore_as_equivalent(self) -> None:
        configure(additional_secret_keys=["badge-id"])
        self.assertTrue(_is_secret_key("badge_id"))
        self.assertTrue(_is_secret_key("BADGE-ID"))

    def test_builtin_secret_keys_still_apply_alongside_custom_keys(self) -> None:
        configure(additional_secret_keys=["ssn"])
        captured = _safe_capture({"api_key": "sk-builtin", "ssn": "123", "ok": "v"})
        self.assertEqual(captured, {"api_key": "[redacted]", "ssn": "[redacted]", "ok": "v"})

    def test_additional_keys_validation(self) -> None:
        cases: list[tuple[str, Any, type[Exception]]] = [
            ("bare string", "token", TypeError),
            ("non-string entry", [123], TypeError),
            ("empty entry", [""], ValueError),
            ("too long entry", ["k" * 201], ValueError),
            ("too many entries", [f"k{i}" for i in range(101)], ValueError),
        ]
        for label, value, expected in cases:
            with self.subTest(case=label):
                with self.assertRaises(expected):
                    configure(additional_secret_keys=cast(Any, value))


class AdditionalRedactionPatternTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_config_for_tests()

    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_patterns_redact_matches_in_captured_strings(self) -> None:
        configure(additional_redaction_patterns=[r"CUST-\d+"])
        self.assertEqual(_redact_secret_text("order CUST-123 shipped"), "order [redacted] shipped")
        self.assertEqual(_safe_capture(["CUST-1", "fine"]), ["[redacted]", "fine"])

    def test_compiled_patterns_are_accepted_and_keep_their_flags(self) -> None:
        configure(additional_redaction_patterns=[re.compile(r"zztop", re.IGNORECASE)])
        self.assertEqual(_redact_secret_text("band ZZTOP here"), "band [redacted] here")

    def test_patterns_redact_repr_fallback(self) -> None:
        configure(additional_redaction_patterns=[r"CUST-\d+"])
        self.assertEqual(_safe_capture(_Box("CUST-7")), "_Box([redacted])")

    def test_patterns_redact_error_text(self) -> None:
        configure(additional_redaction_patterns=[r"CUST-\d+"])
        self.assertEqual(_safe_error(RuntimeError("failed for CUST-9")), "failed for [redacted]")

    def test_builtin_patterns_still_apply_alongside_custom_patterns(self) -> None:
        configure(additional_redaction_patterns=[r"CUST-\d+"])
        # Built-in api_key/sk- rules and the custom rule both fire.
        self.assertEqual(
            _redact_secret_text("api_key=sk-ABCD1234efgh and CUST-1"),
            "api_key=[redacted] and [redacted]",
        )

    def test_patterns_validation(self) -> None:
        cases: list[tuple[str, Any, type[Exception]]] = [
            ("bare string", "abc", TypeError),
            ("single compiled pattern", re.compile("a"), TypeError),
            ("non-string entry", [123], TypeError),
            ("bytes pattern", [re.compile(b"a")], TypeError),
            ("empty entry", [""], ValueError),
            ("invalid regex", ["("], ValueError),
            ("too long entry", ["a" * 1001], ValueError),
            ("too many entries", ["a"] * 101, ValueError),
        ]
        for label, value, expected in cases:
            with self.subTest(case=label):
                with self.assertRaises(expected):
                    configure(additional_redaction_patterns=cast(Any, value))


class BuiltinCredentialFormatRedactionTests(unittest.TestCase):
    """Built-in best-effort rules for Stripe, Azure, and PEM private-key blocks.

    These rules ship on by default and can never be disabled, so they are
    asserted with the default config and across the same capture, repr, and
    error paths the custom rules use.
    """

    # Stripe-shaped sample tokens are assembled from split fragments so no literal
    # ``sk_live_…``/``sk_test_…`` key form exists in this file for repository
    # secret-scanning push protection to flag. The redactor still sees the joined
    # value at runtime, exactly as a real captured payload would contain it.
    # Bodies are kept short (well under the 24+ chars of a real Stripe key) and
    # ``EXAMPLE``-tagged so the sample tokens cannot be mistaken for live secrets,
    # while still satisfying the redactor's {16,} length rule.
    _SK = "sk"
    _RK = "rk"
    STRIPE_LIVE = f"{_SK}_live_EXAMPLEliveKEY1234"
    STRIPE_TEST = f"{_SK}_test_EXAMPLEtestKEY1234"
    STRIPE_RK_LIVE = f"{_RK}_live_EXAMPLErestrictA12"
    STRIPE_RK_TEST = f"{_RK}_test_EXAMPLErestrictB12"
    # A 512-bit key base64-encoded to 88 characters ending in ``==``.
    AZURE_KEY = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwx=="
    PEM_BLOCK = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA7Xn1abcDEF/123ghiJKL+456mnoPQR789stuVWX\n"
        "zyx0987wvu654tsr321qpo+nmlk/jihg==\n"
        "-----END RSA PRIVATE KEY-----"
    )

    def setUp(self) -> None:
        _reset_config_for_tests()

    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_stripe_keys_are_redacted(self) -> None:
        for token in (self.STRIPE_LIVE, self.STRIPE_TEST, self.STRIPE_RK_LIVE, self.STRIPE_RK_TEST):
            with self.subTest(token=token):
                self.assertEqual(_redact_secret_text(f"key {token} end"), "key [redacted] end")

    def test_stripe_near_misses_are_not_redacted(self) -> None:
        # Too-short bodies, the bare prefix, and a non-live/test middle segment
        # must not be touched. Assembled from fragments for the same reason as the
        # positive samples above.
        for benign in (f"{self._SK}_live_short", f"{self._RK}_test_", f"{self._SK}_prod_0123456789abcdef"):
            with self.subTest(benign=benign):
                self.assertEqual(_redact_secret_text(benign), benign)

    def test_azure_storage_key_is_redacted(self) -> None:
        self.assertEqual(_redact_secret_text(f"AccountKey {self.AZURE_KEY}"), "AccountKey [redacted]")

    def test_azure_near_miss_is_not_redacted(self) -> None:
        # A short base64 blob ending in ``==`` is below the anchored length class.
        self.assertEqual(_redact_secret_text("token dGVzdGluZw== here"), "token dGVzdGluZw== here")

    def test_pem_private_key_block_is_redacted(self) -> None:
        captured = _redact_secret_text(f"before\n{self.PEM_BLOCK}\nafter")
        self.assertEqual(captured, "before\n[redacted]\nafter")
        self.assertNotIn("PRIVATE KEY", captured)
        self.assertNotIn("MIIEpAIBAAKCAQEA", captured)

    def test_pem_label_variants_are_redacted(self) -> None:
        for label in ("PRIVATE KEY", "EC PRIVATE KEY", "OPENSSH PRIVATE KEY", "ENCRYPTED PRIVATE KEY"):
            block = f"-----BEGIN {label}-----\nQUJDREVG\n-----END {label}-----"
            with self.subTest(label=label):
                self.assertEqual(_redact_secret_text(block), "[redacted]")

    def test_benign_private_key_prose_is_not_redacted(self) -> None:
        text = "Store your PRIVATE KEY somewhere safe and never share it."
        self.assertEqual(_redact_secret_text(text), text)

    def test_new_formats_redacted_in_repr_fallback_and_error_text(self) -> None:
        self.assertEqual(_safe_capture(_Box(self.STRIPE_LIVE)), "_Box([redacted])")
        self.assertEqual(
            _safe_error(RuntimeError(f"leaked {self.STRIPE_RK_TEST}")),
            "leaked [redacted]",
        )

    def test_new_formats_redacted_in_trace_jsonl(self) -> None:
        with temporary_workdir() as workdir:
            configure(capture_inputs=True, capture_outputs=True)

            @observe(capture_inputs=True, capture_outputs=True)
            def handle(payload: dict[str, Any]) -> str:
                return f"issued {self.STRIPE_LIVE}"

            handle({"pem": self.PEM_BLOCK, "azure": self.AZURE_KEY})

            trace_path = workdir / ".bir" / "traces.jsonl"
            raw = trace_path.read_text(encoding="utf-8")
            self.assertNotIn(self.STRIPE_LIVE, raw)
            self.assertNotIn("MIIEpAIBAAKCAQEA", raw)
            self.assertNotIn(self.AZURE_KEY, raw)
            self.assertIn("[redacted]", raw)

            event = next(e for e in read_events(trace_path) if e["type"] == "trace")
            self.assertEqual(event["input"]["payload"], {"pem": "[redacted]", "azure": "[redacted]"})
            self.assertEqual(event["output"], "issued [redacted]")

    def test_builtin_formats_cannot_be_disabled_by_clearing_custom_rules(self) -> None:
        configure(additional_secret_keys=[], additional_redaction_patterns=[])
        self.assertEqual(_redact_secret_text(self.STRIPE_LIVE), "[redacted]")
        self.assertEqual(_redact_secret_text(self.PEM_BLOCK), "[redacted]")


class BuiltinPanRedactionTests(unittest.TestCase):
    """Built-in best-effort redaction of Luhn-valid credit-card / PAN numbers.

    This rule ships on by default and can never be disabled. Only 13-19 digit
    runs (optionally single-space- or hyphen-separated) that pass the Luhn
    checksum are redacted, so ordinary long integers, ids, and phone numbers are
    left intact. The values below are well-known card-network *test* numbers, not
    live PANs.
    """

    VISA_16 = "4111111111111111"
    VISA_16_SPACES = "4111 1111 1111 1111"
    MASTERCARD_16_HYPHENS = "5555-5555-5555-4444"
    AMEX_15_GROUPED = "3782 822463 10005"
    VISA_13 = "4222222222222"
    UNIONPAY_19 = "6011000000000000001"

    def setUp(self) -> None:
        _reset_config_for_tests()

    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_valid_pans_are_redacted(self) -> None:
        for pan in (
            self.VISA_16,
            self.VISA_16_SPACES,
            self.MASTERCARD_16_HYPHENS,
            self.AMEX_15_GROUPED,
            self.VISA_13,
            self.UNIONPAY_19,
        ):
            with self.subTest(pan=pan):
                self.assertEqual(_redact_secret_text(f"card {pan} end"), "card [redacted] end")

    def test_luhn_failing_runs_are_not_redacted(self) -> None:
        # A 16-digit run and a 19-digit id that both fail the Luhn check, plus a
        # 20-digit integer that is outside the 13-19 digit window entirely.
        for benign in (
            "order 1234567890123456 shipped",
            "request id 1234567890123456789 logged",
            "counter 12345678901234567890 reset",
        ):
            with self.subTest(benign=benign):
                self.assertEqual(_redact_secret_text(benign), benign)

    def test_phone_number_is_not_redacted(self) -> None:
        text = "call +1 415 555 2671 today"
        self.assertEqual(_redact_secret_text(text), text)

    def test_letter_adjacent_digits_are_not_redacted(self) -> None:
        # ``\b`` anchoring keeps a PAN-shaped run wedged inside an alphanumeric
        # token (an opaque id, a hash) from being treated as a card number.
        text = f"token{self.VISA_16}here"
        self.assertEqual(_redact_secret_text(text), text)

    def test_pan_redacted_in_repr_fallback_and_error_text(self) -> None:
        self.assertEqual(_safe_capture(_Box(self.VISA_16)), "_Box([redacted])")
        self.assertEqual(_safe_error(RuntimeError(f"declined {self.VISA_16}")), "declined [redacted]")

    def test_pan_redacted_in_nested_capture(self) -> None:
        captured = _safe_capture({"cards": [self.VISA_16, self.MASTERCARD_16_HYPHENS, "1234567890123456"]})
        self.assertEqual(captured, {"cards": ["[redacted]", "[redacted]", "1234567890123456"]})

    def test_pan_redacted_in_trace_jsonl(self) -> None:
        with temporary_workdir() as workdir:
            configure(capture_inputs=True, capture_outputs=True)

            @observe(capture_inputs=True, capture_outputs=True)
            def charge(payload: dict[str, Any]) -> str:
                return f"charged {self.VISA_16_SPACES}"

            charge({"pan": self.MASTERCARD_16_HYPHENS, "amount": "1234567890123456"})

            trace_path = workdir / ".bir" / "traces.jsonl"
            raw = trace_path.read_text(encoding="utf-8")
            self.assertNotIn("4111", raw)
            self.assertNotIn("5555", raw)
            self.assertIn("[redacted]", raw)

            event = next(e for e in read_events(trace_path) if e["type"] == "trace")
            # The Luhn-failing "amount" value is preserved verbatim.
            self.assertEqual(event["input"]["payload"], {"pan": "[redacted]", "amount": "1234567890123456"})
            self.assertEqual(event["output"], "charged [redacted]")

    def test_pan_rule_cannot_be_disabled_by_clearing_custom_rules(self) -> None:
        configure(additional_secret_keys=[], additional_redaction_patterns=[])
        self.assertEqual(_redact_secret_text(f"pay {self.VISA_16}"), "pay [redacted]")


class CustomRedactionConfigSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_config_for_tests()

    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_default_config_has_no_custom_rules(self) -> None:
        self.assertEqual(_redact_secret_text("CUST-1"), "CUST-1")
        self.assertFalse(_is_secret_key("ssn"))

    def test_passing_argument_replaces_previous_additional_rules(self) -> None:
        configure(additional_secret_keys=["aaa"], additional_redaction_patterns=[r"AAA-\d+"])
        configure(additional_secret_keys=["bbb"], additional_redaction_patterns=[r"BBB-\d+"])
        self.assertFalse(_is_secret_key("aaa"))
        self.assertTrue(_is_secret_key("bbb"))
        self.assertEqual(_redact_secret_text("AAA-1 BBB-2"), "AAA-1 [redacted]")

    def test_empty_iterable_clears_custom_rules_but_keeps_builtins(self) -> None:
        configure(additional_secret_keys=["ssn"], additional_redaction_patterns=[r"CUST-\d+"])
        configure(additional_secret_keys=[], additional_redaction_patterns=[])
        self.assertFalse(_is_secret_key("ssn"))
        self.assertEqual(_redact_secret_text("CUST-1"), "CUST-1")
        # Built-in rules cannot be cleared this way.
        self.assertTrue(_is_secret_key("api_key"))
        self.assertEqual(_redact_secret_text("api_key=sk-ABCD1234efgh"), "api_key=[redacted]")

    def test_omitting_argument_preserves_existing_custom_rules(self) -> None:
        configure(additional_secret_keys=["ssn"], additional_redaction_patterns=[r"CUST-\d+"])
        configure(sample_rate=0.5)
        self.assertTrue(_is_secret_key("ssn"))
        self.assertEqual(_redact_secret_text("CUST-1"), "[redacted]")

    def test_reset_for_tests_clears_custom_rules(self) -> None:
        configure(additional_secret_keys=["ssn"], additional_redaction_patterns=[r"CUST-\d+"])
        _reset_config_for_tests()
        self.assertFalse(_is_secret_key("ssn"))
        self.assertEqual(_redact_secret_text("CUST-1"), "CUST-1")


class CustomRedactionWritePathTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_config_for_tests()

    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_custom_rules_redacted_in_trace_jsonl(self) -> None:
        with temporary_workdir() as workdir:
            configure(
                capture_inputs=True,
                capture_outputs=True,
                additional_secret_keys=["ssn"],
                additional_redaction_patterns=[r"CUST-\d+"],
            )

            @observe(capture_inputs=True, capture_outputs=True)
            def handle(payload: dict[str, Any]) -> str:
                return "receipt CUST-456"

            handle({"ssn": "123-45-6789", "note": "ref CUST-123"})

            trace_path = workdir / ".bir" / "traces.jsonl"
            raw = trace_path.read_text(encoding="utf-8")
            self.assertNotIn("CUST-123", raw)
            self.assertNotIn("CUST-456", raw)
            self.assertNotIn("123-45-6789", raw)
            self.assertIn("[redacted]", raw)

            event = next(e for e in read_events(trace_path) if e["type"] == "trace")
            self.assertEqual(event["input"]["payload"], {"ssn": "[redacted]", "note": "ref [redacted]"})
            self.assertEqual(event["output"], "receipt [redacted]")

    def test_custom_rules_redacted_in_prompt_and_score_metadata(self) -> None:
        with temporary_workdir() as workdir:
            configure(additional_secret_keys=["ssn"], additional_redaction_patterns=[r"CUST-\d+"])

            @observe()
            def answer() -> None:
                with generation(
                    "local.llm",
                    prompt=prompt(
                        "p",
                        template="ref {ref}",
                        variables={"ref": "CUST-123", "ssn": "secret"},
                        capture_variables=True,
                        capture_rendered=True,
                    ),
                ):
                    pass
                score("quality", 1.0, metadata={"note": "saw CUST-999", "ssn": "secret"})

            answer()

            raw = (workdir / ".bir" / "traces.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("CUST-123", raw)
            self.assertNotIn("CUST-999", raw)

            events = read_events(workdir / ".bir" / "traces.jsonl")
            generation_event = next(e for e in events if e["type"] == "generation")
            prompt_metadata = generation_event["metadata"]["prompt"]
            self.assertEqual(prompt_metadata["variables"], {"ref": "[redacted]", "ssn": "[redacted]"})
            self.assertEqual(prompt_metadata["rendered"], "ref [redacted]")
            score_event = next(e for e in events if e["type"] == "score")
            self.assertEqual(score_event["metadata"], {"note": "saw [redacted]", "ssn": "[redacted]"})

    def test_custom_pattern_redacted_in_openai_integration_io(self) -> None:
        with temporary_workdir() as workdir:
            configure(capture_inputs=True, capture_outputs=True, additional_redaction_patterns=[r"CUST-\d+"])

            class FakeCompletion:
                model = "gpt-4o-mini"

                def model_dump(self) -> dict[str, Any]:
                    return {"choices": [{"message": {"content": "done CUST-456"}}]}

            def fake_create(**kwargs: Any) -> FakeCompletion:
                return FakeCompletion()

            with trace("chat"):
                trace_chat_completion(
                    fake_create,
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": "look up CUST-123"}],
                )

            raw = (workdir / ".bir" / "traces.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("CUST-123", raw)
            self.assertNotIn("CUST-456", raw)

            generation_event = next(e for e in load_events() if e.type == "generation")
            self.assertEqual(
                generation_event.input["messages"],
                [{"role": "user", "content": "look up [redacted]"}],
            )
            self.assertEqual(
                generation_event.output["choices"][0]["message"]["content"],
                "done [redacted]",
            )

    def test_custom_rules_redacted_in_experiment_jsonl_and_summary(self) -> None:
        with temporary_workdir() as workdir:
            configure(additional_redaction_patterns=[r"CUST-\d+"])
            result_path = workdir / "experiment.jsonl"

            def task(example_input: dict[str, Any]) -> dict[str, Any]:
                return {"answer": f"resolved {example_input['ref']}", "trace": "CUST-456"}

            def evaluate(output: Any, expected: Any) -> EvalResult:
                return EvalResult(name="leaky", value=1.0, metadata={"seen": "CUST-789"})

            run_experiment(
                "exp",
                dataset=[DatasetExample(id="1", input={"ref": "CUST-123"}, expected={"answer": "x"})],
                task=task,
                evaluators=[custom_evaluator("leaky", evaluate), exact_match()],
                path=result_path,
                raise_on_error=False,
            )

            summary_path = result_path.with_suffix(".summary.json")
            result_raw = result_path.read_text(encoding="utf-8")
            summary_raw = summary_path.read_text(encoding="utf-8")
            for needle in ("CUST-123", "CUST-456", "CUST-789"):
                self.assertNotIn(needle, result_raw)
                self.assertNotIn(needle, summary_raw)
            self.assertIn("[redacted]", result_raw)


class CaptureTruncationOrderingTests(unittest.TestCase):
    """The opt-in capture-size limits must never weaken redaction.

    Truncation runs only inside ``_safe_capture`` and only after redaction, so a
    secret is always replaced before any cut and can never be split into a
    partially visible value. These tests pin that ordering for the built-in
    rules, the additive custom rules, and the persistence path.
    """

    def setUp(self) -> None:
        _reset_config_for_tests()

    def tearDown(self) -> None:
        _reset_config_for_tests()

    def test_builtin_secret_redacted_before_truncation(self) -> None:
        configure(max_value_length=12)
        # The raw secret is 35 chars (> 12), but redaction collapses it to the
        # 10-char marker first, so no truncation marker appears and no key prefix
        # leaks. Truncating first would have produced "sk-ABCDEFGH…[truncated]".
        self.assertEqual(_safe_capture("sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"), "[redacted]")

    def test_custom_pattern_redacted_before_truncation(self) -> None:
        # Truncating first could cut the token right after "CUST-" (no trailing
        # digit), which would no longer match the pattern and would leak it.
        # Redacting first removes the whole match before any cut.
        configure(max_value_length=8, additional_redaction_patterns=[r"CUST-\d+"])
        captured = _safe_capture("abcCUST-123456")
        self.assertEqual(captured, "abc[reda…[truncated]")
        self.assertNotIn("CUST", captured)

    def test_additional_secret_key_value_redacted_regardless_of_limit(self) -> None:
        # A keyed redaction replaces the whole value before the string path runs,
        # so even a tiny limit cannot turn it into a partially visible value.
        configure(max_value_length=3, additional_secret_keys=["ssn"])
        self.assertEqual(_safe_capture({"ssn": "123-45-6789-very-long-value"}), {"ssn": "[redacted]"})

    def test_truncation_may_cut_redaction_marker_but_secret_stays_gone(self) -> None:
        # When the redacted text is still longer than the limit, truncation can
        # cut through the [redacted] marker itself. That is cosmetic only: the
        # secret was already replaced, so no secret bytes are ever exposed.
        configure(max_value_length=10)
        captured = _safe_capture("password=SUPERSECRETVALUE12345")
        self.assertNotIn("SUPERSECRET", captured)
        self.assertTrue(captured.startswith("password=["))
        self.assertTrue(captured.endswith("…[truncated]"))

    def test_error_text_is_redacted_but_not_truncated(self) -> None:
        # Error text is not a capture path, so the size limit does not apply to it
        # even though redaction always does.
        configure(max_value_length=5, additional_redaction_patterns=[r"CUST-\d+"])
        redacted = _safe_error(RuntimeError("failure for CUST-123 " + "x" * 100))
        self.assertNotIn("CUST-123", redacted)
        self.assertIn("[redacted]", redacted)
        self.assertNotIn("…[truncated]", redacted)
        self.assertGreater(len(redacted), 5)

    def test_truncation_after_redaction_in_trace_jsonl(self) -> None:
        with temporary_workdir() as workdir:
            configure(
                capture_inputs=True,
                capture_outputs=True,
                additional_redaction_patterns=[r"CUST-\d+"],
                max_value_length=12,
            )

            @observe(capture_inputs=True, capture_outputs=True)
            def handle(note: str) -> str:
                return "issued sk-SECRETKEY1234567890 for CUST-456 " + "z" * 50

            handle("ref CUST-123 " + "y" * 80)

            trace_path = workdir / ".bir" / "traces.jsonl"
            raw = trace_path.read_text(encoding="utf-8")
            # No secret or custom-pattern value survives, despite aggressive truncation.
            self.assertNotIn("SECRET", raw)
            self.assertNotIn("CUST-123", raw)
            self.assertNotIn("CUST-456", raw)
            # Truncation still happened and the line round-trips as valid JSON.
            self.assertIn("truncated", raw)
            event = next(e for e in read_events(trace_path) if e["type"] == "trace")
            self.assertTrue(event["output"].endswith("…[truncated]"))
            self.assertNotIn("SECRET", event["output"])


if __name__ == "__main__":
    unittest.main()
