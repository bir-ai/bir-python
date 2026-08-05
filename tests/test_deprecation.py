"""The deprecation promise has to be executable, so it is tested like code.

``docs/site/stability.md`` promises a deprecated public name keeps working for
one minor release while warning and naming its replacement, and may only be
removed in the release after that. These tests pin every part a user would
notice: the warning category their filters match on, the wording that tells them
what to switch to and by when, the line the warning blames, and — most
importantly — that the deprecated thing still does its job.
"""

from __future__ import annotations

import unittest
import warnings
from pathlib import Path

import bir
from bir._deprecation import (
    DEPRECATION_GRACE_MINORS,
    _check_removal_release,
    _deprecated,
    _deprecated_attribute_getter,
    _Deprecation,
    _deprecation_message,
    _earliest_removal,
    _warn_deprecated,
)


@_deprecated(replacement="bir.new_answer()", removed_in="0.5.0")
def old_answer(question: str, *, upper: bool = False) -> str:
    """Stand in for a real deprecated public function."""

    answer = f"answered {question}"
    return answer.upper() if upper else answer


class _NewThing:
    pass


module_getattr = _deprecated_attribute_getter(
    "bir.example",
    {"OldThing": _Deprecation(_NewThing, replacement="bir.example.NewThing", removed_in="0.5.0")},
)


class DeprecatedCallableTests(unittest.TestCase):
    """A deprecated function warns once and otherwise behaves as before."""

    def test_it_still_does_its_job(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)

            # Arguments, keywords, and the return value all pass through: a
            # deprecation is a notice, not a behavior change.
            self.assertEqual(old_answer("why"), "answered why")
            self.assertEqual(old_answer("why", upper=True), "ANSWERED WHY")

    def test_it_warns_with_the_category_users_filter_on(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            old_answer("why")

        self.assertEqual(len(caught), 1)
        self.assertIs(caught[0].category, DeprecationWarning)

    def test_the_message_names_the_replacement_and_the_release(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            old_answer("why")

        message = str(caught[0].message)
        self.assertIn("old_answer()", message)
        self.assertIn("removed in 0.5.0", message)
        self.assertIn("bir.new_answer()", message)

    def test_the_warning_blames_the_caller_not_the_sdk(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            old_answer("why")

        # A warning pointing inside Bir tells a user nothing about which of
        # their lines to change, which is the whole reason to warn.
        self.assertEqual(Path(caught[0].filename), Path(__file__))

    def test_it_keeps_the_wrapped_functions_identity(self) -> None:
        self.assertEqual(old_answer.__name__, "old_answer")
        self.assertIn("Stand in for a real deprecated", old_answer.__doc__ or "")

    def test_a_custom_name_replaces_the_qualified_one(self) -> None:
        @_deprecated(replacement="bir.trace()", removed_in="0.5.0", name="bir.record()")
        def record() -> None:
            return None

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            record()

        self.assertIn("bir.record() is deprecated", str(caught[0].message))


class DeprecatedAttributeTests(unittest.TestCase):
    """A deprecated module attribute warns and still resolves."""

    def test_it_returns_the_replacement_value(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            resolved = module_getattr("OldThing")

        self.assertIs(resolved, _NewThing)
        self.assertIs(caught[0].category, DeprecationWarning)
        self.assertIn("bir.example.OldThing", str(caught[0].message))

    def test_the_warning_blames_the_importing_line(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            module_getattr("OldThing")

        self.assertEqual(Path(caught[0].filename), Path(__file__))

    def test_an_unknown_attribute_raises_as_a_module_normally_would(self) -> None:
        # Otherwise a typo would come back as a confusing None instead of the
        # AttributeError every Python user expects.
        with self.assertRaises(AttributeError) as raised:
            module_getattr("NeverExisted")

        self.assertIn("NeverExisted", str(raised.exception))
        self.assertIn("bir.example", str(raised.exception))


class MessageTests(unittest.TestCase):
    """One wording, whatever channel carries it."""

    def test_the_formatter_and_the_warning_agree(self) -> None:
        expected = _deprecation_message("bir.old()", replacement="bir.new()", removed_in="0.5.0")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _warn_deprecated("bir.old()", replacement="bir.new()", removed_in="0.5.0", stacklevel=2)

        # A CLI command prints the formatter's message because
        # DeprecationWarning is invisible outside __main__; the two must not
        # drift into saying different things.
        self.assertEqual(str(caught[0].message), expected)


class RemovalPolicyTests(unittest.TestCase):
    """The promised grace period is arithmetic, not a judgment call."""

    def test_the_earliest_removal_is_two_minor_releases_out(self) -> None:
        self.assertEqual(_earliest_removal("0.3.0"), "0.5.0")
        self.assertEqual(_earliest_removal("1.9.2"), "1.11.0")
        self.assertEqual(DEPRECATION_GRACE_MINORS, 2)

    def test_a_deadline_the_policy_allows_passes(self) -> None:
        for removed_in in ("0.5.0", "0.6.0", "1.0.0"):
            with self.subTest(removed_in=removed_in):
                _check_removal_release(removed_in, current_version="0.3.0")

    def test_an_over_eager_deadline_is_refused(self) -> None:
        for removed_in in ("0.4.0", "0.3.1"):
            with self.subTest(removed_in=removed_in):
                with self.assertRaises(ValueError) as raised:
                    _check_removal_release(removed_in, current_version="0.3.0")
                self.assertIn("0.5.0", str(raised.exception))

    def test_a_malformed_version_is_refused(self) -> None:
        for version in ("1", "", "x.y.z", "1.x"):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    _earliest_removal(version)

    def test_the_policy_is_stated_against_the_shipping_version(self) -> None:
        # The stability page tells users what the grace period is; this keeps
        # that sentence honest as the version moves.
        page = (Path(__file__).resolve().parents[1] / "docs" / "site" / "stability.md").read_text(encoding="utf-8")

        self.assertIn(f"`{_earliest_removal(bir.__version__)}`", page)


class NothingIsDeprecatedYetTests(unittest.TestCase):
    """The SDK ships no deprecated public name, so nothing should warn."""

    def test_importing_and_using_the_public_api_warns_about_nothing(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)

            for name in bir.__all__:
                getattr(bir, name)

        self.assertEqual([str(warning.message) for warning in caught], [])


if __name__ == "__main__":
    unittest.main()
