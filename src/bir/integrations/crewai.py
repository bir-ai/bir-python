"""CrewAI event-bus integration for recording Bir traces.

CrewAI's lowest-coupling, supported observability seam is its event bus: every
crew run emits typed events (``CrewKickoffStartedEvent``, ``TaskStartedEvent``,
``LLMCallStartedEvent``, ``ToolUsageStartedEvent``, and their completed/failed
counterparts) through ``crewai.utilities.events.crewai_event_bus``. The bus
invokes a registered handler with the framework's ``(source, event)`` pair, so an
application can forward those events to ``BirCrewAIHandler.on_event`` and have its
crew runs recorded as Bir traces -- without Bir importing ``crewai``:

    from crewai.utilities.events import crewai_event_bus
    from crewai.utilities.events.base_events import BaseEvent
    from bir.integrations.crewai import BirCrewAIHandler

    handler = BirCrewAIHandler()

    @crewai_event_bus.on(BaseEvent)
    def _forward(source, event):
        handler.on_event(source, event)

Events are read by duck typing -- tolerant of the field changes across CrewAI
versions -- and classified by their ``event.type`` string. A crew-kickoff event
opens a Bir trace root; task and agent-execution events become structural spans;
LLM-call events become generations carrying the model and token usage; and
tool-usage events become tool-call events. A ``*_failed`` / ``*_error`` event
closes its node with error status.

Crew, task, and agent nodes are tracked by the framework's own ids (a crew's
``id``/``fingerprint``, a task's ``id``, an agent's ``id``), exactly as the
LangChain handler tracks ``run_id``, so concurrent and nested crew runs stay
isolated. CrewAI emits LLM-call and tool-usage events without a correlation id, so
those are paired by a per-thread last-in-first-out stack: nested calls match and
concurrent crew runs on separate threads never interleave.
"""

from __future__ import annotations

from threading import get_ident
from typing import Any

from bir import generation, tool_call
from bir._sdk import _current_trace_id, _trace_context
from bir.integrations._common import _string_or_none, _usage_tokens, _value

# ``event.type`` of a node's start mapped to the Bir event the node becomes.
_START_KINDS: dict[str, str] = {
    "crew_kickoff_started": "trace",
    "task_started": "span",
    "agent_execution_started": "span",
    "llm_call_started": "generation",
    "tool_usage_started": "tool_call",
}

# ``event.type`` of a node's end mapped to ``(kind, is_error)``. The error
# variants close the node with error status, mirroring the other agent bridges.
_END_KINDS: dict[str, tuple[str, bool]] = {
    "crew_kickoff_completed": ("trace", False),
    "crew_kickoff_failed": ("trace", True),
    "task_completed": ("span", False),
    "task_failed": ("span", True),
    "agent_execution_completed": ("span", False),
    "agent_execution_error": ("span", True),
    "llm_call_completed": ("generation", False),
    "llm_call_failed": ("generation", True),
    "tool_usage_finished": ("tool_call", False),
    "tool_usage_error": ("tool_call", True),
}

# Kinds CrewAI emits without a correlation id, paired by a per-thread LIFO stack
# instead of a framework id (see the module docstring).
_STACKED_KINDS = frozenset({"generation", "tool_call"})


