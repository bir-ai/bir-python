# Bir Python SDK — Improvement Roadmap

> Generated analysis (audited **2026-06-28**). Not committed automatically. Each
> Phase-4 prompt is a standalone Claude Code task a fresh session can paste in and
> run end to end. **Re-verify every claim against the current code before relying
> on it** — the repo evolves fast. The previous roadmap (2026-06-24) has since been
> fully shipped, so this is a fresh pass over what is *now* genuinely missing.

## Phase 2 — Assessment

### What already exists (excluded from this roadmap)

The repo is well past MVP and the prior roadmap is **entirely implemented**. The
following are done and intentionally not re-proposed:

- **Packaging/typing** — PEP 561 `py.typed` ships via `[tool.setuptools.package-data]`
  plus the `Typing :: Typed` classifier; `python -m bir` entry point
  (`src/bir/__main__.py`); console script `bir`; release verification builds a
  reproducible wheel, checks RECORD, and smoke-installs into a clean venv.
- **Env-var config** — `BIR_TRACE_PATH`, `BIR_CAPTURE_INPUTS`, `BIR_CAPTURE_OUTPUTS`,
  `BIR_SAMPLE_RATE`, `BIR_SERVICE_NAME`, `BIR_ENVIRONMENT`, `BIR_SOURCE`.
- **CLI** — `traces`, `show`, `stats`, `tail`, `experiments`, `experiment-show`,
  `send`, `send-experiment`, `eval-gate`, `export-otel`, `--version`.
- **Sampling** — global `sample_rate` **and** exact-name `sample_rules` overrides.
- **Persistence** — size rotation (`max_bytes`/`backup_count`), `include_rotated`
  on loaders/`send`, in-process + advisory cross-process file locks (`flock`/`msvcrt`),
  opt-in `mark_sent` sidecar.
- **Sending** — bounded retry/backoff on `send_events` and `send_experiment`.
- **Cost** — opt-in `configure(model_prices=...)` derives generation cost from usage.
- **Capture/redaction** — opt-in capture; built-in redaction for labeled secrets,
  bearer, `sk-`, JWT, AWS AKIA/ASIA, GCP `AIza`, Slack `xox*`, GitHub `gh*_`,
  Stripe `sk_/rk_live/test`, Azure 88-char keys, PEM private-key blocks; user-supplied
  `additional_secret_keys` / `additional_redaction_patterns`; capture **depth** cap.
- **Integrations** — openai (chat + responses), anthropic, google (genai), mistral,
  cohere, litellm, langchain, llamaindex, bedrock, vertexai, openai_agents,
  pydantic_ai, instructor, dspy, crewai, haystack, plus the OTLP exporter (`[otel]`).
- **Streaming** — sync `stream=True` across OpenAI/Anthropic/Gemini/Mistral/Cohere/
  LiteLLM/Bedrock/Vertex; async streaming for OpenAI/Anthropic/Gemini/Mistral/Cohere/
  LiteLLM.
- **Evals** — full deterministic evaluator set (incl. RAG heuristics),
  `run_experiment(max_workers=...)`, `run_experiment_async(max_concurrency=...)`,
  `compare_experiments()`/`ExperimentDiff`/`bir eval-gate` (aggregate, per-evaluator
  tolerances, `missing_score` policy), `bir.testing.capture_traces()`.
- **Correlation** — `get_current_trace_id()`/`get_current_span_id()`, `bir.logging`
  filter, `set_metadata()`/`set_model()` on context managers, `@observe(metadata=)`.
- **Docs/CI** — strict MkDocs build + GitHub Pages deploy; CI matrix on Python
  3.10–3.13; shared-fixture drift guard (`scripts/fixtures.py check`).

### Conventions / idioms the code follows

- Frozen dataclasses for config and public records (`_Config`, `TraceEvent`,
  `LoadedTrace`, `PromptRecord`, eval result types).
- Centralized `_validate_*` helpers called from `configure()` and constructors;
  caps on user-supplied tables (`_MAX_*`).
- JSONL writing is lock-serialized; deterministic `json.dumps(..., sort_keys=True,
  separators=(",", ":"), allow_nan=False)`; `schema_version` `"1.0"`.
- Redaction runs on **every** persistence path via `_safe_capture` /
  `_redact_secret_text`; built-ins can never be disabled, user rules only widen.
- Integrations are lazy-import, dependency-free: the user passes the callable/client,
  `bir_`-prefixed kwargs are stripped, shared parsing lives in `integrations/_common.py`,
  public symbols re-exported from `integrations/__init__.py`. Two shapes: call
  wrappers (`trace_<verb>`) and event-bridge handlers (`Bir*Handler`/`Tracer`/`Processor`).
- Tests mix `unittest` + `pytest`; CLI tested via `cli.main([...])` with captured
  stdout/stderr and stubbed network; integrations tested with fake clients/events
  (no provider SDKs, no network).

### Genuinely missing / improvable (this roadmap)

A capture-payload **size** cap (only depth is bounded today); **CLI/library parity**
gaps (`bir send` hides `mark_sent`/`retries`/`backoff`/`timeout`); **untested
cross-platform locking** (CI is ubuntu-only despite a Windows `msvcrt` path);
remaining **async streaming** for Bedrock/Vertex; **per-example** regression detail
in experiment comparison; a local **experiment report** artifact; `bir traces`
**filtering**; an **API-reference** docs page; one more **agent integration**
(AutoGen/AG2); and a **flagged cross-repo** PAN/credit-card redaction rule.

## Phase 3 — Prioritized improvements

