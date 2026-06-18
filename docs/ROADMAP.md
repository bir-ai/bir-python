# Bir Python SDK — Improvement Roadmap

> Generated analysis of `bir-python` (PyPI `bir-sdk`, import `bir`). This file is a
> planning artifact only — it implements nothing. Each Phase-4 prompt is a
> standalone Claude Code prompt a fresh session can paste in and execute end to
> end. Verify details against the current code before relying on them; the repo
> evolves.

## Phase 2 — Assessment

### What already exists (and is therefore excluded from the roadmap)

- **Core primitives** in `src/bir/_sdk.py`: `@observe()` (sync + async), `trace()`,
  `span()`, `generation()`, `tool_call()`, `retrieval()`, `score()`, `prompt()`,
  `configure()`, `load_events()`, `load_traces()`, `send_events()`. All re-exported
  from `src/bir/__init__.py`.
- **Service metadata** (`service_name`, `environment`) — already supported via
  `configure()` and written to `metadata.service` on trace roots.
- **Sampling** (`sample_rate`) — already implemented; decided once per trace root
  and inherited by descendants; never changes control flow.
- **Opt-in capture + best-effort redaction** — secret-like keys and text patterns
  (`authorization`/`bearer`, labeled secrets, `sk-…`) are redacted before writes;
  guarded by `tests/test_redaction_parity.py` against `tests/fixtures/redaction-cases.json`.
- **Evals** in `src/bir/evals.py`: deterministic evaluators (`exact_match`,
  `contains`, `regex_match`, `json_valid`, `field_equals`, `field_contains`,
  `latency_under`, `cost_under`, `numeric_between`, `retrieved_context_contains`,
  `answer_context_overlap`, `answer_contains_citation`, `custom_evaluator`),
  `Dataset`, `run_experiment()`, experiment + `.summary.json` persistence,
  `list_experiments()`, `load_experiment()`, `send_experiment()`, optional
  `record_traces=True`.
- **Integrations** (dependency-free, lazy import): openai, anthropic, google,
  mistral, cohere, litellm, langchain, llamaindex. **OpenAI already has streaming
  support** (`_stream_chat_completion`).
- **CI/release**: `.github/workflows/ci.yml` (unit tests, example smoke tests,
  pyright, `verify_release.py`) and a **tag-driven** `release.yml` (PyPI Trusted
  Publishing + GitHub Release). `scripts/verify_release.py` builds and smoke-tests
  a temporary wheel.

### Conventions / idioms the code follows (every prompt must match these)

- Frozen `@dataclass(frozen=True)` for public value types; `__post_init__`
  validation via `object.__setattr__`.
- Validation helpers: `_validate_event_name`, `_validate_number`,
  `_validate_non_negative_number`, `_validate_non_negative_int`,
  `_validate_sample_rate`, `_validate_currency` (and `evals.py` equivalents).
- JSONL writes are always `json.dumps(..., sort_keys=True, separators=(",", ":"),
  allow_nan=False)`; trace writes go through `_write_event` under `_write_lock`
  and respect the sampled-out (`_current_trace_dropped`) guard.
- Context propagation via `ContextVar`s; context managers reset tokens in reverse.
- Integrations never import the vendor SDK; `bir_`-prefixed kwargs; shared parsing
  in `integrations/_common.py`; output via `model_dump()`/`dict()` fallback.
- `schema_version` is `"1.0"`; `tests/fixtures/event-schema-v1.json` +
  `valid-events.jsonl` are a **shared cross-repo contract** with `bir-app`.
- Tests are heavy and behavior-focused (unittest + pytest), 205 currently green;
  pyright reports 0 errors.

### Genuinely missing (the basis for this roadmap)

- **No `py.typed` marker.** The package is fully typed but ships no PEP 561
  marker, so downstream `mypy`/`pyright` users get *no* types. Confirmed: no
  `py.typed` under `src/bir/`, and `pyproject.toml` declares no package data.
- **`__version__` resolves the wrong distribution name.** `src/bir/__init__.py`
  calls `version("bir")`, but the published distribution is `bir-sdk`. On a real
  `pip install bir-sdk`, `version("bir")` raises `PackageNotFoundError` and falls
  back to the hardcoded `"0.1.1"`, which silently goes stale every release.
  `scripts/verify_release.py` masks this by hand-building a fake wheel named
  `bir` (`Name: bir`), so verification never catches the drift.
- **CI tests only Python 3.12**, although `pyproject.toml` advertises 3.10–3.13.
- **No environment-variable configuration** for `configure()`.
- **No `bir` CLI** / `console_scripts` entry point.
- **No trace-file rotation / size cap** — `.bir/traces.jsonl` grows unbounded.
- **`send_events()` has no retry/backoff** and no opt-in "mark sent" to make
  re-sends cheap.
