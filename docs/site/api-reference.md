# API Reference

This page is generated from the docstrings in the source, so it always matches
the installed version. For task-oriented walkthroughs, start with the
[Core API](core-api.md), [Evals & Experiments](evals-experiments.md), and the
other guides in the navigation; this reference documents the same public surface
symbol by symbol.

Everything below is importable from the top-level `bir` package unless a section
names a submodule (`bir.evals`, `bir.testing`, or `bir.logging`).

## Tracing

The decorator and context managers that record traces, spans, generations, tool
calls, retrievals, scores, and prompt metadata.

::: bir.observe

::: bir.trace

::: bir.span

::: bir.generation

::: bir.tool_call

::: bir.retrieval

::: bir.score

::: bir.prompt

## Configuration

::: bir.configure

## Loading and sending events

Read locally recorded traces back into memory and post them to a Bir server, and
read the active trace and span ids for correlating your own logs and metrics.

::: bir.load_events

::: bir.load_traces

::: bir.send_events

::: bir.get_current_trace_id

::: bir.get_current_span_id

## Data types

The frozen dataclasses returned by the loaders and helpers above.

::: bir.TraceEvent

::: bir.LoadedTrace

::: bir.SendEventsResult

::: bir.PromptRecord

## Evals and experiments

Deterministic evaluators, datasets, and experiment runners from `bir.evals`.

::: bir.evals

## Testing helpers

Assert on your own instrumentation from `bir.testing`.

::: bir.testing

## Logging integration

Stamp standard-library log records with the active trace and span ids using
`bir.logging`.

::: bir.logging
