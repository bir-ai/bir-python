# Bir Python SDK — Improvement Roadmap

> Generated analysis (audited **2026-06-29**). Not committed automatically. Each
> Phase-4 prompt is a standalone Claude Code task a fresh session can paste in and
> run end to end. **Re-verify every claim against the current code before relying
> on it** — the repo evolves fast. The previous roadmap (2026-06-28) has since been
> **fully shipped** (all 10 items landed: `bir send` resilience flags, capture-size
> limits, cross-platform CI, Bedrock/Vertex async streaming, per-example regression
> detail, `experiment-report`, `bir traces` filters, the API-reference page,
> AutoGen, and PAN/Luhn redaction), so this is a fresh pass over what is *now*
> genuinely missing.

## Phase 2 — Assessment

### What already exists (excluded from this roadmap)

The repo is well past MVP; the entire prior roadmap is implemented. The following
are done and intentionally not re-proposed:

- **Packaging / typing** — PEP 561 `py.typed` ships via
  `[tool.setuptools.package-data]` plus the `Typing :: Typed` classifier;
  `python -m bir` entry point (`src/bir/__main__.py`); `bir` console script;
  `scripts/verify_release.py` builds a reproducible **wheel** offline, checks
  RECORD hashes, asserts forbidden local paths are excluded, smoke-installs into a
  clean venv, and verifies the console script.
- **Env-var config** — `BIR_TRACE_PATH`, `BIR_CAPTURE_INPUTS`,
  `BIR_CAPTURE_OUTPUTS`, `BIR_DISABLED`, `BIR_SAMPLE_RATE`, `BIR_SERVICE_NAME`,
  `BIR_ENVIRONMENT`, `BIR_SOURCE`, `BIR_MAX_VALUE_LENGTH`,
  `BIR_MAX_COLLECTION_ITEMS`.
- **CLI** — `traces` (with `--name/--status/--since/--until`), `show`, `stats`,
  `tail`, `experiments`, `experiment-show`, `experiment-report`, `send` (with
  `--mark-sent/--retries/--backoff/--timeout`), `send-experiment`, `eval-gate`,
  `export-otel`, `--version`.
- **Sampling** — global `sample_rate` **and** exact-name `sample_rules` overrides,
  plus a master `enabled` kill switch (`BIR_DISABLED`) that turns all recording off.
- **Persistence** — size rotation (`max_bytes`/`backup_count`), `include_rotated`
  on loaders / `send`, in-process + advisory cross-process file locks
  (POSIX `flock` / Windows `msvcrt`), opt-in `mark_sent` sidecar.
- **Sending** — bounded retry/backoff on `send_events()` and `send_experiment()`.
- **Cost** — opt-in `configure(model_prices=...)` derives generation cost from usage.
- **Capture / redaction** — opt-in capture; built-in redaction for labeled
  secrets, bearer, `sk-`, JWT, AWS AKIA/ASIA, GCP `AIza`, Slack `xox*`, GitHub
  `gh*_`, Stripe `sk_/rk_live/test`, Azure 88-char keys, PEM private-key blocks,
  Luhn-checked credit-card/PAN numbers; user-supplied `additional_secret_keys` /
  `additional_redaction_patterns`; capture **depth** cap and opt-in **size** caps.
- **Integrations** — openai (chat + responses), anthropic, google (genai),
  mistral, cohere, litellm, langchain, llamaindex, bedrock, vertexai,
  openai_agents, pydantic_ai, instructor, dspy, crewai, haystack, autogen, plus the
  OTLP exporter (`[otel]`).
- **Streaming** — sync `stream=True` across OpenAI/Anthropic/Gemini/Mistral/
  Cohere/LiteLLM/Bedrock/Vertex; **async streaming complete** across all of them
  (Bedrock `trace_converse_stream_async`, Vertex async `stream=True`).
- **Evals** — full deterministic evaluator set (incl. RAG heuristics),
  `run_experiment(max_workers=...)`, `run_experiment_async(max_concurrency=...)`,
  `compare_experiments()`/`ExperimentDiff`/`bir eval-gate` (aggregate +
  per-evaluator tolerances + `missing_score` + opt-in per-example deltas),
  `render_experiment_report()`, `bir.testing.capture_traces()`.
- **Correlation** — `get_current_trace_id()`/`get_current_span_id()`, the
  `bir.logging` filter, `set_metadata()`/`set_model()` on context managers,
  `@observe(metadata=)`.
- **Docs / CI** — strict MkDocs build + GitHub Pages deploy; generated
  mkdocstrings API reference; CI matrix of `{ubuntu, windows, macos} × {3.10–3.13}`;
  shared-fixture drift guard (`scripts/fixtures.py check`).

### Conventions / idioms the code follows

- **Frozen dataclasses** for config and public records (`_Config`, `TraceEvent`,
  `LoadedTrace`, `PromptRecord`, `EvalResult`, the experiment result types). User
  tables are stored as validated, sorted tuples so `_Config` stays hashable.
- Centralized **`_validate_*` helpers** called from `configure()` and constructors;
  hard caps on user-supplied tables (`_MAX_*`).
- JSONL writing is **lock-serialized**; deterministic
  `json.dumps(..., sort_keys=True, separators=(",", ":"), allow_nan=False)`;
  `schema_version` is `"1.0"`.
- **Redaction runs on every persistence path** via `_safe_capture` /
  `_redact_secret_text`; built-ins can never be disabled, user rules only widen,
  truncation runs **after** redaction.
- **Integrations are lazy-import, dependency-free**: the caller passes the
  callable/client, `bir_`-prefixed kwargs are stripped, shared response parsing
  lives in `integrations/_common.py`, public symbols are re-exported from
  `integrations/__init__.py`. Two shapes: call wrappers (`trace_<verb>` +
  `_async`) and event-bridge handlers (`Bir*Handler`/`Tracer`/`Processor`).
