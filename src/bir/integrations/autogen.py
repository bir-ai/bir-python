"""AG2 (AutoGen) runtime-logging integration for recording Bir traces.

AG2's lowest-coupling, dependency-free observability seam is its runtime logging:
``autogen.runtime_logging.start(logger=...)`` installs a process-wide logger that
AG2 drives purely by calling its methods by name -- ``log_chat_completion``,
``log_function_use``, ``log_event``, ``log_new_agent`` (and the wrapper/client
variants), plus ``start`` / ``stop`` / ``get_connection``. Because AG2 calls the
logger by method name and never inspects its type, an application can register
``BirAutoGenHandler`` and have its multi-agent runs recorded as Bir traces -- without
Bir importing ``autogen`` / ``ag2``:

    import autogen
    from bir.integrations.autogen import BirAutoGenHandler

    autogen.runtime_logging.start(logger=BirAutoGenHandler())
    user_proxy.initiate_chat(assistant, message="What is Bir?")
    autogen.runtime_logging.stop()

The logger callbacks are read by duck typing -- tolerant of the field changes across
AG2 versions. ``start`` opens one Bir trace per run (a structural span instead when a
Bir trace is already active, so an AG2 run nested in another integration stays a
subtree); each agent's turn becomes a structural span, opened lazily on the first
event whose ``source`` is that agent and closed when the speaking agent changes;
``log_chat_completion`` becomes a generation carrying the model and token usage (read
from the OpenAI-shaped response via the shared ``_common`` helpers) plus the cost AG2
reports; ``log_function_use`` becomes a tool call; and a failing completion, function,
or event (an exception object, an error-flagged result, or an ``exception`` event) is
recorded with error status. ``stop`` closes the run.

AG2's runtime logging is a process-wide singleton without per-call correlation ids,
so open nodes are tracked on a per-thread last-in-first-out stack -- exactly as the
CrewAI handler pairs its uncorrelated events -- and each run is keyed only on that
thread's stack, so concurrent runs on separate threads and nested runs stay isolated.

(AutoGen 0.4+ / ``autogen-core`` instead emits OpenTelemetry spans; that line is
served by the same OTel ``SpanProcessor`` pattern as ``BirPydanticAIHandler`` and is
out of scope for this runtime-logging bridge.)
"""

from __future__ import annotations

from collections.abc import Mapping
from threading import get_ident
from typing import Any
from uuid import uuid4

from bir import generation, tool_call
from bir._sdk import _current_trace_id, _trace_context
from bir.integrations._common import _response_output, _string_or_none, _usage_tokens, _value

# ``log_event`` names that mark an agent-turn boundary rather than a recorded event.
_TURN_EVENTS = frozenset({"received_message"})


