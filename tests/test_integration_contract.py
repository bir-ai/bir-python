"""Declared conformance capabilities for every Bir call-wrapper integration.

Each ``trace_*`` sync/async pair declares what it supports as a
:class:`~integration_contract.WrapperContract`, and the shared matrix in
``integration_contract.py`` turns that declaration into the lifecycle cases the
wrapper must pass. The provider shapes below are intentionally minimal: they
carry only what the shared cases read (model, token usage, incremental output
text), because everything provider-specific — alternate usage keys, terminal
events, nested candidates, tuple results — stays in the per-provider test module
beside the integration.

:class:`IntegrationRegistryTests` closes the loop by refusing to let a new
integration module land undeclared, so adding one means passing this matrix (or
recording, with its provider imports, why it is not a call wrapper).
"""

from __future__ import annotations

import importlib
import pkgutil
import unittest
from collections.abc import Callable, Mapping
from typing import Any

from integration_bridge_contract import ROOT_KEY, BridgeContract, RunDriver, build_bridge_test_case
from integration_contract import (
    ResponseShape,
    StreamCapability,
    UnaryCapability,
    WrapperContract,
    build_contract_test_case,
)
from test_architecture import OPTIONAL_PROVIDER_ROOTS

from bir.integrations import (
    anthropic,
    bedrock,
    cohere,
    dspy,
    google,
    instructor,
    langchain,
    litellm,
    llamaindex,
    mistral,
    ollama,
    openai,
    openai_agents,
    pydantic_ai,
    vertexai,
)

# Every declared response reports the same token counts, so the recorded usage
# is identical across providers even though each spells the counts differently.
RECORDED_USAGE: dict[str, int | float] = {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15}

MESSAGES = [{"role": "user", "content": "hello"}]


def openai_shaped_response(model: str) -> Callable[[], dict[str, Any]]:
    """Build a response using the OpenAI chat token names most providers copy."""

    def build() -> dict[str, Any]:
        return {
            "model": model,
            "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15},
        }

    return build


def openai_shaped_chunk(text: str) -> dict[str, Any]:
    """Build a chunk carrying output text at ``choices[0].delta.content``."""

    return {"choices": [{"delta": {"content": text}}]}


ANTHROPIC_MESSAGES = WrapperContract(
    id="anthropic.messages",
    module="anthropic",
    integration="anthropic",
    default_name="anthropic.messages",
    provider_roots=("anthropic",),
    request={"model": "claude-haiku-4-5", "messages": MESSAGES},
    metadata={"integration": "anthropic"},
    unary=UnaryCapability(
        sync_wrapper=anthropic.trace_messages,
        async_wrapper=anthropic.trace_messages_async,
        response=ResponseShape(
            build=lambda: {
                "model": "claude-haiku-4-5-20251001",
                "usage": {"input_tokens": 11, "output_tokens": 4},
            },
            model="claude-haiku-4-5-20251001",
            usage=RECORDED_USAGE,
        ),
    ),
    streaming=StreamCapability(
        sync_wrapper=anthropic.trace_messages,
        async_wrapper=anthropic.trace_messages_async,
        enable={"stream": True},
        text_chunk=lambda text: {"type": "content_block_delta", "delta": {"text": text}},
        # Text deltas carry no model; only ``message_start`` does, so the
        # request model stands for a stream of deltas alone.
        model="claude-haiku-4-5",
        whole_response=ResponseShape(
            build=lambda: {
                "model": "claude-haiku-4-5-20251001",
                "usage": {"input_tokens": 11, "output_tokens": 4},
            },
            model="claude-haiku-4-5-20251001",
            usage=RECORDED_USAGE,
        ),
    ),
)

BEDROCK_MODEL_ID = "amazon.nova-lite-v1:0"

BEDROCK_CONVERSE_RESPONSE = ResponseShape(
    build=lambda: {"usage": {"inputTokens": 11, "outputTokens": 4, "totalTokens": 15}},
    # Converse responses carry no model, so the request ``modelId`` stands.
    model=BEDROCK_MODEL_ID,
    usage=RECORDED_USAGE,
)

