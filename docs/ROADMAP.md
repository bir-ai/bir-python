# Bir Python SDK — Prioritized Integration Roadmap

Audited against repository state on 2026-06-19. This document is planning
material only; none of the improvements below are implemented here.

## Phase 2 — Assessment

### What already exists

Bir is a zero-runtime-dependency, local-first tracing and deterministic-evals
SDK for Python 3.10+. Its public package re-exports the core tracing contexts,
load/send helpers, event/result dataclasses, prompt records, and configuration
from `src/bir/__init__.py`. `src/bir/evals.py` provides frozen result and
dataset dataclasses, deterministic evaluators, JSONL experiment persistence,
experiment loading/listing/sending, aggregate comparison, and the `eval-gate`
CLI workflow. Provider/framework wrappers are dependency-free callable adapters
that do not import the wrapped library.

The repository already implements these investigated candidates, so they are
excluded from the roadmap:

- PEP 561: `src/bir/py.typed` exists, package data includes it, and packaging
  tests plus the release verifier check it.
- Environment defaults: `BIR_TRACE_PATH`, `BIR_CAPTURE_INPUTS`,
  `BIR_CAPTURE_OUTPUTS`, `BIR_SAMPLE_RATE`, `BIR_SERVICE_NAME`, and
  `BIR_ENVIRONMENT` initialize configuration at import time. Explicit
  `configure()` arguments take precedence.
- CI interpreter coverage: unit and example tests run on Python 3.10–3.13;
  pyright and release verification run once on 3.12.
- CLI: the stdlib-only `bir` console script supports trace listing/tailing,
  experiment listing, event and experiment sending, and eval regression gates.
- Rotation: opt-in `max_bytes` / `backup_count` rotation exists, with
  oldest-first rotated loading.
- Event-send resilience: bounded retry/backoff and opt-in sent-ID sidecar
  bookkeeping exist.
- Integrations: OpenAI chat completions, Anthropic Messages, Google Gemini,
  Mistral, Cohere, LiteLLM, LangChain, LlamaIndex, AWS Bedrock Converse, and
  Vertex AI are present. OpenAI, Anthropic, and Gemini have synchronous iterable
  streaming support.
- Experiment regression detection: `compare_experiments()`,
  `ExperimentDiff`, and `bir eval-gate` exist with one global tolerance.
- Expanded default redaction: JWTs, AWS access-key IDs, Google API keys, Slack
  tokens, and GitHub tokens are covered in addition to secret-like keys and
  labeled/bearer secrets. Default parity remains a cross-repo concern.
- MkDocs: a structured site, strict configuration, and optional `docs` extra
  exist.

### Conventions and idioms

- Configuration and public records use frozen dataclasses; configuration is
  replaced atomically with `dataclasses.replace`.
- Validation is centralized in small `_validate_*` / `_expect_*` helpers
  with explicit errors for booleans, non-finite numbers, negative numbers, and
  malformed stored data.
- Trace nesting uses `ContextVar`; sync and async context-manager paths share
  event construction and preserve exception propagation.
- JSON is deterministic and strict: sorted keys, compact separators, and
  `allow_nan=False`. Trace writes are one JSON object per line and currently
  serialized only by an in-process `threading.Lock`.
- Capture is opt-in. Values pass through bounded, JSON-safe capture and
  redaction before persistence; errors are sanitized too.
- Evals are deterministic, frozen-record based, JSONL-first, and preserve
  dataset/result order.
- Integrations accept provider callables, forward provider arguments unchanged,
  prefix SDK-only options with `bir_`, inspect mapping-or-attribute response
  shapes, and never import the provider package.
- Tests primarily use `unittest`, temporary directories, fake provider
  objects, and patched stdlib HTTP calls. `pytest` is used for the example
  smoke module.
- Release verification constructs and installs a wheel without network access,
  then runs a fresh-environment smoke test.

### Genuinely missing or incomplete

- Current baseline defect: `PYTHONPATH=src ./.venv/bin/python -m unittest
  discover -s tests` passes 289 tests and the example pytest smoke passes 3
  tests, but `./.venv/bin/pyright` fails because it cannot resolve `pytest`
  in `tests/test_examples.py`. Consequently `scripts/verify_release.py`
  also fails at its pyright step.
