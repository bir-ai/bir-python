"""HTTP event transport helpers for the Bir SDK.

This dependency-bottom module owns request serialization, response parsing, and
retry classification.  It uses only the standard library and deliberately does
not depend on tracing or persistence modules.  :mod:`bir._sdk` owns the public
send orchestration and re-exports :class:`SendEventsResult` for compatibility.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class SendEventsResult:
    """Result returned after sending local events to a Bir server."""

    accepted: int
    event_ids: list[str]
    attempted: int = 0

    @property
    def skipped(self) -> int:
        """Return events the server did not newly accept, usually duplicates."""

        return max(self.attempted - self.accepted, 0)


def _set_public_sdk_module(cls: type[Any]) -> None:
    """Restore the public SDK identity of a transport-owned value class."""

    cls.__module__ = "bir._sdk"
    for member in cls.__dict__.values():
        function: Any
        if isinstance(member, (classmethod, staticmethod)):
            function = member.__func__
        elif isinstance(member, property):
            function = member.fget
        else:
            function = member
        if callable(function) and getattr(function, "__module__", None) == __name__:
            function.__module__ = "bir._sdk"


_set_public_sdk_module(SendEventsResult)


class _TransientSendError(Exception):
    """Internal signal that a send attempt failed transiently and may be retried.

    Carries the original cause so :func:`_send_with_retry` can chain it when the
    retries are exhausted and the failure is re-raised as a ``RuntimeError``.
    """

    def __init__(self, message: str, *, cause: BaseException) -> None:
        super().__init__(message)
        self.cause = cause


def _events_endpoint(server_url: str) -> str:
    normalized_url = server_url.rstrip("/")
    if not normalized_url:
        raise ValueError("bir server_url must not be empty")
    return f"{normalized_url}/v1/events"


def _is_retryable_status(status: int) -> bool:
    """Return True for HTTP 5xx, the only status codes worth retrying."""

    return 500 <= status < 600


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect handler that follows nothing, replacing the default one.

    ``urlopen`` uses an opener carrying :class:`urllib.request.HTTPRedirectHandler`,
    which answers a 301, 302, or 303 on a POST by reissuing the request as a GET
    *with no body*, at whatever host the ``Location`` header names. For a send
    that means the events are never posted anywhere, the unconfigured host's
    reply is parsed as the result, and the caller is told the upload succeeded.

    Returning ``None`` from :meth:`redirect_request` leaves the status
    unhandled, so the opener falls through to its default error handler and
    raises :class:`urllib.error.HTTPError` with the status and headers intact —
    which is what lets the send report the redirect it declined.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


# Built once, and used instead of ``urlopen``. ``build_opener`` keeps every other
# default handler -- proxy support included -- and drops only the one this
# subclass replaces. It also means a globally installed opener (from
# ``urllib.request.install_opener``) does not steer where Bir sends, which is
# ambient state no caller asked Bir to obey.
_opener = urllib.request.build_opener(_RefuseRedirects)


def _is_redirect_status(status: int) -> bool:
    """Return True for the 3xx statuses a send declines to follow."""

    return 300 <= status < 400


def _redirect_refusal(endpoint: str, exc: urllib.error.HTTPError) -> str:
    """Return the message for a redirect Bir did not follow, and close the response.

    The ``Location`` is the server's to choose, so it is bounded like every other
    body an error message shows; the CLI escapes it where it prints.
    """

    location = exc.headers.get("Location")
    exc.close()
    target = _reported_body(location) if location else "a Location header it did not send"
    return (
        f"bir server at {endpoint} answered HTTP {exc.code} with a redirect to {target}; "
        "bir does not follow redirects, so nothing was sent. Point the server URL at the "
        "address that serves the API."
    )


# A response body reaches a person only inside an error message, and enough of it
# to show why the server refused is enough. Bounding it keeps a ``--server`` URL
# pointed at the wrong host from putting that host's whole document on the
# terminal, and keeps the exception a program catches to a size worth logging.
_MAX_REPORTED_BODY_CHARS = 500

# Spelled like the capture truncation marker in ``_capture.py`` so a value the
# SDK cut short reads the same wherever it appears. Not imported from there:
# this module is the transport bottom and depends on the standard library only.
_TRUNCATED = "…[truncated]"


def _reported_body(body: str) -> str:
    """Return as much of ``body`` as an error message carries, marking what it cut."""

    if len(body) <= _MAX_REPORTED_BODY_CHARS:
        return body
    return body[:_MAX_REPORTED_BODY_CHARS] + _TRUNCATED


# What a success response may weigh, derived from the request rather than fixed.
# Its whole job is to carry an acceptance count and the ids of the events that
# were sent, so an id's worth per event plus room for the envelope is the most
# any honest reply needs. Generous on both: an id is a 36-character UUID, and
# a server free to spell ids its own way still has room to. Without a bound tied
# to the request, a single reply could be any size at all -- the loaders stream,
# prune is disk-backed, and the upload spool is disk-backed, so this was the one
# value in the SDK that could grow without limit.
_RESPONSE_ENVELOPE_BYTES = 4096
_RESPONSE_BYTES_PER_EVENT = 256


def _max_response_bytes(attempted: int) -> int:
    """Return the largest response the events sent could justify."""

    return _RESPONSE_ENVELOPE_BYTES + attempted * _RESPONSE_BYTES_PER_EVENT


def _read_bounded_response(response: Any, *, attempted: int, endpoint: str) -> str:
    """Read a success response, refusing one larger than the request justifies.

    One byte past the limit is read so a reply that just fits is told from one
    that was cut, the same way :func:`_read_http_error_body` does it.
    """

    limit = _max_response_bytes(attempted)
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise RuntimeError(
            f"bir server at {endpoint} answered with more than {limit} bytes for {attempted} event(s); "
            "a reply that large cannot be the ids of what was sent, so it was not read"
        )
    return raw.decode("utf-8", errors="replace")


def _read_http_error_body(exc: urllib.error.HTTPError) -> str:
    """Read as much of an HTTP error response as a message carries, and close it.

    An error body is never parsed -- it exists only to say why the server
    refused -- so reading past what the message will show buys nothing, and a
    misdirected ``--server`` answering with a large document would otherwise be
    pulled into memory whole. One character past the limit is read so
    :func:`_reported_body` can tell a body that just fits from one that was cut.
    """

    try:
        return exc.read(_MAX_REPORTED_BODY_CHARS + 1).decode("utf-8", errors="replace")
    finally:
        exc.close()


def _post_event_batch(
    endpoint: str,
    events: list[dict[str, Any]],
    *,
    timeout: float,
) -> SendEventsResult | None:
    """Post all events in one request; return None when the server has no batch endpoint."""

    payload = json.dumps(events, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _opener.open(request, timeout=timeout) as response:
            status = response.status
            body = _read_bounded_response(response, attempted=len(events), endpoint=endpoint)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            exc.close()
            return None
        if _is_redirect_status(exc.code):
            raise RuntimeError(_redirect_refusal(endpoint, exc)) from exc
        body = _read_http_error_body(exc)
        message = f"bir server rejected event batch with HTTP {exc.code}: {_reported_body(body)}"
        if _is_retryable_status(exc.code):
            raise _TransientSendError(message, cause=exc) from exc
        raise RuntimeError(message) from exc
    except urllib.error.URLError as exc:
        raise _TransientSendError(f"bir could not send events to {endpoint}: {exc.reason}", cause=exc) from exc
    except TimeoutError as exc:
        # A socket read timeout surfaces as TimeoutError rather than URLError.
        raise _TransientSendError(f"bir could not send events to {endpoint}: {exc}", cause=exc) from exc

    if status < 200 or status >= 300:
        raise RuntimeError(f"bir server rejected event batch with HTTP {status}: {_reported_body(body)}")
    sent_ids = {event["id"] for event in events if isinstance(event.get("id"), str)}
    return _batch_result_from_response(body, attempted=len(events), sent_ids=sent_ids)


def _batch_result_from_response(body: str, *, attempted: int, sent_ids: set[str] | None = None) -> SendEventsResult:
    """Parse a batch response, refusing one that cannot describe the request.

    The shape checks say the reply is the right kind of object. These say it is a
    reply to *this* request: a count no larger than what was sent and no smaller
    than none of it, and ids naming events that were actually posted. Both are
    reported figures a caller acts on — ``accepted`` and ``skipped`` are printed
    and gate pipelines, and ``event_ids`` is what ``--mark-sent`` records as
    delivered, so an id the server invented would be remembered as sent forever.

    ``sent_ids`` is optional so the shape checks can still be exercised on their
    own; the send path always passes it.
    """

    refused = f"bir server returned an invalid batch response: {_reported_body(body)}"
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(refused) from exc
    if not isinstance(payload, Mapping):
        raise RuntimeError(refused)
    accepted = payload.get("accepted")
    event_ids = payload.get("event_ids")
    if isinstance(accepted, bool) or not isinstance(accepted, int):
        raise RuntimeError(refused)
    if not isinstance(event_ids, list) or not all(isinstance(event_id, str) for event_id in event_ids):
        raise RuntimeError(refused)
    if accepted < 0 or accepted > attempted:
        raise RuntimeError(
            f"bir server claimed {accepted} of {attempted} event(s) accepted, which is not a count of "
            f"what was sent: {_reported_body(body)}"
        )
    if len(event_ids) > attempted:
        raise RuntimeError(
            f"bir server returned {len(event_ids)} id(s) for {attempted} event(s) sent: {_reported_body(body)}"
        )
    if sent_ids is not None:
        unknown = [event_id for event_id in event_ids if event_id not in sent_ids]
        if unknown:
            raise RuntimeError(
                f"bir server returned {len(unknown)} id(s) naming events that were not sent, "
                f"first {unknown[0]!r}: {_reported_body(body)}"
            )
    return SendEventsResult(accepted=accepted, event_ids=list(event_ids), attempted=attempted)


def _post_event(endpoint: str, event: Mapping[str, Any], *, timeout: float) -> int:
    payload = json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _opener.open(request, timeout=timeout) as response:
            status = response.status
            body = _read_bounded_response(response, attempted=1, endpoint=endpoint)
    except urllib.error.HTTPError as exc:
        if _is_redirect_status(exc.code):
            raise RuntimeError(_redirect_refusal(endpoint, exc)) from exc
        body = _read_http_error_body(exc)
        message = f"bir server rejected event with HTTP {exc.code}: {_reported_body(body)}"
        if _is_retryable_status(exc.code):
            raise _TransientSendError(message, cause=exc) from exc
        raise RuntimeError(message) from exc
    except urllib.error.URLError as exc:
        raise _TransientSendError(f"bir could not send event to {endpoint}: {exc.reason}", cause=exc) from exc
    except TimeoutError as exc:
        # A socket read timeout surfaces as TimeoutError rather than URLError.
        raise _TransientSendError(f"bir could not send event to {endpoint}: {exc}", cause=exc) from exc

    if status < 200 or status >= 300:
        raise RuntimeError(f"bir server rejected event with HTTP {status}: {_reported_body(body)}")
    return _accepted_count_from_response(body)


def _accepted_count_from_response(body: str) -> int:
    """Return how many of the one posted event the server accepted.

    A body it cannot read means the event was posted and answered with 2xx, which
    is the only thing this path has to decide, so an unreadable reply still counts
    as one. A *readable* count outside ``0..1`` is different: the request carried
    one event, so the server is describing something other than what was sent.
    """

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return 1
    if not isinstance(payload, Mapping):
        return 1
    accepted = payload.get("accepted")
    if isinstance(accepted, int) and not isinstance(accepted, bool):
        if accepted < 0 or accepted > 1:
            raise RuntimeError(
                f"bir server claimed {accepted} of 1 event accepted, which is not a count of "
                f"what was sent: {_reported_body(body)}"
            )
        return accepted
    return 1


def _send_with_retry(operation: Callable[[], T], *, retries: int, backoff: float) -> T:
    """Run ``operation`` and retry transient send failures with exponential backoff.

    A transient failure (network error, timeout, or HTTP 5xx) is raised by the
    callers as :class:`_TransientSendError` and retried up to ``retries`` times,
    sleeping ``backoff * 2**attempt`` seconds before each retry. Permanent failures
    (HTTP 4xx, raised as ``RuntimeError``) propagate immediately. When the retries
    are exhausted the failure is surfaced as ``RuntimeError`` so callers see the
    same exception type a single failed attempt raises.
    """

    attempt = 0
    while True:
        try:
            return operation()
        except _TransientSendError as exc:
            if attempt >= retries:
                raise RuntimeError(str(exc)) from exc.cause
            time.sleep(backoff * (2**attempt))
            attempt += 1
