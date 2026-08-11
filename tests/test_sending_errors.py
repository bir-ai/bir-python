"""How ``send_events`` behaves when the server misbehaves.

The transport's happy path is well covered; its error handling was not, and that
is the half a user meets on a bad day. These tests drive ``bir.send_events``
against a stubbed socket and assert what the user actually experiences — the
message they read, whether the SDK retried, and whether their local store was
touched — rather than that a line executed.

Both request paths are exercised. The batch endpoint is the normal one; the
per-event fallback runs against a server that answers ``/v1/events/batch`` with
404, which is how an older server without the batch route presents itself.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import bir
from bir import _sending
from bir._sdk import _reset_config_for_tests

SERVER = "http://server.test"
BATCH_ENDPOINT = f"{SERVER}/v1/events/batch"


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


def record_one_trace() -> None:
    with bir.trace("request"):
        with bir.generation("llm", model="gpt-4o-mini") as generation:
            generation.set_usage(input_tokens=11, output_tokens=4)


def record_traces(count: int) -> None:
    """Record ``count`` traces, each carrying one generation."""

    for index in range(count):
        with bir.trace(f"request-{index}"):
            with bir.generation("llm", model="gpt-4o-mini") as generation:
                generation.set_usage(input_tokens=11, output_tokens=4)


class FakeResponse:
    """A 2xx response carrying ``body``."""

    def __init__(self, body: str, *, status: int = 200) -> None:
        self.status = status
        self._body = body.encode("utf-8")

    def read(self, amt: int | None = None) -> bytes:
        # http.client.HTTPResponse.read takes an optional byte count, and
        # the transport passes one to bound a success response.
        return self._body if amt is None else self._body[:amt]

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def http_error(code: int, body: str = "denied") -> urllib.error.HTTPError:
    """Build the ``HTTPError`` urllib raises for a non-2xx response."""

    return urllib.error.HTTPError(
        url=BATCH_ENDPOINT,
        code=code,
        msg="error",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body.encode("utf-8")),
    )


class _Server:
    """Answers each request from a queue, recording how many it received.

    Answers are factories rather than values because the SDK consumes what it is
    given — it closes an error response after reading its body — so a retry must
    meet a fresh object, exactly as it would meet a fresh socket.
    """

    def __init__(self, *answers: Callable[[], Any]) -> None:
        self._answers = list(answers)
        self.requests: list[str] = []

    def __call__(self, request: Any, timeout: float = 0.0) -> Any:
        self.requests.append(request.full_url)
        make = self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]
        answer = make()
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @property
    def attempts(self) -> int:
        return len(self.requests)


@contextmanager
def serving(server: _Server) -> Iterator[None]:
    # ``time.sleep`` is stubbed so a retry test costs nothing; the backoff
    # arithmetic itself is covered where it is computed.
    with patch("bir._sending._opener.open", side_effect=server), patch("bir._sending.time.sleep"):
        yield


class BatchErrorTests(unittest.TestCase):
    """The batch request's failures reach the caller intact."""

    def test_a_server_error_is_retried_then_reported(self) -> None:
        with temporary_workdir():
            record_one_trace()
            server = _Server(lambda: http_error(503, "overloaded"))

            with serving(server), self.assertRaises(RuntimeError) as raised:
                bir.send_events(SERVER, retries=2)

            # One attempt plus two retries, and the message names the status and
            # keeps the server's own body so the user can see why.
            self.assertEqual(server.attempts, 3)
            self.assertIn("HTTP 503", str(raised.exception))
            self.assertIn("overloaded", str(raised.exception))

    def test_a_rejected_request_is_not_retried(self) -> None:
        with temporary_workdir():
            record_one_trace()
            server = _Server(lambda: http_error(400, "malformed"))

            with serving(server), self.assertRaises(RuntimeError) as raised:
                bir.send_events(SERVER, retries=5)

            # A 4xx will not become a 2xx by asking again; retrying it would
            # only delay the error the user needs to see.
            self.assertEqual(server.attempts, 1)
            self.assertIn("HTTP 400", str(raised.exception))

    def test_a_network_error_is_retried_and_names_the_endpoint(self) -> None:
        with temporary_workdir():
            record_one_trace()
            server = _Server(lambda: urllib.error.URLError("connection refused"))

            with serving(server), self.assertRaises(RuntimeError) as raised:
                bir.send_events(SERVER, retries=1)

            self.assertEqual(server.attempts, 2)
            self.assertIn(BATCH_ENDPOINT, str(raised.exception))
            self.assertIn("connection refused", str(raised.exception))

    def test_a_read_timeout_is_retried(self) -> None:
        with temporary_workdir():
            record_one_trace()
            # A socket read timeout arrives as TimeoutError, not URLError, so it
            # needs its own branch to be treated as transient.
            server = _Server(lambda: TimeoutError("timed out"))

            with serving(server), self.assertRaises(RuntimeError) as raised:
                bir.send_events(SERVER, retries=1)

            self.assertEqual(server.attempts, 2)
            self.assertIn("timed out", str(raised.exception))

    def test_a_recovered_server_succeeds_after_a_retry(self) -> None:
        with temporary_workdir():
            record_one_trace()
            sent = [event.id for event in bir.load_events()]
            body = json.dumps({"accepted": len(sent), "event_ids": sent})
            server = _Server(lambda: http_error(500, "restarting"), lambda: FakeResponse(body))

            with serving(server):
                result = bir.send_events(SERVER, retries=2)

            self.assertEqual(server.attempts, 2)
            self.assertEqual(result.accepted, len(sent))

    def test_a_non_2xx_success_response_is_refused(self) -> None:
        with temporary_workdir():
            record_one_trace()
            # urllib only raises for some statuses; a 3xx that reaches the code
            # as a normal response must not be read as an accepted send.
            server = _Server(lambda: FakeResponse("moved", status=302))

            with serving(server), self.assertRaises(RuntimeError) as raised:
                bir.send_events(SERVER)

            self.assertIn("HTTP 302", str(raised.exception))

    def test_an_unreadable_batch_response_is_refused(self) -> None:
        with temporary_workdir():
            record_one_trace()

            for name, body in (
                ("not json", "<html>hello</html>"),
                ("not an object", "[1, 2, 3]"),
                ("accepted missing", json.dumps({"event_ids": []})),
                ("accepted is a bool", json.dumps({"accepted": True, "event_ids": []})),
                ("event_ids missing", json.dumps({"accepted": 1})),
                ("event_ids not strings", json.dumps({"accepted": 1, "event_ids": [1]})),
            ):
                with self.subTest(case=name):
                    server = _Server(lambda: FakeResponse(body))
                    with serving(server), self.assertRaises(RuntimeError) as raised:
                        bir.send_events(SERVER)
                    # Guessing at a malformed response would report a send that
                    # may not have happened, so it is refused with the body shown.
                    self.assertIn("invalid batch response", str(raised.exception))


