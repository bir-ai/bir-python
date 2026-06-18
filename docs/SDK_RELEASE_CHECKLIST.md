# Python SDK Release Candidate Checklist

Use this checklist to keep the `bir` SDK at release-candidate quality. Passing
this checklist does not mean a new version must be published — publish only when
you intend to cut a release.

## Candidate Readiness

- Confirm the public API is still intentionally small: `observe`, `span`,
  `generation`, `tool_call`, `retrieval`, `score`, `configure`, `load_events`,
  `load_traces`, and `send_events`.
- Confirm input and output capture remains opt-in by default.
- Confirm common secret-like keys and text patterns are redacted before local
  events are written.
- Confirm schema version `1.0` stays aligned with the wire-contract fixtures in
  `tests/fixtures/` (`event-schema-v1.json`, `valid-events.jsonl`). These
  fixtures are the shared contract with the server and dashboard, which now live
  in the separate `bir-app` repository.
- Confirm `retrieval()` still emits the existing `tool_call` event contract with
  `metadata.kind = "retrieval"` and retrieved records under `output.documents`.
- Confirm retrieval query/document capture follows the same opt-in capture and
  redaction behavior as other SDK events.
- Confirm `prompt()` attaches prompt identity under generation
  `metadata.prompt` without capturing template text, variables, or rendered
  prompts unless explicitly configured.
- Confirm no server is required for the first useful local tracing workflow.
- Confirm `CHANGELOG.md` accurately describes the changes in the version you are
  about to release.

## Local Verification

Run the repeatable release verification script from the repository root:

```bash
./.venv/bin/python scripts/verify_release.py
```

The script runs SDK unit tests, runs `pyright`, builds a temporary pure-Python
wheel from the SDK package files and metadata, checks the wheel contents for
obvious local/generated artifacts, installs the wheel into a fresh temporary
virtual environment, and executes a smoke test that covers trace, span,
retrieval, prompt metadata, generation, usage, cost, score events,
deterministic evaluators, and local experiment writing.

CI runs the same release verification script on pushes and pull requests to
`main`. The server and dashboard contract tests run in the `bir-app`
repository, against the published `bir` package.

To run the unit tests, example smoke tests, and type checks directly from the
repository root:

```bash
PYTHONPATH=src ./.venv/bin/python -m unittest discover -s tests
PYTHONPATH=src ./.venv/bin/python -m pytest tests/test_examples.py
./.venv/bin/pyright
```

The shared wire-contract fixtures live under `tests/fixtures/`. The SDK keeps
its own copy of the redaction logic and the schema contract (the SDK ships zero
dependencies and cannot import server code), so `tests/test_redaction_parity.py`
and the schema-contract assertions in `tests/test_sdk.py` are what keep the SDK
from drifting away from the `bir-app` server and dashboard.

The release verification script does not require `build`, `twine`, or network
access. When you are ready to publish, the package-index checks are:

```bash
python -m build
python -m twine check dist/*
```

If the release verification script cannot be used, manually build and install
the package in a fresh virtual environment before considering a release:

```bash
python -m venv /tmp/bir-sdk-smoke
/tmp/bir-sdk-smoke/bin/python -m pip install dist/*.whl
/tmp/bir-sdk-smoke/bin/python - <<'PY'
from bir import observe, load_traces

@observe()
def answer() -> str:
    return "ok"

answer()
traces = load_traces()
assert len(traces) == 1
assert traces[0].name == "answer"
PY
```

For retrieval smoke coverage, run the same fresh environment with a captured
retrieval event:

```bash
/tmp/bir-sdk-smoke/bin/python - <<'PY'
from bir import configure, load_traces, observe, retrieval

configure(capture_inputs=True, capture_outputs=True)

@observe()
def answer() -> None:
    with retrieval("vector_search", query="hello") as result:
        result.add_document(id="doc-1", text="local context")

answer()
event = next(event for event in load_traces()[0].events if event.name == "vector_search")
assert event.type == "tool_call"
assert event.metadata["kind"] == "retrieval"
assert event.input == {"query": "hello"}
assert event.output == {"documents": [{"id": "doc-1", "text": "local context"}]}
PY
```

## Manual Package Review

- Inspect `pyproject.toml` metadata.
- Inspect the rendered README content on the package index.
- Confirm the wheel contains only the SDK package and expected metadata.
- Confirm no `.env`, local trace files, caches, or generated artifacts are
  included in the package.
- Confirm the version in `pyproject.toml` matches the changelog entry.
- Confirm `README.md` documents `retrieval()` without implying a new event type.

## Publish Gate

Even when you intend to publish, do not publish if any of these are true:

- SDK tests or pyright fail.
- Input/output capture defaults changed to enabled.
- Redaction tests are failing or were removed.
- Retrieval tests are failing or no longer assert `output.documents`.
- The shared wire-contract fixtures in `tests/fixtures/` changed without a
  matching update in the `bir-app` repository.
- The release includes unrelated changes.
- Remote CI has not passed on the release commit.

## Publishing (tag-driven)

Releases are automated by `.github/workflows/release.yml`. Pushing a version tag
that matches the `pyproject.toml` version builds the package, publishes it to
PyPI via Trusted Publishing, and creates a GitHub Release from the matching
`CHANGELOG.md` section.

```bash
# After CI is green on the release commit and the checklist above passes:
git tag v0.1.2          # tag must equal the pyproject version, prefixed with v
git push origin v0.1.2
```

The PyPI publish step uses `skip-existing`, so tagging a version that was already
uploaded manually still completes (the upload is skipped and the GitHub Release
is created). The GitHub Release job is independent of the PyPI job, so a missing
publisher configuration does not block the release from appearing.

### One-time PyPI Trusted Publisher setup

Trusted Publishing avoids storing a PyPI API token in GitHub. Configure it once
on PyPI before the first automated publish:

1. Sign in to PyPI and open the `bir-sdk` project →
   Settings → Publishing → "Add a new publisher".
2. Choose GitHub and enter:
   - Owner: `bir-ai`
   - Repository: `bir-python`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
3. Save. The next `v*.*.*` tag push will publish without any token.

If you prefer an API token instead, remove the `id-token` permission and the
Trusted Publishing step's reliance on OIDC, add a `PYPI_API_TOKEN` repository
secret, and pass `password: ${{ secrets.PYPI_API_TOKEN }}` to the publish step.