- **Anthropic and Google integrations lack streaming** (only OpenAI has it).
- **No experiment regression detection / CI gate** (compare two runs; fail on
  score regression).
- **Redaction covers only a few patterns** — no JWT, AWS `AKIA…`, GCP `AIza…`,
  Slack/GitHub tokens.
- **No published docs site** (README is 557 lines and overloaded); no mkdocs.

### Deferred deliberately (noted, not scheduled here)

- **OpenTelemetry/OTLP export behind an extra.** Strategically valuable for
  interop, but it is the largest/riskiest candidate (new optional dependency,
  span-mapping surface) and pulls against the local-first minimalism. Best made
  as a deliberate post-1.0 bet rather than bundled into this batch.

---

## Phase 3 — Prioritized improvements

| # | Improvement | Category | Priority | Size | Risk | Depends on |
|---|-------------|----------|----------|------|------|-----------|
| 1 | Ship a PEP 561 `py.typed` marker | ci/release (packaging) | P0 | S | low | — |
| 2 | Fix `__version__` distribution name + release-verify parity | ci/release | P0 | S | low | — |
| 3 | CI Python version matrix (3.10–3.13) | ci/release | P0 | S | low | — |
| 4 | Environment-variable configuration for `configure()` | dx / core | P1 | M | low | — |
| 5 | `bir` CLI (`console_scripts`, stdlib only) | dx | P1 | M | low | 2 (entry-point packaging) |
| 6 | Anthropic + Google streaming parity | integrations | P1 | M | low–med | — |
| 7 | Experiment regression detection + CI gate | evals | P1 | M | low | — |
| 8 | AWS Bedrock + Vertex AI integrations | integrations | P2 | M | low | — |
| 9 | Trace-file rotation / size cap (opt-in) | core | P2 | M | med | — |
| 10 | `send_events()` retry/backoff + opt-in "mark sent" | core | P2 | M | med | — |
| 11 | Richer configurable redaction (JWT/AKIA/AIza/tokens) | core | P2 | M | med (cross-repo) | — |
| 12 | mkdocs documentation site | docs | P2 | M | low | — |

Trade-offs flagged: **#9** must keep `.bir/traces.jsonl` parseable and keep
default behavior unchanged (a trace can span rotated files — document it). **#10**
must not alter the event schema (track sent IDs in a sidecar, not in events).
**#11** touches the redaction contract shared with `bir-app` — adding patterns
means updating the fixture in *both* repos in lockstep; the SDK-side parity test
stays green but the cross-repo divergence must be called out loudly.

---

## Phase 4 — Standalone prompts (one per improvement)

### 1. Ship a PEP 561 `py.typed` marker

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Ship a PEP 561 `py.typed` marker so downstream type checkers see the SDK's inline types.

WHY
The package is fully type-annotated and passes pyright, but without a `py.typed` marker
PEP 561 tells mypy/pyright to ignore the package's types entirely for downstream users.

IMPLEMENTATION GUIDANCE
- Add an empty marker file at src/bir/py.typed.
- Make setuptools ship it. In pyproject.toml (build-system already setuptools), add:
    [tool.setuptools.package-data]
    bir = ["py.typed"]
  Keep the existing [tool.setuptools.packages.find] where = ["src"]. One top-level
  marker covers the bir.integrations subpackage (same import package tree).
- Update scripts/verify_release.py so the hand-built verification wheel actually carries
  and checks the marker: in build_wheel() write `bir/py.typed` into the archive, and in
  inspect_wheel() add "bir/py.typed" to required_files.
- Note: verify_release.py's build_wheel only globs src/bir/*.py and does NOT include the
  integrations subpackage; do not try to fix that here — only ensure py.typed ships.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — it is not here.
- Keep the public API small; re-export new public symbols from the right __init__.py
  (none needed for this change).

ACCEPTANCE CRITERIA
- src/bir/py.typed exists.
- A real build (`python -m build`) produces a wheel whose RECORD lists bir/py.typed.
- scripts/verify_release.py builds and inspects a wheel that contains bir/py.typed and
  still passes end to end.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest): assert the marker
is importable/locatable from the installed package, e.g. via importlib.resources.files("bir")
joinpath("py.typed").is_file(), plus the verify_release inspect path covering it.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (note that the package ships inline types / is PEP 561 typed) and add a
CHANGELOG.md entry under an "Unreleased" section (create it if missing). Update
docs/SDK_RELEASE_CHECKLIST.md only if the release contract changes (mention the marker
check).

