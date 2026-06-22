# Integrations

Bir integrations are dependency-free wrappers and callback handlers. Bir does
not import provider SDKs or frameworks; your application installs them and
passes their client callables or handlers to Bir.

Provider wrappers forward arguments unchanged, return the provider response
unchanged, and record a generation inside an active Bir trace. Input and output
payloads still follow Bir's [opt-in capture settings](capture-privacy.md).

## Async clients

Every dependency-free provider wrapper has an asynchronous counterpart named with
an `_async` suffix, for applications using async provider clients such as
`AsyncOpenAI`, `AsyncAnthropic`, the `google-genai` async client,
`litellm.acompletion`, and the async Mistral and Cohere clients. Each awaits the
provider coroutine inside one Bir generation, forwards arguments unchanged, and
returns the awaited provider result:

```python
from bir import trace
from bir.integrations.openai import trace_chat_completion_async

async with trace("chat"):
    response = await trace_chat_completion_async(
        async_client.chat.completions.create,
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "What is Bir?"}],
    )
```

For the streaming surfaces (OpenAI Chat Completions and Responses, Anthropic, and
Gemini), passing `stream=True` resolves to an async iterator that yields the
provider's events unchanged and finalizes the model, output, and usage when the
stream is exhausted, closed (`aclose()`), or raises mid-stream:

```python
async with trace("chat"):
    stream = await trace_chat_completion_async(
        async_client.chat.completions.create,
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Stream it"}],
        stream=True,
    )
    async for chunk in stream:
        ...
```

The async wrappers, by provider:

| Provider | Module | Async wrapper |
| --- | --- | --- |
| OpenAI Chat Completions | `bir.integrations.openai` | `trace_chat_completion_async` |
| OpenAI Responses | `bir.integrations.openai` | `trace_response_async` |
| Anthropic Messages | `bir.integrations.anthropic` | `trace_messages_async` |
| Google Gemini | `bir.integrations.google` | `trace_generate_content_async` |
| Mistral | `bir.integrations.mistral` | `trace_chat_async` |
| Cohere | `bir.integrations.cohere` | `trace_chat_async` |
| LiteLLM | `bir.integrations.litellm` | `trace_completion_async` |

They require an active trace just like the sync wrappers — an async `@observe()`
function or `async with bir.trace(...)` — and take the same `bir_`-prefixed
options. AWS Bedrock, Vertex AI, and the LangChain and LlamaIndex callback
handlers have no async wrapper.

## OpenAI

OpenAI exposes two chat surfaces with different response and streaming shapes, so
Bir ships a wrapper for each: `trace_chat_completion` for Chat Completions and
`trace_response` for the Responses API.

### Chat Completions

```python
from bir import trace
from bir.integrations.openai import trace_chat_completion

with trace("chat"):
    response = trace_chat_completion(
        client.chat.completions.create,
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "What is Bir?"}],
    )
```

The wrapper records response model and token usage when present. Streaming chat
completions are passed through and recorded after consumption; request streamed
usage from OpenAI when token counts are needed.

### Responses API

```python
from bir import trace
from bir.integrations.openai import trace_response

with trace("chat"):
    response = trace_response(
        client.responses.create,
        model="gpt-4o",
        input="What is Bir?",
    )
```

The wrapper records the response model, the aggregated `output_text`, and the
`input_tokens`/`output_tokens`/`total_tokens` usage when present, falling back to
the full response shape when `output_text` is empty. With `stream=True` it
returns a lazy iterable that yields the provider's events unchanged, assembles
the output from `response.output_text.delta` events, and reads the final model
and usage from the terminal `response.completed` event after the stream is
consumed; request streamed usage from OpenAI when token counts are needed.

## Anthropic

```python
from bir import trace
from bir.integrations.anthropic import trace_messages

with trace("chat"):
    response = trace_messages(
        client.messages.create,
        model="claude-haiku-4-5",
        max_tokens=512,
        messages=[{"role": "user", "content": "What is Bir?"}],
    )
```

`stream=True` is supported. Chunks pass through unchanged; Bir accumulates text
and usage from message events as the stream is consumed.

## Mistral

```python
from bir import trace
from bir.integrations.mistral import trace_chat

with trace("chat"):
    response = trace_chat(
        client.chat.complete,
        model="mistral-small-latest",
        messages=[{"role": "user", "content": "What is Bir?"}],
    )
```