| # | Improvement | Category | Priority | Size | Risk | Depends on |
|---|-------------|----------|----------|------|------|------------|
| 1 | Expose `mark_sent`/`retries`/`backoff`/`timeout` on `bir send` | dx | P0 | S | low | — |
| 2 | Bounded capture size (`max_value_length`/`max_collection_items`) | core | P1 | M | med | — |
| 3 | Cross-platform CI matrix (Windows/macOS) to exercise file locking | ci/release | P1 | M | med | — |
| 4 | Finish async streaming: Bedrock `trace_converse_stream_async` + Vertex async `stream=True` | integrations | P1 | M | low | — |
| 5 | Per-example regression detail in `compare_experiments`/`bir eval-gate` | evals | P1 | M | low-med | — |
| 6 | Local experiment report (`bir experiment-report` → HTML/Markdown) | evals/dx | P1 | M | low | — |
| 7 | `bir traces` filtering (`--name`/`--status`/`--since`/`--until`) | dx | P2 | S-M | low | — |
| 8 | mkdocstrings API-reference page in the docs site | docs | P2 | M | low | — |
| 9 | AutoGen (AG2) dependency-free integration bridge | integrations | P2 | M | low | — |
| 10 | PAN / credit-card (Luhn) redaction — **CROSS-REPO** | core/security | P2 | M | **med** | bir-app |

**Trade-off flags.** #10 changes the **shared redaction contract**
(`tests/fixtures/redaction-cases.json`, guarded by `tests/test_redaction_parity.py`
and `scripts/fixtures.py check`), which the separate `bir-app` server maintains an
independent copy of — do not ship the SDK side ahead of the server; it is the one
coordinated cross-repo change here. #2 must redact **before** truncating so a secret
is never half-cut past the redactor, and defaults to unlimited so existing capture-on
behavior is byte-for-byte unchanged. #3 may surface real Windows bugs (that is the
point) — budget for fixing the lock path. Everything else is additive and stays
inside the invariants.

---

## Phase 4 — Standalone prompts

### 1. Expose send resilience/bookkeeping options on `bir send`

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.
The CLI is in src/bir/cli.py. The library `send_events(server_url, *, path, timeout,
retries=2, backoff=0.5, mark_sent=False, include_rotated=False)` already supports
retry/backoff, an opt-in "mark sent" sidecar, and a timeout — but `bir send`
(`_cmd_send`) only forwards `--server`, `--path`, and `--include-rotated`. By
contrast `bir send-experiment` already exposes `--retries`/`--backoff`.

TASK
Add `--mark-sent`, `--retries`, `--backoff`, and `--timeout` options to the
`bir send` subcommand and forward them to `send_events(...)`.

WHY
The library already supports these, but they are unreachable from the CLI, an
inconsistency with `bir send-experiment`; exposing them lets terminal users get
cheap idempotent re-sends and tune transient-failure handling without a script.

