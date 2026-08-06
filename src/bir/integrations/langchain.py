"""LangChain callback integration for recording Bir traces.

The handler intentionally avoids importing LangChain so the Bir SDK stays
dependency-free. LangChain callback managers call methods by name, which lets
applications pass this handler through ``config={"callbacks": [...]}`` when
LangChain is installed in the application environment.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from bir import generation, retrieval, tool_call
from bir._sdk import _set_event_parent, _trace_context

from ._lifecycle import (
    _ActiveRun,
    _enter_framework_root,
    _open_implicit_root,
    _OpenRuns,
    _reclaim_open_root,
)


class BirCallbackHandler:
    """Record LangChain callback events as Bir trace events.

    Chain starts become Bir trace roots when they have no parent run, and nested
    chain starts become spans. LLM/chat model starts become generation events,
    retriever starts become retrieval tool calls, and tool starts become ordinary
    tool call events.
    """

    ignore_agent = False
    ignore_chain = False
    ignore_chat_model = False
    ignore_llm = False
    ignore_retriever = False
    ignore_retry = True
    raise_error = False
    run_inline = True

    def __init__(
        self,
        *,
        capture_inputs: bool | None = None,
        capture_outputs: bool | None = None,
    ) -> None:
        self.capture_inputs = capture_inputs
        self.capture_outputs = capture_outputs
        self._active_runs = _OpenRuns()

    def on_chain_start(
        self,
        serialized: Any,
        inputs: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        del inputs
        name = _callback_name(serialized, kwargs, default="langchain.chain")
        metadata = _metadata("chain", serialized, kwargs, run_id=run_id, parent_run_id=parent_run_id)
        if parent_run_id is None:
            # A chain with no parent is new top-level work, so a root this
            # handler is still holding for an earlier run is reclaimed first.
            _reclaim_open_root()
            context = _trace_context(name=name, metadata=metadata)
            _enter_framework_root(context)
            self._active_runs[_run_key(run_id)] = _ActiveRun("trace", context)
            return

        context = _span_context(name)
        _set_event_parent(context, self._parent_event_id(parent_run_id))
        context.__enter__()
        self._active_runs[_run_key(run_id)] = _ActiveRun("span", context)

    def on_chain_end(self, outputs: Any, *, run_id: Any, **kwargs: Any) -> None:
        del outputs, kwargs
        self._end_run(run_id)

    def on_chain_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        del kwargs
        self._end_run(run_id, error=error)

    def on_llm_start(
        self,
        serialized: Any,
        prompts: list[str],
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        self._start_generation(serialized, {"prompts": prompts}, run_id=run_id, parent_run_id=parent_run_id, **kwargs)

    def on_chat_model_start(
        self,
        serialized: Any,
        messages: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        self._start_generation(serialized, {"messages": messages}, run_id=run_id, parent_run_id=parent_run_id, **kwargs)

    def on_llm_end(self, response: Any, *, run_id: Any, **kwargs: Any) -> None:
        del kwargs
        active_run = self._active_runs.get(_run_key(run_id))
        if active_run is not None and hasattr(active_run.context, "set_output"):
            active_run.context.set_output(_response_payload(response))
            _set_generation_usage(active_run.context, response)
        self._end_run(run_id)

    def on_llm_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        del kwargs
        self._end_run(run_id, error=error)

    def on_tool_start(
        self,
        serialized: Any,
        input_str: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        context = tool_call(
            _callback_name(serialized, kwargs, default="langchain.tool"),
            input=input_str,
            metadata=_metadata("tool", serialized, kwargs, run_id=run_id, parent_run_id=parent_run_id),
            capture_input=self.capture_inputs,
            capture_output=self.capture_outputs,
        )
        implicit_trace = _implicit_trace_context(context.name, parent_run_id)
        _set_event_parent(context, self._parent_event_id(parent_run_id))
        context.__enter__()
        self._active_runs[_run_key(run_id)] = _ActiveRun("tool_call", context, implicit_trace=implicit_trace)

    def on_tool_end(self, output: Any, *, run_id: Any, **kwargs: Any) -> None:
        del kwargs
        active_run = self._active_runs.get(_run_key(run_id))
        if active_run is not None and hasattr(active_run.context, "set_output"):
            active_run.context.set_output(output)
        self._end_run(run_id)

    def on_tool_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        del kwargs
        self._end_run(run_id, error=error)

    def on_retriever_start(
        self,
        serialized: Any,
        query: str,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        context = retrieval(
            _callback_name(serialized, kwargs, default="langchain.retriever"),
            query=query,
            metadata=_metadata("retriever", serialized, kwargs, run_id=run_id, parent_run_id=parent_run_id),
            capture_input=self.capture_inputs,
            capture_output=self.capture_outputs,
        )
        implicit_trace = _implicit_trace_context(context.name, parent_run_id)
        _set_event_parent(context, self._parent_event_id(parent_run_id))
        context.__enter__()
        self._active_runs[_run_key(run_id)] = _ActiveRun("retrieval", context, implicit_trace=implicit_trace)

    def on_retriever_end(self, documents: Any, *, run_id: Any, **kwargs: Any) -> None:
        del kwargs
        active_run = self._active_runs.get(_run_key(run_id))
        if active_run is not None and hasattr(active_run.context, "set_documents"):
            active_run.context.set_documents(_documents_payload(documents))
        self._end_run(run_id)

    def on_retriever_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        del kwargs
        self._end_run(run_id, error=error)

    def _start_generation(
        self,
        serialized: Any,
        input_payload: Any,
        *,
        run_id: Any,
        parent_run_id: Any,
        **kwargs: Any,
    ) -> None:
        context = generation(
            _callback_name(serialized, kwargs, default="langchain.llm"),
            model=_model_name(serialized, kwargs),
            input=input_payload,
            metadata=_metadata("llm", serialized, kwargs, run_id=run_id, parent_run_id=parent_run_id),
            capture_input=self.capture_inputs,
            capture_output=self.capture_outputs,
        )
        implicit_trace = _implicit_trace_context(context.name, parent_run_id)
        _set_event_parent(context, self._parent_event_id(parent_run_id))
        context.__enter__()
        self._active_runs[_run_key(run_id)] = _ActiveRun("generation", context, implicit_trace=implicit_trace)

    def _parent_event_id(self, parent_run_id: Any) -> str | None:
        """Return the Bir event id recorded for ``parent_run_id``, if it is open.

        LangChain names each run's parent, so the tree it reports is authoritative
        even when runs overlap and the open-context stack no longer matches it. A
        parent this handler never started, or one that already ended, maps to
        ``None`` and leaves the surrounding context as the parent.
        """

        if parent_run_id is None:
            return None
        active_run = self._active_runs.get(_run_key(parent_run_id))
        if active_run is None:
            return None
        return getattr(active_run.context, "id", None)

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


def _implicit_trace_context(name: str, parent_run_id: Any) -> Any | None:
    metadata = {
        "integration": "langchain",
        "kind": "implicit_root",
    }
    if parent_run_id is not None:
        metadata["parent_run_id"] = _run_key(parent_run_id)

    return _open_implicit_root(name=name, metadata=metadata)


def _span_context(name: str) -> Any:
    from bir import span

    return span(name)


def _run_key(run_id: Any) -> str:
    return str(run_id)


def _callback_name(serialized: Any, kwargs: Mapping[str, Any], *, default: str) -> str:
    name = kwargs.get("name") or kwargs.get("run_name")
    if isinstance(name, str) and name:
        return name

    if isinstance(serialized, Mapping):
        serialized_name = serialized.get("name")
        if isinstance(serialized_name, str) and serialized_name:
            return serialized_name

        identifier = serialized.get("id")
        if isinstance(identifier, str) and identifier:
            return identifier
        if isinstance(identifier, list) and identifier:
            last_identifier = identifier[-1]
            if isinstance(last_identifier, str) and last_identifier:
                return last_identifier

    return default


def _model_name(serialized: Any, kwargs: Mapping[str, Any]) -> str | None:
    invocation_params = kwargs.get("invocation_params")
    for source in (invocation_params, _serialized_kwargs(serialized), serialized):
        if not isinstance(source, Mapping):
            continue
        for key in ("model", "model_name", "model_id"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _metadata(
    kind: str,
    serialized: Any,
    kwargs: Mapping[str, Any],
    *,
    run_id: Any,
    parent_run_id: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "integration": "langchain",
        "kind": kind,
        "langchain_kind": kind,
        "run_id": _run_key(run_id),
    }
    if parent_run_id is not None:
        payload["parent_run_id"] = _run_key(parent_run_id)

    for key in ("tags", "metadata"):
        value = kwargs.get(key)
        if value is not None:
            payload[key] = value

    if isinstance(serialized, Mapping):
        identifier = serialized.get("id")
        if identifier is not None:
            payload["serialized_id"] = identifier
    return payload


def _serialized_kwargs(serialized: Any) -> Mapping[str, Any] | None:
    if not isinstance(serialized, Mapping):
        return None
    kwargs = serialized.get("kwargs")
    if isinstance(kwargs, Mapping):
        return kwargs
    return None


def _response_payload(response: Any) -> Any:
    if isinstance(response, Mapping):
        return dict(response)

    model_dump = getattr(response, "model_dump", None)
    if callable(model_dump):
        return model_dump()

    as_dict = getattr(response, "dict", None)
    if callable(as_dict):
        return as_dict()

    return response


def _set_generation_usage(context: Any, response: Any) -> None:
    token_usage = _token_usage(response)
    if token_usage is None:
        return

    input_tokens = _numeric_token(token_usage, "input_tokens", "prompt_tokens")
    output_tokens = _numeric_token(token_usage, "output_tokens", "completion_tokens")
    total_tokens = _numeric_token(token_usage, "total_tokens")
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return
    context.set_usage(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)


def _token_usage(response: Any) -> Mapping[str, Any] | None:
    for source in _token_usage_sources(response):
        token_usage = _token_usage_from_source(source)
        if token_usage is not None:
            return token_usage
    return None


def _token_usage_sources(response: Any) -> list[Any]:
    sources = [response]
    llm_output = _mapping_value(response, "llm_output")
    if llm_output is not None:
        sources.append(llm_output)

    generations = _mapping_value(response, "generations")
    if isinstance(generations, list):
        for generation_group in generations:
            group_items = generation_group if isinstance(generation_group, list) else [generation_group]
            for generation_item in group_items:
                sources.append(generation_item)
                message = _mapping_value(generation_item, "message")
                if message is not None:
                    sources.append(message)
    return sources


def _token_usage_from_source(source: Any) -> Mapping[str, Any] | None:
    for key in ("token_usage", "usage", "usage_metadata"):
        value = _mapping_value(source, key)
        if isinstance(value, Mapping):
            return value

    response_metadata = _mapping_value(source, "response_metadata")
    if isinstance(response_metadata, Mapping):
        for key in ("token_usage", "usage", "usage_metadata"):
            value = response_metadata.get(key)
            if isinstance(value, Mapping):
                return value
        if any(_numeric_token(response_metadata, key) is not None for key in _TOKEN_USAGE_KEYS):
            return response_metadata
    return None


_TOKEN_USAGE_KEYS = (
    "input_tokens",
    "prompt_tokens",
    "output_tokens",
    "completion_tokens",
    "total_tokens",
)


def _mapping_value(source: Any, key: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(key)
    return getattr(source, key, None)


def _numeric_token(usage: Mapping[str, Any], *keys: str) -> int | float | None:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return value
    return None


def _documents_payload(documents: Any) -> list[dict[str, Any]]:
    if not isinstance(documents, list):
        return []

    payload: list[dict[str, Any]] = []
    for index, document in enumerate(documents, start=1):
        if isinstance(document, Mapping):
            normalized = dict(document)
        else:
            normalized = {}
            page_content = getattr(document, "page_content", None)
            if isinstance(page_content, str):
                normalized["text"] = page_content
            metadata = getattr(document, "metadata", None)
            if isinstance(metadata, Mapping):
                normalized["metadata"] = dict(metadata)
            document_id = getattr(document, "id", None)
            if isinstance(document_id, str):
                normalized["id"] = document_id
        normalized.setdefault("rank", index)
        payload.append(normalized)
    return payload
