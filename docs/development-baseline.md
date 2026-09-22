# Development baseline

A recorded starting point for refactoring work: what the suite, the gates, and
the benchmarks report on an unmodified tree, so a later change can be checked
against measurements rather than against memory. Nothing here changes behaviour;
every number below was produced by running the commands in this document.

Re-measure before trusting it. A baseline belongs to the commit and the machine
that produced it: re-run the same commands after a refactor and compare, and
re-record the whole file when the commit under test moves on.

## What was measured

| | |
|---|---|
| Commit | `704a49ba9a77cba494f6856066e45dfeeb9b9043` (`v0.3.0-86-g704a49b`), working tree clean — this file was added on top of it and changes nothing it measures |
| Package version | `bir-sdk` 0.3.0, schema version `1.0` |
| Python | CPython 3.14.6 (Clang 21.0.0), not free-threaded (`Py_GIL_DISABLED` is 0) |
| Platform | macOS 26.6.2, arm64 (`darwin`) |
| Runtime source | 40 modules, 19,569 lines under `src/bir` (19 integration bridges) |
| Tests | 57 files (55 `test_*.py` plus 2 shared contract helpers), 36,892 lines |
| Tooling | ruff 0.16.1, coverage 7.15.2 (C extension), pyright 1.1.410, mkdocs 1.6.1 |
| Optional extras installed | `otel` (so `export_otel` benchmarks and OTLP tests run) |

CI pins `pyright==1.1.410`; `npx pyright` resolves to a newer release
(1.1.414 today) and is not the pinned gate. Use `.venv/bin/pyright`.

## Reproducing the whole baseline

Run from the repository root with the project's virtualenv. Each command is the
canonical one — the same invocation CI and `scripts/verify_release.py` use.

```bash
PYTHONPATH=src python -m unittest discover -s tests
```

```bash
PYTHONPATH=src python -m coverage run -m unittest discover -s tests && python -m coverage report
```

```bash
python scripts/verify_release.py
```

```bash
python scripts/benchmarks.py --json baseline.json
```

```bash
python scripts/benchmarks.py --baseline baseline.json
```

```bash
python -m ruff check . && python -m ruff format --check . && .venv/bin/pyright && python -m mkdocs build --strict
```

```bash
PYTHONPATH=src python -m pytest tests/test_examples.py -q && python scripts/fixtures.py check
```

`scripts/verify_release.py` is the canonical gate and runs most of the above
itself: coverage-instrumented unit tests under `PYTHONWARNINGS=error::ResourceWarning`,
the coverage floor, ruff lint and format, pyright, then hermetic wheel and sdist
builds each installed into a fresh virtualenv and smoke-tested through the
installed `bir` console script.

## Test suite

| | |
|---|---|
| Tests run | **1893** |
| Subtests | 2183 |
| Failures / errors | 0 / 0 |
| Skipped | 1 |
| Wall time | 27.9 s plain, 27.0 s under coverage |

The single skip is expected on this machine:

```
test_free_threading.BuildIdentificationTests.test_a_free_threaded_build_reports_its_runtime_gil_state
  -> not a free-threaded build
```

Windows-only paths are likewise never executed here: the `msvcrt` locking
branch, and the one line that dispatches to the Windows half of the prune
sweep's liveness probe (the probe's own logic is covered everywhere, driven
against a stand-in library). CI's Windows leg is the only evidence for the rest.

Single modules must be run through discovery rather than by dotted name —
`tests/test_stability_contract.py` imports `test_integration_contract` by bare
name, which only resolves when `tests/` is on `sys.path`:

```bash
PYTHONPATH=src python -m unittest discover -s tests -p "test_cli.py"
```

```bash
PYTHONPATH=src:tests python -m unittest test_cli.AutomationJsonOutputTests
```

## Coverage

Branch coverage is on (`[tool.coverage.run] branch = true`) and the gate is
`fail_under = 89.0`.

| | |
|---|---|
| Total | **94.52%** branch coverage |
| Statements | 7,992 total, 333 missed |
| Branches | 2,756 total, 234 partial |
| Files at 100% | 7 (skipped in the report) |

Per-module, lowest first — the ones worth watching during a refactor:

