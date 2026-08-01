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


def _read_http_error_body(exc: urllib.error.HTTPError) -> str:
    """Read and close an HTTP error response without leaking its file object."""

    try:
        return exc.read().decode("utf-8", errors="replace")
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
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            exc.close()
            return None
        body = _read_http_error_body(exc)
        message = f"bir server rejected event batch with HTTP {exc.code}: {body}"
        if _is_retryable_status(exc.code):
            raise _TransientSendError(message, cause=exc) from exc
        raise RuntimeError(message) from exc
    except urllib.error.URLError as exc:
        raise _TransientSendError(f"bir could not send events to {endpoint}: {exc.reason}", cause=exc) from exc
    except TimeoutError as exc:
        # A socket read timeout surfaces as TimeoutError rather than URLError.
        raise _TransientSendError(f"bir could not send events to {endpoint}: {exc}", cause=exc) from exc

    if status < 200 or status >= 300:
        raise RuntimeError(f"bir server rejected event batch with HTTP {status}: {body}")
    return _batch_result_from_response(body, attempted=len(events))


def _batch_result_from_response(body: str, *, attempted: int) -> SendEventsResult:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"bir server returned an invalid batch response: {body}") from exc
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"bir server returned an invalid batch response: {body}")
    accepted = payload.get("accepted")
    event_ids = payload.get("event_ids")
    if isinstance(accepted, bool) or not isinstance(accepted, int):
        raise RuntimeError(f"bir server returned an invalid batch response: {body}")
    if not isinstance(event_ids, list) or not all(isinstance(event_id, str) for event_id in event_ids):
        raise RuntimeError(f"bir server returned an invalid batch response: {body}")
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
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = _read_http_error_body(exc)
        message = f"bir server rejected event with HTTP {exc.code}: {body}"
        if _is_retryable_status(exc.code):
            raise _TransientSendError(message, cause=exc) from exc
        raise RuntimeError(message) from exc
    except urllib.error.URLError as exc:
        raise _TransientSendError(f"bir could not send event to {endpoint}: {exc.reason}", cause=exc) from exc
    except TimeoutError as exc:
        # A socket read timeout surfaces as TimeoutError rather than URLError.
        raise _TransientSendError(f"bir could not send event to {endpoint}: {exc}", cause=exc) from exc

    if status < 200 or status >= 300:
        raise RuntimeError(f"bir server rejected event with HTTP {status}: {body}")
    return _accepted_count_from_response(body)


def _accepted_count_from_response(body: str) -> int:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return 1
    if not isinstance(payload, Mapping):
        return 1
    accepted = payload.get("accepted")
    if isinstance(accepted, int) and not isinstance(accepted, bool):
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