BEDROCK_CONVERSE = WrapperContract(
    id="bedrock.converse",
    module="bedrock",
    integration="bedrock",
    default_name="bedrock.converse",
    provider_roots=("boto3", "botocore"),
    request={"modelId": BEDROCK_MODEL_ID, "messages": MESSAGES},
    metadata={"integration": "bedrock"},
    unary=UnaryCapability(
        sync_wrapper=bedrock.trace_converse,
        async_wrapper=bedrock.trace_converse_async,
        response=BEDROCK_CONVERSE_RESPONSE,
    ),
)

BEDROCK_CONVERSE_STREAM = WrapperContract(
    id="bedrock.converse_stream",
    module="bedrock",
    integration="bedrock",
    default_name="bedrock.converse_stream",
    provider_roots=("boto3", "botocore"),
    request={"modelId": BEDROCK_MODEL_ID, "messages": MESSAGES},
    metadata={"integration": "bedrock"},
    # Converse streaming is a separate API call, so it has dedicated wrappers
    # instead of a request keyword that switches an existing one.
    streaming=StreamCapability(
        sync_wrapper=bedrock.trace_converse_stream,
        async_wrapper=bedrock.trace_converse_stream_async,
        text_chunk=lambda text: {"contentBlockDelta": {"delta": {"text": text}}},
        model=BEDROCK_MODEL_ID,
        whole_response=BEDROCK_CONVERSE_RESPONSE,
        # ``converse_stream`` returns a Converse response whose ``stream``
        # member holds the event stream, not the event stream itself.
        envelope=lambda stream: {"stream": stream},
    ),
)

COHERE_RESPONSE = ResponseShape(
    build=lambda: {"usage": {"tokens": {"input_tokens": 11, "output_tokens": 4}}},
    # Cohere responses carry no model, so the request model stands.
    model="command-r-plus",
    usage=RECORDED_USAGE,
)

COHERE_CHAT = WrapperContract(
    id="cohere.chat",
    module="cohere",
    integration="cohere",
    default_name="cohere.chat",
    provider_roots=("cohere",),
    request={"model": "command-r-plus", "messages": MESSAGES},
    metadata={"integration": "cohere"},
    unary=UnaryCapability(
        sync_wrapper=cohere.trace_chat,
        async_wrapper=cohere.trace_chat_async,
        response=COHERE_RESPONSE,
    ),
    streaming=StreamCapability(
        sync_wrapper=cohere.trace_chat,
        async_wrapper=cohere.trace_chat_async,
        enable={"stream": True},
        text_chunk=lambda text: {
            "type": "content-delta",
            "delta": {"message": {"content": {"text": text}}},
        },
        model="command-r-plus",
        whole_response=COHERE_RESPONSE,
    ),
)

DSPY_LM = WrapperContract(
    id="dspy.lm",
    module="dspy",
    integration="dspy",
    default_name="dspy.lm",
    provider_roots=("dspy",),
    request={"model": "gpt-4o-mini", "prompt": "hello"},
    metadata={"integration": "dspy"},
    unary=UnaryCapability(
        sync_wrapper=dspy.trace_lm,
        async_wrapper=dspy.trace_lm_async,
        response=ResponseShape(
            build=openai_shaped_response("gpt-4o-mini-2024-07-18"),
            model="gpt-4o-mini-2024-07-18",
            usage=RECORDED_USAGE,
        ),
    ),
)

GOOGLE_RESPONSE = ResponseShape(
    build=lambda: {"usage_metadata": {"prompt_token_count": 11, "candidates_token_count": 4, "total_token_count": 15}},
    # Gemini responses carry no top-level model, so the request model stands.
    model="gemini-2.0-flash",
    usage=RECORDED_USAGE,
)

GOOGLE_GENERATE_CONTENT = WrapperContract(
    id="google.generate_content",
    module="google",
    integration="google",
    default_name="google.generate_content",
    provider_roots=("google",),
    request={"model": "gemini-2.0-flash", "contents": "hello"},
    metadata={"integration": "google"},
    unary=UnaryCapability(
        sync_wrapper=google.trace_generate_content,
        async_wrapper=google.trace_generate_content_async,
        response=GOOGLE_RESPONSE,
    ),
    streaming=StreamCapability(
        sync_wrapper=google.trace_generate_content,
        async_wrapper=google.trace_generate_content_async,
        enable={"stream": True},
        text_chunk=lambda text: {"text": text},
        model="gemini-2.0-flash",
        whole_response=GOOGLE_RESPONSE,
    ),
)

