"""Optional framework integrations for Bir."""

from . import cohere
from .anthropic import trace_messages
from .bedrock import trace_converse
from .google import trace_generate_content
from .langchain import BirCallbackHandler
from .llamaindex import BirLlamaIndexHandler
from .litellm import trace_completion
from .mistral import trace_chat
from .openai import trace_chat_completion
from .vertexai import trace_generate_content as trace_vertex_generate_content

__all__ = [
    "cohere",
    "trace_messages",
    "trace_converse",
    "trace_generate_content",
    "trace_vertex_generate_content",
    "BirCallbackHandler",
    "BirLlamaIndexHandler",
    "trace_completion",
    "trace_chat",
    "trace_chat_completion",
]
