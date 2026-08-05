"""The traceparent parser is the trust boundary, so it is tested like one.

Extraction is the only place Bir would take an identifier from a caller it does
not control and write it into a local trace store. These tests cover the shapes
the W3C recommendation defines and, more importantly, the shapes an attacker or
a broken client sends: oversized headers, newlines, uppercase hex, reserved
versions, all-zero ids, and truncated fields. Every one of them must come back
as "no usable remote context" rather than as data that reaches storage.
"""

from __future__ import annotations

import unittest
from uuid import UUID, uuid4

from bir._trace_context import (
    MAX_HEADER_LENGTH,
    RemoteTraceContext,
    format_traceparent,
    parse_traceparent,
)

VALID_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
VALID_SPAN_ID = "00f067aa0ba902b7"
VALID_HEADER = f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}-01"


class ParseTraceparentTests(unittest.TestCase):
    """A well-formed header is read; everything else is refused."""

    def test_reads_the_recommendation_example(self) -> None:
        context = parse_traceparent(VALID_HEADER)

        self.assertEqual(
            context,
            RemoteTraceContext(trace_id=VALID_TRACE_ID, span_id=VALID_SPAN_ID, sampled=True),
        )

    def test_reads_the_sampled_flag_from_the_low_bit(self) -> None:
        for flags, expected in (("00", False), ("01", True), ("02", False), ("03", True), ("ff", True)):
            with self.subTest(flags=flags):
                context = parse_traceparent(f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}-{flags}")
                assert context is not None
                self.assertIs(context.sampled, expected)

    def test_a_later_version_may_carry_extra_fields(self) -> None:
        # A version this parser does not know still yields the four fields it
        # does know, so a newer sender is not cut off from an older receiver.
        context = parse_traceparent(f"01-{VALID_TRACE_ID}-{VALID_SPAN_ID}-01-extra")

        assert context is not None
        self.assertEqual(context.trace_id, VALID_TRACE_ID)

    def test_version_zero_must_carry_exactly_four_fields(self) -> None:
        self.assertIsNone(parse_traceparent(f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}-01-extra"))

    def test_reserved_version_is_refused(self) -> None:
        self.assertIsNone(parse_traceparent(f"ff-{VALID_TRACE_ID}-{VALID_SPAN_ID}-01"))

    def test_all_zero_ids_are_refused(self) -> None:
        self.assertIsNone(parse_traceparent(f"00-{'0' * 32}-{VALID_SPAN_ID}-01"))
        self.assertIsNone(parse_traceparent(f"00-{VALID_TRACE_ID}-{'0' * 16}-01"))

    def test_malformed_headers_are_refused(self) -> None:
        cases = {
            "empty": "",
            "missing fields": f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}",
            "short trace id": f"00-{VALID_TRACE_ID[:31]}-{VALID_SPAN_ID}-01",
            "long trace id": f"00-{VALID_TRACE_ID}a-{VALID_SPAN_ID}-01",
            "short span id": f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID[:15]}-01",
            "uppercase hex": f"00-{VALID_TRACE_ID.upper()}-{VALID_SPAN_ID}-01",
            "non-hex trace id": f"00-{'z' * 32}-{VALID_SPAN_ID}-01",
            "non-hex flags": f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}-0z",
            "single-digit flags": f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}-1",
            "single-digit version": f"0-{VALID_TRACE_ID}-{VALID_SPAN_ID}-01",
            "leading space": f" 00-{VALID_TRACE_ID}-{VALID_SPAN_ID}-01",
            "trailing space": f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}-01 ",
            "uuid form": f"00-{uuid4()}-{VALID_SPAN_ID}-01",
        }
        for name, header in cases.items():
            with self.subTest(case=name):
                self.assertIsNone(parse_traceparent(header))

    def test_injection_attempts_are_refused(self) -> None:
        # A trace id reaches a JSONL store, so anything that could break a line
        # or smuggle a payload has to die at the parser.
        cases = {
            "newline": f"00-{VALID_TRACE_ID}\n-{VALID_SPAN_ID}-01",
            "carriage return": f"00-{VALID_TRACE_ID}\r-{VALID_SPAN_ID}-01",
            "null byte": f"00-{VALID_TRACE_ID}\x00-{VALID_SPAN_ID}-01",
            "json fragment": '00-{"trace_id": "x"}-' + f"{VALID_SPAN_ID}-01",
            "quotes": f'00-"{VALID_TRACE_ID}"-{VALID_SPAN_ID}-01',
        }
        for name, header in cases.items():
            with self.subTest(case=name):
                self.assertIsNone(parse_traceparent(header))

    def test_oversized_headers_are_refused_before_parsing(self) -> None:
        oversized = f"01-{VALID_TRACE_ID}-{VALID_SPAN_ID}-01-" + "a" * MAX_HEADER_LENGTH

        self.assertGreater(len(oversized), MAX_HEADER_LENGTH)
        self.assertIsNone(parse_traceparent(oversized))

    def test_non_string_input_is_refused(self) -> None:
        for value in (None, 0, 1.5, b"00-" + VALID_TRACE_ID.encode(), ["00"], {"traceparent": VALID_HEADER}):
            with self.subTest(value=type(value).__name__):
                self.assertIsNone(parse_traceparent(value))  # type: ignore[arg-type]

    def test_recorded_metadata_carries_only_scalars(self) -> None:
        context = parse_traceparent(VALID_HEADER)
        assert context is not None

        metadata = context.to_metadata()

        self.assertEqual(metadata, {"trace_id": VALID_TRACE_ID, "span_id": VALID_SPAN_ID, "sampled": True})
        for value in metadata.values():
            self.assertIsInstance(value, (str, bool))