class PerEventFallbackTests(unittest.TestCase):
    """A server without the batch route falls back to one request per event."""

    def batch_missing(self, *answers: Callable[[], Any]) -> _Server:
        """Answer the batch endpoint with 404, then serve the per-event posts."""

        return _Server(lambda: http_error(404, "no such route"), *answers)

    def test_events_are_posted_one_by_one(self) -> None:
        with temporary_workdir():
            record_one_trace()
            server = self.batch_missing(lambda: FakeResponse(json.dumps({"accepted": 1})))

            with serving(server):
                result = bir.send_events(SERVER)

            # The batch attempt plus one request per recorded event.
            self.assertEqual(server.requests[0], BATCH_ENDPOINT)
            self.assertEqual(server.attempts, 3)
            self.assertEqual(result.accepted, 2)
            self.assertEqual(result.attempted, 2)

    def test_a_server_error_on_an_event_is_retried_then_reported(self) -> None:
        with temporary_workdir():
            record_one_trace()
            server = self.batch_missing(lambda: http_error(502, "bad gateway"))

            with serving(server), self.assertRaises(RuntimeError) as raised:
                bir.send_events(SERVER, retries=1)

            # The batch probe, then the first event's attempt and its retry.
            self.assertEqual(server.attempts, 3)
            self.assertIn("rejected event with HTTP 502", str(raised.exception))
            self.assertIn("bad gateway", str(raised.exception))

    def test_a_rejected_event_is_not_retried(self) -> None:
        with temporary_workdir():
            record_one_trace()
            server = self.batch_missing(lambda: http_error(422, "unprocessable"))

            with serving(server), self.assertRaises(RuntimeError) as raised:
                bir.send_events(SERVER, retries=5)

            self.assertEqual(server.attempts, 2)
            self.assertIn("HTTP 422", str(raised.exception))

    def test_a_network_error_on_an_event_is_retried(self) -> None:
        with temporary_workdir():
            record_one_trace()
            server = self.batch_missing(lambda: urllib.error.URLError("connection reset"))

            with serving(server), self.assertRaises(RuntimeError) as raised:
                bir.send_events(SERVER, retries=1)

            self.assertEqual(server.attempts, 3)
            self.assertIn("could not send event", str(raised.exception))
            self.assertIn("connection reset", str(raised.exception))

    def test_a_read_timeout_on_an_event_is_retried(self) -> None:
        with temporary_workdir():
            record_one_trace()
            server = self.batch_missing(lambda: TimeoutError("timed out"))

            with serving(server), self.assertRaises(RuntimeError) as raised:
                bir.send_events(SERVER, retries=1)

            self.assertEqual(server.attempts, 3)
            self.assertIn("could not send event", str(raised.exception))

    def test_a_non_2xx_event_response_is_refused(self) -> None:
        with temporary_workdir():
            record_one_trace()
            server = self.batch_missing(lambda: FakeResponse("moved", status=301))

            with serving(server), self.assertRaises(RuntimeError) as raised:
                bir.send_events(SERVER)

            self.assertIn("rejected event with HTTP 301", str(raised.exception))

    def test_an_unreadable_event_response_counts_as_one_accepted(self) -> None:
        with temporary_workdir():
            record_one_trace()

            for name, body in (
                ("not json", "OK"),
                ("not an object", "[]"),
                ("accepted missing", json.dumps({"status": "ok"})),
                ("accepted is a bool", json.dumps({"accepted": True})),
            ):
                with self.subTest(case=name):
                    server = self.batch_missing(lambda body=body: FakeResponse(body))
                    with serving(server):
                        result = bir.send_events(SERVER)
                    # The server answered 2xx, so the event was accepted; only
                    # the count is unreadable, and one event was posted per
                    # request, so one is the honest reading.
                    self.assertEqual(result.accepted, 2)


