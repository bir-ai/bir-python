# Bir Python SDK — Improvement Roadmap

> Generated analysis (audited 2026-06-24). Not committed automatically. Each
> Phase-4 prompt is a standalone Claude Code task that a fresh session can paste
> in and run end to end. Re-verify every claim against the current code before
> relying on it — the repo evolves quickly and most of the original "candidate
> areas" are already shipped.

## Phase 2 — Assessment

### What already exists (excluded from the roadmap)

The repo is well past MVP. The following candidate areas are **already done** and
are intentionally not re-proposed:

- **PEP 561 `py.typed`** — ships via `[tool.setuptools.package-data]`, present at
  `src/bir/py.typed`.
- **Environment-variable config** — `BIR_TRACE_PATH`, `BIR_CAPTURE_INPUTS`,
  `BIR_CAPTURE_OUTPUTS`, `BIR_SAMPLE_RATE`, `BIR_SERVICE_NAME`,
  `BIR_ENVIRONMENT`, `BIR_SOURCE` read once at import time.
- **CI Python matrix** — 3.10/3.11/3.12/3.13 with `fail-fast: false`; pyright +
  `verify_release.py` pinned to 3.12.
- **`bir` CLI** — `traces`, `show`, `stats`, `tail`, `experiments`, `send`,
  `send-experiment`, `eval-gate`, plus `bir --version`. Console script wired in
  `pyproject.toml` and asserted by release verification.
- **Trace-file rotation / size cap** — `configure(max_bytes=, backup_count=)`
  with `load_events/load_traces(include_rotated=True)`.
- **`send_events` resilience** — bounded retry/backoff plus opt-in
  `mark_sent=True` sidecar; same for `send_experiment`.
- **Integrations** — openai (Chat + Responses), anthropic, google, mistral,
  cohere, litellm, langchain, llamaindex, **bedrock**, **vertexai**, **openai
  agents** (tracing processor), plus the **OTLP exporter** behind `[otel]`.
- **Streaming** — sync `stream=True` across OpenAI/Anthropic/Gemini/Mistral/
  Cohere/LiteLLM/Bedrock/Vertex; async streaming for OpenAI/Anthropic/Gemini.
- **Experiment regression detection** — `compare_experiments()` /
  `ExperimentDiff` / `bir eval-gate` with per-evaluator tolerances and a
  `missing_score` policy.
- **Richer redaction** — JWT, AWS AKIA/ASIA, GCP `AIza`, Slack `xox*`, GitHub
  `gh*_`, OpenAI `sk-`, labeled-secret + bearer rules, plus user-supplied
  `additional_secret_keys` / `additional_redaction_patterns`.
- **OTLP export behind an extra**, and a **strict MkDocs site** built in CI.
- **Async experiment runner** — `run_experiment_async(max_concurrency=...)`.
- **`configure(model_prices=...)`** local cost table, `set_metadata(...)` on every
  context manager, `get_current_trace_id/span_id()`.

### Conventions / idioms the code follows

- Frozen dataclasses for config and public records (`_Config`, `TraceEvent`,
  `LoadedTrace`, `PromptRecord`, eval result types).
- Centralized `_validate_*` helpers (`_validate_event_name`,
  `_validate_non_negative_int`, `_validate_number`, `_validate_sample_rate`,
  `_validate_model_prices`, …) called from `configure()` and constructors.
- JSONL writing is lock-serialized (in-process `Lock` + advisory
  `_InterProcessFileLock`), `schema_version` `"1.0"`, deterministic
  `sort_keys` / `allow_nan=False` serialization.
- Redaction runs on **every** persistence path via `_safe_capture` /
  `_redact_text`; built-in rules can never be disabled, user rules only widen.
- Integrations are **lazy-import, dependency-free**: callables/clients are passed
  in by the user, `bir_`-prefixed kwargs are stripped, shared parsing lives in
  `integrations/_common.py`, and each public symbol is re-exported from
  `integrations/__init__.py`.
- Tests mix `unittest` and `pytest`; shared wire-contract fixtures live in
  `tests/fixtures/` and are guarded by `scripts/fixtures.py check` (a job mirrored
  in the separate `bir-app` repo).

### Genuinely missing / improvable (this roadmap)

Async **streaming** parity for Mistral/Cohere/LiteLLM; `python -m bir`; a CLI
front-end for the OTLP exporter; a docs **deploy** (the site is built strictly but
never published, despite `site_url` pointing at GitHub Pages); a small in-memory
**test-capture** helper for users instrumenting their own code; a threaded
`max_workers` for the **sync** experiment runner; a few additional dependency-free
integrations (Instructor, Pydantic AI, DSPy, CrewAI); and a **flagged** cross-repo
redaction expansion.

## Phase 3 — Prioritized improvements