- Tests mix **`unittest` + `pytest`**; CLI tested via `cli.main([...])` with
  captured stdout/stderr and stubbed network; integrations tested with fake
  clients/events (no provider SDKs, no network).

### Genuinely missing / improvable (this roadmap)

After excluding everything above, the real gaps are: the release gate verifies the
**wheel but never the sdist**; `bir stats` lacks the **time/name/status filters**
`bir traces` already has; there is **no master on/off switch** for tracing (only
`sample_rate=0`); there is **no native Ollama wrapper** despite the local-first
ethos and an existing demo; the local store has **no manual cleanup** path
(`bir prune`); there is **no fuzzy-similarity evaluator** between `exact_match` and
`contains`; there is **no way to print the effective resolved config**
(`bir config`); there is **no `SECURITY.md`** documenting the redaction guarantees
for a privacy-positioned SDK; the **OTLP exporter drops `environment`/`source`**
(only `service.name` is mapped); and the experiment runners have **no per-example
timeout** to bound a hanging task.

## Phase 3 — Prioritized improvements

| # | Improvement | Category | Priority | Size | Risk | Depends on |
|---|-------------|----------|----------|------|------|------------|
| 1 | Verify the **sdist** (build + contents + install) in `scripts/verify_release.py` | ci/release | P0 | M | med | — |
| 2 | `bir stats` filter parity (`--name`/`--status`/`--since`/`--until`) | dx | P1 | S | low | — |
| 3 | Master tracing kill-switch: `configure(enabled=False)` + `BIR_DISABLED` | core | P1 | M | low-med | — |
| 4 | Native **Ollama** integration (`trace_chat`/`trace_generate` + async) | integrations | P1 | M | low | — |
| 5 | `bir prune` to clean the local trace store (`--before`/`--keep-last`/`--status`/`--dry-run`) | dx | P1 | M | med | — |
| 6 | Fuzzy-similarity deterministic evaluator (`similarity_above`, stdlib `difflib`) | evals | P1 | S-M | low | — |
| 7 | `bir config` — print effective resolved configuration | dx | P2 | S | low | — |
| 8 | `SECURITY.md` — redaction guarantees + vulnerability disclosure | docs | P2 | S | low | — |
| 9 | OTLP exporter: map `environment`/`source` to resource attributes + `gen_ai.system` | integrations | P2 | S-M | low | — |
| 10 | Per-example timeout for `run_experiment` / `run_experiment_async` | evals | P2 | M | low-med | — |

**Trade-off flags.**

- **#1** must stay **hermetic** like the existing wheel check (no network during
  CI). `python -m build --sdist` normally creates an isolated build env that fetches
  setuptools; use `--no-isolation` (the dev extra already provides setuptools) or
  inspect a manually-assembled sdist. The point is to catch an sdist that omits
  `py.typed`/`LICENSE`/`README` or leaks `.bir/`, `build/`, `site/`.
- **#3** threads a new state through the write path; keep it **strictly additive**
  and ensure that when disabled the decorators/context managers still run the user's
  code and still raise — they just write nothing (same contract as `sample_rate=0`).
- **#5** is the only item that **mutates** the local store, so it carries the most
  risk: rewrite under the same advisory lock used for appends, operate on whole
  traces (never split one), default to a **dry run or require an explicit flag**,
  and never touch already-redacted content. Everything else is purely additive.
- None of these change `schema_version` `"1.0"` or `tests/fixtures/*`; none add a
  runtime dependency.

---

## Phase 4 — Standalone prompts

### 1. Verify the sdist in the release gate

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.
The release gate is scripts/verify_release.py; it runs in CI (.github/workflows/ci.yml
on the ubuntu/3.12 leg) and in .github/workflows/release.yml. Today it builds a
pure-Python WHEEL offline, checks its RECORD hashes, asserts no forbidden local paths
are present, smoke-installs it into a clean venv, and verifies the `bir` console
script. It never builds or inspects the source distribution (sdist); the only sdist
coverage anywhere is `twine check dist/*` in release.yml, which validates metadata,
not contents.

TASK
Extend scripts/verify_release.py to also build the sdist (.tar.gz), verify its
contents, and confirm it installs into a clean virtual environment.

WHY
A broken sdist (missing py.typed/LICENSE/README, or leaking .bir/, build/, or site/)
would publish to PyPI undetected because only the wheel is verified today; many
downstream installs and mirrors build from the sdist.

IMPLEMENTATION GUIDANCE
- Add a `build_sdist(...)` step beside the existing `build_wheel(...)`. Keep it
  HERMETIC like the wheel build (CI runs offline): prefer `python -m build --sdist
  --no-isolation` (the `dev` extra / CI interpreter already provides setuptools), and
  if that is unavailable fall back to assembling/inspecting the tarball directly.
- Add an `inspect_sdist(sdist)` mirroring `inspect_wheel`: assert the tarball
  CONTAINS the package sources under `src/bir/` (including `bir/py.typed`),
  `pyproject.toml`, `LICENSE`, and `README.md`; assert it EXCLUDES forbidden
  local/generated paths (reuse the same forbidden-path notion the wheel check uses:
  `.bir/`, `build/`, `site/`, `__pycache__`, `.venv`, `tests/` if the wheel excludes
  them, etc.). Assert the distribution name/version in the sdist match pyproject.
- Add an install smoke test: `pip install --no-index <sdist>` into a fresh venv and
  import `bir`, `bir.evals`, `bir.cli`, and the integration modules (reuse the
  existing smoke-test helper / module list rather than duplicating it).
- Keep the existing wheel verification unchanged; run both in `main()`.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — it is not here.
- Keep the public API small; re-export new public symbols from the right __init__.py.
- The check MUST remain offline/hermetic so CI does not gain a network dependency.