- The release verifier's hand-built wheel includes only `src/bir/*.py`; it
  omits the entire `bir.integrations` package and therefore cannot detect
  integration packaging regressions.
- CI installs the MkDocs extra nowhere and does not run `mkdocs build --strict`.
- Trace write/rotation and sent-sidecar merge safety is process-local, not
  multi-process safe.
- Rotated events can be loaded explicitly but cannot be selected by
  `send_events()` or `bir send`; uploads silently use only the active file.
- `send_experiment()` has no retry/backoff parity with `send_events()`.
- Experiments cannot execute awaitable tasks or bound async concurrency.
- Eval gates have only one global tolerance and do not offer a policy for
  baseline evaluators missing from a candidate.
- OpenAI's Responses API is not wrapped; only Chat Completions is supported.
- `@observe()` does not keep generator or async-generator traces open through
  iteration.
- Default redaction is richer, but applications cannot add domain-specific
  sensitive keys or patterns without forking; any extension must only broaden,
  never replace, the defaults.

Optional OpenTelemetry/OTLP export is deliberately deferred. It adds a sizable
optional dependency and requires a stable mapping to OpenTelemetry semantic
conventions; the robustness and API-coverage items below have higher near-term
leverage. Additional framework integrations beyond OpenAI Responses are also
deferred until there is a concrete, stable callback surface to target.

## Phase 3 — Prioritized improvements

| # | Improvement | Category | Priority | Size | Risk | Depends on |
|---:|---|---|---|---|---|---|
| 1 | Restore hermetic pyright/release verification | ci/release | P0 | S | low | — |
| 2 | Make the verification wheel cover the complete package tree | ci/release | P0 | S | low | 1 |
| 3 | Enforce strict MkDocs builds in CI | ci/release | P0 | S | low | — |
| 4 | Make trace rotation and sent-sidecar writes multi-process safe | core | P0 | M | med | — |
| 5 | Upload rotated trace files explicitly | core / dx | P1 | M | low–med | 4 recommended, not required |
| 6 | Add retry/backoff parity to `send_experiment()` | evals | P1 | S | low | — |
| 7 | Add ordered async experiment execution | evals | P1 | L | med | — |
| 8 | Add per-evaluator eval-gate tolerances and missing-score policy | evals / dx | P1 | M | low | — |
| 9 | Add an OpenAI Responses API wrapper | integrations | P1 | M | med | — |
| 10 | Trace generator and async-generator consumption in `@observe()` | core | P2 | L | med | — |
| 11 | Allow additive application-specific redaction rules | core | P2 | M | med | — |

### Prioritization rationale and acceptance summary

1. **Restore hermetic pyright/release verification.** The advertised required
   verification is currently red, which makes this an immediate release blocker.
   Acceptance: pyright resolves all checked imports in both a project virtual
   environment and CI-style install; no broad type-check suppression; all four
   required commands pass.
2. **Complete verification-wheel coverage.** The custom wheel can pass while
   omitting every integration, so its smoke test does not represent the package.
   Acceptance: recursive package files and `py.typed` are in RECORD; wheel
   inspection requires representative integration files; a fresh venv imports
   every documented integration without provider packages installed.
3. **Strict docs CI.** The new site can drift or break links without CI noticing.
   Acceptance: one non-duplicated CI job installs `.[docs]` and runs
   `mkdocs build --strict`; runtime dependencies remain empty.
4. **Multi-process-safe local persistence.** Real worker deployments and CLI
   uploads can touch the same files from separate processes; the current lock
   protects threads only. Acceptance: stdlib-only cross-process exclusion covers
   append+rotation and sent-sidecar read/merge/replace; stress tests prove valid
   JSONL and no lost accepted IDs. Platform behavior must be explicit; this is
   medium risk because locking and crash cleanup are subtle.
5. **Rotated uploads.** Rotation currently bounds disk use at the cost of
   stranding older events from the upload workflow. Acceptance: additive,
   default-false `include_rotated` support reaches `send_events` and CLI,
   preserves oldest-first/root-first ordering, deduplicates IDs, and works with
   `mark_sent`.
