"""Sending against a real HTTP server rather than a patched opener.

Every other send test stubs Bir's transport seam, which is the right shape for
asserting how a response is parsed but leaves the opener itself untested — and
the opener is what decides where a request ends up. The default one follows a
301, 302, or 303 on a POST by reissuing it as a bodyless GET at whatever host the
``Location`` names, so a send could report a successful upload while the events
went nowhere and an unconfigured host's reply was parsed as the result.

These tests stand up loopback servers and drive the real thing: what the
configured server receives, what a redirect target receives, and that the
ordinary paths still work through the replaced opener.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import bir
from bir._sdk import _reset_config_for_tests
from bir.evals import Dataset, DatasetExample, exact_match, run_experiment, send_experiment

# Redirects a POST may receive. urllib's default handler turns the first three
# into a GET and refuses the last two; Bir now declines all five the same way,
# which is the point of pinning every one of them.
REDIRECT_STATUSES = (301, 302, 303, 307, 308)


class _Recorder(BaseHTTPRequestHandler):
    """Base handler that records what it was asked, and never logs to stderr."""

    received: list[dict[str, Any]]
    reply: dict[str, Any]

    def _record(self, method: str) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.received.append({"method": method, "path": self.path, "body": body})
        return body

    def _send(self, status: int, payload: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _payload(self) -> bytes:
        """The reply as JSON, without the keys that steer this handler."""

        return json.dumps({k: v for k, v in self.reply.items() if not k.startswith("__")}).encode("utf-8")

    def do_GET(self) -> None:
        self._record("GET")
        self._send(200, self._payload())

    def do_POST(self) -> None:
        self._record("POST")
        if self.path.endswith("/batch") and "__batch_status__" in self.reply:
            self._send(int(self.reply["__batch_status__"]), b'{"error": "no batch endpoint"}')
            return
        status = int(self.reply.get("__status__", 200))
        if 300 <= status < 400:
            location = self.reply.get("__location__")
            self._send(status, b"", {"Location": location} if location else {})
            return
        self._send(status, self._payload())

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the handler's stderr logging; the tests assert on responses."""


@contextmanager
def serving() -> Iterator[tuple[str, list[dict[str, Any]], dict[str, Any]]]:
    """Run a loopback server, yielding its URL, what it received, and its reply.

    Port 0 so concurrent runs never collide, and the socket is closed on the way
    out because the suite runs with ``PYTHONWARNINGS=error::ResourceWarning``.
    """

    received: list[dict[str, Any]] = []
    reply: dict[str, Any] = {"accepted": 1, "event_ids": ["id-1"]}
    handler = type("_Handler", (_Recorder,), {"received": received, "reply": reply})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", received, reply
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@contextmanager
def recorded_store(count: int = 3) -> Iterator[Path]:
    """Record ``count`` traces into a temporary store and yield its path."""

    with tempfile.TemporaryDirectory() as directory:
        store = Path(directory) / "traces.jsonl"
        bir.configure(trace_path=str(store))
        for index in range(count):
            with bir.trace(f"request-{index}"):
                pass
        try:
            yield store
        finally:
            _reset_config_for_tests()


def posted(received: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in received if entry["method"] == "POST"]


