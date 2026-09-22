#!/usr/bin/env python3
"""The schema ``1.0`` event contract: a recorded corpus, its drift guard, and a
verifier a consuming repository can run without installing the SDK.

Why this exists
---------------
``tests/fixtures/valid-events.jsonl`` is one hand-written trace. It pins the
field shape of five event types and nothing else: not what the framework bridges
record, not how an event names its parent, not how a consumer is meant to group a
store into traces. A consumer -- ``bir-app``'s ingestion endpoint and dashboard --
reads all of that, and none of it was written down anywhere a machine could
check.

This script produces a corpus that is, by construction, *what the SDK actually
records*: every event here was written by the public API into a real trace store
in this process, not typed out by hand. The only thing faked is what would make
two runs differ -- event ids and the clock -- so the same corpus comes out byte
for byte on every machine and any change to what the SDK writes shows up as a
diff in ``check``.

Modes
-----
export
    Re-record the corpus and write it to ``tests/contract/`` (or ``--out DIR``),
    with a manifest of checksums and counts. Run it when a deliberate change to
    the recorded shape has been made, and commit the result.

check
    Re-record the corpus in memory and compare it against the committed copy,
    then verify the manifest. This is the drift guard: it fails when what the SDK
    records stops matching what is committed, which is the schema ``1.0``
    contract changing without anyone saying so. ``tests/test_schema_contract.py``
    runs the same comparison, so the guard holds in the unit-test job too.

verify [PATH]
    Validate a corpus -- this repository's, or a copy a consumer vendored --
    against ``event-schema-v1.json`` and the structural rules a consumer relies
    on: every event carries the required fields, every non-root event's
    ``parent_id`` resolves to another event in the same trace, each trace has
    exactly one root, and no parent chain loops. Imports nothing but the standard
    library and does not import ``bir``, so a consumer repository can run this
    file on its own.

bundle --out DIR
    Write a self-contained directory a consumer repository can vendor: the
    schema, the corpus, the manifest, this file as ``verify_contract.py``, and a
    README saying what the consumer is expected to do with them.

Cross-repository status
-----------------------
What this cannot do is confirm that ``bir-app`` *accepts* the corpus, because no
``bir-app`` checkout or release is reachable from here. ``tests/contract/CROSS_REPO.json``
records that outright, and a test ties the Beta checklist item in
``docs/site/stability.md`` to it: while that file says the validation has not been
run, the checklist item must stay unchecked.

Stdlib only, like the package it records.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = REPO_ROOT / "tests" / "contract"
SCHEMA_PATH = REPO_ROOT / "tests" / "fixtures" / "event-schema-v1.json"

CORE_EVENTS_NAME = "core-events.jsonl"
BRIDGE_EVENTS_NAME = "bridge-events.jsonl"
MANIFEST_NAME = "MANIFEST.json"
CROSS_REPO_NAME = "CROSS_REPO.json"
CORPUS_NAMES = (CORE_EVENTS_NAME, BRIDGE_EVENTS_NAME)

SCHEMA_VERSION = "1.0"

# The clock and the id generator are the only two things that would differ
# between two runs of the same recording, so both are replaced while exporting.
BASE_TIME = "2026-01-01T00:00:00"
TIME_STEP_MS = 100


# --------------------------------------------------------------------------- #
# Recording the corpus (needs the SDK)
# --------------------------------------------------------------------------- #


def _deterministic_recording(module: Any, counters: dict[str, int]) -> Any:
    """Return a context manager replacing ``module``'s id and clock sources.

    Ids become ``e0001``, ``e0002``, ... in the order the SDK asks for them, and
    each timestamp is 100 ms after the previous one. Recording is otherwise the
    real thing: the same writer, the same redaction, the same field order.

    ``counters`` is owned by the caller and shared by every scenario in one
    export, so ids stay unique across the whole corpus: a per-scenario counter
    would restart at ``e0001`` and give every trace the same root id.
    """

    import contextlib
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch
    from uuid import UUID

    base = datetime.fromisoformat(BASE_TIME).replace(tzinfo=timezone.utc)

    def next_id() -> str:
        counters["id"] += 1
        return f"e{counters['id']:04d}"

    def next_time() -> str:
        counters["time"] += 1
        return (base + timedelta(milliseconds=TIME_STEP_MS * counters["time"])).isoformat()

    def next_uuid() -> UUID:
        counters["uuid"] += 1
        return UUID(int=counters["uuid"])

    @contextlib.contextmanager
    def deterministic() -> Any:
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(module, "_new_id", next_id))
            stack.enter_context(patch.object(module, "_now", next_time))
            # Two bridges mint an id of their own -- AG2's session id and
            # LlamaIndex's fallback event id -- and they reach the record, so
            # they are pinned too or the corpus differs on every run.
            for imported in list(sys.modules.values()):
                name = getattr(imported, "__name__", "")
                if name.startswith("bir.integrations.") and hasattr(imported, "uuid4"):
                    stack.enter_context(patch.object(imported, "uuid4", next_uuid))
            yield

    return deterministic()


def _record(scenario: Any, counters: dict[str, int]) -> bytes:
    """Run one scenario against a throwaway store and return the bytes it wrote."""

    import tempfile

    import bir
    from bir import _sdk

    with tempfile.TemporaryDirectory(prefix="bir-contract-") as directory:
        store = Path(directory) / "traces.jsonl"
        # A scenario must start from no trace at all: recorded inside somebody
        # else's open trace -- a test that left one, a REPL that is tracing --
        # the same scenario records spans where it should record roots, and the
        # corpus would depend on what ran before it.
        outer_context = _sdk._snapshot_context()
        _sdk._restore_context((None, None, None, None, False))
        _sdk._reset_config_for_tests()
        bir.configure(
            trace_path=str(store),
            enabled=True,
            capture_inputs=True,
            capture_outputs=True,
            service_name="rag-api",
            environment="production",
            source="python-sdk",
            model_prices={"demo-model": {"input": 0.000001, "output": 0.000002, "currency": "USD"}},
        )
        try:
            with _deterministic_recording(_sdk, counters):
                scenario()
        finally:
            recorded = store.read_bytes() if store.exists() else b""
            _sdk._reset_config_for_tests()
            _sdk._restore_context(outer_context)
    return recorded


def _core_scenarios() -> list[Any]:
    """Return the scenarios that cover the event types a consumer must handle."""

    import bir

    def answered_question() -> None:
        """One successful trace carrying every event type the schema allows."""

        @bir.observe(name="answer_question", metadata={"route": "/answer"})
        def answer(question: str) -> str:
            with bir.span("retrieve_context"):
                with bir.retrieval("vector_search", query=question) as documents:
                    documents.set_documents(
                        [
                            {
                                "id": "doc-1",
                                "text": "Bir records local traces with JSONL.",
                                "score": 0.82,
                                "source": "docs",
                            }
                        ]
                    )
                with bir.tool_call("search_docs", input={"query": question}) as tool:
                    tool.set_output(["doc-1"])
            prompt = bir.prompt(
                "answer_question",
                version="v1",
                template="Answer {{question}}",
                variables={"question": question},
            )
            with bir.generation(
                "local.llm",
                model="demo-model",
                input={"question": question},
                prompt=prompt,
            ) as generation:
                generation.set_output({"message": "local context: hello"})
                generation.set_usage(input_tokens=12, output_tokens=24)
            bir.score("helpfulness", 0.82, metadata={"evaluator": "heuristic"})
            return "local context: hello"

        answer("hello")

    def failed_request() -> None:
        """One failed trace: the status, the message, and a failed child."""

        @bir.observe(name="answer_question")
        def answer(question: str) -> str:
            with bir.generation("local.llm", model="demo-model", input={"question": question}):
                raise RuntimeError("upstream model refused the request")

        try:
            answer("hello")
        except RuntimeError:
            pass

    return [answered_question, failed_request]


def _bridge_scenarios() -> list[Any]:
    """Return one scenario per shipped framework bridge.

    The drivers come from ``tests/test_integration_contract.py`` rather than from
    copies here: those declarations are what the conformance matrix already runs
    every bridge through, so a corpus built from them cannot describe a tree no
    bridge records, and a newly shipped bridge arrives here with its declaration.
    """

    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from integration_bridge_contract import CHILD_KEY, ROOT_KEY  # noqa: PLC0415
    from test_integration_contract import BRIDGES  # noqa: PLC0415

    def scenario_for(bridge: Any) -> Any:
        def run() -> None:
            handler = bridge.handler()
            bridge.root.start(handler, ROOT_KEY, None)
            bridge.generation.record(handler, CHILD_KEY, ROOT_KEY)
            bridge.root.end(handler, ROOT_KEY)

        run.__name__ = bridge.id.replace(".", "_")
        return run

    return [scenario_for(bridge) for bridge in BRIDGES]


def build_corpus() -> dict[str, bytes]:
    """Record every scenario and return the corpus files as bytes."""

    counters = {"id": 0, "time": 0, "uuid": 0}
    core = b"".join(_record(scenario, counters) for scenario in _core_scenarios())
    bridges = b"".join(_record(scenario, counters) for scenario in _bridge_scenarios())
    return {CORE_EVENTS_NAME: core, BRIDGE_EVENTS_NAME: bridges}


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _counts(data: bytes) -> dict[str, Any]:
    events = [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]
    types: dict[str, int] = {}
    traces: list[str] = []
    for event in events:
        types[event["type"]] = types.get(event["type"], 0) + 1
        if event["trace_id"] not in traces:
            traces.append(event["trace_id"])
    return {
        "events": len(events),
        "traces": len(traces),
        "event_types": dict(sorted(types.items())),
        "sha256": sha256_hex(data),
    }


def render_manifest(corpus: dict[str, bytes]) -> bytes:
    """Render the manifest: what each file holds, and the checksum that pins it."""

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "schema": {
            "file": SCHEMA_PATH.name,
            "sha256": sha256_hex(SCHEMA_PATH.read_bytes()),
        },
        "generated_by": "scripts/contract.py export",
        "files": {name: _counts(corpus[name]) for name in sorted(corpus)},
    }
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- #
# Verification (no SDK import -- a consumer repository runs this)
# --------------------------------------------------------------------------- #


class ContractError(Exception):
    """A corpus that does not meet the contract."""


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    raise ContractError(f"schema uses an unsupported type: {expected!r}")


def validate_against_schema(event: Any, schema: dict[str, Any], where: str) -> list[str]:
    """Validate one event against the subset of JSON Schema the contract uses.

    Supported: ``type`` (one or a list), ``const``, ``enum``, ``minLength``,
    ``required``, ``properties``, ``additionalProperties`` as a schema, and
    ``allOf`` with ``if``/``then``. Anything else in the schema file is refused
    outright rather than skipped, so the contract cannot grow a rule this
    verifier silently ignores.
    """

    errors: list[str] = []
    known = {
        "$schema",
        "$id",
        "title",
        "description",
        "format",
        "type",
        "const",
        "enum",
        "minLength",
        "required",
        "properties",
        "additionalProperties",
        "allOf",
        "if",
        "then",
    }
    unsupported = set(schema) - known
    if unsupported:
        raise ContractError(f"schema uses unsupported keywords: {sorted(unsupported)}")

    expected_type = schema.get("type")
    if expected_type is not None:
        options = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(_type_matches(event, option) for option in options):
            errors.append(f"{where}: expected type {expected_type}, got {type(event).__name__}")
            return errors

    if "const" in schema and event != schema["const"]:
        errors.append(f"{where}: expected {schema['const']!r}, got {event!r}")
    if "enum" in schema and event not in schema["enum"]:
        errors.append(f"{where}: {event!r} is not one of {schema['enum']}")
    if "minLength" in schema and isinstance(event, str) and len(event) < schema["minLength"]:
        errors.append(f"{where}: shorter than {schema['minLength']} characters")

    if isinstance(event, dict):
        for field in schema.get("required", []):
            if field not in event:
                errors.append(f"{where}: missing required field {field!r}")
        properties = schema.get("properties", {})
        for field, subschema in properties.items():
            if field in event:
                errors.extend(validate_against_schema(event[field], subschema, f"{where}.{field}"))
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            for field, value in event.items():
                if field not in properties:
                    errors.extend(validate_against_schema(value, additional, f"{where}.{field}"))

    for branch in schema.get("allOf", []):
        condition = branch.get("if")
        consequence = branch.get("then")
        if condition is None or consequence is None:
            raise ContractError("schema uses an allOf branch without if/then")
        if not validate_against_schema(event, condition, where):
            errors.extend(validate_against_schema(event, consequence, where))

    return errors


def _structural_errors(events: list[dict[str, Any]], where: str) -> list[str]:
    """Check what a consumer needs beyond the field shape of one event.

    These are the rules that make a flat file a tree: ids are unique, a
    ``parent_id`` names an event in the same file and the same trace, each trace
    has exactly one root, and following parents from any event reaches that root
    rather than going round forever.
    """

    errors: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    for event in events:
        event_id = event.get("id")
        if not isinstance(event_id, str):
            errors.append(f"{where}: an event has no string id")
            continue
        if event_id in by_id:
            errors.append(f"{where}: duplicate event id {event_id!r}")
        by_id[event_id] = event

    roots: dict[str, list[str]] = {}
    for event in events:
        event_id, trace_id = event.get("id"), event.get("trace_id")
        if not isinstance(event_id, str) or not isinstance(trace_id, str):
            continue
        parent_id = event.get("parent_id")
        if event.get("type") == "trace":
            roots.setdefault(trace_id, []).append(event_id)
            if event_id != trace_id:
                errors.append(f"{where}: root {event_id!r} does not carry its own trace id {trace_id!r}")
            if parent_id is not None:
                errors.append(f"{where}: root {event_id!r} has a parent {parent_id!r}")
            continue
        if not isinstance(parent_id, str) or not parent_id:
            errors.append(f"{where}: {event_id!r} is a {event.get('type')!r} with no parent")
            continue
        parent = by_id.get(parent_id)
        if parent is None:
            errors.append(f"{where}: {event_id!r} names a parent {parent_id!r} that is not in this corpus")
            continue
        if parent.get("trace_id") != trace_id:
            errors.append(f"{where}: {event_id!r} and its parent {parent_id!r} are in different traces")

    for trace_id, found in sorted(roots.items()):
        if len(found) > 1:
            errors.append(f"{where}: trace {trace_id!r} has {len(found)} root events")
    for event in events:
        trace_id = event.get("trace_id")
        if isinstance(trace_id, str) and trace_id not in roots:
            errors.append(f"{where}: trace {trace_id!r} has no root event")
            break

    errors.extend(_ancestry_errors(events, by_id, where))
    return errors


def _ancestry_errors(events: list[dict[str, Any]], by_id: dict[str, dict[str, Any]], where: str) -> list[str]:
    """Follow every event's parents up to its root, refusing a chain that loops."""

    errors: list[str] = []
    for event in events:
        seen: set[str] = set()
        current = event
        while current.get("type") != "trace":
            current_id = current.get("id")
            if not isinstance(current_id, str) or current_id in seen:
                errors.append(f"{where}: parent chain from {event.get('id')!r} loops")
                break
            seen.add(current_id)
            parent_id = current.get("parent_id")
            parent = by_id.get(parent_id) if isinstance(parent_id, str) else None
            if parent is None:
                break  # already reported as an unresolved parent
            current = parent
    return errors


