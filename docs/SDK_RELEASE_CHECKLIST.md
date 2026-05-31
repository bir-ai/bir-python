# Python SDK Release Checklist

Use this checklist before publishing `packages/python-sdk` as the first usable
Bir SDK package.

## Release Readiness

- Confirm the public API is still intentionally small: `observe`, `span`,
  `generation`, `tool_call`, `retrieval`, `score`, `configure`, `load_events`,
  `load_traces`, and `send_events`.
- Confirm input and output capture remains opt-in by default.
- Confirm common secret-like keys and text patterns are redacted before local
  events are written.
- Confirm schema version `1.0` remains aligned across SDK, server, dashboard,
  and `tests/fixtures`.
- Confirm `retrieval()` still emits the existing `tool_call` event contract with
  `metadata.kind = "retrieval"` and retrieved records under `output.documents`.
- Confirm retrieval query/document capture follows the same opt-in capture and
  redaction behavior as other SDK events.
- Confirm no server is required for the first useful local tracing workflow.
- Confirm `packages/python-sdk/CHANGELOG.md` has an entry for the release.

## Local Verification

From `packages/python-sdk`:

```bash
PYTHONPATH=src ../../.venv/bin/python -m unittest discover -s tests
```

From the repository root:

```bash
./.venv/bin/pyright
```

When server and web dependencies are available, run the contract checks that
protect SDK/server/dashboard alignment:

```bash
cd apps/server
../../.venv/bin/python -m pytest

cd ../web
npm run lint
npm run typecheck
```

When build tooling is available in the environment:

```bash
cd packages/python-sdk
python -m build
python -m twine check dist/*
```

Use a fresh virtual environment for an install smoke test before publishing:

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

- Inspect `packages/python-sdk/pyproject.toml` metadata.
- Inspect the rendered README content on the package index.
- Confirm the wheel contains only the SDK package and expected metadata.
- Confirm no `.env`, local trace files, caches, or generated artifacts are
  included in the package.
- Confirm the version in `pyproject.toml` matches the changelog entry.
- Confirm `README.md` documents `retrieval()` without implying a new event type.

## Publish Gate

Do not publish if any of these are true:

- SDK tests or pyright fail.
- Input/output capture defaults changed to enabled.
- Redaction tests are failing or were removed.
- Retrieval tests are failing or no longer assert `output.documents`.
- SDK-generated events are no longer accepted by the server contract tests.
- The release includes unrelated product features or infrastructure changes.
