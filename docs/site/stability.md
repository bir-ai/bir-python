# API Stability & Compatibility

This page states what the Bir Python SDK considers public, what may change and
how, and the finite checklist a Beta release is evaluated against. Everything
listed here is checked against the code by the test suite, so this page cannot
drift from what the package actually exports.

## Current status

The SDK is published as **Alpha** (`Development Status :: 3 - Alpha`) at version
`0.3.0`. In practice that means:

- The public surface below is stable in day-to-day use and covered by tests, but
  a `0.x` minor release may still change it.
- The recorded event schema is **not** in that category. `schema_version` is
  `1.0` and is a cross-repository contract with `bir-app`; see
  [Recorded data](#recorded-data).
- Anything not listed on this page is internal. Modules whose name starts with
  an underscore (`bir._sdk`, `bir._storage`, `bir._cli_parser`, …) may be split,
  renamed, or removed in any release. They are import paths, not API.

## Public surface

### Core API

Importable from `bir`.

| Name | Kind |
| --- | --- |
| `__version__` | Package version string |
| `configure` | Configuration |
| `observe` | Decorator |
| `trace` | Context manager |
| `span` | Context manager |
| `generation` | Context manager |
| `tool_call` | Context manager |
| `retrieval` | Context manager |
| `prompt` | Prompt record builder |
| `score` | Function |
| `get_current_trace_id` | Function |
| `get_current_span_id` | Function |
| `load_events` | Function |
| `load_traces` | Function |
| `send_events` | Function |
| `TraceEvent` | Dataclass |
| `LoadedTrace` | Dataclass |
| `SendEventsResult` | Dataclass |
| `PromptRecord` | Dataclass |

### Evaluation API

Importable from `bir.evals`.

| Name | Kind |
| --- | --- |
| `Dataset` | Dataclass |
| `DatasetExample` | Dataclass |
| `EvaluationContext` | Dataclass |
| `EvalResult` | Dataclass |
| `DeterministicEvaluator` | Protocol |
| `ExperimentResult` | Dataclass |
| `ExperimentExampleResult` | Dataclass |
| `ExperimentSummary` | Dataclass |
| `ExperimentDiff` | Dataclass |
| `SendExperimentResult` | Dataclass |
| `run_experiment` | Function |
| `run_experiment_async` | Function |
| `compare_experiments` | Function |
| `render_experiment_report` | Function |
| `list_experiments` | Function |
| `load_experiment` | Function |
| `load_experiment_summary` | Function |
| `send_experiment` | Function |
| `custom_evaluator` | Evaluator factory |
| `exact_match` | Evaluator factory |
| `contains` | Evaluator factory |
| `regex_match` | Evaluator factory |
| `similarity_above` | Evaluator factory |
| `json_valid` | Evaluator factory |
| `field_equals` | Evaluator factory |
| `field_contains` | Evaluator factory |
| `numeric_between` | Evaluator factory |
| `latency_under` | Evaluator factory |
| `cost_under` | Evaluator factory |
| `answer_contains_citation` | Evaluator factory |
| `answer_context_overlap` | Evaluator factory |
| `retrieved_context_contains` | Evaluator factory |

### Test helpers

Importable from `bir.testing`.

| Name | Kind |
| --- | --- |
| `capture_traces` | Context manager |
| `CapturedTraces` | Read-back handle |

### Logging

Importable from `bir.logging`.

| Name | Kind |
| --- | --- |
| `install_trace_id_filter` | Function |
| `BirTraceIdFilter` | Logging filter |
| `TRACE_ID_FIELD` | Constant |
| `SPAN_ID_FIELD` | Constant |

### Integrations

Each integration is a module under `bir.integrations`. Import from the module
rather than the package: the package re-exports these names flat, which cannot
express two providers using the same entry-point name.

| Module | Entry points |
| --- | --- |
| `bir.integrations.anthropic` | `trace_messages`, `trace_messages_async` |
| `bir.integrations.autogen` | `BirAutoGenHandler` |
| `bir.integrations.bedrock` | `trace_converse`, `trace_converse_async`, `trace_converse_stream`, `trace_converse_stream_async` |
| `bir.integrations.cohere` | `trace_chat`, `trace_chat_async` |
| `bir.integrations.crewai` | `BirCrewAIHandler` |
| `bir.integrations.dspy` | `trace_lm`, `trace_lm_async` |
| `bir.integrations.google` | `trace_generate_content`, `trace_generate_content_async` |
| `bir.integrations.haystack` | `BirHaystackTracer` |
| `bir.integrations.instructor` | `trace_create`, `trace_create_async` |
| `bir.integrations.langchain` | `BirCallbackHandler` |
| `bir.integrations.litellm` | `trace_completion`, `trace_completion_async` |
| `bir.integrations.llamaindex` | `BirLlamaIndexHandler` |
| `bir.integrations.mistral` | `trace_chat`, `trace_chat_async` |
| `bir.integrations.ollama` | `trace_chat`, `trace_chat_async`, `trace_generate`, `trace_generate_async` |
| `bir.integrations.openai` | `trace_chat_completion`, `trace_chat_completion_async`, `trace_response`, `trace_response_async` |
| `bir.integrations.openai_agents` | `BirAgentsTracingProcessor` |
| `bir.integrations.otel` | `export_traces_to_otlp` |
| `bir.integrations.pydantic_ai` | `BirPydanticAIHandler` |
| `bir.integrations.vertexai` | `trace_generate_content`, `trace_generate_content_async` |

### CLI commands

| Command | Purpose |
| --- | --- |
| `traces` | List local traces |
| `show` | Show one trace as an event tree |
| `stats` | Aggregate counts, tokens, cost, and latency |
| `tail` | Follow the trace file |
| `experiments` | List local experiments |
| `experiment-show` | Show one experiment |
| `experiment-report` | Render an experiment report |
| `send` | Send local events to a server |
| `send-experiment` | Send an experiment to a server |
| `eval-gate` | Fail a build on an evaluation regression |
| `export-otel` | Export local traces over OTLP |
| `prune` | Delete recorded events |
| `config` | Print the effective configuration |

Command names and their documented flags are public. Human-readable output
formatting is not: column widths, ordering hints, and phrasing may change. Parse
the JSON output (`--json`, where offered) rather than the table output.

### Environment variables

| Variable | Configures |
| --- | --- |
| `BIR_TRACE_PATH` | Trace file location |
| `BIR_DISABLED` | Tracing kill switch |
| `BIR_CAPTURE_INPUTS` | Input capture opt-in |
| `BIR_CAPTURE_OUTPUTS` | Output capture opt-in |
| `BIR_SERVICE_NAME` | Service metadata |
| `BIR_ENVIRONMENT` | Service metadata |
| `BIR_SOURCE` | Service metadata |
| `BIR_SAMPLE_RATE` | Trace sampling |
| `BIR_MAX_VALUE_LENGTH` | Capture truncation |
| `BIR_MAX_COLLECTION_ITEMS` | Capture truncation |

## Recorded data

Events are JSONL records carrying `schema_version = "1.0"`. This is the contract
with the Bir server and dashboard, so it is governed more strictly than the
Python API:

- Field names, types, and the meaning of `type`, `status`, `usage`, and `cost`
  do not change within `1.0`.
- New optional fields may be added; consumers must ignore unknown fields.
- A field removal or a meaning change requires a new `schema_version` and a
  coordinated `bir-app` release.
- `tests/fixtures/` holds the shared fixtures both repositories verify against,
  guarded by a checksum manifest. A change there is a contract change.

The *shape* of the event tree is not frozen by the schema. Which events an
integration emits, and which event a `parent_id` points at, can change to record
a framework's structure more accurately; such changes are release-noted.

## Compatibility policy

**Versioning.** The SDK is `0.x`. Breaking changes land in a minor release
(`0.3` → `0.4`) and are listed in `CHANGELOG.md` under a `Changed` or `Removed`
heading with the migration step. Patch releases never remove or rename a public
name.

**Deprecation.** A public name is not deleted outright. It is first kept working
for one minor release while emitting `DeprecationWarning` and naming its
replacement in the message and the changelog; only the release after that may
remove it. This applies to everything on this page, including CLI commands and
environment variables.

**Supported Python.** The package supports every CPython version upstream still
supports, currently **3.10 through 3.14**, and every one of them is tested on
Linux, macOS, and Windows in CI. A version is added once CI passes on it, and
dropped only in a minor release, no earlier than its upstream end-of-life.

**Dependencies.** The runtime package has no third-party dependencies and will
not gain any; optional capabilities ship as extras (`otel`, `dev`, `docs`). This
is a compatibility guarantee in itself: installing Bir cannot change the version
of any package your application already depends on.

## What integration support means

Bir never imports a provider or framework package — a test asserts that importing
every integration module pulls in none of them. So a provider SDK upgrade cannot
break Bir at import time. What an upgrade can change is the *shape* of the
objects an integration reads: where the model lives on a response, how usage is
spelled, which event carries a streamed token.

Two test layers pin that:

- Each integration's own test module encodes the provider shapes it reads, using
  fakes built from the provider's documented response objects.
- Both conformance matrices
  (`tests/test_integration_contract.py`) require every integration to declare
  what it supports and to pass the shared lifecycle cases for that declaration —
  argument forwarding, one event per call, streaming finalization, error
  redaction, and event-tree obligations for the framework bridges.

Because the SDK pins no provider versions, "supported version" is a question
about your environment, not Bir's. To verify an integration against the exact
provider release you deploy, run that provider's example under
`examples/` with the version pinned in your own environment and compare the
recorded model, usage, and output against the provider's response. An
integration that reads a shape a new provider version stopped emitting records
the event without that field rather than failing the call.

## Beta entry checklist

The SDK moves from Alpha to Beta when every item below is true. Each is
checkable, so readiness is a matter of running the list rather than a judgment
call.

- [x] Every public name, CLI command, and environment variable is inventoried on
      this page and guarded against drift by a test.
- [x] A written deprecation policy and supported-Python policy exist.
- [x] Every shipped integration declares its capabilities and passes a shared
      conformance matrix.
- [x] Quality gates run on every supported Python and OS: unit tests, branch
      coverage with a floor, strict resource warnings, lint, format, type check,
      strict docs build, and hermetic wheel/sdist verification.
- [x] The runtime package has no third-party dependencies.
- [x] Capture is opt-in and redaction cannot be disabled.
- [ ] The event-schema `1.0` contract is confirmed against the current `bir-app`
      release, including the event-tree shape the framework bridges now record.
- [ ] Performance baselines exist for trace write, load, prune, send, and
      evaluation runs, so a release can be checked for regressions.
- [ ] A migration note documents every public change made since `0.3.0`.
- [ ] The version is `0.4.0` or later with the `Development Status` classifier
      raised to `4 - Beta`.