class BirAutoGenHandler:
    """Record AG2 runtime-logging callbacks as Bir trace events.

    Implements AG2's ``BaseLogger`` interface by method name without importing
    ``autogen`` / ``ag2``; register it with
    ``autogen.runtime_logging.start(logger=BirAutoGenHandler())``.
    ``capture_inputs`` / ``capture_outputs`` override Bir's global capture settings
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
        self._node_stacks: dict[int, list[_ActiveNode]] = {}

    # -- AG2 BaseLogger lifecycle -------------------------------------------------

    def start(self) -> str:
        """Open the Bir trace for an AG2 run; return a session id for AG2.

        A run opened while another Bir trace is already active becomes a structural
        span instead of a new root, so an AG2 run nested inside another integration
        stays a subtree of the surrounding trace.
        """

        session_id = uuid4().hex
        metadata = {"integration": "autogen", "session_id": session_id}
        if _current_trace_id.get() is None:
            context: Any = _trace_context(name="autogen.run", metadata=metadata)
            context.__enter__()
        else:
            context = _span_context("autogen.run")
            context.__enter__()
            context.set_metadata(metadata)
        self._push(_ActiveNode("run", context))
        return session_id

    def stop(self) -> None:
        """Close this run's open turn span (if any) and its root."""

        node = self._peek()
        if node is None:
            return
        if node.kind == "turn":
            _exit(self._pop(), None)
            node = self._peek()
        if node is not None and node.kind == "run":
            _exit(self._pop(), None)

    def get_connection(self) -> Any:
        # AG2's SQLite logger returns a DB connection here; Bir writes events
        # straight through its own storage, so there is nothing to hand back.
        return None

    # -- AG2 BaseLogger event callbacks -------------------------------------------

    def log_chat_completion(
        self,
        invocation_id: Any = None,
        client_id: Any = None,
        wrapper_id: Any = None,
        source: Any = None,
        request: Any = None,
        response: Any = None,
        is_cached: Any = None,
        cost: Any = None,
        start_time: Any = None,
        **_extra: Any,
    ) -> None:
        """Record an AG2 LLM call as a Bir generation (model + usage + cost)."""

        del invocation_id, client_id, wrapper_id, start_time
        implicit_trace = self._implicit_root()
        self._ensure_turn(source)

        context = generation(
            "autogen.chat_completion",
            model=_model_name(request, response),
            input=_generation_input(request),
            metadata=_event_metadata(source, "chat_completion", is_cached),
            capture_input=self.capture_inputs,
            capture_output=self.capture_outputs,
        )
        context.__enter__()
        error = _response_error(response)
        if error is None:
            output = _generation_output(response)
            if output is not None:
                context.set_output(output)
            usage = _usage(response)
            if usage is not None:
                input_tokens, output_tokens, total_tokens = usage
                context.set_usage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                )
            if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
                context.set_cost(total_cost=cost)
        _exit_context(context, error)
        _exit_context(implicit_trace, error)

    def log_function_use(
        self,
        source: Any = None,
        function: Any = None,
        args: Any = None,
        returns: Any = None,
        **_extra: Any,
    ) -> None:
        """Record an AG2 tool/function execution as a Bir tool call."""

        implicit_trace = self._implicit_root()
        self._ensure_turn(source)

        context = tool_call(
            _function_name(function),
            input=args,
            metadata=_event_metadata(source, "function_use", None),
            capture_input=self.capture_inputs,
            capture_output=self.capture_outputs,
        )
        context.__enter__()
        error = _returns_error(returns)
        if error is None and returns is not None:
            context.set_output(returns)
        _exit_context(context, error)
        _exit_context(implicit_trace, error)

    def log_event(self, source: Any = None, name: Any = None, **kwargs: Any) -> None:
        """Map an AG2 event to a turn boundary or an error, ignoring the rest.

        A ``received_message`` event opens (or switches to) the receiving agent's
        turn span; an ``exception`` / error event becomes an error-status span; every
        other event (state changes, group-chat bookkeeping, ...) is ignored.
        """

        event_name = _string_or_none(name)
        if event_name in _TURN_EVENTS:
            if self._peek() is not None:
                self._ensure_turn(source)
            return

        error = _event_error(event_name, kwargs)
        if error is None:
            return
        implicit_trace = self._implicit_root()
        self._ensure_turn(source)
        context = _span_context(event_name or "autogen.error")
        context.__enter__()
        context.set_metadata(_event_metadata(source, event_name or "error", None))
        _exit_context(context, error)
        _exit_context(implicit_trace, error)

    def log_new_agent(self, agent: Any = None, init_args: Any = None, **_extra: Any) -> None:
        # Agent registration carries no run activity; agent identity is read from
        # each event's ``source`` instead. Defined to satisfy the logger interface.
        return None

    def log_new_wrapper(self, wrapper: Any = None, init_args: Any = None, **_extra: Any) -> None:
        # Client-wrapper construction is configuration, not run activity.
        return None

    def log_new_client(
        self, client: Any = None, wrapper: Any = None, init_args: Any = None, **_extra: Any
    ) -> None:
        # Client construction is configuration, not run activity.
        return None

    # -- turn-span management -----------------------------------------------------

    def _ensure_turn(self, source: Any) -> None:
        """Open or reuse the speaking agent's turn span on the current run.

        AG2 conversations are sequential, so one turn span is open at a time: an
        event from a new agent closes the previous turn and opens a new one keyed by
        the agent's identity. When no run is open on this thread (``start`` was not
        called) the caller's implicit root carries the event instead and no turn span
        is created.
        """

        top = self._peek()
        if top is None:
            return
        agent_id, display = _agent_identity(source)
        if top.kind == "turn":
            if top.agent_id == agent_id:
                return
            _exit(self._pop(), None)

        context = _span_context(display)
        context.__enter__()
        context.set_metadata(_event_metadata(source, "agent_turn", None))
        self._push(_ActiveNode("turn", context, agent_id=agent_id))

    def _implicit_root(self) -> Any | None:
        """Open a Bir trace root for an event arriving with no run on this thread.

        ``start`` normally opens the run before any event fires, so this returns
        ``None``. The fallback mirrors the other agent bridges so an event recorded
        without a started run (defensive only) still attaches to a root instead of
        raising; the point event that triggered it closes it on the same call.
        """

        if self._peek() is not None or _current_trace_id.get() is not None:
            return None
        context = _trace_context(
            name="autogen.run", metadata={"integration": "autogen", "kind": "implicit_root"}
        )
        context.__enter__()
        return context

    # -- per-thread node stack ----------------------------------------------------

    def _push(self, node: _ActiveNode) -> None:
        self._node_stacks.setdefault(get_ident(), []).append(node)

    def _peek(self) -> _ActiveNode | None:
        stack = self._node_stacks.get(get_ident())
        return stack[-1] if stack else None

    def _pop(self) -> _ActiveNode | None:
        thread_id = get_ident()
        stack = self._node_stacks.get(thread_id)
        if not stack:
            return None
        node = stack.pop()
        if not stack:
            self._node_stacks.pop(thread_id, None)
        return node


