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
from bir._sdk import _current_trace_id, _trace_context


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
        self._active_runs: dict[str, _ActiveRun] = {}

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
            context = _trace_context(name=name, metadata=metadata)
            context.__enter__()
            self._active_runs[_run_key(run_id)] = _ActiveRun("trace", context)
            return

        context = _span_context(name)
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
        context.__enter__()
        self._active_runs[_run_key(run_id)] = _ActiveRun("generation", context, implicit_trace=implicit_trace)

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


def _implicit_trace_context(name: str, parent_run_id: Any) -> Any | None:
    if _current_trace_id.get() is not None:
        return None

    metadata = {
        "integration": "langchain",
        "kind": "implicit_root",
    }
    if parent_run_id is not None:
        metadata["parent_run_id"] = _run_key(parent_run_id)

    context = _trace_context(name=name, metadata=metadata)
    context.__enter__()
    return context


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
    llm_output = None
    if isinstance(response, Mapping):
        llm_output = response.get("llm_output")
    else:
        llm_output = getattr(response, "llm_output", None)

    if not isinstance(llm_output, Mapping):
        return None
    token_usage = llm_output.get("token_usage") or llm_output.get("usage")
    if isinstance(token_usage, Mapping):
        return token_usage
    return None


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