ACCEPTANCE CRITERIA
- `python scripts/verify_release.py` builds and verifies BOTH a wheel and an sdist,
  prints clear `==> sdist build / inspect / install` progress, and exits 0 on a healthy tree.
- Deliberately breaking the sdist (e.g. temporarily excluding py.typed or including a
  `.bir/` file) makes the script exit non-zero with a clear message.
- The sdist installs into a clean venv and the smoke imports succeed.
- The CI ubuntu/3.12 leg and release.yml still pass; no network access is required.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest). If
tests/test_packaging.py exists, add cases there asserting the sdist-content
expectations (present files, excluded files) using the same approach as the wheel
tests; otherwise add a focused test module. Cover the happy path and at least one
"missing required file" / "forbidden path present" failure path.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md's release-verification paragraph to say the gate now verifies the
sdist as well as the wheel. Add a CHANGELOG.md entry under "Unreleased" (create the
section if missing). Update docs/SDK_RELEASE_CHECKLIST.md to mention the sdist check.

OUT OF SCOPE
- Do not change pyproject packaging config unless a real sdist defect is found.
- Do not change the wheel build/inspection logic beyond light refactor for reuse.
- Do not add new runtime dependencies or change release.yml's publish flow.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you
commit, do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off
main before committing.
```

### 2. `bir stats` filter parity

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.
The CLI is in src/bir/cli.py. The `bir traces` subcommand already supports `--name`
(case-sensitive substring of the trace name), `--status {success,error}`, and
`--since`/`--until` (inclusive ISO-8601 bounds on trace start time, naive values
treated as UTC), implemented with argparse types `_iso_datetime` and the trace
filtering helpers in cli.py. The `bir stats` subcommand (`_cmd_stats`) aggregates the
same loaded traces but exposes only `--path`, `--include-rotated`, and `--json`.

TASK
Add `--name`, `--status`, `--since`, and `--until` filters to the `bir stats`
subcommand, with the exact same semantics as `bir traces`, so stats can be computed
over a filtered subset of traces.

WHY
Operators frequently want "stats for errors only" or "token/cost usage since
yesterday"; the filters already exist for `bir traces` and reusing them on `stats`
removes an inconsistency without new concepts.

IMPLEMENTATION GUIDANCE
- In `_build_parser`, add the four options to the `stats` subparser, copying the
  argument definitions, `choices`, `metavar`, argparse types (`_iso_datetime`), and
  help text verbatim from the `traces` subparser so behavior matches exactly.
- Factor the trace-filtering logic `_cmd_traces` already uses into a small shared
  helper (e.g. `_filter_traces(traces, args)`) if not already shared, and call it
  from both `_cmd_traces` and `_cmd_stats` so the AND-combination and ordering stay
  identical. Filters must run BEFORE aggregation.
- An empty filtered set must still exit 0 with zeroed counts (current empty-store
  behavior). `--json` output reflects the filtered figures.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — it is not here.
- Keep the public API small; re-export new public symbols from the right __init__.py.
- `load_traces` and the trace schema are untouched; this is CLI-only.

ACCEPTANCE CRITERIA
- `bir stats --status error`, `bir stats --name foo`, and
  `bir stats --since 2026-01-01 --until 2026-02-01` aggregate only matching traces in
  both the table and `--json`.
- Filters combine with AND and match `bir traces` semantics exactly (case-sensitive
  name substring; naive ISO treated as UTC; malformed timestamp exits non-zero).
- `bir stats` with no filters is byte-for-byte unchanged from today.

TESTS
Add/extend tests in tests/test_cli.py in the existing style (call `cli.main([...])`,
capture stdout). Cover each filter, AND-combination, an empty filtered result (exit 0,
zeroed), a malformed `--since` (non-zero exit), and the no-filter regression.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md and docs/site/cli-env.md where `bir stats` is documented to list
the new filters. Add a CHANGELOG.md entry under "Unreleased" (create it if missing).

OUT OF SCOPE
- Do not add filters to other subcommands or change `bir traces`.
- Do not change the stats math, output columns, or JSON keys (only the input set).

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you
commit, do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off
main before committing.
```

### 3. Master tracing kill-switch

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.
`configure(...)` mutates a frozen `_Config` dataclass; it already supports
`sample_rate` (a trace recorded with some probability; when sampled out the function
still runs and still raises but nothing is written) and reads BIR_* env defaults once
at import via the env-config helpers near the bottom of _sdk.py. There is currently NO
explicit on/off switch — disabling tracing requires `sample_rate=0.0`, which is
implicit and still rolls a sampling decision.

TASK
Add an explicit master tracing switch: a boolean `enabled` field on `_Config`, an
`enabled` keyword on `configure(...)`, and a `BIR_DISABLED` (and/or `BIR_ENABLED`)
environment default, so callers can turn ALL recording off cleanly while user code
still runs unchanged.

WHY
Production deployments routinely need a single, intent-revealing kill-switch (feature
flag, incident toggle, tests) rather than the implicit `sample_rate=0.0` workaround;
it is the cleanest way to make Bir a true no-op without touching call sites.

IMPLEMENTATION GUIDANCE
- Add `enabled: bool = True` to the frozen `_Config` dataclass (default True keeps
  current behavior).
- Add `enabled: bool | None = None` to `configure(...)`, validated as a bool, set into
  the config like the other optional fields.
- Add env support in the import-time env-config builder next to BIR_SAMPLE_RATE etc.:
  honor `BIR_DISABLED` (truthy disables) using the existing `_parse_env_bool` helper;
  if you also add `BIR_ENABLED`, document precedence clearly. Explicit
  `configure(enabled=...)` wins over the env default, mirroring the other knobs.