def read_events(path: Path) -> list[dict[str, Any]]:
    """Read one JSONL corpus file, refusing anything that is not an event object."""

    events: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise ContractError(f"{path.name}:{number}: not valid JSON ({error})") from error
        if not isinstance(event, dict):
            raise ContractError(f"{path.name}:{number}: expected an object")
        events.append(event)
    return events


def verify_corpus(directory: Path) -> list[str]:
    """Verify a corpus directory, returning the problems found."""

    schema_path = directory / SCHEMA_PATH.name
    if not schema_path.is_file():
        schema_path = SCHEMA_PATH
    if not schema_path.is_file():
        return [f"{SCHEMA_PATH.name}: not found beside the corpus"]
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    problems: list[str] = []
    for name in CORPUS_NAMES:
        path = directory / name
        if not path.is_file():
            problems.append(f"{name}: missing from {directory}")
            continue
        events = read_events(path)
        if not events:
            problems.append(f"{name}: holds no events")
            continue
        for number, event in enumerate(events, start=1):
            problems.extend(validate_against_schema(event, schema, f"{name}:{number}"))
        problems.extend(_structural_errors(events, name))

    manifest_path = directory / MANIFEST_NAME
    if manifest_path.is_file():
        problems.extend(_manifest_problems(directory, json.loads(manifest_path.read_text(encoding="utf-8"))))
    else:
        problems.append(f"{MANIFEST_NAME}: missing from {directory}")
    return problems