INSTRUCTOR_CREATE = WrapperContract(
    id="instructor.create",
    module="instructor",
    integration="instructor",
    default_name="instructor.create",
    provider_roots=("instructor", "openai"),
    request={"model": "gpt-4o-mini", "messages": MESSAGES},
    metadata={"integration": "instructor"},
    unary=UnaryCapability(
        sync_wrapper=instructor.trace_create,
        async_wrapper=instructor.trace_create_async,
        response=ResponseShape(
            build=openai_shaped_response("gpt-4o-mini-2024-07-18"),
            model="gpt-4o-mini-2024-07-18",
            usage=RECORDED_USAGE,
        ),
    ),
)

LITELLM_COMPLETION = WrapperContract(
    id="litellm.completion",
    module="litellm",
    integration="litellm",
    default_name="litellm.completion",
    provider_roots=("litellm",),
    # A bare model id carries no provider prefix, so the recorded metadata stays
    # the integration tag; the prefix-derived ``provider`` key is covered by the
    # LiteLLM-specific tests.
    request={"model": "gpt-4o-mini", "messages": MESSAGES},
    metadata={"integration": "litellm"},
    unary=UnaryCapability(
        sync_wrapper=litellm.trace_completion,
        async_wrapper=litellm.trace_completion_async,
        response=ResponseShape(
            build=openai_shaped_response("gpt-4o-mini-2024-07-18"),
            model="gpt-4o-mini-2024-07-18",
            usage=RECORDED_USAGE,
        ),
    ),
    streaming=StreamCapability(
        sync_wrapper=litellm.trace_completion,
        async_wrapper=litellm.trace_completion_async,
        enable={"stream": True},
        text_chunk=openai_shaped_chunk,
        model="gpt-4o-mini",
        whole_response=ResponseShape(
            build=openai_shaped_response("gpt-4o-mini-2024-07-18"),
            model="gpt-4o-mini-2024-07-18",
            usage=RECORDED_USAGE,
        ),
    ),
)

MISTRAL_CHAT = WrapperContract(
    id="mistral.chat",
    module="mistral",
    integration="mistral",
    default_name="mistral.chat",
    provider_roots=("mistral", "mistralai"),
    request={"model": "mistral-small-latest", "messages": MESSAGES},
    metadata={"integration": "mistral"},
    unary=UnaryCapability(
        sync_wrapper=mistral.trace_chat,
        async_wrapper=mistral.trace_chat_async,
        response=ResponseShape(
            build=openai_shaped_response("mistral-small-2506"),
            model="mistral-small-2506",
            usage=RECORDED_USAGE,
        ),
    ),
    streaming=StreamCapability(
        sync_wrapper=mistral.trace_chat,
        async_wrapper=mistral.trace_chat_async,
        enable={"stream": True},
        text_chunk=openai_shaped_chunk,
        model="mistral-small-latest",
        whole_response=ResponseShape(
            build=openai_shaped_response("mistral-small-2506"),
            model="mistral-small-2506",
            usage=RECORDED_USAGE,
        ),
    ),
)

OLLAMA_CHAT_RESPONSE = ResponseShape(
    build=lambda: {
        "model": "llama3.2",
        "message": {"role": "assistant", "content": "hi"},
        "prompt_eval_count": 11,
        "eval_count": 4,
    },
    model="llama3.2",
    usage=RECORDED_USAGE,
)

OLLAMA_CHAT = WrapperContract(
    id="ollama.chat",
    module="ollama",
    integration="ollama",
    default_name="ollama.chat",
    provider_roots=("ollama",),
    request={"model": "llama3.2", "messages": MESSAGES},
    metadata={"integration": "ollama"},
    unary=UnaryCapability(
        sync_wrapper=ollama.trace_chat,
        async_wrapper=ollama.trace_chat_async,
        response=OLLAMA_CHAT_RESPONSE,
    ),
    streaming=StreamCapability(
        sync_wrapper=ollama.trace_chat,
        async_wrapper=ollama.trace_chat_async,
        enable={"stream": True},
        text_chunk=lambda text: {"message": {"content": text}},
        model="llama3.2",
        whole_response=OLLAMA_CHAT_RESPONSE,
    ),
)

