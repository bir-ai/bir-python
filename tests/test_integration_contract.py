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
    litellm,
    mistral,
    ollama,
    openai,
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

# Integrations that bridge a framework's own events into Bir instead of wrapping
# one provider call. They record whole event trees driven by the framework, so
# the call-wrapper lifecycle does not apply and their handler tests stay
# framework-specific. Their provider import roots are declared here so the
# fresh-import guard still covers them.
EVENT_BRIDGE_PROVIDER_ROOTS: Mapping[str, tuple[str, ...]] = {
    "autogen": ("ag2", "autogen"),
    "crewai": ("crewai",),
    "haystack": ("haystack",),
    "langchain": ("langchain", "langchain_core"),
    "llamaindex": ("llama_index", "llamaindex"),
    "openai_agents": ("agents", "openai"),
    "otel": ("opentelemetry",),
    "pydantic_ai": ("pydantic_ai",),
}


class IntegrationRegistryTests(unittest.TestCase):
    """Every integration is declared, and every declared provider is guarded."""

    def integration_modules(self) -> set[str]:
        """Return the public integration module names shipped in the package."""

        package = importlib.import_module("bir.integrations")
        search_locations = getattr(package.__spec__, "submodule_search_locations", None)
        assert search_locations is not None
        return {module.name for module in pkgutil.iter_modules(search_locations) if not module.name.startswith("_")}

    def test_every_integration_module_is_declared(self) -> None:
        declared = {contract.module for contract in CONTRACTS} | set(EVENT_BRIDGE_PROVIDER_ROOTS)

        # A new integration must either pass the call-wrapper matrix or say in
        # EVENT_BRIDGE_PROVIDER_ROOTS why the matrix does not apply to it.
        self.assertEqual(self.integration_modules(), declared)

    def test_contract_ids_and_wrapper_pairs_are_unique(self) -> None:
        ids = [contract.id for contract in CONTRACTS]
        self.assertEqual(sorted(ids), sorted(set(ids)))

        names = [(contract.module, contract.default_name) for contract in CONTRACTS]
        self.assertEqual(sorted(names), sorted(set(names)))

    def test_declared_contracts_match_their_module(self) -> None:
        for contract in CONTRACTS:
            with self.subTest(contract=contract.id):
                expected_module = f"bir.integrations.{contract.module}"
                for wrapper in contract.wrappers():
                    self.assertEqual(wrapper.__module__, expected_module)

    def test_declared_provider_roots_match_the_fresh_import_guard(self) -> None:
        declared: set[str] = set()
        for contract in CONTRACTS:
            declared.update(contract.provider_roots)
        for roots in EVENT_BRIDGE_PROVIDER_ROOTS.values():
            declared.update(roots)

        # The architecture suite asserts that importing Bir pulls in none of
        # these packages. Keeping the two lists equal means a new provider is
        # guarded as soon as it is declared, and a retired one stops being
        # listed once nothing imports it.
        self.assertEqual(declared, set(OPTIONAL_PROVIDER_ROOTS))


def _register_contract_cases() -> None:
    """Publish one generated case per declaration for test discovery."""

    for contract in CONTRACTS:
        case = build_contract_test_case(contract)
        # Report the generated cases under this module so a failure names the
        # declaration that produced it, not the harness that assembled it.
        case.__module__ = __name__
        globals()[case.__name__] = case


_register_contract_cases()


if __name__ == "__main__":
    unittest.main()
