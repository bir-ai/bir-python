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

For the streaming surfaces (OpenAI Chat Completions and Responses, Anthropic,
Gemini, Mistral, Cohere, and LiteLLM), passing `stream=True` resolves to an async
iterator that yields the provider's events unchanged and finalizes the model,
output, and usage when the stream is exhausted, closed (`aclose()`), or raises
mid-stream:

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
| Instructor | `bir.integrations.instructor` | `trace_create_async` |
| DSPy | `bir.integrations.dspy` | `trace_lm_async` |

They require an active trace just like the sync wrappers — an async `@observe()`
function or `async with bir.trace(...)` — and take the same `bir_`-prefixed
options. AWS Bedrock, Vertex AI, and the LangChain, LlamaIndex, OpenAI Agents
SDK, Pydantic AI, and CrewAI callback handlers have no async wrapper.

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
with the Gemini wrapper. With `stream=True` the wrapper returns a lazy iterable
that yields Vertex's `GenerationResponse` chunks unchanged and records the
accumulated text (each chunk's `text`, falling back to the first candidate's text
parts) and the final `usage_metadata` after the stream is consumed, refining the
model from a chunk `model_version` when present.

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

The Converse stream API is a separate method, so it has a dedicated
`trace_converse_stream` wrapper:

```python
from bir import trace
from bir.integrations.bedrock import trace_converse_stream

with trace("chat"):
    stream = trace_converse_stream(
        client.converse_stream,
        modelId="anthropic.claude-3-5-sonnet-20240620-v1:0",
        messages=[{"role": "user", "content": [{"text": "What is Bir?"}]}],
    )
    for event in stream:
        ...
```

It yields the Converse stream's events (the items of the response `stream` member)
unchanged, so iterate it directly instead of reaching into `response["stream"]`.
Bir accumulates text from each `contentBlockDelta.delta.text` and records the
`messageStop` stop reason and the terminal `metadata` event's token usage after
the stream is consumed.

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

## Instructor