OUT OF SCOPE
- Do not refactor verify_release.py to package the integrations subpackage.
- Do not add new runtime dependencies or change the public API.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 2. Fix `__version__` distribution name + release-verify parity

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Make `bir.__version__` resolve the real distribution name `bir-sdk`, and fix
scripts/verify_release.py so its verification wheel uses the same `bir-sdk` distribution
name (today it builds a fake wheel named `bir`, which hides the bug).

WHY
src/bir/__init__.py calls importlib.metadata.version("bir"), but the published distribution
is `bir-sdk`. On a real install, version("bir") raises PackageNotFoundError and silently
falls back to a hardcoded version string that goes stale each release. verify_release.py
masks this by hand-building a wheel named `bir`.

IMPLEMENTATION GUIDANCE
- In src/bir/__init__.py change version("bir") -> version("bir-sdk"); keep the
  PackageNotFoundError fallback for running from source (PYTHONPATH=src without an install),
  and add a short comment that the fallback only applies to source runs.
- In scripts/verify_release.py:
    * metadata(): change the wheel METADATA "Name: bir" -> "Name: bir-sdk".
    * build_wheel(): change the wheel filename and *.dist-info dir from bir-{version}/
      bir-{version}.dist-info to the normalized distribution name (bir_sdk-{version}-...
      and bir_sdk-{version}.dist-info) so pip/importlib resolves the dist as bir-sdk.
    * run_install_smoke_test(): after install, assert importlib.metadata.version("bir-sdk")
      equals the pyproject version, so future drift fails verification.
- Note: a fresh `pip install -e ".[dev]"` now registers the dist as `bir-sdk`, so
  version("bir-sdk") resolves locally too.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — it is not here.
- Keep the public API small; __version__ stays the only version surface.

ACCEPTANCE CRITERIA
- After `pip install -e .`, `python -c "import bir; print(bir.__version__)"` prints the
  pyproject version (not the hardcoded fallback).
- scripts/verify_release.py installs a wheel resolvable as `bir-sdk` and asserts its version.
- Running from source (PYTHONPATH=src, no install) still imports without error.

TESTS
Add/extend tests under tests/ in the existing style: assert bir.__version__ is a non-empty
string and matches the expected format; keep the verify smoke-test assertion described above.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md if it references the version surface; add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md if the
verification steps change.

OUT OF SCOPE
- Do not switch to a dynamic/VCS version scheme or add setuptools-scm.
- Do not rename the import package or the distribution.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 3. CI Python version matrix (3.10–3.13)

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Run the CI test job across a Python version matrix (3.10, 3.11, 3.12, 3.13) instead of only
3.12.

WHY
pyproject.toml advertises support for Python 3.10–3.13 and requires-python >=3.10, but
.github/workflows/ci.yml only tests 3.12, so version-specific regressions can ship unnoticed.

IMPLEMENTATION GUIDANCE
- Edit .github/workflows/ci.yml: add `strategy.matrix.python-version: ["3.10", "3.11",
  "3.12", "3.13"]` to the existing `sdk` job and set `setup-python` to
  ${{ matrix.python-version }}. Consider `fail-fast: false` so all versions report.
- Keep the same steps (install -e ".[dev]" pyright, unit tests, example smoke tests, pyright,
  verify_release). If pyright is slow/redundant across all versions, it is acceptable to keep
  pyright + verify_release on a single canonical version (3.12) via a separate job or an `if`,
  but the unit + example tests must run on every matrix version.
- Verify the code is actually 3.10-compatible (e.g. no 3.11+-only stdlib usage); fix any
  incompatibility found rather than narrowing the matrix.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — it is not here.
- Keep the public API small.

ACCEPTANCE CRITERIA
- ci.yml runs unit + example tests on 3.10, 3.11, 3.12, and 3.13.
- The workflow is valid YAML and the matrix expands correctly.
- Any real cross-version incompatibility surfaced by the matrix is fixed in-repo.

TESTS
No new unit tests required, but if you find and fix a version-specific issue, add a
regression test under tests/ in the existing style.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py
- (Optional, if available locally) run the suite under multiple interpreters to confirm
  3.10–3.13 compatibility.

DOCS
Update README.md only if it documents supported versions/CI; add a CHANGELOG.md entry under
an "Unreleased" section (create it if missing).

OUT OF SCOPE
- Do not add OS matrices (Windows/macOS) or coverage tooling here.
- Do not change requires-python or the advertised classifiers.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 4. Environment-variable configuration for `configure()`

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Let environment variables provide defaults for SDK configuration (trace path, capture flags,
sample rate, service name, environment) so deployments can configure Bir without code changes.

WHY
12-factor apps configure behavior via env vars; today configure() only accepts explicit
arguments, so enabling capture or sampling in production requires editing code.

