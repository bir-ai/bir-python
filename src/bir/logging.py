"""Stamp standard-library log records with the active Bir trace and span ids.

Correlating your application logs with Bir traces is the primary documented use of
:func:`bir.get_current_trace_id` / :func:`bir.get_current_span_id`. Doing it by hand
means threading ``extra={"trace_id": ...}`` through every ``logging`` call. This
module removes that plumbing: attach :class:`BirTraceIdFilter` once and every
:class:`logging.LogRecord` that flows through the logger or handler gains two
attributes, ready for any formatter to render::

    import logging

    from bir.logging import install_trace_id_filter

    logging.basicConfig(
        format="%(asctime)s %(levelname)s [trace=%(bir_trace_id)s span=%(bir_span_id)s] %(message)s"
    )
    install_trace_id_filter()

Configure logging first. A filter attached to a *logger* only sees records that
logger creates, so the stamp has to reach the *handlers* an application's loggers
propagate to — which means the handlers have to exist when
:func:`install_trace_id_filter` runs. See its docstring for exactly which records
each target covers.

The stamped attributes mirror the accessors exactly: inside a trace they equal
:func:`bir.get_current_trace_id` / :func:`bir.get_current_span_id`, and outside any
trace they are ``None``. The ids are read from the same task-local context as the
accessors, so each asyncio task and thread sees its own ids and never another's.

Like the accessors, this is read-only: the filter only reads the active ids onto the
record. There is no setter and no cross-process propagation, consistent with the
accessors' design. The filter never raises, so it is safe to leave attached on every
log call, inside or outside a trace.
"""

from __future__ import annotations

import logging
import warnings

from ._sdk import get_current_span_id, get_current_trace_id

__all__ = [
    "BirTraceIdFilter",
    "TRACE_ID_FIELD",
    "SPAN_ID_FIELD",
    "install_trace_id_filter",
]

#: ``LogRecord`` attribute the filter sets to the active trace id. Safe to use in a
#: ``%(...)s`` format string as ``%(bir_trace_id)s``.
TRACE_ID_FIELD = "bir_trace_id"

#: ``LogRecord`` attribute the filter sets to the active span id. Safe to use in a
#: ``%(...)s`` format string as ``%(bir_span_id)s``.
SPAN_ID_FIELD = "bir_span_id"


class BirTraceIdFilter(logging.Filter):
    """A :class:`logging.Filter` that stamps the active Bir ids onto every record.

    On each record it sets two attributes from the current task-local context:

    * ``record.bir_trace_id`` — the active trace's id (see
      :func:`bir.get_current_trace_id`), or ``None`` outside any trace.
    * ``record.bir_span_id`` — the innermost open span/generation/tool-call id, or
      the trace root when none is open (see :func:`bir.get_current_span_id`), or
      ``None`` outside any trace.

    The names match :data:`TRACE_ID_FIELD` and :data:`SPAN_ID_FIELD` and are safe to
    render with ``%(bir_trace_id)s`` / ``%(bir_span_id)s``. The values equal the
    ``trace_id`` / ``parent_id`` later written to the JSONL, so a stamped log lines
    up with the trace. :meth:`filter` always returns ``True`` (it is used purely to
    annotate, never to drop records) and never raises.

    Despite the :class:`logging.Filter` base, this does not filter by logger name —
    pass it to ``addFilter`` on a logger or a handler. Attaching it to a logger
    stamps records created by that logger; attaching it to a handler stamps every
    record the handler emits (including those propagated from child loggers).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.bir_trace_id = get_current_trace_id()
        record.bir_span_id = get_current_span_id()
        return True


def install_trace_id_filter(
    target: logging.Logger | logging.Handler | None = None,
) -> BirTraceIdFilter:
    """Attach a :class:`BirTraceIdFilter` where it will see your records, and return it.

    Which records a filter stamps depends entirely on what it is attached to, and
    the distinction matters more than it looks:

    * On a **handler**, it stamps every record that handler emits, including
      records propagated to it from child loggers. This is what an application
      wants.
    * On a **logger**, it stamps only the records *that logger itself creates*.
      Records from ``logging.getLogger("myapp")`` propagate to the root logger's
      handlers but never through the root logger's filters, so a filter on the
      root logger does not stamp them.

    With no argument the filter is therefore attached to the root logger's current
    handlers, and to the root logger itself for records created directly on it.
    Configure logging before calling this: a handler added afterwards has no
    filter on it, and records it emits reach the formatter unstamped. Pass that
    handler explicitly when you add one.

    An unstamped record is worse than a missing id. A formatter asking for
    ``%(bir_trace_id)s`` raises on a record that does not carry it, and ``logging``
    drops the line and writes the error to stderr instead — so an install that
    misses a handler silently costs log lines, not just correlation. Because of
    that, a no-argument call on a root logger with no handlers warns
    (``RuntimeWarning``) rather than attaching to nothing quietly.

    Returns the filter, which is a single instance attached to each target, so it
    can be removed again with ``target.removeFilter(returned_filter)`` on each of
    them. Calling this more than once attaches independent filters; each stamps
    the same attributes, so the duplication is harmless but you can avoid it by
    reusing the returned instance.
    """

    trace_id_filter = BirTraceIdFilter()
    if target is not None:
        target.addFilter(trace_id_filter)
        return trace_id_filter

    root = logging.getLogger()
    # The handlers carry the stamp for everything that propagates to root; the
    # root logger itself carries it for records created directly on it, which its
    # own filters do see.
    for handler in root.handlers:
        handler.addFilter(trace_id_filter)
    root.addFilter(trace_id_filter)
    if not root.handlers:
        # Nothing to attach the stamp to, so records from application loggers
        # will reach their formatter unstamped once handlers do appear. Silence
        # here is what made this cost log lines rather than just ids, so it is
        # said out loud instead.
        warnings.warn(
            "bir.logging.install_trace_id_filter() found no handlers on the root logger, so records "
            "from your application's loggers will not be stamped. Configure logging first (for example "
            "logging.basicConfig(...)) and then call this, or pass the handler explicitly.",
            RuntimeWarning,
            stacklevel=2,
        )
    return trace_id_filter