- Enforce it at the SAME chokepoint that sampling uses so the contract matches
  exactly: when disabled, `@observe`, `trace()`, `span()`, `generation()`,
  `tool_call()`, `retrieval()`, and `score()` run the user's body and propagate
  exceptions but write NOTHING (reuse/extend the existing "trace dropped" path —
  e.g. `_should_drop_trace` / the dropped-context contextvar — rather than adding a
  parallel mechanism). Disabling must also short-circuit any write attempt.
- Update the configure() docstring to document `enabled` and the env var.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — it is not here.
- Keep the public API small: `enabled` is an additive keyword on the existing
  `configure`; do NOT add a new top-level exported symbol unless clearly justified.
- Default stays `enabled=True`; with the switch untouched, behavior is byte-for-byte
  unchanged.

ACCEPTANCE CRITERIA
- `configure(enabled=False)` makes all of `@observe`, `trace/span/generation/
  tool_call/retrieval`, and `score()` write nothing while the wrapped code still runs
  and still raises on error.
- `get_current_trace_id()`/`get_current_span_id()` behave sensibly when disabled
  (document and test the chosen behavior).
- `BIR_DISABLED=1` (truthy) disables tracing at import; `configure(enabled=True)`
  re-enables; an invalid env value raises a clear error consistent with the other
  BIR_* parsers.
- Re-enabling with `configure(enabled=True)` restores full recording within the process.

TESTS
Add tests in tests/test_sdk.py (and CLI/env tests where appropriate) in the existing
style: disabled produces zero events across every primitive; user code still executes
and exceptions still propagate; env-var parsing (truthy/falsey/invalid); precedence of
explicit configure over env; re-enable restores writes; default unchanged.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (a short "Disabling tracing" note, likely near Sampling) and
docs/site/cli-env.md / capture-privacy.md as appropriate. Add a CHANGELOG.md entry
under "Unreleased" (create it if missing).

OUT OF SCOPE
- Do not remove or change `sample_rate`/`sample_rules` semantics.
- Do not add per-primitive enable flags or runtime hot-reload beyond `configure`.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you
commit, do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off
main before committing.
```

### 4. Native Ollama integration

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.
Integrations are dependency-free and lazy-import: each `bir.integrations.<provider>`
module exposes `trace_<verb>` wrappers that take the provider callable/client plus
arguments, strip `bir_`-prefixed kwargs, call the provider, and record one
`generation` reading model/usage via shared helpers in
`src/bir/integrations/_common.py`; public symbols are re-exported from
`src/bir/integrations/__init__.py`. There is already an `examples/ollama-demo/` that
calls Ollama's HTTP API by hand with `urllib` and a manual `generation()` block, but
there is NO `bir.integrations.ollama` wrapper for the official `ollama` Python client
(`ollama.chat(...)` / `ollama.generate(...)`, and the async `AsyncClient`).

TASK
Add a dependency-free `bir.integrations.ollama` module with `trace_chat` and
`trace_generate` wrappers (plus their `_async` counterparts) for the official
`ollama` Python client, following the existing provider-wrapper pattern exactly.

WHY
Ollama is the canonical local LLM runtime and squarely on-brand for a local-first
SDK; the demo proves the use case but every user must hand-roll instrumentation. A
first-class wrapper closes that gap consistently with the other providers.

IMPLEMENTATION GUIDANCE
- Model the module on an existing simple wrapper (e.g. src/bir/integrations/mistral.py
  or litellm.py). NEVER import `ollama`; accept the provider callable (or a client
  whose `.chat`/`.generate` you call) as the first argument.
- `trace_chat(chat_callable, *, model=..., messages=..., **kwargs)`: forward args
  unchanged, strip `bir_`-prefixed options (`bir_capture_inputs`/`bir_capture_outputs`
  and any name override the others use), open one `generation`, return the provider
  result unchanged. Read the response shape Ollama returns: `model`, the assistant
  text at `message.content` (chat) or `response` (generate), and token usage from
  `prompt_eval_count` (input) and `eval_count` (output), deriving the total. Reuse
  `_common._value` / `_usage_tokens` / `_string_or_none` for tolerant extraction.
- Handle Ollama streaming if it is low-risk and matches the other wrappers: with
  `stream=True` Ollama yields incremental chunks (`message.content` deltas for chat,
  `response` deltas for generate) and a final chunk carrying `done`,
  `prompt_eval_count`, `eval_count`. Return a lazy iterable/async iterator that yields
  chunks unchanged and finalizes model/output/usage on exhaustion — mirroring the
  existing streaming wrappers. If streaming adds disproportionate complexity, record
  the non-streaming path well and note streaming as a follow-up.
- Add `_async` variants awaiting the coroutine (the `ollama.AsyncClient` methods),
  mirroring the other `_async` wrappers.
- Re-export the new public symbols from src/bir/integrations/__init__.py `__all__`.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; `ollama` is NEVER imported and never added as a dep.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — it is not here.
- Keep the public API small but re-export the new wrappers from
  bir/integrations/__init__.py, consistent with the other providers.

ACCEPTANCE CRITERIA
- `from bir.integrations import trace_chat as ...` (or the chosen export names) works
  WITHOUT `ollama` installed; importing the module imports no third-party package.
- A fake chat/generate callable returning an Ollama-shaped response records one
  generation with the right model, output text, and input/output/total token usage.
- Errors from the provider propagate, producing an error-status generation with
  redacted error text, matching the other wrappers.
- (If implemented) streaming yields chunks unchanged and records accumulated
  output + final usage once consumed.

TESTS
Add tests/test_ollama_integration.py in the style of tests/test_mistral_integration.py
(or litellm): fake clients/responses, no network, no provider SDK. Cover chat,
generate, sync + async, token-usage extraction, error propagation, the `bir_`-kwarg
stripping, and streaming if implemented. Add the module to
scripts/verify_release.py's installed-wheel integration-import smoke list.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Add an Ollama section to docs/site/integrations.md and mention it in README.md's
integrations list. Add a CHANGELOG.md entry under "Unreleased" (create it if missing).

OUT OF SCOPE
- Do not rewrite examples/ollama-demo (optionally add a one-line pointer to the new
  wrapper, nothing more).
- Do not add embeddings or other Ollama endpoints beyond chat/generate.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you
commit, do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off
main before committing.
```

