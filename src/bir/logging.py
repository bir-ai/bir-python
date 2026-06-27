"""Stamp standard-library log records with the active Bir trace and span ids.

Correlating your application logs with Bir traces is the primary documented use of
:func:`bir.get_current_trace_id` / :func:`bir.get_current_span_id`. Doing it by hand
means threading ``extra={"trace_id": ...}`` through every ``logging`` call. This
module removes that plumbing: attach :class:`BirTraceIdFilter` once and every
:class:`logging.LogRecord` that flows through the logger or handler gains two
attributes, ready for any formatter to render::

    import logging

    from bir.logging import install_trace_id_filter

    install_trace_id_filter()
    logging.basicConfig(
        format="%(asctime)s %(levelname)s [trace=%(bir_trace_id)s span=%(bir_span_id)s] %(message)s"
    )

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
    """Attach a :class:`BirTraceIdFilter` to a logger or handler and return it.

    With no argument the filter is added to the root logger, which is enough for the
    common case where application loggers propagate to root. Pass a specific
    :class:`logging.Logger` or :class:`logging.Handler` to scope it; attaching to a
    handler is the most reliable way to stamp every record that handler emits,
    including ones propagated from child loggers.

    Returns the created filter so it can later be removed with
    ``target.removeFilter(returned_filter)``. Calling this more than once on the same
    target attaches independent filters; each stamps the same attributes, so the
    duplication is harmless but you can avoid it by reusing the returned instance.
    """

    if target is None:
        target = logging.getLogger()
    trace_id_filter = BirTraceIdFilter()
    target.addFilter(trace_id_filter)
    return trace_id_filter