def _manifest_problems(directory: Path, manifest: dict[str, Any]) -> list[str]:
    """Check the manifest against the files beside it."""

    problems: list[str] = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"{MANIFEST_NAME}: records schema_version {manifest.get('schema_version')!r}")
    for name in CORPUS_NAMES:
        recorded = manifest.get("files", {}).get(name)
        path = directory / name
        if recorded is None:
            problems.append(f"{MANIFEST_NAME}: {name} is not recorded")
            continue
        if not path.is_file():
            continue
        actual = _counts(path.read_bytes())
        if actual != recorded:
            problems.append(
                f"{MANIFEST_NAME}: {name} does not match what is recorded\n"
                f"      recorded: {json.dumps(recorded, sort_keys=True)}\n"
                f"      on disk:  {json.dumps(actual, sort_keys=True)}"
            )
    return problems


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_export(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser() if args.out else CONTRACT_DIR
    corpus = build_corpus()
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name, data in sorted(corpus.items()):
        path = out / name
        if not path.exists() or path.read_bytes() != data:
            path.write_bytes(data)
            written.append(name)
    manifest = render_manifest(corpus)
    manifest_path = out / MANIFEST_NAME
    if not manifest_path.exists() or manifest_path.read_bytes() != manifest:
        manifest_path.write_bytes(manifest)
        written.append(MANIFEST_NAME)

    print(f"Corpus exported to {out}")
    for name, data in sorted(corpus.items()):
        counts = _counts(data)
        print(f"  {name}: {counts['events']} events in {counts['traces']} traces")
    print("  changed: " + (", ".join(written) if written else "nothing"))
    return 0


def cross_repo_status(directory: Path) -> str:
    """Say, in one line, whether a real consumer has ever read this corpus.

    Everything else this script checks is the SDK checking itself. Whether
    ``bir-app`` accepts the corpus is the one question it cannot answer, so the
    answer is read from the attestation and printed rather than left implied by
    a passing run.
    """

    path = directory / CROSS_REPO_NAME
    if not path.is_file():
        return f"cross-repo: UNKNOWN -- no {CROSS_REPO_NAME} beside the corpus"
    try:
        attestation = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return f"cross-repo: UNKNOWN -- {CROSS_REPO_NAME} is not readable ({error})"
    if not attestation.get("validated"):
        reason = str(attestation.get("reason") or "no reason recorded").split(".")[0]
        return f"cross-repo: NOT VALIDATED -- {reason}. See {CROSS_REPO_NAME}"
    consumer = attestation.get("consumer", {})
    ref = consumer.get("release") or consumer.get("commit") or "an unrecorded build"
    return f"cross-repo: validated against {consumer.get('repository', 'the consumer')} {ref} on {attestation.get('validated_at')}"


def cmd_check(_args: argparse.Namespace) -> int:
    recorded = build_corpus()
    problems: list[str] = []
    for name, data in sorted(recorded.items()):
        path = CONTRACT_DIR / name
        if not path.is_file():
            problems.append(f"{name}: missing from {CONTRACT_DIR}")
            continue
        committed = path.read_bytes()
        if committed != data:
            problems.append(
                f"{name}: what the SDK records is not what is committed\n"
                f"      committed: {sha256_hex(committed)} ({len(committed)} bytes)\n"
                f"      recorded:  {sha256_hex(data)} ({len(data)} bytes)"
            )
    manifest_path = CONTRACT_DIR / MANIFEST_NAME
    if not manifest_path.is_file():
        problems.append(f"{MANIFEST_NAME}: missing from {CONTRACT_DIR}")
    elif manifest_path.read_bytes() != render_manifest(recorded):
        problems.append(f"{MANIFEST_NAME}: does not describe the corpus the SDK records")

    problems.extend(verify_corpus(CONTRACT_DIR))
    if problems:
        print("Schema 1.0 contract drift detected:\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nThe committed corpus no longer matches what the SDK records. If the\n"
            "change is deliberate, re-export it and say so in the change:\n"
            "    python scripts/contract.py export",
            file=sys.stderr,
        )
        return 1
    print(f"OK: the committed corpus is what the SDK records, and it meets the {SCHEMA_VERSION} contract")
    print(cross_repo_status(CONTRACT_DIR))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    directory = Path(args.path).expanduser() if args.path else Path(__file__).resolve().parent
    if not (directory / CORE_EVENTS_NAME).is_file() and CONTRACT_DIR.is_dir():
        directory = CONTRACT_DIR
    try:
        problems = verify_corpus(directory)
    except ContractError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if problems:
        print(f"Corpus at {directory} does not meet the schema {SCHEMA_VERSION} contract:\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"OK: the corpus at {directory} meets the schema {SCHEMA_VERSION} contract")
    print(cross_repo_status(directory))
    return 0


BUNDLE_README = """\
# Bir event contract bundle (schema {version})

Vendored from the `bir-python` SDK by `scripts/contract.py bundle`. Everything
here was recorded by the SDK's public API, not written by hand.

| File | What it is |
| --- | --- |
| `{schema}` | the `{version}` event schema |
| `{core}` | one successful and one failed trace, covering every event type |
| `{bridges}` | the event trees the framework bridges record |
| `{manifest}` | checksums and per-file counts |
| `verify_contract.py` | a stdlib-only verifier; needs neither the SDK nor this bundle's origin |

## What a consumer is expected to do

1. `python verify_contract.py verify .` -- confirms the bundle is intact and
   meets the contract on its own terms.
2. Feed both JSONL files through your own ingestion path and assert it accepts
   every event, groups them into the traces the manifest counts, and resolves
   every `parent_id` to an event in the same trace.
3. Report the result back to the SDK repository, which records it in
   `tests/contract/CROSS_REPO.json`. Until that file says a real run happened,
   the SDK's Beta checklist keeps the cross-repo item unchecked.
"""


def cmd_bundle(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    for name in CORPUS_NAMES + (MANIFEST_NAME, CROSS_REPO_NAME):
        source = CONTRACT_DIR / name
        if not source.is_file():
            print(f"error: {name} is missing; run `python scripts/contract.py export` first", file=sys.stderr)
            return 1
        shutil.copyfile(source, out / name)
    shutil.copyfile(SCHEMA_PATH, out / SCHEMA_PATH.name)
    shutil.copyfile(Path(__file__).resolve(), out / "verify_contract.py")
    (out / "README.md").write_text(
        BUNDLE_README.format(
            version=SCHEMA_VERSION,
            schema=SCHEMA_PATH.name,
            core=CORE_EVENTS_NAME,
            bridges=BRIDGE_EVENTS_NAME,
            manifest=MANIFEST_NAME,
        ),
        encoding="utf-8",
    )
    print(f"Bundle written to {out}")
    print("  run it with: python verify_contract.py verify .")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="contract.py",
        description="Record, guard, and verify the schema 1.0 event contract.",
    )
    sub = parser.add_subparsers(dest="mode", required=True)
    p_export = sub.add_parser("export", help="re-record the corpus and write it (needs the SDK)")
    p_export.add_argument("--out", metavar="DIR", help=f"where to write (default: {CONTRACT_DIR})")
    sub.add_parser("check", help="fail if the committed corpus is not what the SDK records (needs the SDK)")
    p_verify = sub.add_parser("verify", help="validate a corpus against the schema and structure (stdlib only)")
    p_verify.add_argument("path", nargs="?", metavar="PATH", help="corpus directory (default: beside this file)")
    p_bundle = sub.add_parser("bundle", help="write a self-contained bundle for a consumer repository")
    p_bundle.add_argument("--out", metavar="DIR", required=True, help="directory to write the bundle into")

    args = parser.parse_args(argv)
    if args.mode == "export":
        return cmd_export(args)
    if args.mode == "check":
        return cmd_check(args)
    if args.mode == "verify":
        return cmd_verify(args)
    return cmd_bundle(args)


if __name__ == "__main__":
    raise SystemExit(main())