### 5. `bir prune` — clean the local trace store

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.
The CLI is in src/bir/cli.py. Traces are line-delimited JSON in `.bir/traces.jsonl`;
appends and size-based rotation (`configure(max_bytes=..., backup_count=...)`) are
serialized by an advisory inter-process file lock (`_InterProcessFileLock`) plus an
in-process lock. `load_traces()`/`load_events()` read the active file (and rotated
siblings with `include_rotated=True`). There is NO command to clean or bound the store
by time/count — rotation only caps the ACTIVE file size during writes; old events
inside retained files are never pruned.

TASK
Add a `bir prune` CLI subcommand that removes old/unwanted traces from the local
store, operating on WHOLE traces, under the same advisory lock used for appends, with
a safe-by-default dry run.

WHY
A long-lived local store grows without bound (or accumulates stale data across rotated
files); users need a first-class, safe way to reclaim space and drop old/erroring
traces without hand-editing JSONL.

IMPLEMENTATION GUIDANCE
- Add a `prune` subparser in `_build_parser` with: `--path` and `--include-rotated`
  (same semantics as `bir traces`); selection filters reusing the existing
  `_iso_datetime` type and status choices — `--before ISO` (drop traces whose start
  time is before the cutoff), `--keep-last N` (keep only the N most recent traces),
  and optionally `--status {success,error}` (only prune matching traces); plus
  `--dry-run`. Make it SAFE BY DEFAULT: either default to `--dry-run` and require an
  explicit `--yes`/`--force` to write, or require at least one selection filter and
  print what would be removed unless confirmed. Pick one and document it.
- Implement `_cmd_prune`: load complete traces via the public loaders, decide which
  trace IDs to keep, then rewrite the trace file(s) keeping only events belonging to
  kept traces, ALWAYS under `_InterProcessFileLock` on the trace path (and the
  in-process lock) so a concurrent appender cannot interleave. Never split a trace
  across the keep/drop boundary; write via a temp file + atomic replace like the
  existing sidecar replacement does.
- Print a summary (`removed=<n traces> kept=<n traces>` and bytes reclaimed if easy);
  in `--dry-run` print the same counts but write nothing. Exit 0 on success.
- Consider factoring a small `prune_traces(...)` helper in _sdk.py if it keeps the CLI
  thin, but only export it if it is genuinely useful as public API; otherwise keep it
  CLI-local.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — it is not here.
- This MUTATES the local store: it must take the same advisory lock as appends, must
  never corrupt or partially write the file (temp file + atomic replace), and must
  default to a non-destructive preview.
- Keep the public API small; re-export a new public symbol only if justified.

ACCEPTANCE CRITERIA
- `bir prune --before <iso>` and `bir prune --keep-last N` remove only whole matching
  traces and leave the remaining store valid JSONL readable by `load_traces()`.
- Default behavior does NOT delete without explicit confirmation/flag; `--dry-run`
  reports counts and writes nothing.
- Pruning is atomic and lock-serialized: a partial failure leaves the original file
  intact, and the operation does not race a concurrent append.
- Counts in the summary are accurate; an empty store and a "nothing matched" case
  both exit 0 and write nothing.

TESTS
Add tests in tests/test_cli.py: prune by `--before` and `--keep-last`, optional
`--status`, dry-run writes nothing, confirmation/flag gating, resulting file is valid
and loadable, atomicity on simulated failure if feasible, and an empty/no-match store.
Follow the existing `cli.main([...])` + temp-trace-path pattern.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Document `bir prune` in README.md (near the CLI overview) and docs/site/cli-env.md,
stressing that it is destructive and safe-by-default. Add a CHANGELOG.md entry under
"Unreleased" (create it if missing).