| # | Improvement | Category | Priority | Size | Risk | Depends on |
|---|-------------|----------|----------|------|------|------------|
| 1 | `python -m bir` entry point (`__main__.py`) | dx | P0 | S | low | — |
| 2 | Async `stream=True` for Mistral / Cohere / LiteLLM wrappers | integrations | P1 | M | low | — |
| 3 | `bir export-otel` CLI subcommand (front-end for existing exporter) | dx | P1 | S | low | — |
| 4 | MkDocs GitHub Pages deploy workflow (+ optional Material theme) | ci/release | P1 | M | low | — |
| 5 | `bir.testing` in-memory trace-capture helper for users' tests | dx | P1 | M | low | — |
| 6 | Threaded `max_workers` for the sync `run_experiment()` | evals | P1 | M | low | — |
| 7 | Instructor integration (`trace_create`) | integrations | P2 | M | low | — |
| 8 | Pydantic AI integration (instrument-events bridge) | integrations | P2 | M | low | — |
| 9 | DSPy integration (LM-callback bridge) | integrations | P2 | M | low | — |
| 10 | CrewAI integration (step/callback bridge) | integrations | P2 | M | low | — |
| 11 | Expanded redaction: Stripe / Azure / PEM private-key blocks | core | P1 | M | **med** | CROSS-REPO |

**Trade-off flags.** #11 touches the **shared redaction contract**
(`tests/fixtures/redaction-cases.json` + `tests/test_redaction_parity.py`), which
the separate `bir-app` server maintains an independent copy of. It is the one item
here that is a coordinated, cross-repo change — do not ship the SDK side ahead of
the server side. #4 only *publishes* docs; keep the strict build gate intact.
Everything else is additive and stays inside the invariants.

---

## Phase 4 — Standalone prompts

### 1. `python -m bir` entry point

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.
The CLI lives in src/bir/cli.py with a `main()` entry point, wired as the `bir`
console script in pyproject.toml ([project.scripts] bir = "bir.cli:main").

TASK
Add a `src/bir/__main__.py` so `python -m bir ...` runs the exact same CLI as the
`bir` console script.

WHY
`python -m bir` is the dependency-free way to invoke the CLI when the console
script isn't on PATH (fresh venvs, `pipx run`, CI), and users expect module
execution to mirror the script.

IMPLEMENTATION GUIDANCE
- Create src/bir/__main__.py that imports `main` from `bir.cli` and calls
  `raise SystemExit(main())` under `if __name__ == "__main__":`, matching how
  cli.main already returns an int exit code.
- Confirm cli.main reads sys.argv (or an optional argv parameter) so behavior is
  identical to the console script. Do not change cli.main's signature unless needed
  for parity.