Bir reads the model, token usage, and `model_dump()` output when available and
capture settings allow it. With `stream=True` (for example `client.chat.stream`)
the wrapper returns a lazy iterable that yields the OpenAI-shaped chunks unchanged
and records the accumulated text and final usage after the stream is consumed.

## Cohere

```python
from bir import trace
from bir.integrations.cohere import trace_chat

with trace("chat"):
    response = trace_chat(
        client.chat,
        model="command-a-03-2025",
        messages=[{"role": "user", "content": "What is Bir?"}],
    )
```

The wrapper records the request model and reads token usage from
`response.usage.tokens` when present. With `stream=True` (for example
`client.chat_stream`) the wrapper yields the v2 events unchanged, accumulates text
from `content-delta` events (`delta.message.content.text`), and reads usage from
the terminal `message-end`/`stream-end` event after the stream is consumed.

## Google Gemini

```python
from bir import trace
from bir.integrations.google import trace_generate_content

with trace("chat"):
    response = trace_generate_content(
        client.models.generate_content,
        model="gemini-2.5-flash",
        contents="What is Bir?",
    )
```

The wrapper supports the current and legacy Google SDK response shapes without
importing either package. `stream=True` returns chunks unchanged and records
accumulated text and final usage after consumption.

## Google Vertex AI

```python
from bir import trace
from bir.integrations.vertexai import trace_generate_content

with trace("chat"):
    response = trace_generate_content(
        model.generate_content,
        "What is Bir?",
        bir_model="gemini-1.5-flash",
    )
```

Vertex binds the model to its `GenerativeModel` instance, so pass `bir_model` to
record it. A response `model_version` refines that value. The wrapper is also
exported as `bir.integrations.trace_vertex_generate_content` to avoid colliding
with the Gemini wrapper.

## AWS Bedrock

```python
from bir import trace
from bir.integrations.bedrock import trace_converse

with trace("chat"):
    response = trace_converse(
        client.converse,
        modelId="anthropic.claude-3-5-sonnet-20240620-v1:0",
        messages=[{"role": "user", "content": [{"text": "What is Bir?"}]}],
    )
```

Pass a `boto3` `bedrock-runtime` client's `converse` method. Bir records
`modelId` and the response's `inputTokens`, `outputTokens`, and `totalTokens`
when present without importing `boto3`.

## LiteLLM

```python
from bir import trace
from bir.integrations.litellm import trace_completion

with trace("chat"):
    response = trace_completion(
        litellm.completion,
        model="anthropic/claude-3-5-sonnet",
        messages=[{"role": "user", "content": "What is Bir?"}],
    )
```

The wrapper reads the OpenAI-shaped response and derives a provider metadata
hint from the model prefix before `/`. With `stream=True` the wrapper returns a
lazy iterable that yields the OpenAI-shaped chunks unchanged and records the
accumulated text and final usage after the stream is consumed.

## LangChain

```python
from bir import configure
from bir.integrations.langchain import BirCallbackHandler

configure(capture_inputs=True, capture_outputs=True)

result = chain.invoke(
    {"question": "What is Bir?"},
    config={"callbacks": [BirCallbackHandler()]},
)
```

Root chains become traces, nested chains become spans, model callbacks become
generations, retrievers become retrieval tool calls, and tools become tool-call
events. Direct model calls create a small implicit trace root. Token usage is
read from common `llm_output`, `usage_metadata`, and `response_metadata` shapes.

## LlamaIndex

```python
from bir.integrations.llamaindex import BirLlamaIndexHandler

handler = BirLlamaIndexHandler()
callback_manager = CallbackManager([handler])
```

Pass the handler to LlamaIndex's callback manager. LLM and chat callbacks become
generations; retrieval callbacks become retrieval events. Operations outside an
explicit callback trace receive an implicit Bir trace root. The handler does not
import LlamaIndex.

## Wrapper-specific options

Provider wrapper options use the `bir_` prefix so they cannot collide with
provider arguments:

- `bir_name` changes the generation event name.
- `bir_metadata` adds event metadata.
- `bir_capture_input` and `bir_capture_output` override capture for that call.

All wrappers require an active trace, such as `with trace(...)` or a function
decorated with `@observe()`.
