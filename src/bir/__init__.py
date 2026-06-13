"""Public API for the Bir Python SDK."""

from importlib.metadata import PackageNotFoundError, version

from ._sdk import (
    LoadedTrace,
    PromptRecord,
    SendEventsResult,
    TraceEvent,
    configure,
    generation,
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
    __version__ = version("bir")
except PackageNotFoundError:  # running from source (PYTHONPATH=src) without an install
    __version__ = "0.1.0"

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
]