IMPLEMENTATION GUIDANCE
- Support these variables, mapping to the existing _Config fields in src/bir/_sdk.py:
  BIR_TRACE_PATH (str path), BIR_CAPTURE_INPUTS (bool), BIR_CAPTURE_OUTPUTS (bool),
  BIR_SAMPLE_RATE (float 0.0–1.0), BIR_SERVICE_NAME (str), BIR_ENVIRONMENT (str).
- Precedence: explicit configure(...) arguments override env vars, which override the current
  hardcoded defaults. Capture MUST remain disabled unless explicitly enabled by env or code.
- Read env at import-time default construction and/or when configure() is called with a field
  left as None. Reuse the existing validators (_validate_event_name, _validate_sample_rate);
  add a small, well-tested bool parser (accept e.g. "1"/"true"/"yes" case-insensitively,
  reject ambiguous values with a clear error) and a float parser that routes through
  _validate_sample_rate.
- Keep _reset_config_for_tests() honest: ensure tests can construct config without ambient
  env leaking in (e.g. read env through a helper that tests can monkeypatch / or have
  _reset_config_for_tests build a pristine _Config).

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only (stdlib os.environ only).
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
  Default-off capture must hold when no env vars are set.
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — it is not here.
- Keep the public API small; configure() stays the single configuration entry point (no new
  public symbols unless clearly justified).

ACCEPTANCE CRITERIA
- With no env set and no configure() call, behavior is unchanged (capture off, sample_rate
  1.0, default trace path).
- Each documented env var sets the corresponding default; invalid values raise a clear error.
- Explicit configure(...) arguments win over env vars.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest) using monkeypatched
env: each variable individually, precedence vs explicit args, invalid bool/float values, and
the all-unset default path.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (extend the Privacy/Capture, Service Metadata, and Sampling sections with the
env-var equivalents) and add a CHANGELOG.md entry under an "Unreleased" section (create it if
missing).

OUT OF SCOPE
- Do not add a config file format (TOML/YAML) or a .env loader.
- Do not add new configurable fields beyond the six listed.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 5. `bir` CLI (console_scripts, stdlib only)

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Add a stdlib-only `bir` command-line interface (console_scripts entry point) to inspect local
traces, list experiments, and send events/experiments to a server from the terminal.

WHY
Bir is local-first; a CLI lets users explore .bir/traces.jsonl and experiment artifacts and
trigger uploads without writing a script, which is a major DX win for the primary workflow.

IMPLEMENTATION GUIDANCE
- Create src/bir/cli.py with a main(argv: list[str] | None = None) -> int using argparse
  (stdlib only). Add a [project.scripts] entry to pyproject.toml: `bir = "bir.cli:main"`.
- Subcommands (build on existing public APIs — do not reimplement reading/sending):
    * `bir traces [--path P] [--limit N] [--json]` — list traces via load_traces(): name,
      status, duration_ms, event count, start_time.
    * `bir tail [--path P]` — follow the trace file and print new events as they are written
      (simple polling on file size/offset; stdlib only).
    * `bir experiments [--dir D] [--json]` — list_experiments(): id, name, status,
      example_count, error_count, aggregate_scores.
    * `bir send [--path P] [--server URL]` — send_events(); print accepted/attempted/skipped.
    * `bir send-experiment PATH [--server URL]` — send_experiment(); print accepted/id.
- Human-readable output by default; `--json` emits machine-readable JSON for scripting.
  Return non-zero exit codes on errors (e.g. server failures, missing files) with clear stderr
  messages. Keep formatting code small and tested.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; the CLI must be stdlib-only (argparse/json/time/sys). Any new dep
  is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
  The CLI must not enable capture or mutate the trace file (except `tail` reading it).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — it is not here.
- Keep the public API small; cli.main is the new public surface. Re-export only if it fits the
  existing __init__ conventions (an entry point does not require re-export).

ACCEPTANCE CRITERIA
- `bir --help` and each subcommand `--help` work; `bir traces`/`experiments` render local data.
- `--json` produces valid JSON; error paths exit non-zero with a message on stderr.
- The console_scripts entry point is installed by a real build and invokable as `bir`.

TESTS
Add tests/test_cli.py in the existing style (unittest + pytest): call cli.main([...]) with a
temp trace_path/experiment dir, capture stdout/stderr and exit codes, cover each subcommand,
--json output, and at least one error path. Stub the network for send/send-experiment.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add a "Command Line" section documenting the subcommands) and add a
CHANGELOG.md entry under an "Unreleased" section (create it if missing).

OUT OF SCOPE
- No interactive TUI, no colors/third-party formatting libraries, no live dashboard.
- Do not add write/delete/prune subcommands here (rotation/pruning is a separate task).

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 6. Anthropic + Google streaming parity

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Add streaming support to the Anthropic Messages and Google Gemini integrations, matching the
existing OpenAI streaming behavior.