- Ensure the new module ships in the wheel (it is under src/bir, already covered by
  setuptools package discovery — verify, don't duplicate package-data).

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- `PYTHONPATH=src python -m bir --version` prints the same string as `bir --version`.
- `PYTHONPATH=src python -m bir traces` behaves identically to the console script.
- The exit code from `python -m bir` matches `cli.main`'s return value.

TESTS
Add/extend tests under tests/ in the existing style (tests/test_cli.py), e.g. a
test that runs `python -m bir --version` (or imports and invokes the module) and
asserts parity with the console-script path and the exit code.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (mention `python -m bir` alongside the `bir` command) and add a
CHANGELOG.md entry under an "Unreleased" section (create it if missing). Update
docs/SDK_RELEASE_CHECKLIST.md only if the release contract changes.

OUT OF SCOPE
Do not add CLI subcommands, change argument parsing, or alter the console-script
entry point. No new dependencies.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 2. Async streaming parity for Mistral / Cohere / LiteLLM

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.
Integrations are lazy-import and dependency-free; shared parsing lives in
src/bir/integrations/_common.py. Sync `stream=True` is already supported across
OpenAI/Anthropic/Gemini/Mistral/Cohere/LiteLLM/Bedrock/Vertex. Async streaming is
supported for OpenAI/Anthropic/Gemini via `_is_async_streamed_response`, but the
async Mistral (`trace_chat_async`), Cohere (`trace_chat_async`), and LiteLLM
(`trace_completion_async`) wrappers currently await and record one-shot only —
passing `stream=True` records the unconsumed async stream object's repr with no
usage.

TASK
Add async `stream=True` support to `trace_chat_async` (mistral and cohere) and
`trace_completion_async` (litellm), matching the existing async streaming behavior
already implemented for OpenAI/Anthropic/Gemini.

WHY
It completes async streaming coverage so applications using the async Mistral,
Cohere, and LiteLLM clients get accurate accumulated output and final token usage
instead of a useless repr, consistent with every other provider wrapper.

IMPLEMENTATION GUIDANCE
- Study how the OpenAI/Anthropic async wrappers detect and consume an async stream
  (`_is_async_streamed_response` in integrations/_common.py and the async iterator
  returned by the OpenAI wrapper) and mirror that shape exactly.
- Reuse the existing per-provider chunk parsers already used by the SYNC streaming
  path (OpenAI-shaped `choices[0].delta.content` + `usage` for Mistral/LiteLLM via
  `_chunk_delta_content`; Cohere v2 typed events `content-delta`/`message-end`). Do
  not duplicate parsing logic — factor shared bits into _common.py if needed.
- Return a lazy async iterator that yields the provider's async-stream events
  unchanged via `async for`, never buffering, and finalizes model/output/usage on
  exhaustion, `aclose()`, or a mid-stream error (re-raised unchanged, error text
  redacted). A provider that ignores streaming and returns a one-shot response must
  still record via the non-streaming path.
- Keep `bir_`-prefixed options out of the forwarded provider kwargs.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; the existing async wrapper names are unchanged (no new
  public symbol expected).

ACCEPTANCE CRITERIA
- `await trace_chat_async(..., stream=True)` (mistral, cohere) and
  `await trace_completion_async(..., stream=True)` (litellm) resolve to an async
  iterator that yields provider events unchanged.
- Once consumed, the recorded generation has the accumulated output text and the
  final token usage; the response model refines the request model when chunks carry
  one.
- A mid-stream error yields an error-status generation, re-raised unchanged with
  redacted error text; `aclose()` finalizes cleanly without buffering.
- Sync wrappers and the non-streaming async path are byte-for-byte unchanged.

TESTS
Add/extend tests under tests/ in the existing style (test_mistral_integration.py,
test_cohere_integration.py, test_litellm_integration.py), covering happy-path async
streaming, the one-shot fallback, `aclose()` early, and a mid-stream error, using
fake async-iterator clients (no real network, no provider SDK installed).

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (the streaming section currently notes the async Mistral/Cohere/
LiteLLM wrappers do not stream yet — update it) and docs/site/integrations.md, and
add a CHANGELOG.md entry under "Unreleased". Update docs/SDK_RELEASE_CHECKLIST.md
only if the release contract changes.

OUT OF SCOPE
Do not touch the sync wrappers, other providers, Bedrock/Vertex, or the callback
handlers. No new dependencies.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 3. `bir export-otel` CLI subcommand

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.
The CLI is in src/bir/cli.py (subcommands: traces, show, stats, tail, experiments,
send, send-experiment, eval-gate). An OTLP exporter already exists in Python at
`bir.integrations.otel.export_traces_to_otlp(...)`, gated behind the optional
`otel` extra (opentelemetry packages imported lazily; runtime stays dep-free).

TASK
Add a stdlib-only `bir export-otel` CLI subcommand that loads local traces and
forwards them to an OTLP endpoint by calling the existing
`export_traces_to_otlp(...)`.

WHY
The exporter is currently reachable only from Python; a CLI front-end lets users
replay local traces to an OTLP backend from the terminal with no script, matching
how every other capability (send, eval-gate) is exposed.

IMPLEMENTATION GUIDANCE
- Add an `export-otel` subparser in src/bir/cli.py following the existing add_parser
  idioms. Options: `--path` and `--include-rotated` (resolved exactly like
  `bir traces`/`bir send`), `--endpoint` (required), repeatable `--header
  KEY=VALUE`, and `--service-name`/`--timeout` passthrough.
- Implement `_cmd_export_otel` that loads with the public `load_traces(...)` and
  calls `export_traces_to_otlp(...)`. Import `bir.integrations.otel` lazily inside
  the command so the CLI keeps importing with no opentelemetry installed; when the
  extra is missing, catch the import error and print a clear message pointing to
  `pip install 'bir-sdk[otel]'`, exiting non-zero.
- Return an int exit code consistent with the other `_cmd_*` functions; print a
  short summary (number of traces exported).

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; the otel packages stay in the optional `otel` extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; this adds a CLI subcommand only — no new top-level symbol.

ACCEPTANCE CRITERIA
- `bir export-otel --endpoint http://localhost:4318/v1/traces` exports loaded traces
  via the existing exporter and prints a summary.
- With the `otel` extra NOT installed, the command exits non-zero with a clear
  "install bir-sdk[otel]" message and the rest of the CLI still imports/runs.
- `--header K=V` (repeatable), `--service-name`, `--path`, `--include-rotated`, and
  `--timeout` are honored.

TESTS
Add/extend tests under tests/ in the existing style (tests/test_cli.py), injecting a
fake exporter (e.g. patching export_traces_to_otlp or passing a fake span exporter)
so no opentelemetry install or network is required. Cover the happy path, header
parsing, malformed `--header`, and the missing-extra error path.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (the OTLP section) and docs/site/cli-env.md + docs/site/sending.md
as appropriate, and add a CHANGELOG.md entry under "Unreleased". Update
docs/SDK_RELEASE_CHECKLIST.md only if the release contract changes.

OUT OF SCOPE
Do not modify the exporter's behavior or attribute mapping, add it to the base
dependencies, or change other subcommands.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 4. MkDocs GitHub Pages deploy workflow

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Docs are a MkDocs site under
docs/site/ with mkdocs.yml at the repo root (docs_dir: docs/site). The docs
toolchain is isolated in the optional `docs` extra. CI (.github/workflows/ci.yml)
already runs `mkdocs build --strict` on every PR and push to main, but the site is
never published — `site_url` already points at https://bir-ai.github.io/bir-python/.

TASK
Add a GitHub Actions workflow that deploys the MkDocs site to GitHub Pages on push
to `main` (only after the strict build passes), without weakening the existing
strict-build gate.

WHY
The documentation is built and validated but never published, so the advertised
`site_url` 404s. A deploy workflow ships the docs users are told to read.

IMPLEMENTATION GUIDANCE
- Add .github/workflows/docs-deploy.yml triggered on push to main (and
  workflow_dispatch). Use the official Pages actions: actions/configure-pages,
  build with the `docs` extra and `mkdocs build --strict`, upload `site/` via
  actions/upload-pages-artifact, deploy via actions/deploy-pages. Set
  `permissions: pages: write, id-token: write` and a `concurrency` group so
  overlapping deploys don't race.
- Keep the existing CI strict-build job intact as the PR gate; the deploy job must
  itself run the strict build so a broken docs change can't publish.
- (Optional, only if low-risk) switch the theme to mkdocs-material by adding
  `mkdocs-material` to the `docs` extra and `theme: name: material` in mkdocs.yml;
  if you do, keep `strict: true` green and update the README docs build note. If
  this risks the strict build, leave the default `mkdocs` theme and note it.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; doc tooling stays in the optional `docs` extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; this is CI/docs only — no code or API change.

ACCEPTANCE CRITERIA
- A new workflow builds the docs with `--strict` and deploys `site/` to GitHub Pages
  on push to main, with correct `pages`/`id-token` permissions and a concurrency
  group.
- The PR-time strict build gate in ci.yml is unchanged (still blocks bad docs).
- `mkdocs build --strict` still passes locally and in CI (theme change, if any,
  included).

TESTS
No Python unit tests are expected for a workflow; if a theme/extra change is made,
keep tests/test_docs_ci.py green and run `mkdocs build --strict` locally to prove
the site builds. Note in your report that the deploy itself can only be fully
exercised on GitHub.

VERIFY (run these and report results)
- python -m pip install -e ".[docs]" && mkdocs build --strict
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md's documentation section to mention the published site URL and add a
CHANGELOG.md entry under "Unreleased". Update docs/SDK_RELEASE_CHECKLIST.md only if
the release contract changes.

OUT OF SCOPE
Do not remove or weaken the strict-build CI gate, do not add runtime dependencies,
and do not restructure the docs content/navigation beyond what a theme change
requires.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 5. `bir.testing` in-memory trace-capture helper

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py writes
line-delimited JSON to `.bir/traces.jsonl` (configurable via
`configure(trace_path=...)`); `load_events()`/`load_traces()` read it back. Public
API is re-exported from src/bir/__init__.py.

TASK
Add a small, public `bir.testing` module exposing a context manager (e.g.
`capture_traces()`) that routes trace writes to a temporary location and yields a
handle to read back the captured events/traces in-memory, so users can assert on
their own instrumentation in tests without touching their real `.bir/` directory.

WHY
Users instrumenting their apps currently have to point `configure(trace_path=...)`
at a temp file and call `load_events()` by hand in every test. A first-class,
self-cleaning helper is a high-leverage DX win that mirrors what the SDK's own tests
already do internally.

IMPLEMENTATION GUIDANCE
- Add src/bir/testing.py. Implement `capture_traces()` as a context manager that:
  swaps `_config.trace_path` to a fresh temp file (using the existing configure /
  internal config plumbing) on enter, restores the prior config on exit, and yields
  an object with methods like `.events()` and `.traces()` that call the public
  `load_events`/`load_traces` against the temp path.
- Reuse public loaders; do not reimplement JSONL parsing. Ensure capture-opt-in and
  redaction behavior are unchanged (the helper only redirects where events are
  written, never what or whether they are captured).
- Restore the prior config (including a user-set trace_path) on exit even on
  exception. Clean up the temp file/directory. Document that it mutates global
  config for its duration (like `configure`).
- Re-export `capture_traces` (and any small handle type) from `bir.testing` only;
  keep the top-level API minimal (prefer `from bir.testing import capture_traces`).

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only (stdlib `tempfile` only).
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; scope new symbols to `bir.testing` rather than the top level.

ACCEPTANCE CRITERIA
- `with capture_traces() as cap: ...; cap.events()` returns the TraceEvents written
  inside the block and nothing from outside it.
- The prior `configure(...)` state (including a user-set trace_path) is fully
  restored after the block, even on exception.
- Redaction and opt-in capture behavior are identical to writing to a real file.
- The temp file/directory is cleaned up on exit.

TESTS
Add tests/test_testing.py in the existing style covering: events captured inside the
block, isolation from prior/outer traces, config restoration after a normal exit and
after an exception, and that capture/redaction still apply.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Add a short "Testing your instrumentation" section to README.md and/or
docs/site/core-api.md, and add a CHANGELOG.md entry under "Unreleased". Update
docs/SDK_RELEASE_CHECKLIST.md only if the release contract changes.

OUT OF SCOPE
Do not add a pytest plugin or fixture auto-registration, do not change how/what is
captured, and do not alter the default trace path behavior outside the context
manager.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 6. Threaded `max_workers` for the sync experiment runner

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Evals live in src/bir/evals.py.
`run_experiment()` runs a task over a Dataset sequentially and persists per-example
results, JSONL rows, and a summary in dataset order under `.bir/experiments/`.
`run_experiment_async(max_concurrency=...)` already runs async/sync tasks
concurrently while preserving dataset order. There is no concurrency for the purely
synchronous `run_experiment()`.

TASK
Add an opt-in `max_workers` parameter to `run_experiment()` that runs examples
concurrently using a stdlib thread pool while preserving dataset-ordered results,
JSONL rows, and summary aggregates.

WHY
Sync tasks that are I/O-bound (network LLM calls behind a sync client) currently run
one example at a time; a thread pool gives a large speedup without forcing users
onto the async runner, matching the concurrency the async runner already offers.

IMPLEMENTATION GUIDANCE
- Default `max_workers=1` so existing behavior is byte-for-byte unchanged
  (sequential, single attempt). Validate it as a positive int with the existing
  `_validate_non_negative_int`-style helpers (reuse, don't invent).
- Use `concurrent.futures.ThreadPoolExecutor`; run each example's task+evaluators in
  a worker but assemble the persisted results, JSONL rows, and summary strictly in
  dataset order regardless of completion order (mirror how `run_experiment_async`
  orders output).
- Preserve identical semantics to the sequential path for redaction,
  `raise_on_error`, `record_traces` (each example must keep an isolated trace tree —
  note trace context is contextvar-based and not shared across threads by default,
  which is the desired isolation), and the persisted schema. Do not change
  `_EXPERIMENT_SCHEMA_VERSION` or the on-disk shape.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; stdlib `concurrent.futures` only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
  The experiment JSONL/summary shape and tests/fixtures/valid-experiment.json must
  stay identical.
- Keep the public API small; `max_workers` is an additive keyword argument only.

ACCEPTANCE CRITERIA
- `run_experiment(..., max_workers=N)` (N>1) executes examples concurrently but
  persists results/rows/summary in dataset order, identical to the sequential run
  for the same inputs.
- `max_workers=1` (default) is behaviorally unchanged from today.
- `raise_on_error`, `record_traces` isolation, redaction, and the persisted schema
  match the sequential path; invalid `max_workers` raises a clear error.

TESTS
Extend tests/test_evals.py in the existing style: assert order-preservation under
concurrency, equivalence of the persisted output to the sequential run, trace-tree
isolation with `record_traces=True`, `raise_on_error` behavior, and validation of
bad `max_workers`. Use a task with controlled timing/barriers to prove real
concurrency without flakiness.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (evals section) and docs/site/evals-experiments.md, and add a
CHANGELOG.md entry under "Unreleased". Update docs/SDK_RELEASE_CHECKLIST.md only if
the release contract changes.

OUT OF SCOPE
Do not change `run_experiment_async`, the evaluator APIs, the experiment schema, or
the comparison/gate code.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 7. Instructor integration

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Optional integrations live in
src/bir/integrations/ and are lazy-import and dependency-free: the user passes the
provider callable/client in, `bir_`-prefixed kwargs are stripped, shared parsing is
in src/bir/integrations/_common.py, and each public symbol is re-exported from
src/bir/integrations/__init__.py. Existing wrappers (openai, anthropic, litellm,
etc.) follow `trace_<verb>(callable, /, *args, bir_name=..., bir_metadata=...,
bir_capture_input=..., bir_capture_output=..., **kwargs)` returning the provider
result unchanged and recording one generation inside the active trace.

TASK
Add a dependency-free Instructor integration that wraps an Instructor-patched
client's structured-output create call (e.g. `client.chat.completions.create`
returning a Pydantic model or `(model, completion)`), recording one generation with
model and token usage without importing `instructor`.

WHY
Instructor is a very common structured-output layer over OpenAI-compatible clients;
a wrapper lets those users trace structured calls with the same one-line pattern as
every other provider, with zero added dependency.

IMPLEMENTATION GUIDANCE
- Add src/bir/integrations/instructor.py with `trace_create(create, /, *args,
  bir_name="instructor.create", bir_metadata=..., bir_capture_input=...,
  bir_capture_output=..., **kwargs)` mirroring the openai wrapper's structure.
- Read model from `kwargs["model"]` (refined from the response when available) and
  token usage from the OpenAI-shaped usage block; Instructor can return either the
  parsed model or `create_with_completion`-style `(model, raw_completion)` — handle
  both by reading usage from the raw completion when present, and serialize the
  parsed model via `_response_output` (`model_dump`/`dict` fallback). Reuse
  `_common.py` helpers (`_value`, `_usage_tokens`, `_string_or_none`,
  `_response_output`); add shared helpers there only if genuinely reused.
- Provide an async counterpart `trace_create_async` if the async create coroutine
  shape is straightforward; otherwise scope to sync and note it. Never import
  instructor/openai. Re-export the public symbol(s) from integrations/__init__.py.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from src/bir/integrations/__init__.py.

ACCEPTANCE CRITERIA
- `trace_create(create, model="gpt-4o-mini", response_model=...)` returns the
  provider result unchanged and records one generation with the model and token
  usage when present.
- Works inside an active trace; raises the same "requires an active trace" error as
  other wrappers when used outside one.
- Both the parsed-model and `(model, completion)` return shapes record usage
  correctly; `bir_`-prefixed options are never forwarded to `create`.
- `instructor` is never imported.

TESTS
Add tests/test_instructor_integration.py in the existing integration-test style with
a fake `create` callable returning both shapes (parsed model and model+completion),
covering happy path, missing usage, capture opt-in, redaction, and the
no-active-trace error. No real network or instructor install.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update docs/site/integrations.md and README.md's integration list, and add a
CHANGELOG.md entry under "Unreleased". Update docs/SDK_RELEASE_CHECKLIST.md only if
the release contract changes. Add the new module to scripts/verify_release.py's
import smoke list if integration modules are enumerated there.

OUT OF SCOPE
Do not modify other integrations, _common.py beyond genuinely shared helpers, or the
core SDK.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 8. Pydantic AI integration

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Optional integrations live in
src/bir/integrations/ and are lazy-import and dependency-free. Two patterns exist:
call wrappers (`trace_<verb>(callable, ...)`) and event-bridge handlers that map a
framework's own callbacks/events into Bir traces without importing the framework
(e.g. BirCallbackHandler for LangChain, BirLlamaIndexHandler for LlamaIndex,
BirAgentsTracingProcessor for the OpenAI Agents SDK). Shared parsing is in
_common.py; public symbols are re-exported from integrations/__init__.py.

TASK
Add a dependency-free Pydantic AI integration that maps a Pydantic AI agent run into
a Bir trace — turning model requests into generations (with model + token usage) and
tool calls into tool_call events — without importing `pydantic_ai`.

WHY
Pydantic AI is a popular typed-agent framework; a dependency-free bridge lets its
users get Bir traces with the same opt-in capture and redaction guarantees as the
other agent integrations.

IMPLEMENTATION GUIDANCE
- Pick the lowest-coupling hook Pydantic AI exposes for instrumentation (its
  event/usage stream or an instrumentation/processor interface) and implement a
  handler class (e.g. `BirPydanticAIHandler`) mirroring the structure of
  BirAgentsTracingProcessor: open a Bir trace per run, map model spans to
  generations and tool spans to tool_calls, mark failures as errors, and track
  active spans by the framework's own id so concurrent/nested runs stay isolated.
- Read model and token usage from the framework's usage objects via duck-typed
  access (reuse `_common.py` helpers); never import pydantic_ai. Respect opt-in
  capture, overridable per handler with `capture_inputs`/`capture_outputs`.
- Re-export `BirPydanticAIHandler` from integrations/__init__.py. If Pydantic AI's
  current API does not offer a clean dependency-free seam, document the exact hook
  you used and keep the surface minimal.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from src/bir/integrations/__init__.py.

ACCEPTANCE CRITERIA
- A simulated Pydantic AI run drives the handler to produce one Bir trace whose model
  events are generations (model + usage when present) and tool events are tool_calls,
  with failures recorded as errors.
- Concurrent/nested runs stay isolated by framework id; capture follows the opt-in
  settings and is overridable per handler.
- `pydantic_ai` is never imported.

TESTS
Add tests/test_pydantic_ai_integration.py in the existing bridge-handler test style
(see test_openai_agents_integration.py / test_langchain_integration.py), feeding
synthetic framework events, asserting the produced Bir events, isolation, error
mapping, capture opt-in, and redaction. No real framework install.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update docs/site/integrations.md and README.md's agent-framework section, and add a
CHANGELOG.md entry under "Unreleased". Update docs/SDK_RELEASE_CHECKLIST.md only if
the release contract changes. Add the module to scripts/verify_release.py's import
smoke list if integration modules are enumerated there.

OUT OF SCOPE
Do not modify other integrations or the core SDK; do not add pydantic_ai as a
dependency.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 9. DSPy integration

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Optional integrations live in
src/bir/integrations/ and are lazy-import and dependency-free. Patterns: call
wrappers (`trace_<verb>(callable, ...)`) and event-bridge handlers (LangChain,
LlamaIndex, OpenAI Agents). Shared parsing is in _common.py; public symbols are
re-exported from integrations/__init__.py.

TASK
Add a dependency-free DSPy integration that records DSPy language-model calls as Bir
generations (model + token usage) without importing `dspy`.

WHY
DSPy is a widely used program-of-thought / prompt-optimization framework; tracing
its underlying LM calls with the standard opt-in capture and redaction lets DSPy
users see real cost/latency without a new dependency.

IMPLEMENTATION GUIDANCE
- Choose the cleanest dependency-free seam: either a `trace_lm(lm_call, /, *args,
  bir_name="dspy.lm", ...)` wrapper around a DSPy LM `__call__`/`request`, or a
  callback/inspection hook DSPy exposes for LM calls. Mirror the existing wrapper
  signature (`bir_`-prefixed options stripped, provider result returned unchanged,
  one generation recorded inside the active trace).
- Read model and OpenAI-shaped token usage via `_common.py` helpers; serialize the
  output with `_response_output`. Never import dspy. Provide an async variant only if
  the call shape is clearly async.
- Re-export the public symbol(s) from integrations/__init__.py. Document exactly
  which DSPy surface you wrap.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from src/bir/integrations/__init__.py.

ACCEPTANCE CRITERIA
- Wrapping a DSPy LM call records one generation with model and token usage when
  present and returns the provider result unchanged.
- Works only inside an active trace (same error as other wrappers otherwise);
  `bir_`-prefixed options are never forwarded.
- `dspy` is never imported.

TESTS
Add tests/test_dspy_integration.py in the existing integration-test style with a fake
LM call, covering happy path, missing usage, capture opt-in, redaction, and the
no-active-trace error. No real network or dspy install.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update docs/site/integrations.md and README.md's integration list, and add a
CHANGELOG.md entry under "Unreleased". Update docs/SDK_RELEASE_CHECKLIST.md only if
the release contract changes. Add the module to scripts/verify_release.py's import
smoke list if integration modules are enumerated there.

OUT OF SCOPE
Do not modify other integrations or the core SDK; do not add dspy as a dependency.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 10. CrewAI integration

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Optional integrations live in
src/bir/integrations/ and are lazy-import and dependency-free. Event-bridge handlers
(BirCallbackHandler for LangChain, BirLlamaIndexHandler for LlamaIndex,
BirAgentsTracingProcessor for OpenAI Agents) map a framework's own callbacks into Bir
traces without importing the framework. Shared parsing is in _common.py; public
symbols are re-exported from integrations/__init__.py.

TASK
Add a dependency-free CrewAI integration that maps a CrewAI crew/agent run into a Bir
trace — agent/task steps as spans, LLM calls as generations, tool usage as
tool_calls — without importing `crewai`.

WHY
CrewAI is a popular multi-agent orchestration framework; a dependency-free bridge
gives CrewAI users Bir traces with the same opt-in capture and redaction guarantees
as the other agent integrations.

IMPLEMENTATION GUIDANCE
- Use CrewAI's step/callback or event hooks (e.g. step_callback / task_callback or
  its event bus) as the dependency-free seam. Implement a handler (e.g.
  `BirCrewAIHandler`) mirroring BirAgentsTracingProcessor: open one Bir trace per
  crew run, map LLM events to generations (model + usage), tool events to tool_calls,
  and agent/task steps to spans; record failures as errors; track active nodes by the
  framework's own ids so concurrent/nested runs stay isolated.