IMPLEMENTATION GUIDANCE
- In `_build_parser`, extend the existing `send` subparser: add a `--mark-sent`
  store_true flag, `--retries` (reuse the `_non_negative_int` argparse type, default
  2), `--backoff` (reuse `_non_negative_float`, default 0.5), and `--timeout` (reuse
  `_non_negative_float`; default None so the library's 10.0 applies). Mirror the help
  text wording already used on `send-experiment`.
- In `_cmd_send`, forward the new values to `send_events(...)`. Only pass `timeout`
  when provided (or pass the library default) so behavior is unchanged when omitted.
- Keep the printed summary line (`accepted=... attempted=... skipped=...`) intact.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; this is CLI wiring only — no new top-level symbol.

ACCEPTANCE CRITERIA
- `bir send --mark-sent` records accepted IDs in the `<trace_path>.sent` sidecar and
  skips them on a second run (attempted reflects only not-yet-sent events).
- `--retries`, `--backoff`, and `--timeout` are parsed, validated (non-negative), and
  forwarded to `send_events(...)`.
- `bir send` with no new flags behaves exactly as before.

TESTS
Extend tests/test_cli.py in the existing style (stub the network like the current
send tests): assert mark-sent skips on the second send, that retries/backoff/timeout
are forwarded (patch send_events or the HTTP layer and inspect the call), and that a
negative value is rejected with a non-zero exit.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (the sending section) and docs/site/sending.md + docs/site/cli-env.md
to document the new flags, and add a CHANGELOG.md entry under an "Unreleased" section
(create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the release
contract changes.

OUT OF SCOPE
Do not change `send_events`/`send_experiment` behavior, other subcommands, or the
sidecar format. No new dependencies.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 2. Bounded capture size

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.
When capture is enabled, `_safe_capture()` in src/bir/_sdk.py redacts and serializes
captured inputs/outputs/metadata. It caps recursion DEPTH (`_MAX_CAPTURE_DEPTH = 6`
-> `[max_depth]`) but does NOT cap the LENGTH of captured strings or the SIZE of
captured lists/dicts, so a single large captured value (a base64 image, a megabyte
of model output) is written whole into `.bir/traces.jsonl`.

TASK
Add opt-in capture-size limits — `configure(max_value_length=..., max_collection_items=...)`
— that truncate over-long captured strings and over-large captured collections during
`_safe_capture`, leaving redaction and the event schema intact.

WHY
Capture-on traces can grow unbounded and bloat the local store with a single huge
payload; a bounded capture keeps `.bir/traces.jsonl` manageable and reduces the
chance of persisting large unredactable blobs, complementing the existing depth cap
and `max_bytes` rotation.

IMPLEMENTATION GUIDANCE
- Add two fields to the frozen `_Config` (default `None` = unlimited, so behavior is
  byte-for-byte unchanged unless opted in): `max_value_length: int | None` and
  `max_collection_items: int | None`. Add matching keyword args to `configure()` and
  validate with the existing `_validate_non_negative_int`-style helpers (reuse, do not
  invent). Consider matching `BIR_MAX_VALUE_LENGTH` / `BIR_MAX_COLLECTION_ITEMS`
  env-var defaults in `_config_from_env` for parity with the other settings.
- In `_safe_capture`, after redacting a string, if `max_value_length` is set and the
  result exceeds it, truncate and append a clear marker (mirror the `[max_depth]`
  idiom, e.g. a `…[truncated]` suffix). CRITICAL ORDERING: redact FIRST, then
  truncate, so a secret is never cut in a way that defeats the redactor.
- For lists/tuples/sets and mappings, when `max_collection_items` is set and the
  collection is larger, keep the first N items and record a small marker for the
  remainder (e.g. an extra string element / a sentinel key) so the truncation is
  visible but the output stays valid JSON.
- Keep the result JSON-serializable with the existing `allow_nan=False` rules; do not
  change `_event(...)` or any field names. Apply uniformly so every capture path
  (inputs, outputs, metadata, repr fallbacks, eval/dataset capture via evals.py
  `_safe_capture`) is bounded the same way.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; stdlib only.
- Capture stays opt-in; NEVER weaken redaction — redact before truncating and keep
  tests/test_redaction_parity.py green.
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
  (Truncation changes only captured VALUES, not the schema or field set.)
- Keep the public API small; these are additive `configure()` keywords only — no new
  top-level symbol. Default None must leave current output unchanged.

ACCEPTANCE CRITERIA
- With `configure(max_value_length=N)`, a captured string longer than N is truncated
  with a visible marker; shorter strings are unchanged; a secret embedded in a long
  string is still fully redacted (redaction wins over truncation).
- With `configure(max_collection_items=M)`, a captured list/dict larger than M is
  truncated to M items plus a remainder marker and stays valid JSON.
- With neither set (default), captured output is byte-for-byte identical to today.
- Invalid values raise a clear `ValueError`/`TypeError` at `configure()` time.

TESTS
Extend tests/test_sdk.py (and tests/test_custom_redaction.py for the redact-then-
truncate ordering) in the existing style: long-string truncation, large-collection
truncation, the no-config default is unchanged, secret-in-long-string still redacted,
nested/deep interaction with the depth cap, and validation of bad limits. If env-var
defaults are added, cover them like the other BIR_* tests.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (capture & privacy section) and docs/site/capture-privacy.md
(+ docs/site/cli-env.md if env vars are added), and add a CHANGELOG.md entry under
"Unreleased". Update docs/SDK_RELEASE_CHECKLIST.md only if the release contract changes.

OUT OF SCOPE
Do not change the depth cap, redaction rules, the event schema, or rotation. Do not
truncate non-captured fields (names, models, ids).

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 3. Cross-platform CI matrix to exercise file locking

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, tests in
tests/, CI in .github/workflows/ci.yml. The SDK serializes local writes with an
advisory cross-process lock (`_InterProcessFileLock` in src/bir/_sdk.py) that uses
`fcntl.flock` on POSIX and `msvcrt.locking` on Windows. The CI `sdk` job runs a
Python 3.10–3.13 matrix but ONLY on `runs-on: ubuntu-latest`, so the Windows
`msvcrt` lock branch (and any path separator / temp-file behavior differences) is
never exercised in CI.

TASK
Extend the CI test matrix to also run the SDK unit tests on Windows (and optionally
macOS) so the cross-platform locking and persistence code paths are actually tested.

WHY
The README advertises Windows support via byte-range locking, but that code never
runs in CI; adding `windows-latest` exercises the `msvcrt` lock path and surfaces
platform regressions before release instead of in users' environments.

IMPLEMENTATION GUIDANCE
- In .github/workflows/ci.yml, change the `sdk` job to a matrix over `os`
  (`ubuntu-latest`, `windows-latest`, and optionally `macos-latest`) crossed with the
  existing Python versions, keeping `fail-fast: false`. Use `runs-on: ${{ matrix.os }}`.
- The unit-test and example-smoke steps use `PYTHONPATH=src ...`, which is not valid
  shell on Windows `cmd`/`powershell`. Make these steps cross-platform: either set
  `PYTHONPATH` via `env:` on the step, or use `actions/setup-python` + a shell that
  honors it (e.g. `shell: bash` works on the GitHub Windows runner). Pick the
  lowest-churn option and apply it consistently.
- Keep the interpreter-pinned, OS-pinned single runs of `pyright` and
  `python scripts/verify_release.py` on `ubuntu-latest` + Python 3.12 only (guard with
  `if: matrix.os == 'ubuntu-latest' && matrix.python-version == '3.12'`) to avoid
  redundant work; verify_release builds/installs a wheel and need not run per-OS.
- If the Windows run reveals a genuine bug in the lock/temp-file/rotation code, fix it
  in src/bir/_sdk.py with a focused change and a regression test; do not paper over it
  by skipping Windows.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; this is CI (and possibly a focused lock fix) only.

ACCEPTANCE CRITERIA
- CI runs the SDK unit tests and example smoke tests on Windows (and macOS if added)
  across the supported Python versions, with `fail-fast: false`.
- pyright and verify_release still run exactly once on the canonical ubuntu/3.12 combo.
- All matrix legs pass (any real Windows lock/persistence bug uncovered is fixed in
  src/bir/_sdk.py with a regression test).

TESTS
The change is primarily CI config; locally run the unit suite (which already covers
concurrent writes/rotation) to confirm no regressions. If a Windows-specific fix is
made, add a unit test in tests/test_sdk.py that targets the corrected behavior in an
OS-agnostic way. Note in your report that the Windows leg can only be fully exercised
on GitHub's runners.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Add a CHANGELOG.md entry under "Unreleased" noting the expanded CI matrix (and any
lock fix). Update docs/SDK_RELEASE_CHECKLIST.md only if the release/verification
contract changes. README's persistence/concurrency section already claims Windows
support — adjust only if the behavior changes.

OUT OF SCOPE
Do not add runtime dependencies, do not change the locking design, and do not run
pyright/verify_release across every OS (keep them pinned to one leg).

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 4. Finish async streaming for Bedrock and Vertex AI

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Optional integrations live in
src/bir/integrations/ and are lazy-import and dependency-free; shared parsing is in
src/bir/integrations/_common.py (`_is_async_streamed_response`, `_value`,
`_usage_tokens`, `_string_or_none`, `_response_output`). Sync streaming exists for
all providers. Async streaming exists for OpenAI/Anthropic/Gemini/Mistral/Cohere/
LiteLLM. The two remaining gaps are AWS Bedrock and Vertex AI: bedrock.py has
`trace_converse` (sync), `trace_converse_async` (async non-streaming), and
`trace_converse_stream` (sync stream) but NO async Converse-stream; vertexai.py has
`trace_generate_content` (sync, supports `stream=True`) and
`trace_generate_content_async` (async non-streaming only).

TASK
Add `trace_converse_stream_async` to bir.integrations.bedrock and async `stream=True`
support to the Vertex `trace_generate_content_async`, mirroring the existing async
streaming wrappers, to complete async streaming coverage across every provider.

WHY
Applications using async AWS Bedrock (Converse stream) or async Vertex clients
currently cannot stream through Bir with accurate accumulated output and final token
usage; this closes the last documented async-streaming gap consistently with the
other providers.

IMPLEMENTATION GUIDANCE
- Study the async streaming wrappers in src/bir/integrations/openai.py
  (`_stream_chat_completion_async`) and the existing SYNC Bedrock/Vertex stream
  recorders (`trace_converse_stream` in bedrock.py and the `stream=True` path in
  vertexai.py) and reuse their exact accumulation logic — Bedrock Converse events
  (`contentBlockDelta.delta.text`, `messageStop` stop reason, terminal `metadata`
  with `inputTokens`/`outputTokens`/`totalTokens`) and Vertex chunks (`text` / first
  candidate text parts, `model_version`, final `usage_metadata`).
- Bedrock: add `async def trace_converse_stream_async(converse_stream, /, *args,
  bir_name=..., bir_metadata=..., bir_capture_input=..., bir_capture_output=...,
  **kwargs)` that awaits the provider call, detects an async stream with
  `_is_async_streamed_response`, and returns a lazy async iterator yielding the
  stream's events unchanged via `async for` while finalizing model/output/usage on
  exhaustion, `aclose()`, or a mid-stream error (re-raised unchanged, error text
  redacted). A provider that returns a one-shot response still records via the
  non-streaming path.
- Vertex: extend `trace_generate_content_async` so that with `stream=True` it resolves
  to the same kind of lazy async iterator (await the coroutine, branch on
  `_is_async_streamed_response`). Keep the non-streaming async path byte-for-byte
  unchanged.
- Strip `bir_`-prefixed options from forwarded provider kwargs. Re-export any new
  public symbol (`trace_converse_stream_async`) from src/bir/integrations/__init__.py
  and add it to scripts/verify_release.py's REQUIRED_PACKAGE_FILES coverage only if a
  new module is introduced (it is not — same files).

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; never import boto3/aioboto3 or vertexai.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from src/bir/integrations/__init__.py.
  Sync wrappers and the async non-streaming paths stay byte-for-byte unchanged.

ACCEPTANCE CRITERIA
- `await trace_converse_stream_async(...)` and
  `await trace_generate_content_async(..., stream=True)` resolve to async iterators
  that yield provider events unchanged.
- Once consumed, the recorded generation has the accumulated output text and the final
  token usage; the model is refined from the stream when present.
- A mid-stream error yields an error-status generation re-raised unchanged with
  redacted error text; `aclose()` finalizes cleanly without buffering; a non-streaming
  response still records via the one-shot path.
- Neither provider SDK is imported; `bir_`-prefixed options are never forwarded.

TESTS
Extend tests/test_bedrock_integration.py and tests/test_vertexai_integration.py in
the existing style with fake async-iterator clients (no boto3/vertexai, no network):
happy-path async streaming, the one-shot fallback, `aclose()` early, and a mid-stream
error.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (the streaming section currently states the Bedrock/Vertex streaming
surfaces stay synchronous — update it) and docs/site/integrations.md, and add a
CHANGELOG.md entry under "Unreleased". Update docs/SDK_RELEASE_CHECKLIST.md only if
the release contract changes.

OUT OF SCOPE
Do not touch other providers, the sync wrappers, or the callback handlers. No new
dependencies.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 5. Per-example regression detail in experiment comparison

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Evals live in src/bir/evals.py.
`compare_experiments(baseline, candidate, *, tolerance, score_tolerances,
missing_score)` returns an `ExperimentDiff` comparing AGGREGATE (mean) evaluator
scores between two runs, and `bir eval-gate` (src/bir/cli.py `_cmd_eval_gate`) exits
non-zero on regression and prints `diff.to_dict()`. Today the diff is aggregate-only:
it reports which evaluators regressed but not WHICH dataset examples drove the change.

TASK
Add opt-in per-example regression detail to the experiment comparison — surfacing,
per shared evaluator, which example_ids dropped between baseline and candidate — via
an additive field on `ExperimentDiff` and a `bir eval-gate` flag to include it.

WHY
When a gate fails, users need to know which examples regressed to debug; aggregate
means hide that, forcing a manual JSONL diff. Per-example detail makes the local
eval-gate workflow actionable without a server.

IMPLEMENTATION GUIDANCE
- `ExperimentResult` already exposes per-example `results` with `example_id` and
  `scores`. Add an opt-in parameter to `compare_experiments(..., per_example=False)`
  that, when True, computes per-(evaluator, example_id) deltas for examples present in
  both runs and records them on an ADDITIVE `ExperimentDiff` field (e.g.
  `example_deltas: dict[str, dict[str, float]]` keyed by evaluator then example_id,
  defaulting to empty so existing callers and `to_dict()` consumers are unaffected).
- Keep `ExperimentDiff` frozen and deterministic (sorted keys), matching the existing
  `effective_tolerances`/`regression_reasons` style; include the new field in
  `to_dict()` only when populated (or always as `{}`), but do not reorder or rename
  existing keys. Do not change `has_regressions` semantics — this is reporting detail,
  not a new gate trigger (unless you add a clearly separate, documented, opt-in flag).
- In src/bir/cli.py add a `--per-example` flag to `eval-gate` that passes
  `per_example=True`; keep the default output identical when the flag is absent.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; stdlib only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0", _EXPERIMENT_SCHEMA_VERSION, or tests/fixtures/*
  (shared contract with the bir-app repo) unless the task explicitly is a schema change
  — if so, call it out loudly. The persisted experiment JSONL/summary shape must be
  unchanged; this only adds a field to the in-memory/serialized DIFF.
- Keep the public API small; `per_example` is an additive keyword and the new
  `ExperimentDiff` field is additive with a safe default.

ACCEPTANCE CRITERIA
- `compare_experiments(..., per_example=True)` populates per-(evaluator, example_id)
  deltas for examples shared by both runs; `per_example=False` (default) leaves the
  diff identical to today.
- `bir eval-gate --per-example baseline.jsonl candidate.jsonl` includes the per-example
  detail in its JSON output; without the flag the output is unchanged.
- `to_dict()` stays deterministic; existing keys/values are unchanged; the gate
  exit-code semantics are unchanged.

TESTS
Extend tests/test_evals.py and tests/test_cli.py in the existing style: per-example
deltas for shared examples, examples present in only one run handled gracefully,
default-off equivalence to current output, deterministic serialization, and the new
CLI flag.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (eval-gate section) and docs/site/evals-experiments.md, and add a
CHANGELOG.md entry under "Unreleased". Update docs/SDK_RELEASE_CHECKLIST.md only if
the release contract changes.

OUT OF SCOPE
Do not change aggregate comparison semantics, the experiment on-disk schema, the
evaluator APIs, or the default gate behavior.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 6. Local experiment report (HTML/Markdown)

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Evals live in src/bir/evals.py and
persist per-example results (`.bir/experiments/<name>-<id>.jsonl`) plus a summary
(`.summary.json`). The CLI (src/bir/cli.py) can list (`bir experiments`) and show
(`bir experiment-show`) experiments in the terminal, and `load_experiment` /
`list_experiments` are public loaders. There is no way to produce a shareable,
self-contained report artifact (HTML/Markdown) from a local experiment.

TASK
Add a `bir experiment-report <experiment-id>` CLI command (and a small public helper
in evals.py) that renders one persisted experiment to a self-contained, stdlib-only
HTML (or Markdown) file: the summary, per-evaluator aggregates, and the per-example
table of statuses and scores.

WHY
Local-first users want to share or archive an experiment result without standing up
the server/dashboard; a single self-contained file is the natural artifact, matching
the SDK's "first useful workflow needs no server" invariant.

IMPLEMENTATION GUIDANCE
- Add a public function in src/bir/evals.py (e.g. `render_experiment_report(result,
  *, format="html") -> str`) that takes an `ExperimentResult` (and its
  `ExperimentSummary` aggregates) and returns a string. Build HTML with the standard
  library only (`html.escape`, plain string templating) — no Jinja, no new dep. Offer
  a `markdown` format too if cheap. Reuse the existing aggregate/score formatting
  idioms from cli.py (`_format_scores`) where sensible; do not duplicate redaction —
  the persisted values are already redacted.
- Add a `bir experiment-report` subparser in src/bir/cli.py mirroring
  `experiment-show` (`<experiment-id>`, `--dir`, plus `--format {html,markdown}` and
  `--output PATH`; default writes to stdout or a derived filename). Resolve the
  experiment via `list_experiments`/`load_experiment` exactly like `_cmd_experiment_show`,
  including the unknown-id non-zero exit.
- Keep output deterministic (sorted evaluator/example ordering) so reports are stable.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; stdlib only (no templating/markdown libraries).
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green) —
  render only already-persisted (already-redacted) values.