class EndpointTests(unittest.TestCase):
    """A server URL that cannot form an endpoint fails before any request."""

    def test_an_empty_server_url_is_refused(self) -> None:
        with temporary_workdir():
            record_one_trace()

            def fail(*_args: Any, **_kwargs: Any) -> None:
                raise AssertionError("no request should be made for an empty server URL")

            with patch("bir._sending._opener.open", side_effect=fail):
                for server_url in ("", "/", "///"):
                    with self.subTest(server_url=server_url):
                        with self.assertRaises(ValueError) as raised:
                            bir.send_events(server_url)
                        self.assertIn("must not be empty", str(raised.exception))


class ResponseBodyBoundTests(unittest.TestCase):
    """A server's response body is carried only as far as a message needs it.

    The body exists to say why the server refused, and a ``--server`` URL
    pointing at something that is not a Bir server can answer with a document.
    Bounding it keeps that document out of the exception, out of whatever logs
    the exception, and off the terminal.
    """

    def test_a_body_within_the_bound_is_carried_whole(self) -> None:
        body = "x" * _sending._MAX_REPORTED_BODY_CHARS

        self.assertEqual(_sending._reported_body(body), body)

    def test_a_longer_body_is_cut_and_says_so(self) -> None:
        reported = _sending._reported_body("x" * (_sending._MAX_REPORTED_BODY_CHARS + 1))

        self.assertTrue(reported.startswith("x" * _sending._MAX_REPORTED_BODY_CHARS))
        self.assertTrue(reported.endswith("…[truncated]"))

    def test_an_error_body_is_not_read_past_the_bound(self) -> None:
        # Bounding the message alone would still pull the whole document into
        # memory, so the read itself stops. The response counts what was taken
        # from it, because the SDK closes it and a closed one cannot be asked.
        class CountingBody(io.BytesIO):
            def __init__(self, data: bytes) -> None:
                super().__init__(data)
                self.bytes_read = 0

            def read(self, size: int | None = -1) -> bytes:
                chunk = super().read(size)
                self.bytes_read += len(chunk)
                return chunk

        fp = CountingBody(b"x" * 100_000)
        exc = urllib.error.HTTPError(
            url=BATCH_ENDPOINT,
            code=400,
            msg="error",
            hdrs=None,  # type: ignore[arg-type]
            fp=fp,
        )

        body = _sending._read_http_error_body(exc)

        self.assertEqual(len(body), _sending._MAX_REPORTED_BODY_CHARS + 1)
        self.assertEqual(fp.bytes_read, _sending._MAX_REPORTED_BODY_CHARS + 1)

    def test_a_rejected_send_reports_a_bounded_message(self) -> None:
        with temporary_workdir():
            record_one_trace()
            server = _Server(lambda: http_error(400, "x" * 100_000))

            with serving(server), self.assertRaises(RuntimeError) as raised:
                bir.send_events(SERVER)

            message = str(raised.exception)
            self.assertIn("HTTP 400", message)
            self.assertIn("…[truncated]", message)
            self.assertLess(len(message), 1_000)

    def test_a_success_body_the_batch_justifies_is_read_whole(self) -> None:
        # The message bound is not the read bound: a batch's accepted ids are
        # legitimately longer than any message would carry, so a reply in
        # proportion to the request is still parsed whole.
        with temporary_workdir():
            record_traces(500)
            sent = [event.id for event in bir.load_events()]
            body = json.dumps({"accepted": len(sent), "event_ids": sent})
            self.assertGreater(len(body), _sending._MAX_REPORTED_BODY_CHARS)
            self.assertLessEqual(len(body), _sending._max_response_bytes(len(sent)))
            server = _Server(lambda: FakeResponse(body))

            with serving(server):
                result = bir.send_events(SERVER)

            self.assertEqual(result.accepted, len(sent))
            self.assertEqual(len(result.event_ids), len(sent))

    def test_a_success_body_the_batch_cannot_justify_is_not_read(self) -> None:
        # The one value in the SDK that could otherwise grow without limit: the
        # loaders stream, prune is disk-backed, and the upload spool is
        # disk-backed, but a single reply used to be read whatever its size.
        with temporary_workdir():
            record_one_trace()
            sent = [event.id for event in bir.load_events()]
            oversized = json.dumps({"accepted": len(sent), "event_ids": sent, "note": "x" * 200_000})
            self.assertGreater(len(oversized), _sending._max_response_bytes(len(sent)))
            server = _Server(lambda: FakeResponse(oversized))

            with serving(server), self.assertRaises(RuntimeError) as raised:
                bir.send_events(SERVER)

            message = str(raised.exception)
            self.assertIn("answered with more than", message)
            self.assertIn(f"{len(sent)} event(s)", message)
            # The message itself stays small; it does not carry what it refused.
            self.assertLess(len(message), 1_000)