- Read model/usage via duck-typed access and `_common.py` helpers; never import
  crewai. Respect opt-in capture, overridable per handler with
  `capture_inputs`/`capture_outputs`. Re-export `BirCrewAIHandler` from
  integrations/__init__.py. Document the exact hook used.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from src/bir/integrations/__init__.py.

ACCEPTANCE CRITERIA
- A simulated CrewAI run drives the handler to produce one Bir trace with LLM events
  as generations, tool events as tool_calls, and steps as spans; failures recorded as
  errors.
- Concurrent/nested runs stay isolated by framework id; capture follows opt-in
  settings and is overridable per handler.
- `crewai` is never imported.

TESTS
Add tests/test_crewai_integration.py in the existing bridge-handler test style,
feeding synthetic framework events and asserting the produced Bir events, isolation,
error mapping, capture opt-in, and redaction. No real framework install.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update docs/site/integrations.md and README.md's agent-framework section, and add a
CHANGELOG.md entry under "Unreleased". Update docs/SDK_RELEASE_CHECKLIST.md only if
the release contract changes. Add the module to scripts/verify_release.py's import
smoke list if integration modules are enumerated there.

OUT OF SCOPE
Do not modify other integrations or the core SDK; do not add crewai as a dependency.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 11. Expanded redaction (Stripe / Azure / PEM private-key blocks) — CROSS-REPO

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Best-effort capture redaction lives
in src/bir/_sdk.py (`_redact_text` and the secret-key rules), already covering
labeled secrets, bearer tokens, OpenAI `sk-`, JWTs, AWS AKIA/ASIA, GCP `AIza`, Slack
`xox*`, and GitHub `gh*_`. Users can widen redaction with
`configure(additional_secret_keys=..., additional_redaction_patterns=...)`. The
redaction rules are a SHARED CONTRACT with the separate `bir-app` server repo:
tests/fixtures/redaction-cases.json (guarded by tests/test_redaction_parity.py and
scripts/fixtures.py check) must match an independently maintained redactor in
bir-app.

