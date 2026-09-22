"""The schema ``1.0`` event contract, held to what the SDK actually records.

``tests/fixtures/event-schema-v1.json`` says what one event may look like, and
``tests/fixtures/valid-events.jsonl`` is one hand-written trace shaped like that.
Neither says what a *store* looks like: how an event names its parent, how a
consumer turns a flat file into traces, or what the framework bridges record.
``bir-app`` reads all of it, and nothing here could check any of it.

``tests/contract/`` is that corpus, and it is recorded rather than written --
every line came out of the public API into a real store, with only the ids and
the clock pinned. These tests hold it three ways:

* it is still what the SDK records, so a change to the recorded shape cannot
  merge without the corpus changing in the same commit, and a corpus edited by
  hand cannot merge at all;
* every event meets the schema, and the corpus covers every event type the
  schema allows and every bridge the package ships;
* the structure a consumer depends on holds -- unique ids, parents that resolve
  inside their own trace, one root per trace, and grouping that does not depend
  on the order events happen to be written in.

What is *not* checked here is whether ``bir-app`` accepts any of it: no checkout
of it is reachable. ``tests/contract/CROSS_REPO.json`` records that in machine-
readable form, and :class:`CrossRepoAttestationTests` keeps the Beta checklist
item in ``docs/site/stability.md`` tied to it.
"""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

from test_integration_contract import BRIDGES

from bir import _sdk, load_events, load_traces
from bir._sdk import _reset_config_for_tests

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = REPO_ROOT / "tests" / "contract"
SCHEMA_PATH = REPO_ROOT / "tests" / "fixtures" / "event-schema-v1.json"
CROSS_REPO_PATH = CONTRACT_DIR / "CROSS_REPO.json"
STABILITY_PAGE = REPO_ROOT / "docs" / "site" / "stability.md"
CORE_EVENTS = CONTRACT_DIR / "core-events.jsonl"
BRIDGE_EVENTS = CONTRACT_DIR / "bridge-events.jsonl"

# The checklist line this corpus exists to make checkable. Its text is matched,
# not its position, so re-ordering the list does not silently pass the test.
CROSS_REPO_CHECKLIST_ITEM = "The event-schema `1.0` contract is confirmed against the current `bir-app`"