class _ActiveNode:
    def __init__(self, kind: str, context: Any, *, agent_id: str | None = None) -> None:
        self.kind = kind
        self.context = context
        self.agent_id = agent_id


def _span_context(name: str) -> Any:
    # Imported lazily, mirroring the other bridges, so the ``span`` builder never
    # collides with a ``span`` local elsewhere.
    from bir import span

    return span(name)


def _exit(node: _ActiveNode | None, error: BaseException | None) -> None:
    if node is not None:
        _exit_context(node.context, error)


def _exit_context(context: Any, error: BaseException | None) -> None:
    if context is None:
        return
    if error is None:
        context.__exit__(None, None, None)
    else:
        context.__exit__(type(error), error, None)


def _agent_identity(source: Any) -> tuple[str, str]:
    """Read a stable id and a display name for the agent an event came from.

    AG2 passes ``source`` as either an agent object exposing ``name`` or the agent
    name as a plain string, so both shapes resolve to the same identity and a
    started turn matches its continuation.
    """

    if isinstance(source, str):
        text = source or "autogen.agent"
        return text, text
    name = _string_or_none(_value(source, "name")) if source is not None else None
    if name is not None:
        return name, name
    return "autogen.agent", "autogen.agent"


def _source_name(source: Any) -> str | None:
    if source is None:
        return None
    if isinstance(source, str):
        return source or None
    return _string_or_none(_value(source, "name"))


def _event_metadata(source: Any, event: str, is_cached: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {"integration": "autogen", "autogen_event": event}
    agent = _source_name(source)
    if agent is not None:
        metadata["agent"] = agent
    if isinstance(is_cached, bool) and is_cached:
        metadata["cached"] = True
    elif isinstance(is_cached, int) and not isinstance(is_cached, bool) and is_cached:
        metadata["cached"] = True
    return metadata


def _model_name(request: Any, response: Any) -> str | None:
    model = _string_or_none(_value(response, "model"))
    if model is not None:
        return model
    return _string_or_none(_value(request, "model"))


def _usage(response: Any) -> tuple[Any, Any, Any] | None:
    usage = _value(response, "usage")
    if usage is None:
        return None
    input_tokens = _usage_tokens(usage, "prompt_tokens", "input_tokens")
    output_tokens = _usage_tokens(usage, "completion_tokens", "output_tokens")
    total_tokens = _usage_tokens(usage, "total_tokens")
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    return input_tokens, output_tokens, total_tokens


def _generation_input(request: Any) -> Any:
    if request is None:
        return None
    messages = _value(request, "messages")
    if messages is not None:
        return messages
    return request


def _generation_output(response: Any) -> Any:
    if response is None:
        return None
    return _response_output(response)


def _response_error(response: Any) -> BaseException | None:
    if isinstance(response, BaseException):
        return response
    if isinstance(response, Mapping):
        error = response.get("error")
        if error is not None:
            text = _string_or_none(error) or _string_or_none(str(error))
            return RuntimeError(text or "autogen chat_completion error")
    return None


def _function_name(function: Any) -> str:
    name = _string_or_none(getattr(function, "__name__", None))
    if name is not None:
        return name
    name = _string_or_none(_value(function, "name"))
    if name is not None:
        return name
    text = _string_or_none(function) or (_string_or_none(str(function)) if function is not None else None)
    return text or "autogen.tool"


def _returns_error(returns: Any) -> BaseException | None:
    """Build an exception from a function result AG2 reports as a failure.

    A registered tool that raises is surfaced by AG2 as its return value rather
    than re-raised, so an exception object, an ``error`` / ``exception`` field, or an
    ``is_error`` flag synthesizes one whose message Bir redacts on exit.
    """

    if isinstance(returns, BaseException):
        return returns
    if isinstance(returns, Mapping):
        for key in ("error", "exception"):
            value = returns.get(key)
            if value is not None:
                text = _string_or_none(value) or _string_or_none(str(value))
                return RuntimeError(text or "autogen function error")
        if returns.get("is_error"):
            content = returns.get("content")
            text = _string_or_none(content) or _string_or_none(str(content))
            return RuntimeError(text or "autogen function error")
    return None


def _event_error(name: str | None, kwargs: Mapping[str, Any]) -> BaseException | None:
    lowered = name.lower() if isinstance(name, str) else ""
    for key in ("exception", "error"):
        value = kwargs.get(key)
        if isinstance(value, BaseException):
            return value
        if value is not None:
            text = _string_or_none(value) or _string_or_none(str(value))
            return RuntimeError(text or f"autogen {name or 'event'}")
    if "exception" in lowered or "error" in lowered:
        message = _string_or_none(kwargs.get("message"))
        return RuntimeError(message or f"autogen {name}")
    return None