WHY
src/bir/integrations/openai.py already records streamed responses (accumulates output text and
final usage), but src/bir/integrations/anthropic.py and google.py only handle non-streaming
calls, so streamed Anthropic/Gemini generations record no output or usage.

IMPLEMENTATION GUIDANCE
- Mirror the OpenAI pattern in openai.py (_stream_chat_completion, _is_streamed_response,
  _chunk_delta_content) and the shared helpers in integrations/_common.py.
- Anthropic (anthropic.py / trace_messages): when the caller requests streaming (e.g.
  stream=True), wrap the returned iterator/stream in a generator that runs inside the same
  `with generation(...)` block, accumulates text from content deltas (Anthropic emits events
  such as content_block_delta with delta.text; message_start/message_delta carry usage), sets
  gen.set_output(...) and gen.set_usage(...) in a finally, and yields chunks unchanged.
- Google (google.py / trace_generate_content): support the streaming form (e.g.
  generate_content(..., stream=True) yielding chunks with .text and usage_metadata, typically
  on the final chunk). Accumulate text, read usage from usage_metadata, set output/usage in a
  finally, yield chunks unchanged.
- Keep parsing best-effort and tolerant of dict-or-object chunks via _common helpers; if the
  returned object is not actually iterable as a stream, fall back to the existing non-streaming
  recording path (as openai.py does).

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; never import the anthropic or google packages (lazy/duck-typed
  parsing only). Any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — it is not here.
- Keep the public API small; the wrappers' bir_-prefixed options stay consistent across
  providers.

ACCEPTANCE CRITERIA
- A streamed Anthropic call records one generation with accumulated output text and usage when
  the stream provides it; chunks pass through unchanged.
- A streamed Gemini call records one generation with accumulated output text and usage when
  available; chunks pass through unchanged.
- Non-streaming behavior for both integrations is unchanged.

TESTS
Extend tests/test_anthropic_integration.py and tests/test_google_integration.py in the
existing style with fake streaming clients (iterables of fake chunk objects/dicts), asserting
accumulated output, derived/total usage, model handling, and chunk pass-through. Mirror the
streaming assertions in tests/test_openai_integration.py.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (note streaming support in the Anthropic and Google sections) and add a
CHANGELOG.md entry under an "Unreleased" section (create it if missing).

OUT OF SCOPE
- Do not add async streaming unless the non-async path is trivially shared; keep scope to
  sync streaming parity.
- Do not add streaming to other integrations in this task.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 7. Experiment regression detection + CI gate

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Add the ability to compare two persisted experiment runs and detect aggregate-score
regressions, with a way to fail (non-zero exit) when a regression exceeds a threshold.

WHY
Bir's evals are positioned for local regression checks, but there is no built-in way to
compare a baseline run to a candidate run or gate CI on score drops, which is the core
"catch a prompt/model regression before merge" workflow.

IMPLEMENTATION GUIDANCE
- Add to src/bir/evals.py a pure comparison function, e.g.
  compare_experiments(baseline: ExperimentResult | str | Path, candidate: ExperimentResult |
  str | Path, *, tolerance: float = 0.0) returning a frozen dataclass (e.g. ExperimentDiff)
  with per-evaluator deltas (candidate_mean - baseline_mean), the set of regressed evaluator
  names (delta < -tolerance), improved/unchanged sets, and any evaluators present in only one
  run. Accept either loaded ExperimentResult objects or paths (load via load_experiment()).
- Reuse ExperimentResult.aggregate_scores; follow existing frozen-dataclass + to_dict()
  conventions and the _validate_finite_number helper for the tolerance.
- Provide a thin gate helper, e.g. ExperimentDiff.has_regressions or a regressions list, so a
  caller (or the `bir` CLI if present) can exit non-zero. If a CLI exists, add a
  `bir eval-gate BASELINE CANDIDATE [--tolerance T]` subcommand that prints the diff and exits
  1 on regression; otherwise document the function-based gate. Keep it deterministic and
  network-free.