TASK
Add built-in best-effort redaction for additional common credential formats — Stripe
secret/restricted keys (`sk_live_`/`sk_test_`/`rk_live_`/`rk_test_`), Azure-style
keys, and PEM private-key blocks (`-----BEGIN ... PRIVATE KEY-----` ...
`-----END ... PRIVATE KEY-----`) — without weakening any existing rule.

WHY
These are high-value secrets that frequently appear in captured payloads and are not
yet covered; adding them strengthens the default privacy posture.

IMPLEMENTATION GUIDANCE
- THIS IS A CROSS-REPO CONTRACT CHANGE. Loudly flag it: the new patterns and any new
  entries in tests/fixtures/redaction-cases.json must be mirrored by bir-app's
  independent redactor and its copy of the fixture BEFORE or WITH this change. Do not
  let the SDK ship ahead of the server. Confirm the coordination plan in your report
  and add an explicit CROSS-REPO note in the CHANGELOG entry (mirroring how the 0.2.0
  redaction expansion was documented).
- Add the new `re.sub` rules in `_redact_text` after the existing built-in patterns
  and before the user-supplied `additional_redaction_patterns` loop, each replacing
  its whole match with the `[redacted]` marker. Anchor them tightly to avoid
  over-redaction (PEM block spanning multiple lines via a non-greedy, DOTALL-scoped
  match; Stripe/Azure with word boundaries and length classes like the existing
  AKIA/AIza rules).