def load_contract_script() -> ModuleType:
    """Load ``scripts/contract.py`` by path (``scripts/`` is not a package)."""

    script_path = REPO_ROOT / "scripts" / "contract.py"
    spec = importlib.util.spec_from_file_location("bir_contract_script", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contract = load_contract_script()


def read_corpus(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class ContractTestCase(unittest.TestCase):
    """Shared corpus, read once."""

    schema: dict[str, Any]
    core: list[dict[str, Any]]
    bridge: list[dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.core = read_corpus(CORE_EVENTS)
        cls.bridge = read_corpus(BRIDGE_EVENTS)

    def setUp(self) -> None:
        _reset_config_for_tests()

    def tearDown(self) -> None:
        _reset_config_for_tests()

    @property
    def events(self) -> list[dict[str, Any]]:
        return [*self.core, *self.bridge]


class CorpusIsWhatTheSdkRecordsTests(ContractTestCase):
    """The committed corpus is the SDK's own output, not a copy of it."""

    def test_the_committed_corpus_is_what_the_sdk_records_now(self) -> None:
        recorded = contract.build_corpus()

        for name, data in sorted(recorded.items()):
            with self.subTest(name):
                committed = (CONTRACT_DIR / name).read_bytes()
                if committed == data:
                    continue
                # The bytes are long; say which events differ rather than
                # printing two files at each other.
                self.fail(
                    f"{name} is not what the SDK records.\n"
                    f"committed {len(committed)} bytes, recorded {len(data)} bytes.\n"
                    "Re-export it with `python scripts/contract.py export` if the change was intended."
                )

    def test_recording_the_corpus_twice_produces_the_same_bytes(self) -> None:
        # Without this, "the corpus drifted" and "the corpus is not reproducible"
        # would look identical in the test above.
        self.assertEqual(contract.build_corpus(), contract.build_corpus())

    def test_the_corpus_does_not_depend_on_a_trace_that_is_already_open(self) -> None:
        # Recorded inside somebody else's open trace -- a test module that left
        # one, a REPL that is tracing -- each scenario's root would be recorded
        # as a span instead, and the corpus would depend on what ran before it.
        # That is a flaky drift failure, so the exporter clears the context and
        # this holds it to that.
        clean = contract.build_corpus()
        trace_token = _sdk._current_trace_id.set("someone-elses-trace")
        parent_token = _sdk._current_parent_id.set("someone-elses-span")
        try:
            inside = contract.build_corpus()
        finally:
            _sdk._current_trace_id.reset(trace_token)
            _sdk._current_parent_id.reset(parent_token)

        self.assertEqual(clean, inside)

    def test_the_corpus_has_one_line_ending_on_every_platform(self) -> None:
        # The store is appended to in text mode, so the same events recorded on
        # Windows come back CRLF-terminated and hash differently. The corpus has
        # one canonical form; without this the drift guard would fail on Windows
        # for a reason that is not drift.
        for name, data in contract.build_corpus().items():
            with self.subTest(name):
                self.assertNotIn(b"\r\n", data)
        for name in contract.CORPUS_NAMES:
            with self.subTest(f"committed {name}"):
                self.assertNotIn(b"\r\n", (CONTRACT_DIR / name).read_bytes())

    def test_a_store_written_with_windows_line_endings_records_the_same_corpus(self) -> None:
        # What a Windows recording produces, reproduced here by translating the
        # bytes the way text mode does there.
        real_read_bytes = Path.read_bytes

        def as_windows_wrote_it(path: Path) -> bytes:
            data = real_read_bytes(path)
            return data.replace(b"\n", b"\r\n") if path.suffix == ".jsonl" else data

        with patch.object(Path, "read_bytes", new=as_windows_wrote_it):
            recorded = contract.build_corpus()

        self.assertEqual(recorded, contract.build_corpus())

    def test_the_manifest_describes_the_committed_corpus(self) -> None:
        manifest = json.loads((CONTRACT_DIR / contract.MANIFEST_NAME).read_text(encoding="utf-8"))

        self.assertEqual(manifest["schema_version"], "1.0")
        # The corpus is verified against one schema; the manifest pins which.
        self.assertEqual(manifest["schema"]["file"], SCHEMA_PATH.name)
        self.assertEqual(manifest["schema"]["sha256"], contract.sha256_hex(SCHEMA_PATH.read_bytes()))
        for name, path in ((CORE_EVENTS.name, CORE_EVENTS), (BRIDGE_EVENTS.name, BRIDGE_EVENTS)):
            with self.subTest(name):
                self.assertEqual(manifest["files"][name], contract._counts(path.read_bytes()))

    def test_the_script_reports_a_corpus_that_matches(self) -> None:
        self.assertEqual(contract.verify_corpus(CONTRACT_DIR), [])
        self.assertEqual(contract.main(["check"]), 0)


class EveryEventMeetsTheSchemaTests(ContractTestCase):
    """Each recorded event validates, and the corpus covers the whole schema."""

    def test_every_recorded_event_validates_against_the_schema(self) -> None:
        for index, event in enumerate(self.events):
            with self.subTest(event=event["id"]):
                self.assertEqual(contract.validate_against_schema(event, self.schema, f"event {index}"), [])

    def test_the_corpus_covers_every_event_type_the_schema_allows(self) -> None:
        allowed = set(self.schema["properties"]["type"]["enum"])

        self.assertEqual({event["type"] for event in self.events}, allowed)

    def test_the_corpus_covers_both_statuses_and_a_recorded_error(self) -> None:
        self.assertEqual({event["status"] for event in self.events}, set(self.schema["properties"]["status"]["enum"]))
        failed = [event for event in self.core if event["status"] == "error"]

        self.assertTrue(failed, "the corpus records no failed event")
        for event in failed:
            with self.subTest(event["id"]):
                # A consumer renders this string; an error row with no message
                # would be a worse contract than no error row.
                self.assertIsInstance(event["error"], str)
                self.assertTrue(event["error"])

    def test_a_retrieval_is_recorded_as_a_tool_call_a_consumer_can_recognize(self) -> None:
        # The schema has no retrieval type: the RAG shape is a tool call that
        # says so in metadata and carries documents. A dashboard keys on that.
        retrievals = [event for event in self.core if event["metadata"].get("kind") == "retrieval"]

        self.assertEqual(len(retrievals), 1)
        retrieval = retrievals[0]
        self.assertEqual(retrieval["type"], "tool_call")
        self.assertIn("documents", retrieval["output"])
        self.assertEqual(
            sorted(retrieval["output"]["documents"][0]),
            ["id", "score", "source", "text"],
        )

    def test_a_generation_carries_the_priced_fields_a_consumer_sums(self) -> None:
        generation = next(
            event for event in self.core if event["type"] == "generation" and event["status"] == "success"
        )

        self.assertEqual(generation["model"], "demo-model")
        self.assertEqual(generation["usage"], {"input_tokens": 12, "output_tokens": 24, "total_tokens": 36})
        self.assertEqual(sorted(generation["cost"]), ["input_cost", "output_cost", "total_cost"])
        self.assertEqual(generation["currency"], "USD")
        self.assertEqual(generation["metadata"]["prompt"]["name"], "answer_question")
        self.assertEqual(generation["metadata"]["prompt"]["version"], "v1")

    def test_service_metadata_is_recorded_on_the_root_and_only_there(self) -> None:
        # Where a consumer attributes a trace to a service: the root carries the
        # service block and the source, and children do not repeat it. Filtering
        # by service therefore means filtering roots and taking their trace.
        for event in self.events:
            with self.subTest(event["id"]):
                if event["type"] == "trace":
                    self.assertEqual(event["metadata"]["service"], {"name": "rag-api", "environment": "production"})
                    self.assertEqual(event["metadata"]["source"], "python-sdk")
                else:
                    self.assertNotIn("service", event["metadata"])
                    self.assertNotIn("source", event["metadata"])


class VerifierRefusesWhatItShouldTests(ContractTestCase):
    """The verifier's answers only mean something if it refuses a broken corpus."""

    def schema_errors(self, event: dict[str, Any]) -> list[str]:
        return contract.validate_against_schema(event, self.schema, "case")

    def structural_errors(self, events: list[dict[str, Any]]) -> list[str]:
        return contract._structural_errors(events, "case")

    def without(self, event: dict[str, Any], field: str) -> dict[str, Any]:
        return {key: value for key, value in event.items() if key != field}

    def test_the_schema_check_refuses_a_malformed_event(self) -> None:
        trace = next(event for event in self.core if event["type"] == "trace")
        span = next(event for event in self.core if event["type"] == "span")
        score = next(event for event in self.core if event["type"] == "score")
        generation = next(event for event in self.core if event["type"] == "generation")
        cases = {
            "a missing required field": self.without(trace, "status"),
            "a different schema version": {**trace, "schema_version": "2.0"},
            "an event type nothing produces": {**trace, "type": "embedding"},
            "a status outside the enum": {**trace, "status": "partial"},
            "an empty name": {**trace, "name": ""},
            "a root with a parent": {**trace, "parent_id": trace["id"]},
            "a child without a parent": {**span, "parent_id": None},
            "a score without a value": self.without(score, "value"),
            "usage that is not numeric": {**generation, "usage": {"input_tokens": "twelve"}},
        }
        for label, event in cases.items():
            with self.subTest(label):
                self.assertNotEqual(self.schema_errors(event), [])

    def test_the_schema_check_refuses_a_schema_it_does_not_fully_understand(self) -> None:
        # A rule this verifier cannot evaluate must stop it, not be skipped:
        # silently ignoring a keyword would turn the contract into a subset of
        # itself without anyone noticing.
        trace = next(event for event in self.core if event["type"] == "trace")

        with self.assertRaisesRegex(contract.ContractError, "unsupported keywords"):
            self.schema_errors  # noqa: B018 - the call below is the assertion
            contract.validate_against_schema(trace, {"patternProperties": {}}, "case")

    def test_the_structure_check_refuses_a_broken_tree(self) -> None:
        def mutate(event_id: str, **changes: Any) -> list[dict[str, Any]]:
            return [{**event, **changes} if event["id"] == event_id else event for event in self.core]

        span = next(event for event in self.core if event["type"] == "span")
        trace = next(event for event in self.core if event["type"] == "trace")
        cases = {
            "a parent that is not in the corpus": mutate(span["id"], parent_id="nowhere"),
            "a parent in a different trace": mutate(span["id"], trace_id="another-trace"),
            "a duplicate event id": mutate(span["id"], id=trace["id"]),
            "a second root in one trace": [
                *self.core,
                {**trace, "id": "second-root", "trace_id": trace["trace_id"]},
            ],
            "a root that is not its own trace": mutate(trace["id"], id="renamed-root"),
            "a trace with no root at all": [event for event in self.core if event["id"] != trace["id"]],
        }
        for label, events in cases.items():
            with self.subTest(label):
                self.assertNotEqual(self.structural_errors(events), [])

    def test_the_structure_check_refuses_a_parent_chain_that_loops(self) -> None:
        span = next(event for event in self.core if event["type"] == "span")
        tool = next(event for event in self.core if event["type"] == "tool_call")
        looped = [
            {**event, "parent_id": tool["id"]} if event["id"] == span["id"] else event
            for event in self.core
            if event["type"] != "trace"
        ]

        errors = self.structural_errors(looped)

        self.assertTrue(any("loops" in error for error in errors), errors)

    def test_the_verifier_reports_a_corpus_that_is_not_there(self) -> None:
        problems = contract.verify_corpus(REPO_ROOT / "tests" / "fixtures")

        self.assertTrue(any(contract.CORE_EVENTS_NAME in problem for problem in problems), problems)


class ParentageAndGroupingTests(ContractTestCase):
    """The rules that turn a flat file into traces."""

    def test_every_event_id_is_unique_across_the_corpus(self) -> None:
        ids = [event["id"] for event in self.events]

        self.assertEqual(len(ids), len(set(ids)))

    def test_every_parent_id_resolves_to_a_local_event_in_the_same_trace(self) -> None:
        for name, events in (("core", self.core), ("bridge", self.bridge)):
            by_id = {event["id"]: event for event in events}
            for event in events:
                with self.subTest(f"{name}:{event['id']}"):
                    if event["type"] == "trace":
                        self.assertIsNone(event["parent_id"])
                        self.assertEqual(event["id"], event["trace_id"])
                        continue
                    parent = by_id.get(event["parent_id"])
                    self.assertIsNotNone(parent, f"{event['id']} names an unknown parent {event['parent_id']!r}")
                    assert parent is not None
                    self.assertEqual(parent["trace_id"], event["trace_id"])

    def test_every_event_reaches_its_root_by_following_parents(self) -> None:
        by_id = {event["id"]: event for event in self.events}
        for event in self.events:
            with self.subTest(event["id"]):
                current, hops = event, 0
                while current["type"] != "trace":
                    current = by_id[current["parent_id"]]
                    hops += 1
                    self.assertLess(hops, len(self.events), "parent chain does not terminate")
                self.assertEqual(current["id"], event["trace_id"])

    def test_each_trace_has_exactly_one_root(self) -> None:
        roots: dict[str, int] = {}
        for event in self.events:
            if event["type"] == "trace":
                roots[event["trace_id"]] = roots.get(event["trace_id"], 0) + 1

        self.assertEqual(sorted(roots.values()), [1] * len({event["trace_id"] for event in self.events}))

    def test_a_child_is_written_before_its_parent(self) -> None:
        # The fact a consumer must not assume tree order: an event is written
        # when it finishes, so a store is in completion order and the root of a
        # trace is the last of its events to arrive.
        order = [event["id"] for event in self.core]
        first_trace = next(event for event in self.core if event["type"] == "trace")
        children = [event for event in self.core if event["trace_id"] == first_trace["trace_id"]]

        self.assertNotEqual(order[0], first_trace["id"])
        for child in children:
            if child["id"] == first_trace["id"]:
                continue
            with self.subTest(child["id"]):
                self.assertLess(order.index(child["id"]), order.index(first_trace["id"]))

    def test_the_sdk_loader_groups_the_corpus_the_way_the_manifest_counts_it(self) -> None:
        manifest = json.loads((CONTRACT_DIR / contract.MANIFEST_NAME).read_text(encoding="utf-8"))
        for path in (CORE_EVENTS, BRIDGE_EVENTS):
            with self.subTest(path.name):
                traces = load_traces(path)
                recorded = manifest["files"][path.name]

                self.assertEqual(len(traces), recorded["traces"])
                self.assertEqual(sum(len(trace.events) for trace in traces), recorded["events"])
                for trace in traces:
                    # Grouping is by trace_id and nothing else: every event the
                    # loader hands back under a trace belongs to it, and the
                    # trace's own name comes from its root.
                    self.assertTrue(all(event.trace_id == trace.id for event in trace.events))
                    root = next(event for event in trace.events if event.type == "trace")
                    self.assertEqual(trace.name, root.name)
                    self.assertEqual(trace.status, root.status)

    def test_the_loader_reads_every_recorded_event_back(self) -> None:
        for path, expected in ((CORE_EVENTS, self.core), (BRIDGE_EVENTS, self.bridge)):
            with self.subTest(path.name):
                events = load_events(path)

                self.assertEqual([event.id for event in events], [raw["id"] for raw in expected])
                self.assertEqual([event.raw for event in events], expected)


class BridgeCoverageTests(ContractTestCase):
    """Every shipped bridge has its recorded tree in the corpus."""

    def test_every_bridge_event_names_the_integration_that_recorded_it(self) -> None:
        # How a consumer tells framework-recorded work from the application's
        # own: every event a bridge writes says which integration wrote it.
        for event in self.bridge:
            with self.subTest(event["id"]):
                self.assertIn("integration", event["metadata"])

    def test_every_declared_bridge_contributes_a_trace(self) -> None:
        roots = [event for event in self.bridge if event["type"] == "trace"]

        self.assertEqual(len(roots), len(BRIDGES))
        self.assertEqual([root["name"] for root in roots], [bridge.root_name for bridge in BRIDGES])

    def test_every_bridge_trace_carries_the_generation_it_declares(self) -> None:
        by_id = {event["id"]: event for event in self.bridge}
        roots = {event["trace_id"]: event for event in self.bridge if event["type"] == "trace"}
        for bridge, root in zip(
            BRIDGES, [roots[event["trace_id"]] for event in self.bridge if event["type"] == "trace"]
        ):
            with self.subTest(bridge.id):
                generations = [
                    event
                    for event in self.bridge
                    if event["trace_id"] == root["trace_id"] and event["type"] == "generation"
                ]
                self.assertEqual([event["name"] for event in generations], [bridge.generation_name])
                generation = generations[0]
                self.assertEqual(generation["model"], bridge.model)
                if bridge.usage is not None:
                    self.assertEqual(generation["usage"], dict(bridge.usage))
                parent = by_id[generation["parent_id"]]
                if bridge.intermediate_run_name is None:
                    self.assertEqual(parent["id"], root["id"])
                else:
                    # AG2 hangs its generations from the speaking agent's turn.
                    self.assertEqual(parent["name"], bridge.intermediate_run_name)
                    self.assertEqual(parent["parent_id"], root["id"])


class CrossRepoAttestationTests(ContractTestCase):
    """The Beta checklist item is held to what was actually run."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.attestation = json.loads(CROSS_REPO_PATH.read_text(encoding="utf-8"))
        cls.page = STABILITY_PAGE.read_text(encoding="utf-8")

    def checklist_line(self) -> str:
        lines = [line for line in self.page.splitlines() if CROSS_REPO_CHECKLIST_ITEM in line]
        self.assertEqual(len(lines), 1, "the cross-repo checklist item is not on the stability page exactly once")
        return lines[0]

    def test_the_attestation_says_what_was_run_and_against_what(self) -> None:
        self.assertIsInstance(self.attestation["validated"], bool)
        self.assertEqual(self.attestation["schema_version"], "1.0")
        self.assertEqual(sorted(self.attestation["covers"]), sorted(contract.CORPUS_NAMES))
        self.assertTrue(self.attestation["how_to_record_a_validation"])

    def test_an_unvalidated_contract_keeps_the_checklist_item_unchecked(self) -> None:
        line = self.checklist_line()
        if self.attestation["validated"]:
            self.assertTrue(line.strip().startswith("- [x]"), line)
            # A claim needs evidence: which release was read, and when.
            self.assertTrue(self.attestation["consumer"]["release"] or self.attestation["consumer"]["commit"])
            self.assertTrue(self.attestation["validated_at"])
            return
        self.assertTrue(line.strip().startswith("- [ ]"), line)
        self.assertTrue(self.attestation["reason"], "an unvalidated contract must say why")
        self.assertIsNone(self.attestation["validated_at"])

    def test_the_page_points_at_the_corpus_that_would_be_handed_over(self) -> None:
        # Asserted without printing the page back: a reader of the checklist has
        # to be able to find the corpus the item is about.
        self.assertTrue("tests/contract/" in self.page, "the stability page does not mention tests/contract/")
        self.assertTrue(CROSS_REPO_PATH.name in self.page, f"the page does not mention {CROSS_REPO_PATH.name}")


if __name__ == "__main__":
    unittest.main()