6. **Experiment send retries.** Trace upload tolerates transient outages while
   experiment upload does not. Acceptance: matching bounded exponential backoff
   retries network errors/timeouts/5xx, never retries 4xx, and CLI exposes the
   controls without changing healthy-request behavior.
7. **Async experiments.** LLM application tasks are frequently async, and forcing
   sync adapters is both awkward and error-prone. Acceptance: an additive async
   runner supports sync or awaitable tasks, bounded concurrency, deterministic
   output order, existing evaluators/persistence/tracing, and cancellation-safe
   completion.
8. **Stronger eval-gate policy.** A single tolerance is too coarse and a removed
   evaluator currently cannot fail a gate. Acceptance: global defaults remain;
   per-score tolerances override them; an explicit strict missing-score policy
   can fail baseline-only metrics; JSON output explains every decision.
9. **OpenAI Responses API.** Current OpenAI users increasingly call
   `client.responses.create`, which is structurally different from Chat
   Completions. Acceptance: dependency-free sync and iterable-stream handling
   records model, output text, usage, errors, redaction, and unchanged provider
   return objects/events.
10. **Generator-aware `@observe()`.** The decorator currently closes a trace
    when a generator object is returned, before user work runs. Acceptance: sync
    and async generators remain lazy, hold context during iteration, finalize on
    exhaustion/close/error, and avoid unbounded output buffering. This is P2
    because protocol and cancellation semantics require careful testing.
11. **Additive custom redaction.** Domain secrets need protection without waiting
    for a release, but configurability must not let callers disable defaults.
    Acceptance: callers may add exact key names and compiled text patterns;
    defaults always run; invalid/unsafe configuration fails clearly; custom
    rules affect every existing capture/write path. Default fixture behavior
    stays unchanged, so no schema change is involved.

## Phase 4 — Standalone Claude Code prompts

### 1. Restore hermetic pyright/release verification

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Restore a hermetic, clean pyright and release-verification workflow by fixing the current unresolved `pytest` import in tests/test_examples.py across local-venv and CI-style environments.

WHY
The unit and example tests pass, but pyright currently reports that `pytest` cannot be resolved in tests/test_examples.py, which also makes scripts/verify_release.py fail. A required release gate must not depend on incidental interpreter discovery.

IMPLEMENTATION GUIDANCE
Reproduce the failure first and inspect pyrightconfig.json, pyproject.toml, tests/test_examples.py, CI installation, and scripts/verify_release.py. Choose the smallest robust fix that works both when dependencies are installed into .venv and when CI installs them into the active interpreter; prefer making the test/type-check setup structurally correct over blanket ignores or excluding the file. Keep pytest as a dev-only dependency. If tests/test_examples.py is refactored to remove its pytest runtime import, retain equivalent pytest collection, automatic SDK reset, tmp-path isolation, and approximate numeric assertions. Add a focused regression check if configuration behavior can otherwise drift.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- `pyright` reports zero errors in a normal project virtual environment with `.[dev]` installed.
- The CI installation pattern still resolves all checked imports without requiring a repository-local .venv.
- scripts/verify_release.py completes its pyright stage.
- tests/test_examples.py remains collected by pytest and preserves all three offline smoke scenarios.
- No broad `type: ignore`, whole-file exclusion, or disabling of reportMissingImports is introduced.
- pyproject.toml still has `dependencies = []`; pytest remains optional development tooling.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest), covering the happy
path, edge cases, and any validation/error paths.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add/adjust the relevant section) and add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the
release contract changes.