class ResponseCountsMustDescribeTheRequestTests(unittest.TestCase):
    """A reply is refused when it cannot be a reply to what was sent.

    ``accepted`` is printed by ``bir send`` and gates pipelines, ``skipped`` is
    computed from it, and ``event_ids`` is what ``--mark-sent`` records as
    delivered — so a number or an id the server invented is not a cosmetic
    problem. Before these checks a server could make the CLI print
    ``accepted=-5 attempted=3 skipped=8``.
    """

    def setUp(self) -> None:
        _reset_config_for_tests()

    def _send_with_reply(self, reply: dict[str, object], *, traces: int = 3) -> str:
        with temporary_workdir():
            record_traces(traces)
            server = _Server(lambda: FakeResponse(json.dumps(reply)))
            with serving(server), self.assertRaises(RuntimeError) as raised:
                bir.send_events(SERVER)
            return str(raised.exception)

    def test_more_accepted_than_attempted_is_refused(self) -> None:
        message = self._send_with_reply({"accepted": 99, "event_ids": []})

        self.assertIn("claimed 99 of", message)
        self.assertIn("not a count of what was sent", message)

    def test_a_negative_acceptance_is_refused(self) -> None:
        # This one used to reach the terminal as "accepted=-5 ... skipped=8":
        # more events skipped than were ever attempted.
        message = self._send_with_reply({"accepted": -5, "event_ids": []})

        self.assertIn("claimed -5 of", message)

    def test_more_ids_than_events_sent_is_refused(self) -> None:
        message = self._send_with_reply({"accepted": 1, "event_ids": ["a", "b", "c", "d", "e", "f", "g"]})

        self.assertIn("id(s) for", message)

    def test_ids_naming_events_that_were_not_sent_are_refused(self) -> None:
        # --mark-sent records these, so an invented id would be remembered as
        # delivered for good.
        message = self._send_with_reply({"accepted": 1, "event_ids": ["not-a-real-id"]})

        self.assertIn("naming events that were not sent", message)
        self.assertIn("not-a-real-id", message)

    def test_a_reply_that_describes_the_request_is_accepted(self) -> None:
        with temporary_workdir():
            record_traces(3)
            sent = [event.id for event in bir.load_events()]
            # Fewer accepted than attempted is ordinary: the rest are duplicates.
            reply = json.dumps({"accepted": len(sent) - 1, "event_ids": sent[:-1]})
            server = _Server(lambda: FakeResponse(reply))

            with serving(server):
                result = bir.send_events(SERVER)

            self.assertEqual(result.accepted, len(sent) - 1)
            self.assertEqual(result.skipped, 1)

    def test_the_per_event_fallback_checks_its_count_too(self) -> None:
        # A 404 on the batch endpoint must not be a way round the check.
        with temporary_workdir():
            record_one_trace()
            responses = [
                lambda: http_error(404, "no batch endpoint"),
                lambda: FakeResponse(json.dumps({"accepted": 7})),
            ]
            server = _Server(*responses)

            with serving(server), self.assertRaises(RuntimeError) as raised:
                bir.send_events(SERVER)

            self.assertIn("claimed 7 of 1 event accepted", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
