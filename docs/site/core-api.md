# Core API

The public tracing API is exported from `bir`. Context managers for trace work
support both `with` and `async with`; `@observe()` supports synchronous and
coroutine functions.

## `observe()`

`observe()` decorates a function and records one trace for a top-level call or
a nested span when called inside an active trace.

```python
from bir import observe

@observe(name="answer", capture_inputs=False, capture_outputs=False)
def answer(question: str) -> str:
    return question.upper()
```

The name defaults to the function name. Capture overrides apply only to that
function call and inherit into nested work.

## `trace()`

`trace()` creates an explicit trace root and accepts optional metadata:

```python
from bir import trace

with trace("answer_question", metadata={"request_kind": "interactive"}):
    ...
```

## `span()`

Use a span for nested application work inside a trace:

```python
from bir import span

with span("prepare_context"):
    ...
```

## `generation()`

Use `generation()` for an LLM call. It can record the model, usage, explicit
cost, input, output, metadata, and prompt identity.

```python
from bir import generation

with generation("local.llm", model="demo-model") as gen:
    response = "ok"
    gen.set_output(response)
    gen.set_usage(input_tokens=12, output_tokens=24, total_tokens=36)
    gen.set_cost(input_cost=0.001, output_cost=0.002, total_cost=0.003)
```

Usage and cost setters require at least one field. Values must be non-negative
and finite. Cost values are user-provided; Bir defaults currency to `USD` and
does not calculate provider pricing.

Pass `input=`, `metadata=`, `prompt=`, `capture_input=`, or `capture_output=` to
the context manager when needed. Capture flags default to the active trace or
global configuration.

## `tool_call()`

Use `tool_call()` for external functions or tools:

```python
from bir import tool_call

with tool_call("weather", input={"city": "Istanbul"}) as call:
    result = {"temperature_c": 24}
    call.set_output(result)
```

Like generations, tool calls accept metadata and per-event capture overrides.

## `retrieval()`

`retrieval()` records RAG lookups using the tool-call event contract. It sets
`metadata.kind` to `retrieval`, stores the query at `input.query` when input
capture is enabled, and stores documents at `output.documents` when output
capture is enabled.

```python
from bir import retrieval

with retrieval("vector_search", query="What is Bir?") as result:
    result.add_document(
        id="doc-1",
        rank=1,
        score=0.82,
        source="docs",
        text="Bir records local traces with JSONL.",
    )
```

Document ranks must be non-negative integers and document scores must be
non-negative finite numbers.

## `set_metadata()`

The `trace()`, `span()`, `generation()`, `tool_call()`, and `retrieval()`
context managers each expose `set_metadata(...)` to attach metadata discovered
while the body runs — a resolved route, a cache-hit flag, or a request id —
before the event is written:

```python
from bir import span

with span("retrieve_context") as current_span:
    documents = lookup()
    current_span.set_metadata({"cache_hit": False, "documents": len(documents)})
```

It merges into any metadata passed at creation time, with later keys winning
across repeated calls, and the merged metadata is redacted at exit with the same
rules as captured inputs and outputs. The generation `prompt` identity, the
retrieval `kind`, and the trace `service` metadata are preserved. `set_metadata`
works with both `with` and `async with`; the argument must be a mapping, and a
non-mapping raises `TypeError`.

## `score()`

Attach a finite numeric score to the active trace:

```python
from bir import score

score("faithfulness", 0.4, metadata={"reason": "answer cites no context"})
```

`score()` requires an active trace. Its optional metadata is redacted with the
same rules as captured inputs and outputs.

## `prompt()`

Use `prompt()` to attach prompt identity and version metadata to a generation.
Template text, variables, and rendered prompts are not captured unless you opt
in.

```python
from bir import generation, prompt

answer_prompt = prompt(
    "answer_question",
    version="v1",
    template="Answer using this context: {context}",
    variables={"context": "local context"},
)

with generation("local.llm", model="demo-model", prompt=answer_prompt):
    ...
```

The event records the prompt name, version, and a template SHA-256 digest when
a template is present. To capture the payload, set `capture_template=True`,
`capture_variables=True`, or `capture_rendered=True`. Those fields use the same
best-effort redaction as other captured values.

## `get_current_trace_id()` and `get_current_span_id()`

Read the active ids to stamp your own logs and metrics so they can be correlated
with Bir traces later:

```python
import logging

from bir import get_current_span_id, get_current_trace_id, observe


@observe()
def answer(question: str) -> str:
    logging.info(
        "handling question",
        extra={"trace_id": get_current_trace_id(), "span_id": get_current_span_id()},
    )
    return "ok"
```

Both return `None` outside any trace and never raise. `get_current_trace_id()`
returns the active trace root id; `get_current_span_id()` returns the innermost
open node — the current `span()`, `generation()`, or `tool_call()`, or the trace
root when none is open. The values are exactly the `trace_id` and `parent_id`
written to the JSONL for an event created at that point, and they are read from a
task-local context, so concurrent asyncio tasks and threads each see their own
ids. They are read-only: there is no setter and the underlying context is not
exposed for injection or cross-process propagation.

## `configure()`

Configure process-local defaults:

```python
from bir import configure

configure(
    trace_path="tmp/bir-traces.jsonl",
    capture_inputs=False,
    capture_outputs=False,
    service_name="rag-api",
    environment="production",
    sample_rate=0.1,
    max_bytes=5_000_000,
    backup_count=3,
)
```

Arguments that are omitted retain the current setting. Environment defaults are
read once when `bir` is imported; explicit `configure()` arguments take
precedence. See [CLI & Environment Config](cli-env.md).

## Event loading

`load_events()` validates JSONL records against the current event schema and
raises `ValueError` for malformed rows, unsupported event types, invalid
timestamps, or unsupported schema versions.

```python
from bir import load_events, load_traces

events = load_events()
traces = load_traces()
```

Both functions read only the active file by default. Pass
`include_rotated=True` to read rotated files oldest-first. Because rotation can
occur mid-trace, a logical trace may be split across files.