- Export new public symbols (compare_experiments, ExperimentDiff) from evals.py __all__.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only (stdlib only here).
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or the experiment summary schema / tests/fixtures/*
  (shared contract with the bir-app repo) unless the task explicitly is a schema change — it is
  not here. This is additive comparison logic, not a persisted-format change.
- Keep the public API small and additive; match the evals.py naming/idioms.

ACCEPTANCE CRITERIA
- compare_experiments() returns correct per-evaluator deltas and a correct regressed set for
  representative inputs, accepting both ExperimentResult objects and file paths.
- A tolerance lets small dips pass and flags drops beyond it.
- A documented gate (function or CLI subcommand) exits non-zero exactly when a regression is
  detected.

TESTS
Extend tests/test_evals.py in the existing style: improved/regressed/unchanged cases, the
tolerance boundary, evaluators present in only one run, and loading from paths. If a CLI gate
is added, test exit codes in tests/test_cli.py.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (extend the Local Evals And Experiments section with a regression-comparison
example and the CI-gate usage) and add a CHANGELOG.md entry under an "Unreleased" section
(create it if missing).

OUT OF SCOPE
- No statistical significance testing or per-example diffing beyond aggregate scores in this
  task (per-example diff can be a follow-up).
- Do not add an LLM judge or any server requirement.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 8. AWS Bedrock + Vertex AI integrations

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Add two new dependency-free integrations following the existing pattern: AWS Bedrock (Converse
API) and Google Vertex AI generative models.

WHY
Bedrock and Vertex are widely used enterprise LLM entry points; supporting them broadens
adoption while costing nothing at runtime, since integrations import no vendor SDKs.

IMPLEMENTATION GUIDANCE
- Study src/bir/integrations/openai.py, anthropic.py, google.py and the shared
  src/bir/integrations/_common.py helpers (_value, _string_or_none, _usage_tokens,
  _response_output). Reuse them; do not import boto3 or vertexai.
- Create src/bir/integrations/bedrock.py exposing a wrapper (e.g. trace_converse) that takes a
  callable (normally bedrock-runtime client.converse), forwards *args/**kwargs unchanged inside
  one `with generation(...)`, returns the response untouched, and reads model from the request
  (modelId) plus token usage from the Converse response usage block (inputTokens/outputTokens/
  totalTokens). Use bir_-prefixed wrapper options like the others.
- Create src/bir/integrations/vertexai.py exposing a wrapper (e.g. trace_generate_content) for
  Vertex generative models' generate_content; record model and usage_metadata
  (prompt_token_count/candidates_token_count/total_token_count), mirroring google.py. Pick the
  name that best matches the Vertex call shape and document it in the docstring.
- Export the new symbols from src/bir/integrations/__init__.py following the existing style
  (function exports like trace_messages/trace_chat_completion).

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; never import boto3/botocore/vertexai/google-cloud-aiplatform. Any
  new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — it is not here.
- Keep the public API small and consistent; new wrappers match the bir_-prefixed option
  convention.

ACCEPTANCE CRITERIA
- trace_converse records one generation with model and usage from a Converse-shaped response
  and returns it unchanged; runs inside an active trace.
- The Vertex wrapper records one generation with model and usage from a Gemini-shaped response
  and returns it unchanged.
- Both are importable from bir.integrations and never import the vendor SDK.

TESTS
Add tests/test_bedrock_integration.py and tests/test_vertexai_integration.py in the existing
style (mirror tests/test_anthropic_integration.py / test_google_integration.py): fake clients
returning dict/object responses, assert recorded model, usage (including derived totals),
metadata.integration, argument pass-through, and active-trace requirement.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add Bedrock and Vertex AI sections mirroring the existing provider sections)
and add a CHANGELOG.md entry under an "Unreleased" section (create it if missing).

OUT OF SCOPE
- No streaming for these providers in this task (can be a follow-up).
- Do not add boto3/vertexai to dependencies or extras.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 9. Trace-file rotation / size cap (opt-in)

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Add opt-in size-based rotation for the local trace file so .bir/traces.jsonl stays bounded.

WHY
_write_event in src/bir/_sdk.py always appends, so a long-running app grows
.bir/traces.jsonl without limit; an opt-in size cap keeps local disk usage predictable.

IMPLEMENTATION GUIDANCE
- Add configuration to _Config / configure() in src/bir/_sdk.py: max_bytes (int | None,
  default None = unlimited / current behavior) and backup_count (int, default e.g. 3). Validate
  with the existing non-negative-int helper.
- In _write_event, under the existing _write_lock and after mkdir, when max_bytes is set and
  the file would exceed it, rotate before writing: traces.jsonl -> traces.jsonl.1 ->
  traces.jsonl.2 ... up to backup_count, dropping the oldest. Rotate on whole-line boundaries
  so each file stays valid JSONL (never split a JSON object across files).
- Decide and document read semantics: keep load_events()/load_traces() reading the single
  active file by default (unchanged), and add an opt-in to include rotated files (e.g.
  include_rotated=True, reading newest-first or oldest-first deterministically). Clearly
  document that a single logical trace may be split across rotated files when rotation occurs
  mid-trace.
- Keep the default path (max_bytes=None) byte-for-byte unchanged from today.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; stdlib only (pathlib/os). Any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or the JSONL line format / tests/fixtures/* (shared
  contract with the bir-app repo) unless the task explicitly is a schema change — it is not
  here. Rotation must not alter individual event lines.
- Keep the public API small; rotation config goes through configure(), not new top-level
  functions (a read flag on load_* is acceptable and additive).

ACCEPTANCE CRITERIA
- With max_bytes unset, behavior is identical to today (single growing file).
- With max_bytes set, the active file is kept under the cap and at most backup_count rotated
  files are retained; every file remains valid line-delimited JSON.
- Rotation is safe under the existing write lock (no interleaving/corruption).

TESTS
Extend tests/test_sdk.py in the existing style: rotation triggers at the threshold, backup_count
is respected (oldest dropped), each rotated file parses via load_events(), the include-rotated
read path works, and the default (no rotation) path is unchanged.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (document rotation under a storage/config section) and add a CHANGELOG.md
entry under an "Unreleased" section (create it if missing).

OUT OF SCOPE
- No time-based rotation, compression, or background threads.
- Do not change default behavior or the event schema.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 10. `send_events()` retry/backoff + opt-in "mark sent"

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Make send_events() resilient with bounded retry/backoff on transient failures, and add an
opt-in way to "mark sent" so repeated sends skip already-uploaded events cheaply.

WHY
send_events() in src/bir/_sdk.py does one HTTP attempt and re-reads/re-posts the whole file
each call; transient network blips fail the whole send, and re-sends needlessly re-post every
event even though the server is idempotent on event IDs.

IMPLEMENTATION GUIDANCE
- Retry/backoff: add bounded retries (e.g. retries: int = 2, backoff: float = 0.5) to
  send_events(). Retry only on transient errors (urllib URLError, timeouts, HTTP 5xx); never
  retry on 4xx (raise as today). Use exponential backoff via time.sleep (stdlib). Apply to both
  the batch (_post_event_batch) and per-event (_post_event) paths. Preserve current
  SendEventsResult semantics (accepted/attempted/skipped).
- Opt-in "mark sent": add a flag (e.g. mark_sent: bool = False). When enabled, persist the set
  of successfully-accepted event IDs to a sidecar file next to the trace file (e.g.
  <trace_path>.sent or .bir/sent-ids.json) and, on subsequent sends, skip events whose IDs are
  already recorded. The sidecar is SDK-local bookkeeping only.
- Default behavior must be unchanged: no marking unless mark_sent=True; with retries defaulting
  small, a healthy single-attempt send behaves as before.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; stdlib only (urllib/time/json). Any new dep is an optional extra
  only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo). CRITICAL: do NOT add a "sent" field to events or otherwise modify the event JSONL —
  track sent state ONLY in a separate sidecar file.
- Keep the public API small; extend send_events() with keyword args rather than adding new
  top-level functions where possible.

ACCEPTANCE CRITERIA
- A transient failure (URLError/timeout/5xx) is retried up to the configured count, then
  raises if still failing; 4xx raises immediately without retry.
- With mark_sent=True, a second send_events() skips already-accepted events (attempted reflects
  only un-sent events) and the event JSONL is untouched.
- With defaults, send_events() behaves as it does today (and remains safe to re-run).

TESTS
Extend tests/test_sdk.py in the existing style with a stubbed HTTP layer (monkeypatch
urllib.request.urlopen): transient-then-success retry, exhausted retries, no-retry on 4xx,
backoff invoked (patch time.sleep), and the mark_sent sidecar skip path. Assert the trace JSONL
is unchanged when marking.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (extend the "Send Events To The Server" section with retry and mark-sent
behavior) and add a CHANGELOG.md entry under an "Unreleased" section (create it if missing).

OUT OF SCOPE
- No deleting/pruning of sent events from the trace file.
- No async sending, queues, or background workers.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 11. Richer configurable redaction (JWT / AKIA / AIza / provider tokens)

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Strengthen best-effort redaction to also catch common high-signal secret formats — JWTs,
AWS access key IDs (AKIA...), Google API keys (AIza...), and provider tokens (e.g. Slack
xox*-..., GitHub ghp_/gho_/ghs_...) — without weakening any existing redaction.

WHY
_redact_secret_text in src/bir/_sdk.py currently catches labeled secrets, Authorization/Bearer,
and sk-... tokens, but misses other very recognizable credential formats that frequently leak
into captured payloads.

IMPLEMENTATION GUIDANCE
- Extend _redact_secret_text in src/bir/_sdk.py with additional, well-anchored regexes that
  replace matches with the existing _REDACTED marker. Suggested patterns (tune precisely to
  avoid over-redaction): JWT (three base64url segments separated by dots, leading eyJ...),
  AWS access key id (\b(?:AKIA|ASIA)[0-9A-Z]{16}\b), GCP API key (\bAIza[0-9A-Za-z_\-]{35}\b),
  Slack tokens (\bxox[baprs]-[0-9A-Za-z-]+\b), GitHub tokens (\b(?:ghp|gho|ghs|ghu|ghr)_[0-9A-
  Za-z]{36,}\b). Keep existing patterns and ordering intact; only add.
- All existing redaction cases MUST still pass byte-for-byte. New patterns must not redact
  ordinary prose/numbers (test with realistic non-secret strings).
- CRITICAL CROSS-REPO CONTRACT: tests/fixtures/redaction-cases.json and the SDK redactor are a
  shared contract with the SEPARATE bir-app repo (the server keeps its own copy of the redactor
  + fixture, because the SDK ships zero deps and cannot import server code; see
  tests/test_redaction_parity.py). Adding patterns here will cause the SDK and server redactors
  to DIVERGE unless bir-app is updated in lockstep. Add the new cases to
  tests/fixtures/redaction-cases.json AND state LOUDLY, in the PR/commit description and the
  CHANGELOG, that bir-app's redactor and its copy of redaction-cases.json must be updated to
  match before/with this change. Do NOT remove or alter existing fixture cases.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; stdlib re only. Any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction. tests/test_redaction_parity.py must stay green.
- The shared fixture change is the explicitly-allowed exception here, but it is a cross-repo
  contract change — flag it loudly and do NOT touch event-schema-v1.json or valid-events.jsonl.
- Keep the public API small. If configurability is added (e.g. an optional list of extra
  patterns via configure()), keep it additive and default to the built-in set; never let it
  disable the built-in redaction.

ACCEPTANCE CRITERIA
- The new secret formats are redacted in both string capture and nested-value capture.
- All pre-existing redaction-cases.json cases still pass unchanged.
- Realistic non-secret strings are not over-redacted (covered by tests).
- The cross-repo lockstep requirement for bir-app is documented in CHANGELOG and the commit/PR.

TESTS
Extend tests/test_redaction_parity.py via new fixture cases and add focused tests in
tests/test_sdk.py for each new pattern plus negative (non-secret) cases, in the existing style.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (Privacy And Capture section: list the additional patterns and reiterate
best-effort) and add a CHANGELOG.md entry under an "Unreleased" section (create it if missing)
that explicitly flags the bir-app cross-repo redaction sync.

OUT OF SCOPE
- Do not change the JSON event schema or the schema/valid-events fixtures.
- Do not add a way to disable built-in redaction.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 12. mkdocs documentation site

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Add an mkdocs documentation site (sourced from the existing README/docs content) buildable with
`mkdocs build --strict`, with mkdocs kept strictly as an optional extra.

WHY
README.md is ~557 lines and overloaded; a structured docs site improves discoverability of the
core API, integrations, and evals without bloating the package or its runtime dependencies.

IMPLEMENTATION GUIDANCE
- Add mkdocs.yml at the repo root (mkdocs, optionally mkdocs-material). Create a docs site
  source tree (e.g. docs/site/ or docs/*.md pages) with a sensible nav: Overview/Quickstart,
  Core API (observe/trace/span/generation/tool_call/retrieval/score/prompt/configure),
  Capture & Privacy, Sampling & Service Metadata, Sending to a Server, Integrations, Evals &
  Experiments, and (if those tasks have shipped) CLI / Env Config.
- Reuse and split existing README.md content rather than rewriting; keep README as a concise
  entry point that links to the site. Do not duplicate content that will drift — prefer moving.
- Add the docs toolchain ONLY as an optional extra in pyproject.toml, e.g.
  [project.optional-dependencies] docs = ["mkdocs>=1.5", "mkdocs-material>=9"]. Do NOT add it to
  runtime dependencies or to the dev extra's required install path for unit tests.
- Ensure `mkdocs build --strict` passes (no broken links/nav). Keep the built site out of
  version control (add site/ to .gitignore).

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; mkdocs/material are an optional `docs` extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — it is not here.
- Keep the public API small; this is docs only — no code/API changes.

ACCEPTANCE CRITERIA
- `pip install ".[docs]"` then `mkdocs build --strict` succeeds with no warnings/errors.
- The nav covers core API, integrations, and evals; pages render from the migrated content.
- Runtime install (`pip install bir-sdk`) still pulls zero dependencies; docs deps are
  isolated to the extra.
- The built site directory is gitignored.

TESTS
No unit tests for content, but ensure the existing suite is unaffected. Optionally add a CI doc
that `mkdocs build --strict` is the docs check (do not wire it into the SDK unit-test job).

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py
- pip install -e ".[docs]" && mkdocs build --strict

DOCS
This task IS docs: trim README.md to an overview that links to the site, and add a CHANGELOG.md
entry under an "Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md
only if the release contract changes.

OUT OF SCOPE
- Do not set up hosting/deploy (GitHub Pages workflow) in this task — local build only.
- Do not add API auto-generation (mkdocstrings) unless trivial and dependency-isolated.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```