class FormatTraceparentTests(unittest.TestCase):
    """Bir ids map outbound without inventing anything the ids do not carry."""

    def test_formats_a_header_a_w3c_parser_accepts(self) -> None:
        trace_id = str(uuid4())
        span_id = str(uuid4())

        header = format_traceparent(trace_id, span_id, sampled=True)

        assert header is not None
        parsed = parse_traceparent(header)
        assert parsed is not None
        self.assertEqual(parsed.trace_id, UUID(trace_id).hex)
        # A W3C parent-id is half a UUID, so the span id narrows on the way out.
        self.assertEqual(parsed.span_id, UUID(span_id).hex[:16])
        self.assertIs(parsed.sampled, True)

    def test_sampled_flag_round_trips(self) -> None:
        for sampled in (True, False):
            with self.subTest(sampled=sampled):
                header = format_traceparent(str(uuid4()), str(uuid4()), sampled=sampled)
                assert header is not None
                parsed = parse_traceparent(header)
                assert parsed is not None
                self.assertIs(parsed.sampled, sampled)

    def test_trace_id_survives_the_round_trip_intact(self) -> None:
        # The trace id is what joins events across services, so it must come
        # back byte-for-byte after a UUID is rendered and re-read.
        trace_id = str(uuid4())

        header = format_traceparent(trace_id, str(uuid4()), sampled=False)
        assert header is not None
        parsed = parse_traceparent(header)
        assert parsed is not None

        self.assertEqual(UUID(parsed.trace_id), UUID(trace_id))

    def test_ids_that_are_not_uuids_produce_no_header(self) -> None:
        for name, trace_id, span_id in (
            ("empty trace id", "", str(uuid4())),
            ("empty span id", str(uuid4()), ""),
            ("non-uuid trace id", "not-a-uuid", str(uuid4())),
            ("hex without dashes is accepted by UUID", "x" * 32, str(uuid4())),
            ("all-zero uuid", "00000000-0000-0000-0000-000000000000", str(uuid4())),
        ):
            with self.subTest(case=name):
                self.assertIsNone(format_traceparent(trace_id, span_id, sampled=False))

    def test_a_dashless_uuid_is_still_accepted(self) -> None:
        # ``uuid.UUID`` accepts the dashless form, and a caller that stripped
        # dashes is passing the same id, not a different one.
        trace_id = uuid4()

        header = format_traceparent(trace_id.hex, str(uuid4()), sampled=False)

        assert header is not None
        self.assertIn(trace_id.hex, header)


if __name__ == "__main__":
    unittest.main()