OLLAMA_GENERATE_RESPONSE = ResponseShape(
    build=lambda: {"model": "llama3.2", "response": "hi", "prompt_eval_count": 11, "eval_count": 4},
    model="llama3.2",
    usage=RECORDED_USAGE,
)

OLLAMA_GENERATE = WrapperContract(
    id="ollama.generate",
    module="ollama",
    integration="ollama",
    default_name="ollama.generate",
    provider_roots=("ollama",),
    request={"model": "llama3.2", "prompt": "hello"},
    metadata={"integration": "ollama"},
    unary=UnaryCapability(
        sync_wrapper=ollama.trace_generate,
        async_wrapper=ollama.trace_generate_async,
        response=OLLAMA_GENERATE_RESPONSE,
    ),
    streaming=StreamCapability(
        sync_wrapper=ollama.trace_generate,
        async_wrapper=ollama.trace_generate_async,
        enable={"stream": True},
        text_chunk=lambda text: {"response": text},
        model="llama3.2",
        whole_response=OLLAMA_GENERATE_RESPONSE,
    ),
)

OPENAI_CHAT_COMPLETIONS = WrapperContract(
    id="openai.chat_completions",
    module="openai",
    integration="openai",
    default_name="openai.chat.completions",
    provider_roots=("openai",),
    request={"model": "gpt-4o-mini", "messages": MESSAGES},
    metadata={"integration": "openai"},
    unary=UnaryCapability(
        sync_wrapper=openai.trace_chat_completion,
        async_wrapper=openai.trace_chat_completion_async,
        response=ResponseShape(
            build=openai_shaped_response("gpt-4o-mini-2024-07-18"),
            model="gpt-4o-mini-2024-07-18",
            usage=RECORDED_USAGE,
        ),
    ),
    streaming=StreamCapability(
        sync_wrapper=openai.trace_chat_completion,
        async_wrapper=openai.trace_chat_completion_async,
        enable={"stream": True},
        text_chunk=openai_shaped_chunk,
        model="gpt-4o-mini",
        whole_response=ResponseShape(
            build=openai_shaped_response("gpt-4o-mini-2024-07-18"),
            model="gpt-4o-mini-2024-07-18",
            usage=RECORDED_USAGE,
        ),
    ),
)

OPENAI_RESPONSES_RESPONSE = ResponseShape(
    build=lambda: {
        "model": "gpt-4o-mini-2024-07-18",
        "output_text": "hi",
        "usage": {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15},
    },
    model="gpt-4o-mini-2024-07-18",
    usage=RECORDED_USAGE,
)

OPENAI_RESPONSES = WrapperContract(
    id="openai.responses",
    module="openai",
    integration="openai",
    default_name="openai.responses",
    provider_roots=("openai",),
    request={"model": "gpt-4o-mini", "input": "hello"},
    metadata={"integration": "openai"},
    unary=UnaryCapability(
        sync_wrapper=openai.trace_response,
        async_wrapper=openai.trace_response_async,
        response=OPENAI_RESPONSES_RESPONSE,
    ),
    streaming=StreamCapability(
        sync_wrapper=openai.trace_response,
        async_wrapper=openai.trace_response_async,
        enable={"stream": True},
        text_chunk=lambda text: {"type": "response.output_text.delta", "delta": text},
        # Only the lifecycle events wrapping a response carry the model, so a
        # stream of text deltas alone leaves the request model in place.
        model="gpt-4o-mini",
        whole_response=OPENAI_RESPONSES_RESPONSE,
    ),
)

VERTEXAI_RESPONSE = ResponseShape(
    build=lambda: {
        "model_version": "gemini-2.0-flash-001",
        "usage_metadata": {"prompt_token_count": 11, "candidates_token_count": 4, "total_token_count": 15},
    },
    model="gemini-2.0-flash-001",
    usage=RECORDED_USAGE,
)