- Update tests/fixtures/redaction-cases.json with positive and negative cases and
  regenerate the checksum manifest with scripts/fixtures.py (use whatever regenerate
  command the repo provides; run `scripts/fixtures.py check` to confirm). Built-in
  rules must remain impossible to disable.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; stdlib `re` only.
- Capture stays opt-in; NEVER weaken redaction — only widen. tests/test_redaction_parity.py
  must stay green.
- This task DOES change tests/fixtures/redaction-cases.json (and its checksum), which
  is a shared contract with the bir-app repo — call this out loudly and ensure
  parity; do NOT change schema_version "1.0" or any other fixture.
- Keep the public API small; no new public symbol (built-in rules only).

ACCEPTANCE CRITERIA
- Stripe (`sk_live_`/`sk_test_`/`rk_live_`/`rk_test_`), Azure-style keys, and PEM
  private-key blocks are redacted to `[redacted]` in captured strings, repr
  fallbacks, error text, prompt/score metadata, and integration payloads.
- No existing redaction case regresses; no over-redaction of benign text in the
  negative fixture cases.
- tests/test_redaction_parity.py and scripts/fixtures.py check both pass with the
  updated fixture + manifest.

TESTS
Extend tests/test_custom_redaction.py / the redaction unit tests with positive and
negative cases for each new format (including a multi-line PEM block and a
benign-string negative case), plus the parity test against the updated fixture.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- python scripts/fixtures.py check
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (capture/privacy section) and docs/site/capture-privacy.md, and add
a CHANGELOG.md entry under "Unreleased" with a prominent CROSS-REPO CONTRACT note
(mirroring the 0.2.0 Security entry). Update docs/SDK_RELEASE_CHECKLIST.md if the
redaction contract is part of the release gate.

OUT OF SCOPE
Do not refactor the existing redaction rules, do not change the user-supplied
redaction config behavior, and do not touch unrelated fixtures or the event schema.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing. Coordinate the cross-repo fixture change with the bir-app maintainers.
```

### Also-considered (deferred)

- **Haystack integration** — same dependency-free bridge pattern as #8/#10; defer
  behind the higher-traffic frameworks.
- **Opt-in LLM-judge evaluator** — deliberately excluded: it would require a
  network/provider call, conflicting with the "first useful workflow never requires a
  server / deterministic, local-first evals" invariants. Only revisit as a clearly
  opt-in, provider-callable-injected evaluator that never runs by default.
- **Cross-process / W3C trace-context propagation** — the accessors are intentionally
  read-only with no setter; adding propagation is a larger design change, not a quick
  win.
