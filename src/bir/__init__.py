"""Public API for the Bir Python SDK."""

from importlib.metadata import PackageNotFoundError, version

from ._sdk import (
    LoadedTrace,
    PromptRecord,
    SendEventsResult,
    TraceEvent,
    configure,
    generation,
    get_current_span_id,
    get_current_trace_id,
    load_events,
    load_traces,
    observe,
    prompt,
    retrieval,
    score,
    send_events,
    span,
    tool_call,
    trace,
)

try:
    # The published distribution is "bir-sdk"; the import package is "bir".
    __version__ = version("bir-sdk")
except PackageNotFoundError:
    # Fallback only applies when running from source (PYTHONPATH=src) without an
    # install, where no distribution metadata exists.
    __version__ = "0.2.0"

__all__ = [
    "__version__",
    "TraceEvent",
    "LoadedTrace",
    "SendEventsResult",
    "PromptRecord",
    "configure",
    "load_events",
    "load_traces",
    "send_events",
    "observe",
    "trace",
    "prompt",
    "span",
    "generation",
    "tool_call",
    "retrieval",
    "score",
    "get_current_trace_id",
    "get_current_span_id",
]