VERTEXAI_GENERATE_CONTENT = WrapperContract(
    id="vertexai.generate_content",
    module="vertexai",
    integration="vertexai",
    default_name="vertexai.generate_content",
    provider_roots=("google", "vertexai"),
    # Vertex binds the model to the client, so the request carries none and the
    # caller names it with the wrapper's own ``bir_model`` option.
    request={"contents": "hello"},
    metadata={"integration": "vertexai"},
    extra_options=("bir_model",),
    unary=UnaryCapability(
        sync_wrapper=vertexai.trace_generate_content,
        async_wrapper=vertexai.trace_generate_content_async,
        response=VERTEXAI_RESPONSE,
    ),
    streaming=StreamCapability(
        sync_wrapper=vertexai.trace_generate_content,
        async_wrapper=vertexai.trace_generate_content_async,
        enable={"stream": True},
        text_chunk=lambda text: {"text": text},
        # Without ``bir_model`` and without a chunk ``model_version``, a streamed
        # call records no model rather than inventing one.
        model=None,
        whole_response=VERTEXAI_RESPONSE,
    ),
)

CONTRACTS: tuple[WrapperContract, ...] = (
    ANTHROPIC_MESSAGES,
    BEDROCK_CONVERSE,
    BEDROCK_CONVERSE_STREAM,
    COHERE_CHAT,
    DSPY_LM,
    GOOGLE_GENERATE_CONTENT,
    INSTRUCTOR_CREATE,
    LITELLM_COMPLETION,
    MISTRAL_CHAT,
    OLLAMA_CHAT,
    OLLAMA_GENERATE,
    OPENAI_CHAT_COMPLETIONS,
    OPENAI_RESPONSES,
    VERTEXAI_GENERATE_CONTENT,
)

# Integrations that neither wrap one provider call nor bridge framework events:
# handlers driven by a shape the bridge matrix does not yet cover (an event bus,
# a logger protocol, a context-manager tracer) and the OTLP exporter, which
# reads finished traces instead of recording them. Their provider import roots
# are declared here so the fresh-import guard still covers them.
UNDECLARED_PROVIDER_ROOTS: Mapping[str, tuple[str, ...]] = {
    "autogen": ("ag2", "autogen"),
    "crewai": ("crewai",),
    "haystack": ("haystack",),
    "otel": ("opentelemetry",),
}


def langchain_serialized(name: str | None = None) -> dict[str, Any]:
    """Build the ``serialized`` mapping LangChain passes to every callback."""

    return {"name": name} if name is not None else {}


