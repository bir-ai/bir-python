# Integrations

Bir integrations are dependency-free wrappers and callback handlers. Bir does
not import provider SDKs or frameworks; your application installs them and
passes their client callables or handlers to Bir.

Provider wrappers forward arguments unchanged, return the provider response
unchanged, and record a generation inside an active Bir trace. Input and output
payloads still follow Bir's [opt-in capture settings](capture-privacy.md).

## OpenAI

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
capture settings allow it.

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
`response.usage.tokens` when present.

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
hint from the model prefix before `/`.

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