[Instructor](https://python.useinstructor.com/) patches OpenAI-compatible clients
to return validated Pydantic models. `trace_create` wraps the patched
`client.chat.completions.create` callable and records one generation with the
model and token usage from the underlying completion.

Instructor can return the parsed model directly (`create`) or a
`(parsed_model, raw_completion)` tuple (`create_with_completion`). Both shapes
are handled automatically.

```python
import instructor
import openai
from bir import trace
from bir.integrations.instructor import trace_create

client = instructor.from_openai(openai.OpenAI())

with trace("structured"):
    user = trace_create(
        client.chat.completions.create,
        model="gpt-4o-mini",
        response_model=User,
        messages=[{"role": "user", "content": "Extract: Jason is 25 years old"}],
    )
```

For async clients use `trace_create_async`:

```python
import instructor
import openai
from bir import trace
from bir.integrations.instructor import trace_create_async

client = instructor.from_openai(openai.AsyncOpenAI())

async with trace("structured"):
    user = await trace_create_async(
        client.chat.completions.create,
        model="gpt-4o-mini",
        response_model=User,
        messages=[{"role": "user", "content": "Extract: Jason is 25 years old"}],
    )
```

## DSPy

[DSPy](https://dspy.ai/) routes every language-model call through a `dspy.LM`
instance whose underlying request method (`LM.forward`, historically
`LM.request`) returns the raw LiteLLM-style response carrying the model and an
OpenAI-shaped token `usage` block. `trace_lm` wraps that bound method and records
one generation with the model and token usage from the response.

```python
import dspy
from bir import trace
from bir.integrations.dspy import trace_lm

lm = dspy.LM("openai/gpt-4o-mini")

with trace("dspy"):
    response = trace_lm(
        lm.forward,
        messages=[{"role": "user", "content": "What is Bir?"}],
    )
```

The request model is read from the bound `LM` instance (`lm.model`) or an explicit
`model` keyword, then refined from the response's `model` when the provider echoes
one back. For DSPy's async request method use `trace_lm_async` with `lm.aforward`.
`dspy` is never imported.

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

## OpenAI Agents SDK

```python
from agents import Runner, add_trace_processor
from bir.integrations.openai_agents import BirAgentsTracingProcessor

add_trace_processor(BirAgentsTracingProcessor())

result = Runner.run_sync(agent, "What is Bir?")
```

`BirAgentsTracingProcessor` implements the Agents SDK tracing-processor interface
(`on_trace_start`/`on_trace_end` and `on_span_start`/`on_span_end`). Register it
with `add_trace_processor` and each agent run's trace becomes a Bir trace root.
Spans are mapped by their `span_data.type`: model spans (`generation` and
`response`) become generations carrying the model and token usage when present,
tool spans (`function` and `mcp_tools`) become tool calls, and every other kind
(`agent`, `handoff`, `guardrail`, `custom`, ...) becomes a span. A failed span is
recorded with error status. Active traces and spans are tracked by their Agents
id, so concurrent and nested runs stay isolated. The processor does not import the
`openai-agents` package, and input/output capture follows the same
[opt-in settings](capture-privacy.md) as every other integration, overridable per
processor with `capture_inputs`/`capture_outputs`.

## Pydantic AI

```python
from opentelemetry.sdk.trace import TracerProvider
from pydantic_ai import Agent
from bir.integrations.pydantic_ai import BirPydanticAIHandler

provider = TracerProvider()
provider.add_span_processor(BirPydanticAIHandler())

agent = Agent("openai:gpt-4o", instrument=True)
result = agent.run_sync("What is Bir?")
```

Pydantic AI's lowest-coupling observability seam is its OpenTelemetry
instrumentation: constructing an agent with `Agent(instrument=True)` (or
`Agent.instrument_all()`) makes every run emit OpenTelemetry spans following the
GenAI semantic conventions. `BirPydanticAIHandler` implements the OpenTelemetry
`SpanProcessor` interface (`on_start`/`on_end`/`shutdown`/`force_flush`), so adding
it to the tracer provider Pydantic AI uses turns each instrumented agent run into a
Bir trace.

Spans are read by duck typing — tolerant of the attribute-key changes across
Pydantic AI instrumentation versions — and classified by `gen_ai.operation.name`
(falling back to the span name): an agent-run span (`invoke_agent` / `agent run`)
opens a Bir trace root, a model span (`chat`) becomes a generation carrying the
model (`gen_ai.request.model` / `gen_ai.response.model`) and token usage
(`gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens`), and a tool span
(`execute_tool` / `running tool`) becomes a tool call. Every other span becomes a
Bir span. A failed span (OpenTelemetry `ERROR` status or a recorded `exception`
event) is recorded with error status. Active runs are tracked by OpenTelemetry span
id, so concurrent and nested runs stay isolated. The handler imports neither
`pydantic_ai` nor `opentelemetry`, and input/output capture follows the same
[opt-in settings](capture-privacy.md) as every other integration, overridable per
handler with `capture_inputs`/`capture_outputs`.

## CrewAI

```python
from crewai.utilities.events import crewai_event_bus
from crewai.utilities.events.base_events import BaseEvent
from bir.integrations.crewai import BirCrewAIHandler

handler = BirCrewAIHandler()

@crewai_event_bus.on(BaseEvent)
def _forward(source, event):
    handler.on_event(source, event)

crew.kickoff(inputs={"topic": "Bir"})
```

CrewAI's lowest-coupling observability seam is its event bus: every crew run emits
typed events (`CrewKickoffStartedEvent`, `TaskStartedEvent`, `LLMCallStartedEvent`,
`ToolUsageStartedEvent`, and their completed/failed counterparts) through
`crewai.utilities.events.crewai_event_bus`. The bus calls a registered handler with
the framework's `(source, event)` pair, so forwarding those to
`BirCrewAIHandler.on_event` turns each crew run into a Bir trace.

Events are read by duck typing — tolerant of the field changes across CrewAI
versions — and classified by their `event.type` string: a crew-kickoff event opens a
Bir trace root, task and agent-execution events become structural spans, LLM-call
events become generations carrying the model and token usage, and tool-usage events
become tool calls. A `*_failed` / `*_error` event closes its node with error status.
Crew, task, and agent nodes are tracked by their framework id (a crew's
`id`/`fingerprint`, a task's `id`, an agent's `id`); LLM-call and tool-usage events,
which CrewAI emits without a correlation id, are paired by a per-thread
last-in-first-out stack, so concurrent and nested runs stay isolated. The handler
does not import the `crewai` package, and input/output capture follows the same
[opt-in settings](capture-privacy.md) as every other integration, overridable per
handler with `capture_inputs`/`capture_outputs`.

## Wrapper-specific options

Provider wrapper options use the `bir_` prefix so they cannot collide with
provider arguments:

- `bir_name` changes the generation event name.
- `bir_metadata` adds event metadata.
- `bir_capture_input` and `bir_capture_output` override capture for that call.

All wrappers require an active trace, such as `with trace(...)` or a function
decorated with `@observe()`.