class RedirectsAreRefusedTests(unittest.TestCase):
    """A send never follows a redirect, and never reports one as a delivery."""

    def setUp(self) -> None:
        _reset_config_for_tests()

    def test_every_redirect_status_is_refused_and_nothing_reaches_the_target(self) -> None:
        with serving() as (elsewhere_url, elsewhere_received, _elsewhere_reply):
            with serving() as (configured_url, configured_received, configured_reply):
                with recorded_store() as store:
                    for status in REDIRECT_STATUSES:
                        with self.subTest(status=status):
                            elsewhere_received.clear()
                            configured_reply.clear()
                            configured_reply.update(
                                {"__status__": status, "__location__": f"{elsewhere_url}/v1/events"}
                            )

                            with self.assertRaises(RuntimeError) as raised:
                                bir.send_events(path=str(store), server_url=configured_url, retries=0)

                            message = str(raised.exception)
                            self.assertIn(f"HTTP {status}", message)
                            self.assertIn(elsewhere_url, message)
                            self.assertIn("does not follow redirects", message)
                            # The whole point: the host nobody configured was
                            # never contacted, by any method.
                            self.assertEqual(elsewhere_received, [])
                            # And the configured one only ever saw the POST it
                            # answered with a redirect.
                            self.assertTrue(posted(configured_received))
                            configured_received.clear()

    def test_a_redirect_without_a_location_is_still_refused(self) -> None:
        with serving() as (url, _received, reply):
            with recorded_store() as store:
                reply.clear()
                reply.update({"__status__": 302})

                with self.assertRaises(RuntimeError) as raised:
                    bir.send_events(path=str(store), server_url=url, retries=0)

                message = str(raised.exception)
                self.assertIn("HTTP 302", message)
                # Named as absent rather than rendered as an empty target.
                self.assertIn("a Location header it did not send", message)
                self.assertIn("does not follow redirects", message)

    def test_an_over_long_location_is_bounded_in_the_message(self) -> None:
        with serving() as (url, _received, reply):
            with recorded_store() as store:
                # The Location is the server's to choose, so it is bounded like
                # every other body an error message shows.
                reply.clear()
                reply.update({"__status__": 302, "__location__": "http://x/" + "a" * 4000})

                with self.assertRaises(RuntimeError) as raised:
                    bir.send_events(path=str(store), server_url=url, retries=0)

                message = str(raised.exception)
                self.assertIn("…[truncated]", message)
                self.assertLess(len(message), 1000)

    def test_send_experiment_refuses_a_redirect_too(self) -> None:
        with serving() as (elsewhere_url, elsewhere_received, _reply):
            with serving() as (configured_url, _configured_received, configured_reply):
                with tempfile.TemporaryDirectory() as directory:
                    bir.configure(trace_path=str(Path(directory) / "traces.jsonl"))
                    result = run_experiment(
                        "faq",
                        dataset=Dataset([DatasetExample(id="q1", input="hi", expected="ok")]),
                        task=lambda _question: "ok",
                        evaluators=[exact_match()],
                        path=Path(directory) / "faq.jsonl",
                    )
                    configured_reply.clear()
                    configured_reply.update({"__status__": 302, "__location__": f"{elsewhere_url}/v1/experiments"})

                    with self.assertRaises(RuntimeError) as raised:
                        send_experiment(result.path or "", server_url=configured_url, retries=0)

                    self.assertIn("HTTP 302", str(raised.exception))
                    self.assertEqual(elsewhere_received, [])


class TheOrdinaryPathsStillWorkTests(unittest.TestCase):
    """Replacing the opener must not change how a normal exchange behaves."""

    def setUp(self) -> None:
        _reset_config_for_tests()

    def test_a_successful_batch_really_posts_the_events(self) -> None:
        with serving() as (url, received, reply):
            with recorded_store(count=3) as store:
                events = bir.load_events(path=str(store))
                reply.clear()
                reply.update({"accepted": len(events), "event_ids": [event.id for event in events]})

                result = bir.send_events(path=str(store), server_url=url, retries=0)

                self.assertEqual(result.accepted, len(events))
                self.assertEqual(result.attempted, len(events))
                # The bytes really went over a socket, to the configured host.
                requests = posted(received)
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]["path"], "/v1/events/batch")
                self.assertEqual(len(json.loads(requests[0]["body"])), len(events))

    def test_a_rejection_is_still_permanent_and_a_server_error_still_retries(self) -> None:
        for status, expected_posts in ((400, 1), (500, 3)):
            with self.subTest(status=status):
                with serving() as (url, received, reply):
                    with recorded_store() as store:
                        reply.clear()
                        reply.update({"__status__": status})

                        with self.assertRaises(RuntimeError) as raised:
                            bir.send_events(path=str(store), server_url=url, retries=2, backoff=0.0)

                        self.assertIn(f"HTTP {status}", str(raised.exception))
                        # 4xx is raised on the first answer; 5xx is retried.
                        self.assertEqual(len(posted(received)), expected_posts)

    def test_a_batch_endpoint_that_is_absent_falls_back_to_one_request_per_event(self) -> None:
        with serving() as (url, received, reply):
            with recorded_store(count=2) as store:
                events = bir.load_events(path=str(store))
                # 404 on the batch path is how a server says it has no batch
                # endpoint; the fallback then posts each event on its own.
                reply.clear()
                reply.update({"__batch_status__": 404, "accepted": 1})

                result = bir.send_events(path=str(store), server_url=url, retries=0)

                self.assertEqual(result.accepted, len(events))
                requests = posted(received)
                # One refused batch attempt, then one request per event.
                self.assertEqual(len(requests), 1 + len(events))
                self.assertTrue(requests[0]["path"].endswith("/batch"))
                self.assertTrue(all(not entry["path"].endswith("/batch") for entry in requests[1:]))


if __name__ == "__main__":
    unittest.main()