| Module | Stmts | Miss | Branch | BrPart | Cover |
|---|---|---|---|---|---|
| `src/bir/__main__.py` | 4 | 1 | 2 | 1 | 66.67% |
| `src/bir/__init__.py` | 7 | 2 | 0 | 0 | 71.43% |
| `src/bir/integrations/crewai.py` | 229 | 25 | 104 | 12 | 87.09% |
| `src/bir/integrations/autogen.py` | 232 | 16 | 96 | 20 | 89.02% |
| `src/bir/integrations/_common.py` | 70 | 8 | 22 | 2 | 89.13% |
| `src/bir/integrations/langchain.py` | 244 | 23 | 100 | 11 | 89.53% |
| `src/bir/integrations/llamaindex.py` | 256 | 16 | 122 | 20 | 90.48% |
| `src/bir/_cli_present.py` | 139 | 10 | 42 | 7 | 90.61% |
| `src/bir/integrations/openai_agents.py` | 227 | 10 | 108 | 14 | 91.64% |
| `src/bir/_eval_models.py` | 275 | 16 | 84 | 13 | 91.92% |
| `src/bir/_storage.py` | 920 | 59 | 322 | 37 | 91.95% |
| `src/bir/integrations/pydantic_ai.py` | 249 | 13 | 120 | 10 | 93.22% |
| `src/bir/integrations/haystack.py` | 163 | 7 | 54 | 7 | 93.55% |
| `src/bir/cli.py` | 490 | 27 | 134 | 8 | 94.39% |
| `src/bir/evals.py` | 809 | 33 | 264 | 19 | 94.78% |
| `src/bir/integrations/vertexai.py` | 110 | 5 | 44 | 3 | 94.81% |

Those are every module under 95%; the other 17 sit above it, the largest
being `src/bir/_sdk.py` at 95.39% (933 statements) and
`src/bir/_capture.py` at 99.32%. `python -m coverage report -m` prints the missing
lines; `python -m coverage html` writes a browsable report.

## Contract tests that guard the public surface

Each of these was run on its own and passes. They are the cheapest early signal
that a refactor moved something it should not have.

| Guard | Command | Result |
|---|---|---|
| Public API inventory (exported names, import identities, packaging) | `PYTHONPATH=src python -m unittest discover -s tests -p "test_architecture.py"` | 9 tests, OK |
| Public API documented surface (every export on a docs page, and back) | `PYTHONPATH=src python -m unittest discover -s tests -p "test_stability_contract.py"` | 12 tests, OK |
| CLI `--json` output contract | `PYTHONPATH=src:tests python -m unittest test_cli.AutomationJsonOutputTests` | 7 tests, OK |
| Event schema `1.0` against the shared artifact | `PYTHONPATH=src:tests python -m unittest test_sdk.SdkTests.test_sdk_event_contract_matches_schema_artifact test_sdk.SdkTests.test_load_events_accepts_schema_contract_fixtures test_sdk.SdkTests.test_load_events_rejects_invalid_schema` | 3 tests, OK |
| Experiment and redaction fixtures | `PYTHONPATH=src:tests python -m unittest test_experiment_contract test_redaction_parity` | 5 tests, OK |
| Shared fixture checksums | `python scripts/fixtures.py check` | `OK: 4 shared fixtures match tests/fixtures/CHECKSUMS.sha256` |

Inventory sizes a refactor must not change silently: `bir.__all__` 19 names,
`bir.evals.__all__` 32, `bir.testing.__all__` 2, `bir.logging.__all__` 4,
`bir.integrations.__all__` 35 across 19 bridge modules, 13 CLI commands
(`traces show stats tail experiments experiment-show experiment-report send
send-experiment eval-gate export-otel prune config`), and a conformance matrix
of 7 bridges against 14 contracts.

## Release gate

```
python scripts/verify_release.py   ->   "Bir SDK release verification passed."   (exit 0, ~41 s)
```

Its stages, in the order `main()` runs them: unit tests with branch coverage
(preceded by a coverage reset) and the coverage gate, ruff check, ruff format
check, pyright, wheel build, wheel inspect, wheel install into a fresh venv and a
smoke test through the installed `bir` console script, then sdist build, sdist
inspect, sdist install into a second fresh venv, and its smoke test. Everything
happens inside one temporary directory that is removed afterwards.

The `==> wheel build` lines that appear *during* the test phase are
`tests/test_packaging.py` building its own artifacts, not the gate's.

Standalone results of the same gates:

| Gate | Result |
|---|---|
| `ruff check .` | All checks passed! |
| `ruff format --check .` | 104 files already formatted |
| `.venv/bin/pyright` | 0 errors, 0 warnings, 0 informations |
| `mkdocs build --strict` | built, no warnings |
| `pytest tests/test_examples.py` | 3 passed |

## Benchmarks

Full run (not `--smoke`), 5 repeats per case, time and peak memory measured in
separate passes. Recorded JSON envelope: `schema: 1`, `repeat: 5`, environment
`CPython 3.14.6 / darwin / arm64 / b364602`.