- Do NOT change schema_version "1.0", _EXPERIMENT_SCHEMA_VERSION, or tests/fixtures/*
  (shared contract with the bir-app repo) unless the task explicitly is a schema change
  — if so, call it out loudly.
- Keep the public API small; expose at most one new helper in bir.evals and one CLI
  subcommand. Re-export from bir.evals' __all__ if it becomes public.

ACCEPTANCE CRITERIA
- `bir experiment-report <id>` writes a self-contained file (no external assets) with
  the summary, evaluator aggregates, and per-example rows; `--format markdown` and
  `--output` work; an unknown id exits non-zero and prints nothing to stdout.
- `render_experiment_report(...)` returns a deterministic string for a given experiment
  and escapes user-derived text (no HTML injection from example data).
- No runtime dependency is added; values are rendered as already persisted.

TESTS
Add coverage in tests/test_cli.py and tests/test_evals.py in the existing style: a
report renders the expected sections, output is deterministic, HTML-escaping of
example text, markdown format, `--output` writes a file, and the unknown-id exit path.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (evals section) and docs/site/evals-experiments.md + docs/site/cli-env.md,
and add a CHANGELOG.md entry under "Unreleased". Update docs/SDK_RELEASE_CHECKLIST.md
only if the release contract changes.

OUT OF SCOPE
Do not add chart/JS dependencies, do not change the persisted experiment format, and
do not add server upload behavior.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 7. `bir traces` filtering

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. The CLI is in src/bir/cli.py.
`bir traces` lists local traces (newest first) with `--path`, `--limit`,
`--include-rotated`, and `--json`, reading via the public `load_traces(...)`. There
is no way to filter the listing by trace name, status, or time window — users must
eyeball the table or post-process the `--json` output.

TASK
Add filtering options to `bir traces` — `--name` (substring or exact match),
`--status {success,error}`, and `--since`/`--until` (ISO timestamps) — applied after
loading and before `--limit`.

WHY
As local trace files grow, scanning every trace is tedious; simple server-free
filtering makes the CLI usable for "show me failed checkout traces from today"
without piping through external tools.

IMPLEMENTATION GUIDANCE
- Extend the `traces` subparser in `_build_parser` with `--name` (match against
  `LoadedTrace.name`; document whether it is substring or exact and keep it simple and
  case-sensitive unless you add a clearly-named flag), `--status` (choices
  `success`/`error`, matched against `LoadedTrace.status`), and `--since`/`--until`
  (ISO datetime strings; parse with `datetime.fromisoformat` and compare against
  `LoadedTrace.start_time`). Reject malformed timestamps with a clear argparse error.
- Apply filters in `_cmd_traces` after `load_traces(...)` (and the existing reverse)
  and before `--limit`, so `--json` and the table both honor them. An empty result
  prints the existing "No traces found" message (table) or `[]` (json).
- Keep ordering and all existing flags unchanged; filters are purely additive.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; stdlib only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; CLI-only — no new top-level symbol or loader change
  (filter in the command, not in `load_traces`).

ACCEPTANCE CRITERIA
- `bir traces --name X`, `--status error`, and `--since/--until` filter the listing in
  both table and `--json` modes; combined filters AND together.
- `--limit` is applied after filtering; malformed `--since/--until` exits non-zero with
  a clear message; no filters reproduces today's output exactly.

TESTS
Extend tests/test_cli.py in the existing style with traces produced via the public API,
covering each filter, combined filters, the interaction with `--limit`, empty results,
and a malformed timestamp.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (CLI mention) and docs/site/cli-env.md, and add a CHANGELOG.md entry
under "Unreleased". Update docs/SDK_RELEASE_CHECKLIST.md only if the release contract
changes.

OUT OF SCOPE
Do not change `load_traces`, other subcommands, or the table/JSON shapes beyond
filtering the rows. No new dependencies.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 8. mkdocstrings API-reference page

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Docs are a MkDocs site under
docs/site/ with mkdocs.yml at the repo root (`docs_dir: docs/site`, `theme: mkdocs`,
`strict: true`), built in CI and deployed to GitHub Pages. The docs toolchain is
isolated in the optional `docs` extra in pyproject.toml. All API docs are currently
hand-written prose; there is no generated API reference, even though the public
symbols in src/bir/__init__.py, src/bir/evals.py, and src/bir/integrations/ carry
thorough docstrings.

TASK
Add a generated API-reference page to the docs site using mkdocstrings (Python
handler), wired into the nav and the optional `docs` extra, without weakening the
strict build.

WHY
The codebase has excellent docstrings that never reach the published site; an
auto-generated reference keeps the API docs in sync with the code for free and
complements the existing hand-written guides.

IMPLEMENTATION GUIDANCE
- Add `mkdocstrings[python]` to the `docs` extra in pyproject.toml (docs tooling only;
  the runtime install stays dependency-free). Enable the plugin in mkdocs.yml
  (`plugins: [search, mkdocstrings]`) — note adding a `plugins:` list means you must
  keep `search` explicitly since it is otherwise the implicit default.
- Add docs/site/api-reference.md that uses mkdocstrings `:::` identifier blocks for the
  public surface: the top-level `bir` symbols (observe, trace, span, generation,
  tool_call, retrieval, score, prompt, configure, load_events, load_traces,
  send_events, get_current_trace_id, get_current_span_id, and the public dataclasses),
  `bir.evals`, `bir.testing`, and `bir.logging`. Add the page to the `nav:` in
  mkdocs.yml. mkdocstrings imports the package to read signatures, so ensure the build
  can import `bir` (install `-e .` or set PYTHONPATH in the docs build/CI step as
  needed).
- Keep `strict: true` green: fix any missing-docstring/reference warnings the strict
  build raises rather than disabling strict. Keep the existing hand-written pages.
- If CI's docs job or .github/workflows/docs-deploy.yml needs the package importable,
  adjust the install step (e.g. `pip install -e ".[docs]"`) — verify the strict build
  passes in both the PR gate and the deploy workflow.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; mkdocstrings stays in the optional `docs` extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; this is docs only — no code or API change (do not add
  docstrings-as-API or new exports just to populate the page).

ACCEPTANCE CRITERIA
- `pip install -e ".[docs]" && mkdocs build --strict` succeeds locally and produces an
  API-reference page rendering the public symbols from their docstrings.
- The page is in the nav; the existing guides and strict build gate are intact; the
  CI docs job and the Pages deploy still pass the strict build.
- The runtime install remains dependency-free (mkdocstrings only under `[docs]`).

TESTS
No Python unit tests are expected for docs; keep tests/test_docs_ci.py green (update it
if it asserts on nav/plugins) and prove the site builds with `mkdocs build --strict`.
Report that the deploy itself is only fully exercised on GitHub.

VERIFY (run these and report results)
- python -m pip install -e ".[docs]" && mkdocs build --strict
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
This task IS docs; ensure the new page and nav are coherent and add a CHANGELOG.md
entry under "Unreleased". Update README.md's documentation section to mention the API
reference. Update docs/SDK_RELEASE_CHECKLIST.md only if the release contract changes.

OUT OF SCOPE
Do not switch themes, restructure the existing guides, or add runtime dependencies.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 9. AutoGen (AG2) integration bridge

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Optional integrations live in
src/bir/integrations/ and are lazy-import and dependency-free. The event-bridge
pattern (BirCallbackHandler for LangChain, BirLlamaIndexHandler for LlamaIndex,
BirAgentsTracingProcessor for OpenAI Agents, BirPydanticAIHandler for Pydantic AI,
BirCrewAIHandler for CrewAI, BirHaystackTracer for Haystack) maps a framework's own
events into Bir traces WITHOUT importing the framework. Shared parsing is in
_common.py; public symbols are re-exported from integrations/__init__.py. AutoGen /
AG2 (multi-agent conversations) is not yet covered.

TASK
Add a dependency-free AutoGen (AG2) integration that maps an AutoGen multi-agent run
into a Bir trace — agent turns/conversations as spans, LLM calls as generations
(model + token usage), and tool/function executions as tool_calls — without importing
`autogen`/`ag2`.

WHY
AutoGen/AG2 is a widely used multi-agent framework with no Bir coverage; a
dependency-free bridge gives its users Bir traces with the same opt-in capture and
redaction guarantees as the other agent integrations.

IMPLEMENTATION GUIDANCE
- First, identify AutoGen/AG2's lowest-coupling, dependency-free observability seam
  (e.g. its runtime/event hooks, message/usage callbacks, or its OpenTelemetry
  instrumentation if that is the cleanest — mirroring how BirPydanticAIHandler hooks
  OTel without importing pydantic_ai). Document the exact seam you chose in the module
  docstring; do not import the framework.
- Implement a handler class (e.g. `BirAutoGenHandler`) mirroring the structure of
  BirCrewAIHandler/BirAgentsTracingProcessor: open one Bir trace per run, classify
  events by duck-typed fields (tolerant across versions), map LLM events to
  generations (model + usage via `_common.py` helpers), tool/function events to
  tool_calls, and agent/turn boundaries to spans; record failures as errors; track
  active nodes by the framework's own ids so concurrent/nested runs stay isolated.
- Respect opt-in capture, overridable per handler with `capture_inputs`/`capture_outputs`,
  exactly like the other bridges. Re-export `BirAutoGenHandler` from
  integrations/__init__.py and add src/bir/integrations/autogen.py to
  scripts/verify_release.py's REQUIRED_PACKAGE_FILES so release verification imports it.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; never import autogen/ag2/openai/opentelemetry at module top
  level (defer any optional import the same way the existing bridges do).
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from src/bir/integrations/__init__.py.

ACCEPTANCE CRITERIA
- A simulated AutoGen run drives the handler to produce one Bir trace whose LLM events
  are generations (model + usage when present), tool/function events are tool_calls,
  and agent turns are spans, with failures recorded as errors.
- Concurrent/nested runs stay isolated by framework id; capture follows the opt-in
  settings and is overridable per handler.
- `autogen`/`ag2` is never imported; the module imports cleanly with the framework
  absent (verified by release verification).

TESTS
Add tests/test_autogen_integration.py in the existing bridge-handler test style (see
tests/test_crewai_integration.py / tests/test_pydantic_ai_integration.py), feeding
synthetic framework events and asserting the produced Bir events, isolation, error
mapping, capture opt-in, and redaction. No real framework install, no network.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md's agent-framework section and docs/site/integrations.md, and add a
CHANGELOG.md entry under "Unreleased". Update docs/SDK_RELEASE_CHECKLIST.md only if
the release contract changes.

OUT OF SCOPE
Do not modify other integrations or the core SDK; do not add autogen/ag2 as a
dependency. If AutoGen offers no clean dependency-free seam, document why and propose
the minimal alternative rather than importing the framework.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 10. PAN / credit-card (Luhn) redaction — CROSS-REPO

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Best-effort capture redaction lives
in src/bir/_sdk.py (`_redact_secret_text` plus the secret-key rules), already covering
labeled secrets, bearer tokens, OpenAI `sk-`, JWTs, AWS AKIA/ASIA, GCP `AIza`, Slack
`xox*`, GitHub `gh*_`, Stripe `sk_/rk_live/test`, Azure 88-char keys, and PEM
private-key blocks. Users can widen redaction with
`configure(additional_secret_keys=..., additional_redaction_patterns=...)`. These
rules are a SHARED CONTRACT with the separate `bir-app` server repo:
tests/fixtures/redaction-cases.json (guarded by tests/test_redaction_parity.py and
`python scripts/fixtures.py check`) must match an independently maintained redactor in
bir-app, and tests/fixtures/CHECKSUMS.sha256 pins all shared fixtures. PANs (credit-
card numbers) are a high-value secret not yet covered.

TASK
Add a built-in best-effort redaction rule for credit-card / PAN numbers (13–19 digit
sequences, with optional spaces/hyphens, validated by the Luhn checksum to avoid
over-redacting arbitrary digit strings), without weakening any existing rule.

WHY
PANs frequently appear in captured payloads and are a high-impact leak; Luhn-checked
redaction adds meaningful default protection while keeping false positives low.

IMPLEMENTATION GUIDANCE
- THIS IS A CROSS-REPO CONTRACT CHANGE. Loudly flag it: the new pattern and any new
  entries in tests/fixtures/redaction-cases.json MUST be mirrored by bir-app's
  independent redactor and its copy of the fixture BEFORE or WITH this change. Do not
  let the SDK ship ahead of the server. State the coordination plan in your report and
  add a prominent "CROSS-REPO CONTRACT" note in the CHANGELOG entry, mirroring how the
  0.2.0 and the Stripe/Azure/PEM Security entries were documented.
- Implement in `_redact_secret_text` AFTER the existing built-in patterns and BEFORE
  the user-supplied `additional_redaction_patterns` loop. Match candidate digit groups
  (allowing single spaces/hyphens between groups), strip separators, accept length
  13–19, and replace with the `[redacted]` marker ONLY when the Luhn check passes. A
  pure regex cannot do Luhn, so use a regex to find candidates and a small Luhn helper
  to gate replacement (e.g. via `re.sub` with a function). Anchor with boundaries so
  you do not consume surrounding text, and ensure a non-PAN long digit run (e.g. a
  19-digit id failing Luhn) is left intact.
- Update tests/fixtures/redaction-cases.json with positive cases (valid PANs, with and
  without separators) and negative cases (Luhn-failing digit runs, phone numbers,
  ordinary long integers), then regenerate the manifest with `python scripts/fixtures.py
  sync` (or the repo's documented regenerate flow — see tests/fixtures/README.md) and
  confirm `python scripts/fixtures.py check` passes. Never hand-edit CHECKSUMS.sha256.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; stdlib `re` only.
- Capture stays opt-in; NEVER weaken redaction — only widen. tests/test_redaction_parity.py
  must stay green and no existing case may regress.
- This task DOES change tests/fixtures/redaction-cases.json and its checksum, which is a
  shared contract with the bir-app repo — call it out loudly and ensure parity; do NOT
  change schema_version "1.0", event-schema-v1.json, valid-events.jsonl, or
  valid-experiment.json.
- Keep the public API small; built-in rule only — no new public symbol.

ACCEPTANCE CRITERIA
- Valid Luhn PANs (13–19 digits, with/without spaces or hyphens) are redacted to
  `[redacted]` in captured strings, repr fallbacks, error text, prompt/score metadata,
  and integration payloads.
- Luhn-failing digit runs, phone numbers, and ordinary long integers in the negative
  fixtures are NOT redacted; no existing redaction case regresses.
- tests/test_redaction_parity.py and `python scripts/fixtures.py check` both pass with
  the updated fixture + regenerated manifest.

TESTS
Extend tests/test_custom_redaction.py / the redaction unit tests with positive and
negative PAN cases (separated and unseparated valid cards; Luhn-failing and benign
negatives), plus the parity test against the updated fixture.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- python scripts/fixtures.py check
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (capture & privacy section) and docs/site/capture-privacy.md, and add
a CHANGELOG.md entry under "Unreleased" with a prominent CROSS-REPO CONTRACT note
(mirroring the 0.2.0 Security entry). Update docs/SDK_RELEASE_CHECKLIST.md if the
redaction contract section needs the new rule named.

OUT OF SCOPE
Do not refactor existing redaction rules, do not change the user-supplied redaction
config behavior, and do not touch unrelated fixtures or the event schema.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing. Coordinate the cross-repo fixture change with the bir-app maintainers.
```

### Also-considered (deferred)

- **Cross-process / W3C trace-context propagation** — `get_current_trace_id/span_id()`
  are intentionally read-only with no setter; adding inbound/outbound propagation is a
  larger design change, not a single-session win. Revisit deliberately.
- **Opt-in LLM-judge evaluator** — deliberately excluded: it requires a network/provider
  call, conflicting with the deterministic, local-first, server-free evals invariant.
  Only revisit as a clearly opt-in, provider-callable-injected evaluator that never runs
  by default.
- **Buffered / async trace writer** — every event currently writes under a lock; a
  batching writer could help very high-throughput apps but adds flush/ordering/durability
  complexity and risk. Defer unless a real throughput need appears.
- **`bir prune` / local-store housekeeping** — handy but destructive; needs careful
  confirmation/age-based selection. Lower priority than the items above.
- **ruff lint/format CI gate** — reasonable additive `[lint]` extra + CI job, but likely
  introduces one-time style churn and imposes tooling opinions; defer unless desired.
- **More integrations (LangGraph, Smolagents, Semantic Kernel, Ollama-native)** — the
  bridge pattern scales to these, but LangGraph rides LangChain's existing callbacks and
  Ollama is reachable via the OpenAI-compatible/litellm wrappers, so marginal value is
  lower than AutoGen. Add on demand.
```