OUT OF SCOPE
- Do not change rotation, `send`, or the loaders' default behavior.
- Do not add experiment pruning in this task (traces only).

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you
commit, do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off
main before committing.
```

### 6. Fuzzy-similarity deterministic evaluator

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.
`bir.evals` ships deterministic evaluator factories that return a frozen
`DeterministicEvaluator` wrapping an `evaluate(output, example_expected) -> EvalResult`
closure (see `exact_match`, `contains`, `regex_match`, `json_valid`, `field_equals`,
`numeric_between`, the RAG heuristics). `EvalResult(name, value, metadata)` requires a
finite numeric value. Evaluators that compare to a per-example expected value use the
`_expected_value(configured, example_expected, name)` / `_USE_EXAMPLE_EXPECTED`
sentinel pattern. All public evaluators are listed in `evals.__all__`. There is
currently a gap between exact equality (`exact_match`) and substring (`contains`):
no FUZZY string-similarity evaluator.

TASK
Add a deterministic `similarity_above(threshold, ...)` evaluator that scores 1.0 when
the normalized similarity ratio between the output text and the expected text is at or
above `threshold`, using only the standard library (`difflib.SequenceMatcher`).

WHY
Real LLM outputs rarely match a reference exactly; teams need a deterministic,
dependency-free fuzzy check (typos, reordering, minor wording) that sits between
`exact_match` and `contains`, without pulling in an embedding model or new dependency.

IMPLEMENTATION GUIDANCE
- Add the factory to src/bir/evals.py next to `contains`/`exact_match`, following the
  same shape: `similarity_above(threshold: float, expected: ... = _USE_EXAMPLE_EXPECTED,
  *, case_sensitive: bool = True, name: str = "similarity_above") -> DeterministicEvaluator`.
- Validate `threshold` with the existing helpers (finite, 0.0–1.0; reuse
  `_validate_finite_number` / a range check consistent with how `sample_rate`/ratios
  are validated elsewhere). Resolve the expected value via `_expected_value(...)`; it
  must be a string (raise `TypeError` otherwise, like `contains`).
- Compute `ratio = difflib.SequenceMatcher(None, a, b).ratio()` on the output text
  (`"" if output is None else str(output)`) and the expected text, lowercasing both
  when `case_sensitive=False`. Score `1.0 if ratio >= threshold else 0.0`. Record the
  achieved ratio and threshold in `EvalResult.metadata` (e.g.
  `{"expected": ..., "ratio": ratio, "threshold": threshold}`) so failures are
  inspectable.
- Add `"similarity_above"` to `evals.__all__`.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only (use stdlib `difflib`).
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — it is not here.
- Keep the public API small but DO export the new evaluator via `evals.__all__`,
  consistent with the other evaluator factories.

ACCEPTANCE CRITERIA
- `similarity_above(0.8)` scores 1.0 for near-identical strings and 0.0 for clearly
  different ones; the boundary at exactly `threshold` is inclusive (>=).
- Works both with a configured expected value and with a per-example expected value
  (the `_USE_EXAMPLE_EXPECTED` path), and `case_sensitive=False` lowercases both sides.
- Invalid `threshold` (out of range / non-finite) and a non-string expected raise
  clear errors at construction/evaluation consistent with the other evaluators.
- `EvalResult.metadata` carries the achieved ratio and threshold.

TESTS
Add cases to tests/test_evals.py in the existing style: identical text → 1.0,
sub-threshold difference → 0.0, exact-boundary inclusivity, case-insensitive option,
per-example expected, output `None`, and the validation/error paths. Optionally assert
it composes inside `run_experiment(...)`.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Document the evaluator in docs/site/evals-experiments.md (and the README evals section
if it enumerates evaluators). Add it to docs/EVALUATOR_IMPLEMENTATION_GUIDE.md if that
guide lists the built-ins. Add a CHANGELOG.md entry under "Unreleased" (create it if
missing).

OUT OF SCOPE
- Do not add embedding/semantic similarity or any model-backed evaluator.
- Do not change existing evaluators' behavior or signatures.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you
commit, do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off
main before committing.
```

### 7. `bir config` — print effective configuration

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.
The CLI is in src/bir/cli.py. Configuration lives in a frozen `_Config` (fields
include trace_path, capture_inputs, capture_outputs, service_name, environment,
source, sample_rate, sample_rules, max_bytes, backup_count, model_prices,
max_value_length, max_collection_items, plus user redaction additions) initialized
from BIR_* environment variables at import and updatable via `configure(...)`. There
is no way to SEE the effective configuration; users debugging "why isn't capture on"
or "which trace path is active" have to read code.

TASK
Add a `bir config` CLI subcommand that prints the effective resolved SDK
configuration (the active `_Config` values, the resolved trace path, and which BIR_*
environment variables are set), with a `--json` option.

WHY
A read-only "what is my config right now" command is a high-value, low-risk debugging
aid — it answers the most common support question (capture/trace-path/sampling state)
without a Python REPL.

IMPLEMENTATION GUIDANCE
- Add a `config` subparser in `_build_parser` with `--json`. Implement `_cmd_config`
  that reads the live config (use the SDK's accessor for the active `_Config` —
  reference `_sdk._config` or add a tiny read-only accessor if one is cleaner; do NOT
  expose a mutable handle).
- Print a human-readable table of the effective values: trace_path (resolved absolute
  path), capture_inputs/outputs, sample_rate, sample_rules (if any), service_name,
  environment, source, max_bytes/backup_count, max_value_length/max_collection_items,
  and whether additional redaction rules / a model_prices table are configured (count
  only — do NOT print user secret patterns or prices verbatim beyond counts/keys).
  Also show which BIR_* env vars are currently set (names and that they are set; avoid
  echoing values that could be sensitive like a custom trace path is fine, but keep it
  simple and non-leaky).
- `--json` emits a deterministic object of the same fields (sorted keys). Exit 0.
- Do NOT mutate config; this command is purely read-only.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
  Do not print user-supplied redaction patterns or model prices in a way that could
  leak secrets — counts/keys only.
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — it is not here.
- Keep the public API small; this is CLI-only. Add at most a tiny read-only config
  accessor if needed, not a new mutable surface.

ACCEPTANCE CRITERIA
- `bir config` prints the effective trace path, capture flags, sampling, service
  metadata, rotation, and capture-size limits in a readable table.
- `bir config --json` emits a deterministic, sorted JSON object with the same fields.
- Setting a BIR_* env var or calling `configure(...)` in-process is reflected; the
  command never mutates config and always exits 0.
- No user secret patterns or raw prices are printed (counts/keys only).

TESTS
Add tests in tests/test_cli.py: default output reflects defaults; after `configure(...)`
the values change; `--json` shape is deterministic and sorted; env-var presence is
reported; redaction patterns/prices are summarized not dumped. Use the existing
`cli.main([...])` + captured-stdout pattern, resetting config between tests as other
config tests do.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Document `bir config` in README.md and docs/site/cli-env.md. Add a CHANGELOG.md entry
under "Unreleased" (create it if missing).