class BirCrewAIHandler:
    """Record CrewAI event-bus events as Bir trace events.

    Forward each ``(source, event)`` the CrewAI event bus emits to
    :meth:`on_event` without importing ``crewai``.
    ``capture_inputs``/``capture_outputs`` override Bir's global capture settings
    for the events this handler records, exactly like the other Bir callback
    handlers.
    """

    def __init__(
        self,
        *,
        capture_inputs: bool | None = None,
        capture_outputs: bool | None = None,
    ) -> None:
        self.capture_inputs = capture_inputs
        self.capture_outputs = capture_outputs
        self._active_runs: dict[str, _ActiveRun] = {}
        self._call_stacks: dict[int, list[_ActiveRun]] = {}

    def on_event(self, source: Any, event: Any) -> None:
        """Dispatch one CrewAI event to its open or close handler.

        Matches the ``(source, event)`` signature the CrewAI event bus calls a
        registered handler with. Events whose ``type`` is not a node boundary
        (progress, streaming-chunk, memory, ...) are ignored.
        """

        if event is None:
            return
        event_type = _string_or_none(_value(event, "type"))
        if event_type is None:
            return

        start_kind = _START_KINDS.get(event_type)
        if start_kind is not None:
            self._start(start_kind, source, event)
            return

        end = _END_KINDS.get(event_type)
        if end is not None:
            kind, is_error = end
            self._end(kind, source, event, is_error)

    def _start(self, kind: str, source: Any, event: Any) -> None:
        metadata = _base_metadata(event)

        if kind == "trace":
            key = f"crew:{_crew_id(source, event)}"
            if _current_trace_id.get() is None:
                context = _trace_context(name=_trace_name(source, event), metadata=metadata)
                context.__enter__()
                self._active_runs[key] = _ActiveRun("trace", context)
                return
            # A crew kickoff arriving inside an active trace (a nested crew)
            # becomes a structural span, still keyed by the crew id so the
            # matching crew-end event closes it.
            context = _span_context(_trace_name(source, event))
            context.__enter__()
            context.set_metadata(metadata)
            self._active_runs[key] = _ActiveRun("span", context)
            return

        implicit_trace = _implicit_trace_context(event)

        if kind == "generation":
            context = generation(
                "crewai.llm_call",
                model=_model_name(event),
                input=_generation_input(event),
                metadata=metadata,
                capture_input=self.capture_inputs,
                capture_output=self.capture_outputs,
            )
            context.__enter__()
            self._push(_ActiveRun("generation", context, implicit_trace=implicit_trace))
            return

        if kind == "tool_call":
            context = tool_call(
                _tool_name(event),
                input=_tool_input(event),
                metadata=metadata,
                capture_input=self.capture_inputs,
                capture_output=self.capture_outputs,
            )
            context.__enter__()
            self._push(_ActiveRun("tool_call", context, implicit_trace=implicit_trace))
            return

        # A task or agent-execution step becomes a structural span.
        context = _span_context(_span_name(event))
        context.__enter__()
        context.set_metadata(metadata)
        self._active_runs[_span_key(event)] = _ActiveRun("span", context, implicit_trace=implicit_trace)

    def _end(self, kind: str, source: Any, event: Any, is_error: bool) -> None:
        if kind in _STACKED_KINDS:
            active_run = self._pop()
        elif kind == "trace":
            active_run = self._active_runs.pop(f"crew:{_crew_id(source, event)}", None)
        else:
            active_run = self._active_runs.pop(_span_key(event), None)

        if active_run is None:
            return

        if active_run.kind == "generation":
            _finish_generation(active_run.context, event)
        elif active_run.kind == "tool_call":
            output = _tool_output(event)
            if output is not None and hasattr(active_run.context, "set_output"):
                active_run.context.set_output(output)

        error = _event_error(event) if is_error else None
        if error is None:
            active_run.context.__exit__(None, None, None)
            if active_run.implicit_trace is not None:
                active_run.implicit_trace.__exit__(None, None, None)
            return
        active_run.context.__exit__(type(error), error, None)
        if active_run.implicit_trace is not None:
            active_run.implicit_trace.__exit__(type(error), error, None)

    def _push(self, active_run: _ActiveRun) -> None:
        self._call_stacks.setdefault(get_ident(), []).append(active_run)

    def _pop(self) -> _ActiveRun | None:
        thread_id = get_ident()
        stack = self._call_stacks.get(thread_id)
        if not stack:
            return None
        active_run = stack.pop()
        if not stack:
            self._call_stacks.pop(thread_id, None)
        return active_run


class _ActiveRun:
    def __init__(self, kind: str, context: Any, *, implicit_trace: Any | None = None) -> None:
        self.kind = kind
        self.context = context
        self.implicit_trace = implicit_trace


def _implicit_trace_context(event: Any) -> Any | None:
    """Open a Bir trace root for an event that arrives with no active Bir trace.

    A crew kickoff normally opens the root before any task, agent, LLM, or tool
    event fires, so this returns ``None``. The fallback mirrors the LangChain
    handler so an event recorded without a started trace (defensive only) still
    attaches to a root instead of raising.
    """

    if _current_trace_id.get() is not None:
        return None

    metadata: dict[str, Any] = {"integration": "crewai", "kind": "implicit_root"}
    context = _trace_context(name="crewai.crew", metadata=metadata)
    context.__enter__()
    return context


def _span_context(name: str) -> Any:
    # Imported lazily, mirroring the LangChain handler, so the ``span`` builder
    # never collides with the ``span`` local elsewhere in the module.
    from bir import span

    return span(name)


def _finish_generation(context: Any, event: Any) -> None:
    output = _generation_output(event)
    if output is not None and hasattr(context, "set_output"):
        context.set_output(output)
    # The response model is only known once the call completes, so fill it in here
    # when it was unknown at the started event.
    if getattr(context, "model", None) is None:
        model = _model_name(event)
        if model is not None:
            context.model = model
    usage = _usage(event)
    if usage is not None and hasattr(context, "set_usage"):
        input_tokens, output_tokens, total_tokens = usage
        context.set_usage(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)


def _span_key(event: Any) -> str:
    """Key a task or agent-execution span by the framework id of its subject.

    An agent-execution event is keyed by its agent id and a task event by its task
    id, so a started event and its completed/failed counterpart resolve to the
    same entry while concurrent runs (with distinct ids) never collide.
    """

    event_type = _string_or_none(_value(event, "type")) or ""
    if event_type.startswith("agent_execution"):
        return f"agent:{_identity(_value(event, 'agent'))}"
    return f"task:{_identity(_value(event, 'task'))}"


