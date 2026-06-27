"""Optional framework integrations for Bir."""

from . import cohere
from .anthropic import trace_messages, trace_messages_async
from .crewai import BirCrewAIHandler
from .dspy import trace_lm, trace_lm_async
from .instructor import trace_create, trace_create_async
from .bedrock import trace_converse, trace_converse_async, trace_converse_stream
from .google import trace_generate_content, trace_generate_content_async
from .haystack import BirHaystackTracer
from .langchain import BirCallbackHandler
from .llamaindex import BirLlamaIndexHandler
from .litellm import trace_completion, trace_completion_async
from .mistral import trace_chat, trace_chat_async
from .openai import (
    trace_chat_completion,
    trace_chat_completion_async,
    trace_response,
    trace_response_async,
)
from .openai_agents import BirAgentsTracingProcessor
from .otel import export_traces_to_otlp
from .pydantic_ai import BirPydanticAIHandler
from .vertexai import trace_generate_content as trace_vertex_generate_content
from .vertexai import trace_generate_content_async as trace_vertex_generate_content_async

__all__ = [
    "cohere",
    "export_traces_to_otlp",
    "trace_lm",
    "trace_lm_async",
    "trace_create",
    "trace_create_async",
    "trace_messages",
    "trace_messages_async",
    "trace_converse",
    "trace_converse_async",
    "trace_converse_stream",
    "trace_generate_content",
    "trace_generate_content_async",
    "trace_vertex_generate_content",
    "trace_vertex_generate_content_async",
    "BirHaystackTracer",
    "BirCallbackHandler",
    "BirLlamaIndexHandler",
    "BirAgentsTracingProcessor",
    "BirPydanticAIHandler",
    "BirCrewAIHandler",
    "trace_completion",
    "trace_completion_async",
    "trace_chat",
    "trace_chat_async",
    "trace_chat_completion",
    "trace_chat_completion_async",
    "trace_response",
    "trace_response_async",
]
