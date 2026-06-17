"""LlamaIndex callback integration for recording Bir traces.

The handler intentionally avoids importing LlamaIndex so the Bir SDK stays
dependency-free. LlamaIndex callback managers call methods by name, which lets
applications pass this handler when LlamaIndex is installed in the application
environment.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from bir import generation, retrieval
from bir._sdk import _current_trace_id, _trace_context
from bir.integrations._common import _response_output, _usage_tokens


class BirLlamaIndexHandler:
    """Record LlamaIndex LLM/chat and retrieval callback events as Bir events."""

    def __init__(
        self,
        *,
        capture_inputs: bool | None = None,
        capture_outputs: bool | None = None,
    ) -> None:
        self.capture_inputs = capture_inputs
        self.capture_outputs = capture_outputs
        self._active_runs: dict[str, _ActiveRun] = {}

    def start_trace(self, trace_id: Any = None) -> None:
        if _current_trace_id.get() is not None:
            return

        context = _trace_context(
            name=_trace_name(trace_id),
            metadata=_trace_metadata(trace_id),
        )
        context.__enter__()
        self._active_runs[_trace_run_key(trace_id)] = _ActiveRun("trace", context)

    def end_trace(self, trace_id: Any = None, trace_map: Any = None) -> None:
        del trace_map
        self._end_run(_trace_run_key(trace_id))

    def on_event_start(
        self,
        event_type: Any,
        payload: Mapping[Any, Any] | None = None,
        event_id: Any = "",
        parent_id: Any = "",
        **kwargs: Any,
    ) -> str:
        del kwargs
        kind = _event_kind(event_type)
        run_id = _event_id(event_id)
        if kind is None:
            return run_id

        event_payload = payload or {}
        if kind in {"llm", "chat"}:
            context = generation(
                _event_name(kind),
                model=_model_name(event_payload),
                input=_generation_input(event_payload),
                metadata=_metadata(kind, run_id, parent_id),
                capture_input=self.capture_inputs,
                capture_output=self.capture_outputs,
            )
            implicit_trace = _implicit_trace_context(context.name, parent_id)
            context.__enter__()
            self._active_runs[run_id] = _ActiveRun("generation", context, implicit_trace=implicit_trace)
            return run_id

        context = retrieval(
            "llamaindex.retrieve",
            query=_query_from_payload(event_payload),
            metadata=_metadata("retrieve", run_id, parent_id),
            capture_input=self.capture_inputs,
            capture_output=self.capture_outputs,
        )
        implicit_trace = _implicit_trace_context(context.name, parent_id)
        context.__enter__()
        self._active_runs[run_id] = _ActiveRun("retrieval", context, implicit_trace=implicit_trace)
        return run_id

    def on_event_end(
        self,
        event_type: Any,
        payload: Mapping[Any, Any] | None = None,
        event_id: Any = "",
        **kwargs: Any,
    ) -> None:
        del event_type
        active_run = self._active_runs.get(_run_key(event_id))
        if active_run is None:
            return

        event_payload = payload or {}
        if active_run.kind == "generation" and hasattr(active_run.context, "set_output"):
            response = _generation_response(event_payload)
            active_run.context.set_output(_generation_output(response))
            _set_generation_usage(active_run.context, response)
        elif active_run.kind == "retrieval" and hasattr(active_run.context, "set_documents"):
            active_run.context.set_documents(_nodes_payload(_payload_value(event_payload, "nodes")))

        error = kwargs.get("error") or kwargs.get("exception")
        self._end_run(_run_key(event_id), error=error if isinstance(error, BaseException) else None)

    def _end_run(self, run_id: Any, *, error: BaseException | None = None) -> None:
        active_run = self._active_runs.pop(_run_key(run_id), None)
        if active_run is None:
            return

        if error is None:
            active_run.context.__exit__(None, None, None)
            if active_run.implicit_trace is not None:
                active_run.implicit_trace.__exit__(None, None, None)
            return
        active_run.context.__exit__(type(error), error, None)
        if active_run.implicit_trace is not None:
            active_run.implicit_trace.__exit__(type(error), error, None)


class _ActiveRun:
    def __init__(self, kind: str, context: Any, *, implicit_trace: Any | None = None) -> None:
        self.kind = kind
        self.context = context
        self.implicit_trace = implicit_trace


def _implicit_trace_context(name: str, parent_id: Any) -> Any | None:
    if _current_trace_id.get() is not None:
        return None

    metadata = {
        "integration": "llamaindex",
        "kind": "implicit_root",
    }
    if parent_id not in (None, ""):
        metadata["parent_id"] = _run_key(parent_id)

    context = _trace_context(name=name, metadata=metadata)
    context.__enter__()
    return context


def _trace_name(trace_id: Any) -> str:
    if trace_id not in (None, ""):
        return str(trace_id)
    return "llamaindex.trace"


def _trace_metadata(trace_id: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {"integration": "llamaindex", "kind": "trace"}
    if trace_id not in (None, ""):
        metadata["llamaindex_trace_id"] = str(trace_id)
    return metadata


def _trace_run_key(trace_id: Any) -> str:
    if trace_id in (None, ""):
        return "__llamaindex_trace__"
    return f"trace:{trace_id}"


def _event_kind(event_type: Any) -> str | None:
    for name in _string_names(event_type):
        if name in {"llm", "chat", "retrieve"}:
            return name
    return None


def _event_name(kind: str) -> str:
    if kind == "chat":
        return "llamaindex.chat"
    return "llamaindex.llm"


def _event_id(event_id: Any) -> str:
    if event_id in (None, ""):
        return str(uuid4())
    return _run_key(event_id)


def _run_key(value: Any) -> str:
    return str(value)


def _metadata(kind: str, event_id: str, parent_id: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "integration": "llamaindex",
        "kind": kind,
        "llamaindex_kind": kind,
        "event_id": event_id,
    }
    if parent_id not in (None, ""):
        metadata["parent_id"] = _run_key(parent_id)
    return metadata


def _generation_input(payload: Mapping[Any, Any]) -> dict[str, Any] | None:
    input_payload: dict[str, Any] = {}
    messages = _payload_value(payload, "messages")
    prompt = _payload_value(payload, "prompt")
    if messages is not None:
        input_payload["messages"] = messages
    if prompt is not None:
        input_payload["prompt"] = prompt
    return input_payload or None


def _model_name(payload: Mapping[Any, Any]) -> str | None:
    for key in ("model", "model_name", "model_id"):
        value = _payload_value(payload, key)
        if isinstance(value, str) and value:
            return value
    return None


def _query_from_payload(payload: Mapping[Any, Any]) -> Any:
    for key in ("query", "query_str", "prompt"):
        value = _payload_value(payload, key)
        if value is not None:
            return value
    return None


def _generation_response(payload: Mapping[Any, Any]) -> Any:
    for key in ("response", "completion"):
        value = _payload_value(payload, key)
        if value is not None:
            return value
    return None


def _generation_output(response: Any) -> Any:
    text = _response_text(response)
    if text is not None:
        return {"text": text}
    return _response_output(response)


def _response_text(response: Any) -> Any:
    if response is None:
        return None
    if isinstance(response, str):
        return response
    for key in ("text", "response", "completion", "content"):
        value = _mapping_value(response, key)
        if value is not None:
            return value
    message = _mapping_value(response, "message")
    if message is not None and message is not response:
        return _response_text(message)
    for method_name in ("get_content", "get_text"):
        method = getattr(response, method_name, None)
        if callable(method):
            try:
                return method()
            except TypeError:
                continue
    return None


def _set_generation_usage(context: Any, response: Any) -> None:
    usage = _token_usage(response)
    if usage is None:
        return

    input_tokens = _usage_tokens(usage, "input_tokens", "prompt_tokens")
    output_tokens = _usage_tokens(usage, "output_tokens", "completion_tokens")
    total_tokens = _usage_tokens(usage, "total_tokens")
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return
    context.set_usage(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)


def _token_usage(response: Any) -> Any:
    raw = _mapping_value(response, "raw")
    for source in (raw, _mapping_value(raw, "usage"), _mapping_value(response, "usage")):
        if source is not None and _has_token_usage(source):
            return source
    return None


def _has_token_usage(source: Any) -> bool:
    return any(
        _usage_tokens(source, key) is not None
        for key in ("input_tokens", "prompt_tokens", "output_tokens", "completion_tokens", "total_tokens")
    )


def _nodes_payload(nodes: Any) -> list[dict[str, Any]]:
    if nodes is None:
        return []
    if not isinstance(nodes, list):
        nodes = [nodes]

    documents: list[dict[str, Any]] = []
    for node_with_score in nodes:
        node = _mapping_value(node_with_score, "node") or node_with_score
        document: dict[str, Any] = {}

        document_id = _node_id(node)
        if document_id is not None:
            document["id"] = document_id

        text = _node_text(node)
        if text is not None:
            document["text"] = text

        score = _mapping_value(node_with_score, "score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            document["score"] = score

        documents.append(document)
    return documents


def _node_id(node: Any) -> str | None:
    for key in ("id", "id_", "node_id", "ref_doc_id"):
        value = _mapping_value(node, key)
        if isinstance(value, str) and value:
            return value
    return None


def _node_text(node: Any) -> Any:
    for method_name in ("get_content", "get_text"):
        method = getattr(node, method_name, None)
        if callable(method):
            try:
                return method()
            except TypeError:
                continue
    for key in ("text", "content"):
        value = _mapping_value(node, key)
        if value is not None:
            return value
    return None


def _payload_value(payload: Mapping[Any, Any], key: str) -> Any:
    for payload_key, value in payload.items():
        if _matches_key(payload_key, key):
            return value
    return None


def _mapping_value(source: Any, key: str) -> Any:
    if isinstance(source, Mapping):
        return _payload_value(source, key)
    return getattr(source, key, None)


def _matches_key(value: Any, key: str) -> bool:
    normalized_key = key.lower()
    return any(name == normalized_key for name in _string_names(value))


def _string_names(value: Any) -> list[str]:
    candidates = [value, getattr(value, "value", None), getattr(value, "name", None)]
    names: list[str] = []
    for candidate in candidates:
        if candidate is None:
            continue
        text = str(candidate).lower()
        names.append(text)
        if "." in text:
            names.append(text.rsplit(".", 1)[-1])
    return names