OUT OF SCOPE
Do not change SDK runtime behavior, event schema, evaluator behavior, provider integrations, CI Python versions, or package version; do not add runtime dependencies.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 2. Make the verification wheel cover the complete package tree

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Make scripts/verify_release.py build, inspect, and smoke-test the complete `bir` package tree, including `bir.integrations`, instead of only top-level src/bir/*.py files.

WHY
The current hand-built verification wheel omits the entire integrations subpackage, so release verification can pass for an artifact that cannot import documented integrations. Packaging checks should represent the actual setuptools package contents.

IMPLEMENTATION GUIDANCE
Update scripts/verify_release.py to recursively include package Python files beneath src/bir while preserving paths, deterministic ordering, RECORD hashes/sizes, py.typed handling, console entry points, and the zero-network wheel build. Avoid accidentally packaging caches or non-package artifacts; derive package inclusion from package directories rather than an unrestricted repository glob. Strengthen inspect_wheel() required files to include bir/evals.py, bir/cli.py, bir/integrations/__init__.py, bir/integrations/_common.py, and representative integration modules. Extend the fresh-venv smoke test to import all modules exported by bir.integrations without installing any provider SDK. Update tests/test_packaging.py with archive-content and missing-subpackage failure cases.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- The verification wheel contains every intended Python module under src/bir, including all current integration modules, with correct RECORD entries.
- inspect_wheel() rejects a wheel missing the integrations package or another required package module.
- A clean installed-wheel smoke test imports bir, bir.evals, bir.cli, and every documented bir.integrations module with no provider package installed.
- The wheel excludes caches, .bir data, local environment files, tests, docs, and generated artifacts.
- Packaging remains deterministic and the installed distribution is still named `bir-sdk`.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest), covering the happy
path, edge cases, and any validation/error paths.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add/adjust the relevant section) and add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the
release contract changes.

OUT OF SCOPE
Do not replace the offline verifier with a network-dependent release flow, change setuptools package discovery, add provider dependencies, change integration behavior, or publish artifacts.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 3. Enforce strict MkDocs builds in CI

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Add one CI gate that installs the optional docs tooling and runs `mkdocs build --strict` for the existing documentation site.

WHY
The MkDocs site and optional `docs` extra already exist, but CI never validates navigation, links, or strict build warnings. A small isolated gate prevents documentation regressions without affecting runtime installs.

IMPLEMENTATION GUIDANCE
Inspect mkdocs.yml, pyproject.toml, .github/workflows/ci.yml, and the existing CI matrix. Add a docs build step or separate job that runs only once per workflow, not once per Python version, and installs `.[docs]` without moving MkDocs into runtime dependencies. Keep CI easy to diagnose and avoid coupling docs generation to release publishing. If the strict build reveals existing warnings, fix only the relevant docs/nav issues. Add a lightweight repository test only if it usefully guards the optional-extra or workflow contract without duplicating MkDocs itself.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- Pull requests and pushes to main run `mkdocs build --strict` exactly once.
- The job installs documentation tooling only from the optional `docs` extra.
- Broken nav entries, unresolved internal links that MkDocs reports, and strict warnings fail CI.
- The Python 3.10–3.13 SDK test matrix remains unchanged.
- `dependencies = []` remains unchanged and the generated site directory is not committed.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest), covering the happy
path, edge cases, and any validation/error paths.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add/adjust the relevant section) and add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the
release contract changes.

OUT OF SCOPE
Do not deploy the site, change its theme, rewrite documentation content, alter release.yml, add analytics, or add runtime dependencies.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 4. Make trace rotation and sent-sidecar writes multi-process safe

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Add stdlib-only cross-process locking for trace append/rotation and sent-ID sidecar merge/replace operations.

WHY
The current threading.Lock prevents corruption only inside one process; multiple workers or a worker plus `bir send` can race during rotation or lose sent-sidecar updates. Local-first persistence must remain valid under common multi-process deployments.

IMPLEMENTATION GUIDANCE
Study _write_event(), _rotate_trace_file_if_needed(), _rotate_trace_files(), _record_sent_ids(), and existing concurrency tests in src/bir/_sdk.py and tests/test_sdk.py. Introduce a private lock abstraction around a stable sibling lock file, using stdlib advisory locking with explicit POSIX and Windows handling where feasible; keep the in-process lock to avoid thread-level platform quirks. Hold the same trace lock across size check, rotation, and append. Hold a sidecar-specific lock across read, merge, temp write, and replace, and use unique temp names so processes cannot overwrite each other's temporary file. Define lock ordering to prevent deadlocks and ensure exceptions release locks. Do not lock provider calls or network requests. Add subprocess stress tests that synchronize their start and verify all expected IDs/lines, valid JSONL per rotated file, and no orphan temp files.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- Concurrent processes appending to one unrotated trace path produce complete, valid JSONL with every expected event exactly once.
- Concurrent processes writing with rotation enabled do not corrupt files or race renames; retained files obey backup_count.
- Concurrent sent-ID merges preserve the union of all accepted IDs and always leave valid JSON.
- Locks and temporary files are released/cleaned after success and exceptions.
- Single-process behavior, file names, JSON formatting, and default unlimited rotation behavior remain unchanged.
- The implementation is stdlib only and platform support/limitations are documented and tested where CI permits.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest), covering the happy
path, edge cases, and any validation/error paths.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add/adjust the relevant section) and add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the
release contract changes.

OUT OF SCOPE
Do not introduce a database, background writer, daemon, third-party lock dependency, network coordination, schema change, or changes to event ordering semantics.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 5. Upload rotated trace files explicitly

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Add opt-in rotated-file upload support to `send_events()` and `bir send` while keeping active-file-only behavior as the default.

WHY
`load_events(include_rotated=True)` can read retained history, but `send_events()` silently loads only the active file, so rotation can strand unsent events. An explicit upload switch closes that workflow gap without changing existing network behavior.

IMPLEMENTATION GUIDANCE
Add keyword-only `include_rotated: bool = False` to send_events() and thread it through _events_for_sending(), load_events(), and load_traces(). Preserve oldest-file-first chronology, root-before-child ordering within complete traces, and stable ordering for orphan events; deduplicate by event ID when a copied/rotated file overlaps. Keep the existing sent sidecar anchored to the configured active path so mark_sent=True applies across the selected file set. Add `--include-rotated` to `bir send`; also consider the same flag for `bir traces` only if it stays a small direct reuse of the public loader. Update docs/site/sending.md and docs/site/cli-env.md. Do not make rotated upload the default.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- Existing send_events() calls still upload only the active file.
- send_events(include_rotated=True) uploads retained rotated files oldest-first plus the active file.
- Duplicate event IDs across selected files are sent once.
- Complete traces are ordered root-first; orphan events are retained rather than dropped.
- mark_sent=True skips already recorded IDs across active and rotated files.
- `bir send --include-rotated` forwards the option and reports the correct attempted/accepted/skipped counts.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest), covering the happy
path, edge cases, and any validation/error paths.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add/adjust the relevant section) and add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the
release contract changes.

OUT OF SCOPE
Do not change rotation naming/retention, delete uploaded trace files, enable mark_sent by default, alter server endpoints, add compression, or change the wire schema.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 6. Add retry/backoff parity to send_experiment()

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Add bounded exponential retry/backoff controls to `send_experiment()` and expose them through `bir send-experiment`.

WHY
Event uploads already recover from transient network errors, timeouts, and HTTP 5xx, while experiment uploads fail immediately. Matching semantics makes terminal and CI workflows predictable.

IMPLEMENTATION GUIDANCE
Follow the existing send_events() retry idiom without exposing private core implementation as public API. Add keyword-only `retries: int = 2` and `backoff: float = 0.5` to send_experiment(), validate them with existing eval/core validation conventions, and retry only urllib network errors, TimeoutError, and HTTP 5xx. Sleep `backoff * 2**attempt`; do not sleep or retry HTTP 4xx, invalid local files, or invalid success responses. A successful first attempt must perform one request and no sleep. Add matching non-negative `--retries` and `--backoff` CLI options and thread them through. Mock urlopen and time.sleep in tests so they are deterministic and fast.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- Healthy send_experiment() behavior remains one request with unchanged result parsing.
- Network errors, timeouts, and HTTP 5xx retry at the documented delays up to the configured limit.
- HTTP 4xx, malformed local experiment files, and invalid 2xx response bodies fail immediately.
- Negative, boolean, non-numeric, NaN, and infinite retry/backoff inputs raise clear validation errors as appropriate.
- `bir send-experiment --retries ... --backoff ...` forwards validated values and preserves useful exit errors.
- No trace-event send behavior changes.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest), covering the happy
path, edge cases, and any validation/error paths.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add/adjust the relevant section) and add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the
release contract changes.

OUT OF SCOPE
Do not add queues, background retries, jitter, persistent experiment send markers, event-upload refactors, server changes, or new dependencies.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 7. Add ordered async experiment execution

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Add an additive `run_experiment_async()` API for sync or awaitable tasks with bounded concurrency and deterministic result ordering.

WHY
Most provider clients expose async APIs, but the current experiment runner is sync-only. A native async runner avoids unsafe event-loop adapters while preserving Bir's local deterministic evaluation and persistence model.

IMPLEMENTATION GUIDANCE
Study run_experiment(), _run_example(), _run_traced_example(), task argument binding, error handling, score recording, and JSONL/summary persistence in src/bir/evals.py. Add `async def run_experiment_async(..., max_concurrency: int = 1)` with arguments aligned to run_experiment(). Accept a task whose return is either a direct value or an awaitable; use inspect.isawaitable on the returned value rather than only inspecting the callable. Bound in-flight examples with asyncio primitives, store results by original dataset index, then persist JSONL and summaries in dataset order with the existing strict JSON helper. Reuse evaluator execution and redaction logic rather than duplicating semantics. Preserve record_traces behavior with ContextVar isolation for concurrent examples and give generated trace names the same format. On cancellation, cancel and await spawned tasks, avoid writing a misleading successful summary, and re-raise cancellation. Re-export only from bir.evals unless repository conventions establish an evals __all__.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- run_experiment_async() handles async tasks, sync tasks, and sync callables returning awaitables.
- max_concurrency is a positive non-boolean integer; observed concurrent task count never exceeds it.
- Returned results, result JSONL lines, and summary aggregates follow dataset order regardless of completion order.
- Existing evaluator results, continue_on_error behavior, error redaction, task input binding, and summary fields match run_experiment().
- record_traces=True creates isolated trace trees for concurrently running examples.
- Cancellation cleans up child tasks, re-raises CancelledError, and does not write a false success summary.
- Existing run_experiment() behavior and signature remain unchanged.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest), covering the happy
path, edge cases, and any validation/error paths.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add/adjust the relevant section) and add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the
release contract changes.

OUT OF SCOPE
Do not add distributed execution, process/thread pools, async evaluators, retries, rate limiting, provider dependencies, or change the persisted experiment schema.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 8. Add per-evaluator eval-gate tolerances and missing-score policy

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Extend experiment comparison and `bir eval-gate` with per-evaluator tolerances and an explicit policy for baseline scores missing from the candidate.

WHY
Different scores have different acceptable movement, and silently ignoring a removed evaluator can let a CI gate pass after losing coverage. The current global-tolerance behavior should remain the default while stricter policy becomes opt-in.

IMPLEMENTATION GUIDANCE
Extend compare_experiments() additively with a mapping of evaluator name to non-negative finite tolerance; values override the existing global tolerance for matching names. Add a missing-score policy with a small validated vocabulary such as `ignore` (current default) and `regress` (baseline-only names make has_regressions true). Evolve ExperimentDiff to expose deterministic effective tolerances and the policy/reasons needed for machine-readable CLI output while keeping existing fields meaningful. Decide and document whether unknown override names are reported or rejected; prefer a typo-safe explicit error. Add repeatable CLI flags, for example `--score-tolerance NAME=VALUE` and `--missing-score {ignore,regress}`, with clear argparse errors for malformed values. Preserve strict-boundary math.isclose behavior per evaluator.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- Existing compare_experiments(..., tolerance=x) and CLI calls retain current results by default.
- A named tolerance overrides the global tolerance only for that shared evaluator.
- Invalid names/values, booleans, negative values, NaN, infinity, malformed CLI assignments, and duplicate conflicting assignments fail clearly.
- The strict missing-score mode treats baseline_only evaluators as regressions; ignore mode preserves current behavior.
- ExperimentDiff.to_dict() deterministically explains deltas, effective tolerances, missing-score policy, and regression reasons.
- eval-gate exits 1 exactly when the configured policy reports a regression and 0 otherwise.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest), covering the happy
path, edge cases, and any validation/error paths.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add/adjust the relevant section) and add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the
release contract changes.

OUT OF SCOPE
Do not add statistical significance tests, per-example diffs, remote baselines, automatic baseline selection, change experiment persistence schemas, or require a server.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 9. Add an OpenAI Responses API wrapper

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Add a dependency-free OpenAI Responses API tracing wrapper with non-streaming and synchronous iterable-stream support.

WHY
Bir currently wraps `client.chat.completions.create` but not `client.responses.create`, whose response and streaming event shapes differ. Supporting it covers the current OpenAI API without importing or requiring the OpenAI package.

IMPLEMENTATION GUIDANCE
Add a clearly named wrapper such as `trace_response()` to src/bir/integrations/openai.py and re-export it from src/bir/integrations/__init__.py. Follow trace_chat_completion(): accept the provider callable positionally, forward args/kwargs unchanged, prefix SDK options with `bir_`, require an active trace, and use generation() with integration metadata. For normal responses, extract model, `output_text` when available (fall back to the JSON-safe full response shape), and usage fields such as input_tokens, output_tokens, and total_tokens from mapping or attribute objects. For `stream=True`, return a lazy iterable, yield original events unchanged, collect text only from documented response output-text delta event shapes, and use terminal/completed response or usage events to refine model/usage. Finalize on exhaustion, close, or error; never buffer arbitrary full event objects. Put reusable mapping/attribute logic in _common.py only when it benefits multiple wrappers. Use fake response/event objects in tests; never import openai.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- Non-streaming calls return the exact provider response object and record one generation with model/output/usage when present.
- Streaming calls remain lazy, yield the exact original event objects in order, assemble only output-text deltas, and finalize usage/model from supported terminal shapes.
- Sparse mapping and attribute response shapes degrade gracefully without fabricated usage.
- Provider arguments are forwarded unchanged; `bir_*` options are not forwarded.
- Provider exceptions and mid-stream exceptions create error-status generation events and are re-raised with secret text redacted in persisted errors.
- Input/output capture remains off by default and explicit capture is redacted.
- Importing bir.integrations.openai succeeds without the openai package installed.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest), covering the happy
path, edge cases, and any validation/error paths.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add/adjust the relevant section) and add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the
release contract changes.

OUT OF SCOPE
Do not import/install openai, add async client support in this change, alter Chat Completions behavior, implement tool execution, calculate prices, or change the event schema.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 10. Trace generator and async-generator consumption in @observe()

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Extend `@observe()` so sync generator and async-generator functions are traced for their actual iteration lifetime rather than only generator creation.

WHY
Generator bodies execute after the decorated function returns its iterator, so the current wrapper closes the trace before any work, errors, or child events occur. Correct lifecycle tracing is important for streaming LLM applications.

IMPLEMENTATION GUIDANCE
Detect inspect.isgeneratorfunction and inspect.isasyncgenfunction before the existing coroutine/sync branches. Return generator/async-generator wrappers that remain lazy: no user body execution and no trace write until first iteration. Start one observe state when iteration begins; keep ContextVar trace/parent/capture state active while advancing the underlying generator; finalize success on normal exhaustion, error on exceptions from the body, and a documented non-error terminal state on explicit close/aclose or consumer cancellation. Ensure GeneratorExit, throw/athrow, send/asend, close/aclose semantics are faithfully proxied and user exceptions are re-raised. Do not collect all yielded items; when output capture is enabled, record bounded metadata such as yielded-item count rather than changing the wire contract or buffering content. Preserve nested generator calls and concurrent async-generator task isolation. Refactor shared observe finalization only as needed and retain functools metadata/signatures/types as well as pyright permits.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- Decorated sync and async generators remain lazy and yield the same values in the same order.
- The root trace spans first iteration through exhaustion/close/error, and child spans/generations attach to it.
- Exceptions raised during iteration are persisted as redacted errors and re-raised unchanged to the consumer.
- send/throw/close and asend/athrow/aclose behavior is preserved, including finally blocks in the wrapped generator.
- Early close/cancellation resets all ContextVars and does not leak an active trace into later work.
- Output capture remains opt-in and does not buffer an unbounded stream or persist yielded content by default.
- Existing sync-function and coroutine `@observe()` behavior is unchanged.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest), covering the happy
path, edge cases, and any validation/error paths.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add/adjust the relevant section) and add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the
release contract changes.

OUT OF SCOPE
Do not add provider-specific streaming parsing, buffer complete streams, change event statuses/schema, change context-manager APIs, or add async support to integration wrappers.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```

### 11. Allow additive application-specific redaction rules

```text
CONTEXT
Repo: bir-python (PyPI package `bir-sdk`, import name `bir`) — a minimal, zero-runtime-
dependency, local-first LLM tracing + evals SDK. Core in src/bir/_sdk.py, evals in
src/bir/evals.py, optional integrations in src/bir/integrations/, tests in tests/.

TASK
Allow applications to add custom secret-key names and text-redaction patterns while making the built-in redaction rules impossible to disable or replace.

WHY
Organizations often have domain-specific credential formats that Bir cannot know in advance. Additive configuration improves safety without weakening secure defaults or changing the stored schema.

IMPLEMENTATION GUIDANCE
Extend the frozen private _Config and configure() with narrowly named additive options, preferably immutable tuples such as `additional_secret_keys` and `additional_redaction_patterns`. Define exact, case-insensitive key matching separately from the existing substring/name rules; custom patterns should replace the entire matched text with `[redacted]` and run in addition to every built-in pattern. Accept strings and/or compiled stdlib re.Pattern only if typing and flags remain predictable; compile and validate once during configure(), reject empty patterns, invalid regexes, non-string keys, and unboundedly large configuration with clear errors. Never expose a switch that disables defaults or changes the `[redacted]` marker. Ensure the custom rules flow through _safe_capture(), _safe_repr(), _safe_error(), prompt metadata, dataset/result persistence, all integration capture, and every existing write path by extending central redaction helpers rather than patching call sites. Reset test configuration fully in _reset_config_for_tests(). Default behavior must remain byte-for-byte compatible with tests/fixtures/redaction-cases.json; because only user-added rules differ, do not edit the shared fixture.

CONSTRAINTS (do not violate)
- Keep `dependencies = []`; any new dep is an optional extra only.
- Capture stays opt-in; never weaken redaction (keep tests/test_redaction_parity.py green).
- Do NOT change schema_version "1.0" or tests/fixtures/* (shared contract with the bir-app
  repo) unless the task explicitly is a schema change — if so, call it out loudly.
- Keep the public API small; re-export new public symbols from the right __init__.py.

ACCEPTANCE CRITERIA
- With no custom configuration, every existing redaction fixture and serialized payload remains unchanged.
- Added key names redact matching mapping keys case-insensitively while built-in secret-key rules continue to apply.
- Added text patterns redact matches in captured strings, repr fallbacks, exception text, prompts, eval metadata, and integration inputs/outputs.
- Multiple configure() calls have documented replace-or-extend semantics and cannot remove built-in rules.
- Invalid keys/patterns and pathological configuration sizes fail early with clear ValueError/TypeError behavior.
- Raw secrets never appear in trace JSONL or experiment JSONL/summary files in new tests.
- No fixture, schema version, provider dependency, or runtime dependency changes.

TESTS
Add/extend tests under tests/ in the existing style (unittest + pytest), covering the happy
path, edge cases, and any validation/error paths.

VERIFY (run these and report results)
- PYTHONPATH=src python -m unittest discover -s tests
- PYTHONPATH=src python -m pytest tests/test_examples.py
- pyright
- python scripts/verify_release.py

DOCS
Update README.md (add/adjust the relevant section) and add a CHANGELOG.md entry under an
"Unreleased" section (create it if missing). Update docs/SDK_RELEASE_CHECKLIST.md only if the
release contract changes.

OUT OF SCOPE
Do not allow disabling/replacing built-in redaction, add reversible masking, store raw originals, change the redaction marker, edit shared fixtures, add remote policy loading, or change schema_version.

NOTES
Do not bump the version, build, publish, or push tags unless explicitly asked. If you commit,
do NOT add a `Co-Authored-By: Claude` trailer (repo convention). Branch off main before
committing.
```