def _crew_id(source: Any, event: Any) -> str:
    ident = _identity(source)
    if ident is not None:
        return ident
    name = _string_or_none(_value(event, "crew_name")) or _string_or_none(_value(source, "name"))
    if name is not None:
        return name
    return "__crewai__"


def _identity(obj: Any) -> str | None:
    """Read a stable string id from a CrewAI domain object by duck typing.

    Crews, tasks, and agents expose a UUID ``id``; agents also expose a stable
    ``key`` and crews a ``fingerprint``. The first that yields a non-empty string
    wins so a node's start and end events resolve to the same id.
    """

    if obj is None:
        return None
    for key in ("id", "fingerprint", "key"):
        ident = _stringify_id(_value(obj, key))
        if ident is not None:
            return ident
    return None


def _stringify_id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = _string_or_none(value)
    if text is not None:
        return text
    # A CrewAI ``Fingerprint`` exposes its value as ``uuid_str``; a ``UUID`` (and
    # other id objects) stringify stably for the same instance.
    uuid_str = _value(value, "uuid_str")
    if isinstance(uuid_str, str) and uuid_str:
        return uuid_str
    if isinstance(value, int):
        return str(value)
    try:
        text = str(value)
    except Exception:
        return None
    return text or None


def _trace_name(source: Any, event: Any) -> str:
    name = _string_or_none(_value(event, "crew_name")) or _string_or_none(_value(source, "name"))
    return name if name else "crewai.crew"


def _span_name(event: Any) -> str:
    event_type = _string_or_none(_value(event, "type")) or ""
    if event_type.startswith("agent_execution"):
        role = _string_or_none(_value(_value(event, "agent"), "role")) or _string_or_none(
            _value(event, "agent_role")
        )
        return role if role else "crewai.agent"
    task = _value(event, "task")
    name = _string_or_none(_value(task, "name")) or _string_or_none(_value(event, "task_name"))
    return name if name else "crewai.task"


def _tool_name(event: Any) -> str:
    name = _string_or_none(_value(event, "tool_name"))
    return name if name else "crewai.tool"


def _base_metadata(event: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {"integration": "crewai"}
    event_type = _string_or_none(_value(event, "type"))
    if event_type is not None:
        metadata["crewai_event"] = event_type
    agent_role = _string_or_none(_value(_value(event, "agent"), "role")) or _string_or_none(
        _value(event, "agent_role")
    )
    if agent_role is not None:
        metadata["agent_role"] = agent_role
    task_id = _identity(_value(event, "task"))
    if task_id is not None:
        metadata["task_id"] = task_id
    tool_name = _string_or_none(_value(event, "tool_name"))
    if tool_name is not None:
        metadata["tool_name"] = tool_name
    return metadata


def _model_name(event: Any) -> str | None:
    model = _string_or_none(_value(event, "model"))
    if model is not None:
        return model
    response = _value(event, "response")
    return _string_or_none(_value(response, "model")) if response is not None else None


def _usage(event: Any) -> tuple[Any, Any, Any] | None:
    usage = _value(event, "usage")
    if usage is None:
        response = _value(event, "response")
        usage = _value(response, "usage") if response is not None else None
    if usage is None:
        return None

    input_tokens = _usage_tokens(usage, "input_tokens", "prompt_tokens")
    output_tokens = _usage_tokens(usage, "output_tokens", "completion_tokens")
    total_tokens = _usage_tokens(usage, "total_tokens")
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    return input_tokens, output_tokens, total_tokens


def _generation_input(event: Any) -> Any:
    for key in ("messages", "prompt"):
        value = _value(event, key)
        if value is not None:
            return value
    return None


def _generation_output(event: Any) -> Any:
    return _value(event, "response")


def _tool_input(event: Any) -> Any:
    for key in ("tool_args", "args", "input"):
        value = _value(event, key)
        if value is not None:
            return value
    return None


def _tool_output(event: Any) -> Any:
    for key in ("output", "result"):
        value = _value(event, key)
        if value is not None:
            return value
    return None


def _event_error(event: Any) -> BaseException:
    """Build an exception from a CrewAI ``*_failed`` / ``*_error`` event.

    CrewAI reports a failure as an ``error`` string (or, less often, an
    ``Exception``) on the event rather than raising, so synthesize one whose
    message is redacted by Bir's normal error handling on exit.
    """

    for key in ("error", "exception", "error_message"):
        value = _value(event, key)
        if value is None:
            continue
        if isinstance(value, BaseException):
            return value
        text = _string_or_none(value) or _string_or_none(str(value))
        if text is not None:
            return RuntimeError(text)
    return RuntimeError("crewai event error")