OUT OF SCOPE
- Do not add config-WRITING via the CLI (no `bir config set`).
- Do not change `configure()` or `_Config`.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you
commit, do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off
main before committing.
```

### 8. `SECURITY.md` — redaction guarantees + disclosure

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.
Privacy is a headline feature: input/output capture is OPT-IN and disabled by default,
and when capture is on, best-effort redaction (in _sdk.py: `_redact_secret_text`,
`_safe_capture`, `_is_secret_key`, the labeled/bearer/PAN matchers) replaces
secret-like keys and text — labeled secrets, bearer/`sk-`, JWTs, AWS AKIA/ASIA, GCP
`AIza`, Slack `xox*`, GitHub `gh*_`, Stripe `sk_/rk_`, Azure 88-char keys, PEM private
keys, and Luhn-checked credit-card/PAN numbers — before anything is written. Built-in
rules can never be disabled; users can only WIDEN them via
`configure(additional_secret_keys=..., additional_redaction_patterns=...)`. The
redaction contract is shared with the separate bir-app repo via
`tests/fixtures/redaction-cases.json` (guarded by `tests/test_redaction_parity.py` and
`scripts/fixtures.py check`). There is currently NO SECURITY.md.

TASK
Add a top-level SECURITY.md that documents Bir's privacy/security posture — what is and
is not captured, the redaction guarantees and their best-effort limits, how to widen
redaction — and a responsible vulnerability-disclosure process.

WHY
For a privacy-positioned SDK, a clear security policy is table stakes: it tells users
exactly what reaches local storage, sets correct expectations about best-effort
redaction, and gives a defined channel for reporting issues.

IMPLEMENTATION GUIDANCE
- Create SECURITY.md at the repo root. Cover, accurately and verified against the
  current code (do not overstate):
  - Capture is opt-in and disabled by default (no inputs/outputs stored unless enabled).
  - The redaction model: built-in rules always apply and cannot be disabled; list the
    categories actually implemented today (verify against _sdk.py before listing);
    note redaction runs on EVERY persistence path and BEFORE any truncation.
  - Best-effort limitations: pattern-based redaction can miss novel secret formats;
    advise widening via `configure(additional_secret_keys/additional_redaction_patterns)`
    and keeping capture off for highly sensitive payloads.
  - Local-first storage: data lives in `.bir/` on the user's machine; sending to a
    server is explicit.
  - The cross-repo redaction contract note (fixtures shared with bir-app) at a high
    level.
  - Supported versions and a vulnerability-reporting channel (use the GitHub
    Security Advisories / "report privately" flow for bir-ai/bir-python, or a contact;
    do NOT invent an email — prefer the GitHub private reporting link).
- Keep it concise and link to docs/site/capture-privacy.md for detail rather than
  duplicating it.
- Optionally add a one-line link to SECURITY.md from README.md.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — it is not here.
- This is docs-only: do NOT change any code, redaction rules, or fixtures.
- Every claim in SECURITY.md MUST match the current implementation — verify before writing.

ACCEPTANCE CRITERIA
- SECURITY.md exists at the repo root, accurately describes opt-in capture and the
  built-in (non-disableable, best-effort) redaction categories actually implemented,
  explains how to widen redaction, and defines a disclosure process.
- No code, redaction rule, schema, or fixture is changed.
- If docs CI lints links (mkdocs --strict only covers docs/site), ensure any added
  links resolve.

TESTS
No code change, so no new unit tests are required. If the repo has a docs/link test
(e.g. tests/test_docs_ci.py) that could cover a README link to SECURITY.md, extend it
minimally; otherwise none.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
SECURITY.md is the deliverable. Add a CHANGELOG.md entry under "Unreleased" (create it
if missing). Optionally link SECURITY.md from README.md.

OUT OF SCOPE
- Do not modify redaction code, fixtures, or capture defaults.
- Do not add a CONTRIBUTING.md or CODE_OF_CONDUCT.md in this task.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you
commit, do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off
main before committing.
```

### 9. OTLP exporter: environment/source resource attributes + `gen_ai.system`

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.
`src/bir/integrations/otel.py` provides an opt-in `export_traces_to_otlp(...)` (behind
the `[otel]` extra; opentelemetry imported lazily) that replays locally recorded Bir
traces as OpenTelemetry spans. Today it sets only `service.name` on the OTel
`Resource` (from the `service_name` argument, default `"bir"`) and maps per-span
attributes including GenAI semantic conventions (`gen_ai.request.model`,
`gen_ai.usage.input_tokens`/`output_tokens`) plus `bir.*` attributes. Bir trace roots
already carry `metadata.service` (service_name + environment) and `metadata.source`
(see `configure(environment=..., source=...)` and `_service_metadata()` in _sdk.py),
but the exporter ignores environment and source.