| Benchmark | Group | Units | Best (ms) | Median (ms) | Per unit (µs) | Peak (KiB) |
|---|---|---|---|---|---|---|
| `trace_disabled` | tracing | 20000 | 141.50 | 142.38 | 7.08 | 1.9 |
| `trace_sampled_out` | tracing | 20000 | 144.28 | 145.06 | 7.21 | 2.2 |
| `trace_recorded` | tracing | 2000 | 151.58 | 161.40 | 75.79 | 262.4 |
| `generation_recorded` | tracing | 1000 | 149.16 | 150.08 | 149.16 | 263.9 |
| `capture_redaction` | capture | 5000 | 104.38 | 105.02 | 20.88 | 1.6 |
| `capture_large_value` | capture | 500000 | 83.92 | 84.60 | 0.17 | 1419.8 |
| `store_rotation` | storage | 1000 | 177.12 | 181.96 | 177.12 | 263.8 |
| `load_events` | storage | 5000 | 109.90 | 111.50 | 21.98 | 22761.0 |
| `load_traces` | storage | 5000 | 123.85 | 124.60 | 24.77 | 24162.1 |
| `prune_keep_last` | storage | 2000 | 94.27 | 95.23 | 47.13 | 299.4 |
| `send_batched` | transport | 2000 | 149.40 | 150.86 | 74.70 | 2403.8 |
| `cli_traces` | cli | 2000 | 47.99 | 48.36 | 24.00 | 1999.0 |
| `cli_stats` | cli | 2000 | 49.07 | 49.76 | 24.54 | 1998.6 |
| `cli_show` | cli | 2000 | 42.57 | 42.60 | 21.29 | 229.0 |
| `export_otel` | cli | 2000 | 157.21 | 157.60 | 78.60 | 363.5 |
| `experiment_sync` | evals | 500 | 14.56 | 15.03 | 29.12 | 622.0 |
| `experiment_async` | evals | 500 | 16.90 | 16.97 | 33.80 | 1087.5 |

Mapping the cases to the operations this baseline is meant to protect: the
**write** path is `trace_recorded`, `generation_recorded`, and `store_rotation`
(`trace_disabled` and `trace_sampled_out` measure the cost when nothing is
written, which is what makes leaving tracing in place affordable); **load** is
`load_events`, `load_traces`, and the three `cli_*` cases, which read the same
stores through the CLI; **prune** is `prune_keep_last`; **send** is
`send_batched`; **evals** are `experiment_sync` and `experiment_async`. The send
case stubs the HTTP call, so it measures batching and bookkeeping rather than the
network — no benchmark opens a socket.

**How much noise to expect.** The same tree measured twice, compared with
`--baseline`, moved by at most 2.5% on time and 0.7% on memory — except the two
cases whose peak is a couple of KiB (`trace_sampled_out` −14.2%,
`capture_redaction` +17.4%), where a percentage of ~2 KiB is meaningless. Treat a
time change under ~3%, or a memory change on a sub-3 KiB case, as noise. The
harness's own `--tolerance` defaults to 25% and failed nothing on the repeat run.

`--baseline` refuses a file written by a different result schema and warns when
the recorded Python version differs from the running one.

## Known failures, warnings, and noise

Nothing here is a failure of the suite; these are the things a first-time reader
would otherwise mistake for one.

- **One skipped test**, `test_free_threading` — this interpreter is not a
  free-threaded build. CI runs a free-threaded 3.14 leg on Linux.
- **stdout noise during the suite.** `test_store_permissions.StorePermissionTests.test_a_pruned_store_is_still_private`
  calls the CLI with `--json` without redirecting stdout, so a prune result JSON
  object is printed into the test output. Cosmetic, deterministic, and unrelated
  to the assertions.
- **stderr noise during the suite.** The experiment-timeout tests deliberately
  exercise the "workers still running" reports, so lines such as
  `bir experiment 'timeout' returned with 1 task(s) from timed-out examples still
  running` appear during `verify_release.py`'s run. They are the behaviour under
  test.
- **pyright version notice.** The pinned 1.1.410 prints
  `WARNING: there is a new pyright version available (v1.1.410 -> v1.1.414)` on
  every run. The pin is deliberate; CI installs the same version.
- **Coverage gaps that are structural rather than untested:** the Windows
  `msvcrt` import and lock branch in `src/bir/_storage.py`, `src/bir/__main__.py`'s
  `__main__` guard, and the free-threaded build branch. None can execute on this
  machine.
- **No network, no provider SDKs.** Every integration test drives a fake client;
  installing a provider SDK is not required and does not change the counts above.