LANGCHAIN = BridgeContract(
    id="langchain.callbacks",
    module="langchain",
    integration="langchain",
    provider_roots=("langchain", "langchain_core"),
    handler=langchain.BirCallbackHandler,
    # A chain start with no parent run is LangChain's own root.
    root=RunDriver(
        start=lambda handler, key, parent: handler.on_chain_start(
            langchain_serialized(),
            {"question": "hello"},
            run_id=key,
            parent_run_id=parent,
        ),
        end=lambda handler, key: handler.on_chain_end({"answer": "hi"}, run_id=key),
        fail=lambda handler, key, error: handler.on_chain_error(error, run_id=key),
    ),
    root_name="langchain.chain",
    generation=RunDriver(
        start=lambda handler, key, parent: handler.on_llm_start(
            langchain_serialized(),
            ["hello"],
            run_id=key,
            parent_run_id=parent,
            invocation_params={"model": "gpt-4o-mini"},
        ),
        end=lambda handler, key: handler.on_llm_end(
            {"llm_output": {"token_usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}}},
            run_id=key,
        ),
        fail=lambda handler, key, error: handler.on_llm_error(error, run_id=key),
    ),
    generation_name="langchain.llm",
    # LangChain's implicit root borrows the name of the event that needed it.
    implicit_root_name="langchain.llm",
    model="gpt-4o-mini",
    usage=RECORDED_USAGE,
)

LLAMAINDEX_RESPONSE = {
    "response": {
        "text": "hi",
        "raw": {"usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}},
    }
}

LLAMAINDEX = BridgeContract(
    id="llamaindex.callbacks",
    module="llamaindex",
    integration="llamaindex",
    provider_roots=("llama_index", "llamaindex"),
    handler=llamaindex.BirLlamaIndexHandler,
    root=RunDriver(
        start=lambda handler, key, parent: handler.start_trace(key),
        end=lambda handler, key: handler.end_trace(key),
        # LlamaIndex has no failing trace callback; a failed run ends normally.
        fail=lambda handler, key, error: handler.end_trace(key),
    ),
    # LlamaIndex names its trace root after the trace id it was given.
    root_name=ROOT_KEY,
    generation=RunDriver(
        start=lambda handler, key, parent: handler.on_event_start(
            "llm",
            {"messages": [{"role": "user", "content": "hello"}], "model": "gpt-4o-mini"},
            event_id=key,
            parent_id=parent or "",
        ),
        end=lambda handler, key: handler.on_event_end("llm", LLAMAINDEX_RESPONSE, event_id=key),
        fail=lambda handler, key, error: handler.on_event_end("llm", LLAMAINDEX_RESPONSE, event_id=key, error=error),
    ),
    generation_name="llamaindex.llm",
    implicit_root_name="llamaindex.llm",
    model="gpt-4o-mini",
    usage=RECORDED_USAGE,
)


def agents_trace(key: str) -> dict[str, Any]:
    """Build the Agents SDK trace object handed to the trace callbacks."""

    return {"trace_id": key}


def agents_span(key: str, parent: str | None = None, *, error: BaseException | None = None) -> dict[str, Any]:
    """Build an Agents SDK generation span, optionally carrying a span error."""

    return {
        "span_id": key,
        "parent_id": parent,
        "trace_id": "agents-trace",
        "span_data": {
            "type": "generation",
            "model": "gpt-4o-mini",
            "input": [{"role": "user", "content": "hello"}],
            "output": [{"role": "assistant", "content": "hi"}],
            "usage": {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15},
        },
        # The Agents SDK reports a failure as a SpanError mapping on the span
        # handed to the ordinary end callback, not as a raised exception.
        "error": {"message": str(error)} if error is not None else None,
    }


OPENAI_AGENTS = BridgeContract(
    id="openai_agents.processor",
    module="openai_agents",
    integration="openai_agents",
    provider_roots=("agents", "openai"),
    handler=openai_agents.BirAgentsTracingProcessor,
    root=RunDriver(
        start=lambda handler, key, parent: handler.on_trace_start(agents_trace(key)),
        end=lambda handler, key: handler.on_trace_end(agents_trace(key)),
        fail=lambda handler, key, error: handler.on_trace_end(agents_trace(key)),
    ),
    root_name="openai_agents.trace",
    generation=RunDriver(
        start=lambda handler, key, parent: handler.on_span_start(agents_span(key, parent)),
        end=lambda handler, key: handler.on_span_end(agents_span(key)),
        fail=lambda handler, key, error: handler.on_span_end(agents_span(key, error=error)),
    ),
    generation_name="openai_agents.generation",
    implicit_root_name="openai_agents.trace",
    model="gpt-4o-mini",
    usage=RECORDED_USAGE,
)


def pydantic_ai_agent_span(key: str) -> dict[str, Any]:
    """Build the agent-run span Pydantic AI opens around a whole agent call."""

    return {"name": "agent run", "context": {"span_id": key, "trace_id": "otel-trace"}, "attributes": {}}


def pydantic_ai_chat_span(
    key: str,
    parent: str | None = None,
    *,
    error: BaseException | None = None,
) -> dict[str, Any]:
    """Build a Pydantic AI chat span, optionally carrying an exception event."""

    span: dict[str, Any] = {
        "name": "chat gpt-4o-mini",
        "context": {"span_id": key, "trace_id": "otel-trace"},
        "parent": {"span_id": parent} if parent is not None else None,
        "attributes": {
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": "gpt-4o-mini",
            "gen_ai.input.messages": [{"role": "user", "content": "hello"}],
            "gen_ai.output.messages": [{"role": "assistant", "content": "hi"}],
            "gen_ai.usage.input_tokens": 11,
            "gen_ai.usage.output_tokens": 4,
            "gen_ai.usage.total_tokens": 15,
        },
    }
    if error is not None:
        # OpenTelemetry reports a failure as an ``exception`` span event on the
        # span handed to the ordinary end callback.
        span["events"] = [{"name": "exception", "attributes": {"exception.message": str(error)}}]
    return span


PYDANTIC_AI = BridgeContract(
    id="pydantic_ai.processor",
    module="pydantic_ai",
    integration="pydantic_ai",
    provider_roots=("pydantic_ai",),
    handler=pydantic_ai.BirPydanticAIHandler,
    root=RunDriver(
        start=lambda handler, key, parent: handler.on_start(pydantic_ai_agent_span(key)),
        end=lambda handler, key: handler.on_end(pydantic_ai_agent_span(key)),
        fail=lambda handler, key, error: handler.on_end(pydantic_ai_agent_span(key)),
    ),
    root_name="agent run",
    generation=RunDriver(
        start=lambda handler, key, parent: handler.on_start(pydantic_ai_chat_span(key, parent)),
        end=lambda handler, key: handler.on_end(pydantic_ai_chat_span(key)),
        fail=lambda handler, key, error: handler.on_end(pydantic_ai_chat_span(key, error=error)),
    ),
    generation_name="chat gpt-4o-mini",
    implicit_root_name="pydantic_ai.agent_run",
    model="gpt-4o-mini",
    usage=RECORDED_USAGE,
)

BRIDGES: tuple[BridgeContract, ...] = (
    LANGCHAIN,
    LLAMAINDEX,
    OPENAI_AGENTS,
    PYDANTIC_AI,
)


class IntegrationRegistryTests(unittest.TestCase):
    """Every integration is declared, and every declared provider is guarded."""

    def integration_modules(self) -> set[str]:
        """Return the public integration module names shipped in the package."""

        package = importlib.import_module("bir.integrations")
        search_locations = getattr(package.__spec__, "submodule_search_locations", None)
        assert search_locations is not None
        return {module.name for module in pkgutil.iter_modules(search_locations) if not module.name.startswith("_")}

    def test_every_integration_module_is_declared(self) -> None:
        declared = (
            {contract.module for contract in CONTRACTS}
            | {bridge.module for bridge in BRIDGES}
            | set(UNDECLARED_PROVIDER_ROOTS)
        )

        # A new integration must either pass one of the contract matrices or say
        # in UNDECLARED_PROVIDER_ROOTS why neither matrix applies to it.
        self.assertEqual(self.integration_modules(), declared)

    def test_contract_ids_and_wrapper_pairs_are_unique(self) -> None:
        ids = [contract.id for contract in CONTRACTS] + [bridge.id for bridge in BRIDGES]
        self.assertEqual(sorted(ids), sorted(set(ids)))

        names = [(contract.module, contract.default_name) for contract in CONTRACTS]
        self.assertEqual(sorted(names), sorted(set(names)))

    def test_declared_contracts_match_their_module(self) -> None:
        for contract in CONTRACTS:
            with self.subTest(contract=contract.id):
                expected_module = f"bir.integrations.{contract.module}"
                for wrapper in contract.wrappers():
                    self.assertEqual(wrapper.__module__, expected_module)

        for bridge in BRIDGES:
            with self.subTest(bridge=bridge.id):
                self.assertEqual(bridge.handler.__module__, f"bir.integrations.{bridge.module}")

    def test_declared_provider_roots_match_the_fresh_import_guard(self) -> None:
        declared: set[str] = set()
        for contract in CONTRACTS:
            declared.update(contract.provider_roots)
        for bridge in BRIDGES:
            declared.update(bridge.provider_roots)
        for roots in UNDECLARED_PROVIDER_ROOTS.values():
            declared.update(roots)

        # The architecture suite asserts that importing Bir pulls in none of
        # these packages. Keeping the two lists equal means a new provider is
        # guarded as soon as it is declared, and a retired one stops being
        # listed once nothing imports it.
        self.assertEqual(declared, set(OPTIONAL_PROVIDER_ROOTS))


def _register_contract_cases() -> None:
    """Publish one generated case per declaration for test discovery."""

    generated: list[type[unittest.TestCase]] = [build_contract_test_case(contract) for contract in CONTRACTS]
    generated.extend(build_bridge_test_case(bridge) for bridge in BRIDGES)

    for case in generated:
        # Report the generated cases under this module so a failure names the
        # declaration that produced it, not the harness that assembled it.
        case.__module__ = __name__
        globals()[case.__name__] = case


_register_contract_cases()


if __name__ == "__main__":
    unittest.main()