TASK
Enrich the OTLP export so the OpenTelemetry Resource includes
`deployment.environment` (from a trace's recorded environment) and a `bir.source`
(from `metadata.source`), and so model spans carry `gen_ai.system` (the provider)
when it can be derived — without changing existing attributes or the local JSONL.

WHY
OTel backends filter and group by `deployment.environment` and provider
(`gen_ai.system`); today exported Bir traces lose the environment/source/provider the
SDK already records, so cross-environment dashboards can't separate them.

IMPLEMENTATION GUIDANCE
- In otel.py, allow an optional `environment` parameter (and/or read it from the trace
  roots' `metadata.service.environment`) and set `deployment.environment` on the
  Resource alongside `service.name`. Add `bir.source` from `metadata.source` when
  present. Keep `service.name` defaulting exactly as today.
- If a single export call spans multiple environments/sources, choose a clear,
  documented rule: prefer an explicit `environment=` argument; otherwise derive per
  the trace roots and, if they conflict, omit the Resource-level attribute (and/or set
  the value per-span as `bir.environment`). Keep it deterministic and documented.
- Add `gen_ai.system` to generation spans when the provider is derivable. Provider is
  not a first-class field on the event, so derive conservatively from existing data
  (e.g. `metadata.provider` if integrations recorded it, or leave unset when unknown).
  Do NOT guess from the model string unless it is unambiguous; prefer omission over a
  wrong value.
- Update the `bir export-otel` CLI (src/bir/cli.py, `_cmd_export_otel`) only if a new
  passthrough flag (e.g. `--environment`) is warranted; keep it optional and
  backward-compatible.
- Reuse the lazy-import structure; opentelemetry stays in the `[otel]` extra.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; opentelemetry remains in the optional `[otel]` extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
  Exported attribute values come from already-redacted recorded events; do not bypass that.
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — it is not here. The local JSONL
  is read-only to the exporter.
- Keep the public API small; `export_traces_to_otlp` stays the single entry point
  (additive keyword only).
- Existing exported attributes (service.name, gen_ai.*, bir.*) must remain
  byte-compatible when the new inputs are absent.

ACCEPTANCE CRITERIA
- A trace recorded with `configure(environment="prod", source="checkout")` exports with
  `deployment.environment="prod"` and `bir.source="checkout"` on the Resource (or
  per-span per the documented multi-environment rule).
- Generation spans carry `gen_ai.system` when the provider is derivable and OMIT it
  otherwise (never a guessed/wrong value).
- With no environment/source/provider present, the export is unchanged from today.
- Calling the exporter without the `[otel]` extra still raises the existing clear
  install-hint error.

TESTS
Extend tests/test_otel_integration.py in the existing style (inject a fake/in-memory
span exporter; no real OTLP, no network): assert the Resource attributes for
environment/source, the per-span `gen_ai.system` when derivable and its absence
otherwise, and the no-new-input regression (attributes unchanged).

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update the "Forwarding traces to OpenTelemetry" section of README.md and
docs/site/integrations.md (and cli-env.md if a CLI flag is added) to document the new
resource attributes and `gen_ai.system`. Add a CHANGELOG.md entry under "Unreleased"
(create it if missing).

OUT OF SCOPE
- Do not add a live/streaming OTel span processor or context propagation (export of
  recorded traces only).
- Do not add OTel metrics/logs export.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you
commit, do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off
main before committing.
```

### 10. Per-example timeout for the experiment runners

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.
`bir.evals.run_experiment(name, *, dataset, task, evaluators, raise_on_error=...,
record_traces=..., max_workers=1)` runs a task over a dataset (optionally concurrently
in a ThreadPoolExecutor when `max_workers > 1`), recording an `ExperimentExampleResult`
per example (status success/error, scores, error text) and persisting results + a
summary under `.bir/experiments/`. `run_experiment_async(..., max_concurrency=1)` is
the asyncio counterpart. Errors are captured per example via the
`_error_example_result(...)` path when `raise_on_error=False`. There is NO per-example
TIMEOUT, so a single hanging task (a stuck network LLM call) can block an entire
experiment indefinitely.

TASK
Add an opt-in per-example timeout to `run_experiment` and `run_experiment_async` so a
task exceeding the limit is recorded as an error result (or raises when
`raise_on_error=True`) without hanging the whole run.

WHY
Experiments commonly call real, network-backed model clients; one stuck example
shouldn't stall a CI eval. A bounded per-example timeout makes runs reliably
terminating, consistent with the existing error-capture model.

IMPLEMENTATION GUIDANCE
- Add `timeout: float | None = None` (seconds, positive when set; default None =
  unlimited, byte-for-byte unchanged behavior) to both runners. Validate with the
  existing helpers (e.g. a positive/finite check consistent with `max_workers` /
  tolerance validation).
- Sync (`run_experiment`): in the threaded path, use
  `concurrent.futures.Future.result(timeout=...)` and treat a `TimeoutError` as a
  failed example via the same `_error_example_result(...)` shape (clear error text
  like `"task timed out after Ns"`), honoring `raise_on_error`. For the serial
  (`max_workers=1`) path, decide and document the behavior: either run each example on
  a worker so the timeout is enforceable, or document that the timeout is enforced via
  the executor path — keep it deterministic and tested. Note: a timed-out thread may
  keep running in the background (Python can't force-kill threads); record the timeout
  result and move on, and document this limitation.
- Async (`run_experiment_async`): wrap each example coroutine in
  `asyncio.wait_for(..., timeout)`; on `asyncio.TimeoutError` record the error result
  (or raise when `raise_on_error`), and ensure the cancelled task is awaited so no
  warning leaks. Preserve dataset-order results and the existing cancellation behavior.
- Results, JSONL rows, and summary aggregates stay in dataset order and on the same
  schema; a timeout is just another error-status example.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; stdlib only (`concurrent.futures`, `asyncio`).
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — it is not here. The persisted
  experiment JSONL/summary schema is unchanged (a timeout reuses the error fields).
- Keep the public API small: `timeout` is an additive keyword on the two existing
  runners; no new exported symbol.
- `timeout=None` keeps current behavior exactly.

ACCEPTANCE CRITERIA
- `run_experiment(..., timeout=0.1)` with a slow task records that example as
  status="error" with a timeout message (or raises when `raise_on_error=True`) and
  completes the rest of the run.
- `run_experiment_async(..., timeout=0.1)` behaves equivalently via `wait_for`, awaits
  the cancelled task, and preserves dataset order.
- Invalid `timeout` (non-positive / non-finite) raises a clear error at call time.
- `timeout=None` (default) is byte-for-byte identical to today, including the persisted
  files.

TESTS
Add tests to tests/test_evals.py in the existing style: a deliberately slow sync task
times out and is recorded as error (and raises under raise_on_error=True); the async
runner times out via wait_for and keeps order; mixed fast/slow examples produce correct
ordered results; invalid timeout validation; and the `timeout=None` regression. Avoid
real sleeps longer than necessary (use very small timeouts / controllable fakes).

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Document the `timeout` keyword in docs/site/evals-experiments.md and the README evals
section. Add a CHANGELOG.md entry under "Unreleased" (create it if missing).

OUT OF SCOPE
- Do not add ret/retry-on-timeout, global wall-clock budgets, or process-based
  isolation in this task.
- Do not change evaluator execution or the persisted schema beyond reusing error fields.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you
commit, do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off
main before committing.
```
