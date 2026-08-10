"""Shared conformance contract for Bir's event-bridge integrations.

The framework handlers solve one recurring problem: a framework announces runs
through start/end/error callbacks, and the handler has to turn that stream into
Bir's event tree. The callback vocabularies differ — LangChain passes run ids,
LlamaIndex passes event ids, the OpenAI Agents and Pydantic AI processors pass
span objects — but the obligations do not: open a root when nothing else did,
nest children under the run that owns them, finalize on the matching end
callback, record failures with a redacted message, and stay quiet when a
callback arrives for a run the handler never saw.

This module holds those obligations. A handler declares how to drive one run of
each kind as a :class:`BridgeContract`, and :func:`build_bridge_test_case` turns
the declaration into the shared cases; the declarations live in
``test_integration_contract.py`` beside the call-wrapper contracts. Framework
payload parsing — which attribute carries the model, how usage is spelled, which
span types map to tool calls — stays in the per-framework test module.

The drivers build the framework's own objects from plain mappings, which the
handlers read through the same accessors they use for real provider objects.
:class:`RunDriver` hides where a failure is reported: LangChain and LlamaIndex
have error callbacks, while the span processors report a failure on the span
handed to the ordinary end callback. Each contract also declares where its
framework hands an object over, as ``hostile_generation``, so
:data:`HOSTILE_FRAMEWORK_OBJECTS` can be put there and every handler is held to
the same rule: reading a framework's object for a record may not fail the call
being recorded.
"""

from __future__ import annotations

import contextvars
import inspect
import unittest
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from integration_contract import REDACTED_TEXT, SECRET_TEXT, temporary_workdir

from bir import TraceEvent, configure, load_events, load_traces, span, trace
from bir._sdk import _reset_config_for_tests
from bir.integrations._lifecycle import _MAX_OPEN_RUNS

# Capture options every handler accepts, each defaulting to ``None`` so an unset
# option leaves the configured capture settings in charge.
HANDLER_CAPTURE_OPTIONS = ("capture_inputs", "capture_outputs")

ROOT_KEY = "run-root"
CHILD_KEY = "run-child"


class HostileObject:
    """A framework object whose every read runs code that fails.

    Attribute access raises, and so does each accessor a framework exposes for
    the same value — LlamaIndex's ``get_text()`` raises outright for a node that
    is not a ``TextNode``, and a pydantic ``model_dump`` raises on a field it
    cannot serialize. Dunder lookups raise ``AttributeError`` instead, so the
    object still behaves like an object to the interpreter and only the reads a
    handler makes for a record are hostile.
    """

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        raise RuntimeError(f"framework read of {name!r} failed")

    def get_content(self) -> str:
        raise ValueError("Node must be a TextNode to get text.")

    def get_text(self) -> str:
        raise ValueError("Node must be a TextNode to get text.")

    def model_dump(self) -> Any:
        raise RuntimeError("model_dump failed")

    def __str__(self) -> str:
        raise RuntimeError("__str__ failed")


class HostileMapping(Mapping):
    """A framework mapping whose lookup and iteration run code that fails.

    Several frameworks hand over a mapping rather than an object — LangChain's
    ``serialized`` and ``LLMResult``, LlamaIndex's event payload — and a handler
    reads those with ``get`` and ``items`` instead of ``getattr``. Both are the
    framework's code, so both belong in the sweep.
    """

    def __getitem__(self, key: Any) -> Any:
        raise RuntimeError(f"framework lookup of {key!r} failed")

    def get(self, key: Any, default: Any = None) -> Any:
        raise RuntimeError(f"framework lookup of {key!r} failed")

    def items(self) -> Any:
        raise RuntimeError("framework mapping cannot be walked")

    def __iter__(self) -> Iterator[Any]:
        raise RuntimeError("framework mapping cannot be walked")

    def __len__(self) -> int:
        return 1


#: The framework objects every bridge is driven with. Each declaration says
#: where its framework hands one over; the shared case puts these there.
HOSTILE_FRAMEWORK_OBJECTS: tuple[tuple[str, Any], ...] = (
    ("object whose reads raise", HostileObject()),
    ("mapping whose reads raise", HostileMapping()),
)


@dataclass(frozen=True)
class RunDriver:
    """Drive one framework run through its start, end, and failure callbacks.

    Each callable receives the handler and the run's key; ``start`` also
    receives the key of the run that owns it, or ``None`` at the top level. The
    key is whatever the framework identifies a run by — a run id, an event id,
    or a span id — so the driver can rebuild an equivalent callback payload for
    every step of the same run.
    """

    start: Callable[[Any, str, str | None], None]
    end: Callable[[Any, str], None]
    fail: Callable[[Any, str, BaseException], None]

    def record(self, handler: Any, key: str, parent: str | None) -> None:
        """Run one complete run from start to end."""

        self.start(handler, key, parent)
        self.end(handler, key)

    def record_failure(self, handler: Any, key: str, parent: str | None, error: BaseException) -> None:
        """Run one complete run that ends in failure."""

        self.start(handler, key, parent)
        self.fail(handler, key, error)


@dataclass(frozen=True)
class PointDriver:
    """Drive a run the framework reports in a single call.

    AG2's logger protocol hands a finished LLM call to ``log_chat_completion``
    rather than announcing its start and end separately, so such a run has no
    observable middle: nothing can happen inside it, its end cannot go missing,
    and it cannot overlap another. A contract declaring one of these gets the
    cases that a complete run must still satisfy and none that require a run to
    be observed while it is open.
    """

    log: Callable[[Any, str, str | None], None]
    fail: Callable[[Any, str, BaseException], None]

    def record(self, handler: Any, key: str, parent: str | None) -> None:
        self.log(handler, key, parent)

    def record_failure(self, handler: Any, key: str, parent: str | None, error: BaseException) -> None:
        self.fail(handler, key, error)


@dataclass(frozen=True)
class BridgeContract:
    """One event-bridge handler's declared conformance capabilities.

    ``root`` drives the framework's own trace-level run and ``generation``
    drives a nested run the handler records as a generation. The declared names
    are what Bir must record for those runs, including ``implicit_root_name``
    for the root the handler opens when a nested run arrives with no trace at
    all.

    ``hostile_generation`` drives the same generation run with the framework
    objects the handler reads for a record replaced by one that raises. Only the
    declaration knows where its framework hands an object over — ``serialized``
    and an ``LLMResult`` for LangChain, an event payload for LlamaIndex, a span's
    attributes for the processors — so it places the object and the case that
    drives it is shared. What identifies and classifies the run stays readable,
    because a framework object that cannot say which kind of run it is has not
    been misread; it has said nothing.
    """

    id: str
    module: str
    integration: str
    provider_roots: tuple[str, ...]
    handler: Callable[..., Any]
    root: RunDriver
    root_name: str
    generation: RunDriver | PointDriver
    generation_name: str
    implicit_root_name: str
    hostile_generation: Callable[[Any, str, Any], None]
    model: str | None = None
    usage: dict[str, int | float] | None = None
    # Whether the framework names each run's parent, and correlates a run's end
    # callback with its start. A framework that does neither (CrewAI emits its
    # LLM-call and tool-usage events with no correlation id at all) can only pair
    # and nest by the order events arrive, which the matrix asserts instead.
    reports_parents: bool = True
    # Whether the framework itself guarantees every start is matched by exactly
    # one end — true for a context-manager tracer or a single-call report, false
    # for a callback stream, where an end can arrive unmatched, twice, or never.
    guarantees_paired_callbacks: bool = False
    # Whether the framework says, at the start of a top-level run, that the run
    # is top-level. A handler that is told can reclaim a root it is still holding
    # for an earlier run, because new top-level work means the earlier run is
    # gone. A handler that is not told (CrewAI's kickoff and LlamaIndex's
    # ``start_trace`` carry no parent, so a nested one is indistinguishable from
    # one following an abandoned run) must not guess: turning nested work into a
    # second root is worse than the leak, so it relies on the bounded registry.
    reclaims_abandoned_roots: bool = False
    # A structural node the handler always inserts between the framework root and
    # a run, named here. AG2 wraps every event in the speaking agent's turn span,
    # so its generations hang from that turn rather than from the run root.
    intermediate_run_name: str | None = None


class BridgeContractTestCase(unittest.TestCase):
    """The obligations every event-bridge handler shares."""

    contract: ClassVar[BridgeContract]

    def tearDown(self) -> None:
        _reset_config_for_tests()

    def build_handler(self, **options: Any) -> Any:
        return self.contract.handler(**options)

    def events(self, event_type: str) -> list[TraceEvent]:
        return [event for event in load_events() if event.type == event_type]

    def single(self, event_type: str) -> TraceEvent:
        found = self.events(event_type)
        self.assertEqual(len(found), 1, f"expected exactly one {event_type} event, recorded {len(found)}")
        return found[0]

    def record_generation(self, handler: Any, *, parent: str | None = None) -> None:
        """Run one complete nested generation, however the framework reports it."""

        self.contract.generation.record(handler, CHILD_KEY, parent)

    def assert_parented_by_root(self, event: TraceEvent, root: TraceEvent) -> None:
        """Assert the event hangs from the root, through any declared node between."""

        intermediate = self.contract.intermediate_run_name
        if intermediate is None:
            self.assertEqual(event.parent_id, root.id)
            return

        parent = next((found for found in self.events("span") if found.id == event.parent_id), None)
        self.assertIsNotNone(parent, f"expected an intermediate {intermediate!r} span above the run")
        assert parent is not None
        self.assertEqual(parent.name, intermediate)
        self.assertEqual(parent.parent_id, root.id)

    def separable_generation(self) -> RunDriver:
        """Return the generation driver for a case that needs it mid-flight."""

        driver = self.contract.generation
        assert isinstance(driver, RunDriver)  # guaranteed by build_bridge_test_case
        return driver

    def test_handler_declares_the_shared_capture_options(self) -> None:
        parameters = inspect.signature(self.contract.handler).parameters

        for name in HANDLER_CAPTURE_OPTIONS:
            with self.subTest(option=name):
                parameter = parameters[name]
                # Keyword-only so the options cannot be confused with the
                # framework arguments a handler may later accept positionally.
                self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
                self.assertIsNone(parameter.default)

    def test_framework_root_records_a_trace(self) -> None:
        with temporary_workdir():
            handler = self.build_handler()

            self.contract.root.start(handler, ROOT_KEY, None)
            self.contract.root.end(handler, ROOT_KEY)

            event = self.single("trace")
            self.assertEqual(event.name, self.contract.root_name)
            self.assertEqual(event.status, "success")
            self.assertEqual(event.metadata["integration"], self.contract.integration)
            self.assertIsNone(event.parent_id)

    def test_generation_nests_under_the_framework_root(self) -> None:
        with temporary_workdir():
            handler = self.build_handler()

            self.contract.root.start(handler, ROOT_KEY, None)
            self.record_generation(handler, parent=ROOT_KEY)
            self.contract.root.end(handler, ROOT_KEY)

            root = self.single("trace")
            event = self.single("generation")
            self.assertEqual(event.name, self.contract.generation_name)
            self.assertEqual(event.status, "success")
            self.assertEqual(event.trace_id, root.trace_id)
            self.assert_parented_by_root(event, root)
            self.assertEqual(event.model, self.contract.model)
            self.assertEqual(event.usage, self.contract.usage)

    def test_generation_attaches_to_an_active_bir_trace(self) -> None:
        with temporary_workdir():
            handler = self.build_handler()

            with trace("app"):
                self.record_generation(handler)

            # The application already owns a root, so the handler adds no second
            # one and its event lands inside the caller's trace.
            root = self.single("trace")
            self.assertEqual(root.name, "app")
            event = self.single("generation")
            self.assertEqual(event.trace_id, root.trace_id)
            self.assertEqual(event.parent_id, root.id)

    def test_generation_opens_an_implicit_root_when_no_trace_is_active(self) -> None:
        with temporary_workdir():
            handler = self.build_handler()

            self.record_generation(handler)

            # Without a framework root or an application trace the handler still
            # opens a root, so a nested run is never dropped for lack of one.
            root = self.single("trace")
            self.assertEqual(root.name, self.contract.implicit_root_name)
            self.assertEqual(root.metadata["integration"], self.contract.integration)
            self.assertEqual(root.metadata["kind"], "implicit_root")
            event = self.single("generation")
            self.assertEqual(event.trace_id, root.trace_id)
            self.assertEqual(event.parent_id, root.id)

    def test_failed_run_records_a_redacted_error(self) -> None:
        with temporary_workdir():
            handler = self.build_handler()

            self.contract.generation.record_failure(handler, CHILD_KEY, None, RuntimeError(f"run failed {SECRET_TEXT}"))

            event = self.single("generation")
            self.assertEqual(event.status, "error")
            error = event.error or ""
            self.assertIn(REDACTED_TEXT, error)
            self.assertNotIn(SECRET_TEXT, error)

    def test_capture_is_off_by_default(self) -> None:
        with temporary_workdir():
            handler = self.build_handler()

            self.record_generation(handler)

            event = self.single("generation")
            self.assertIsNone(event.input)
            self.assertIsNone(event.output)

    def test_handler_capture_options_override_the_configuration(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=False, capture_outputs=False)
            handler = self.build_handler(capture_inputs=True, capture_outputs=True)

            self.record_generation(handler)

            event = self.single("generation")
            self.assertIsNotNone(event.input)
            self.assertIsNotNone(event.output)

    def test_handler_capture_options_can_disable_a_configured_capture(self) -> None:
        with temporary_workdir():
            configure(capture_inputs=True, capture_outputs=True)
            handler = self.build_handler(capture_inputs=False, capture_outputs=False)

            self.record_generation(handler)

            event = self.single("generation")
            self.assertIsNone(event.input)
            self.assertIsNone(event.output)

    def test_run_reporting_an_unknown_parent_falls_back_to_the_open_context(self) -> None:
        with temporary_workdir():
            handler = self.build_handler()

            with trace("app"):
                # The named parent was never started here (a handler attached
                # mid-run, or a parent the handler chose not to record), so the
                # event belongs to whatever is open around it rather than
                # pointing at an id that was never written.
                self.record_generation(handler, parent="run-never-started")

            root = self.single("trace")
            event = self.single("generation")
            self.assertEqual(event.trace_id, root.trace_id)
            self.assertEqual(event.parent_id, root.id)

    def test_run_survives_a_framework_object_whose_own_code_raises(self) -> None:
        """Reading the framework's object may not fail the call being recorded.

        A handler reads a framework's object to build a record: a document's
        ``page_content``, a node's ``get_text()``, a span's attributes, a
        response's ``model_dump``. All of that is somebody else's code, and it
        runs after the work it describes has already finished, so a failure in it
        is a bookkeeping problem rather than the call's answer.

        The recovery is what is asserted, not merely the absence of an exception:
        the run is finalized and its event written with whatever was readable, so
        the store holds one complete trace instead of events stranded under a
        root that was never written.
        """

        for label, hostile in HOSTILE_FRAMEWORK_OBJECTS:
            with self.subTest(framework_object=label), temporary_workdir():
                handler = self.build_handler(capture_inputs=True, capture_outputs=True)

                self.contract.hostile_generation(handler, CHILD_KEY, hostile)

                # The handler opened the root this run needed, exactly as it does
                # for a readable one (see the implicit-root case above), and the
                # run hangs from it.
                root = self.single("trace")
                event = self.single("generation")
                self.assertEqual(event.status, "success")
                self.assertEqual(event.trace_id, root.trace_id)
                self.assertEqual(event.parent_id, root.id)
                # One whole trace, and nothing left pointing at a root that is
                # not in it.
                events = load_events()
                self.assertEqual(len(load_traces()), 1)
                written = {found.id for found in events}
                self.assertTrue(all(found.trace_id in written for found in events))

    def test_one_handler_keeps_sequential_runs_apart(self) -> None:
        with temporary_workdir():
            handler = self.build_handler()

            for suffix in ("a", "b"):
                root_key = f"{ROOT_KEY}-{suffix}"
                self.contract.root.start(handler, root_key, None)
                self.contract.generation.record(handler, f"{CHILD_KEY}-{suffix}", root_key)
                self.contract.root.end(handler, root_key)

            # A reused handler must not carry state between runs: two roots,
            # two generations, and no crossed parent links.
            roots = self.events("trace")
            generations = self.events("generation")
            self.assertEqual(len(roots), 2)
            self.assertEqual(len(generations), 2)
            self.assertEqual(len({root.trace_id for root in roots}), 2)
            for root, event in zip(roots, generations):
                self.assertEqual(event.trace_id, root.trace_id)
                self.assert_parented_by_root(event, root)


class NestedWorkTests(BridgeContractTestCase):
    """Handlers whose runs can be observed mid-flight own what happens inside."""

    def test_application_events_inside_a_run_nest_under_it(self) -> None:
        with temporary_workdir():
            handler = self.build_handler()
            generation_driver = self.separable_generation()

            self.contract.root.start(handler, ROOT_KEY, None)
            generation_driver.start(handler, CHILD_KEY, ROOT_KEY)
            # An application's own work inside a framework callback — a provider
            # wrapper called from a tool, a nested @observe() function — still
            # belongs to the run that is executing, so a handler run stays the
            # surrounding parent even though it records its own parent from the
            # framework's tree.
            with span("application-work"):
                pass
            generation_driver.end(handler, CHILD_KEY)
            self.contract.root.end(handler, ROOT_KEY)

            generation = self.single("generation")
            inner = self.single("span")
            self.assertEqual(inner.name, "application-work")
            self.assertEqual(inner.parent_id, generation.id)


class UnpairedCallbackTests(BridgeContractTestCase):
    """Handlers fed by a callback stream survive ends that do not line up."""

    def test_end_callback_for_an_unknown_run_is_ignored(self) -> None:
        with temporary_workdir():
            handler = self.build_handler()

            # Frameworks emit end callbacks the handler never saw a start for
            # (a handler attached mid-run, a run the handler chose to skip).
            self.separable_generation().end(handler, CHILD_KEY)

            self.assertEqual(load_events(), [])

    def test_repeated_end_callback_records_one_event(self) -> None:
        with temporary_workdir():
            handler = self.build_handler()

            self.record_generation(handler)
            self.separable_generation().end(handler, CHILD_KEY)

            self.assertEqual(len(self.events("generation")), 1)

    def test_unfinished_run_records_nothing(self) -> None:
        with temporary_workdir():
            handler = self.build_handler()

            # A run that never ends leaves its context open, so nothing is
            # written. Drive it in a copied context so the unbalanced context
            # variables cannot leak into the rest of the suite.
            contextvars.copy_context().run(self.separable_generation().start, handler, CHILD_KEY, None)

            self.assertEqual(load_events(), [])


class AbandonedRunTests(BridgeContractTestCase):
    """Handlers told when a run is top-level reclaim what an earlier one left open.

    A run that never ends holds the ambient trace context, so everything recorded
    afterwards joins a trace whose root is never written and no trace-shaped
    reader can find it. Nothing in the callback stream says the run is gone, but
    a framework starting new top-level work in the same context has said it by
    implication: the earlier run is finished and written rather than stranded.
    """

    def drive_abandoned_then_new_root(self, handler: Any) -> None:
        """Abandon one run, then let the framework start unrelated top-level work.

        Driven in a copied context because an abandoned run is precisely a run
        that leaves its context entered: without the copy the unbalanced
        contextvars would leak into every test that runs after this one.
        """

        def sequence() -> None:
            self.separable_generation().start(handler, CHILD_KEY, None)
            self.contract.root.start(handler, ROOT_KEY, None)
            self.contract.root.end(handler, ROOT_KEY)

        contextvars.copy_context().run(sequence)

    def test_new_top_level_work_reclaims_an_abandoned_run(self) -> None:
        with temporary_workdir():
            self.drive_abandoned_then_new_root(self.build_handler())

            # The abandoned run is a complete trace rather than stranded events,
            # and the new root is its own trace rather than a child of it.
            self.assertEqual(len(load_traces()), 2)
            abandoned = [event for event in load_events() if event.metadata.get("abandoned")]
            self.assertTrue(abandoned, "the reclaimed run must say it was never closed")
            self.assertEqual({event.metadata["abandoned"] for event in abandoned}, {"superseded"})

    def test_the_context_is_the_application_s_again_after_a_reclaim(self) -> None:
        with temporary_workdir():
            handler = self.build_handler()

            def sequence() -> None:
                self.separable_generation().start(handler, CHILD_KEY, None)
                self.contract.root.start(handler, ROOT_KEY, None)
                self.contract.root.end(handler, ROOT_KEY)
                # Unrelated work recorded after the reclaim opens its own trace
                # instead of joining one whose root will never be written.
                with trace("application-work"):
                    pass

            contextvars.copy_context().run(sequence)

            self.assertIn("application-work", {loaded.name for loaded in load_traces()})

    def test_a_run_the_framework_nests_is_not_reclaimed(self) -> None:
        with temporary_workdir():
            handler = self.build_handler()

            self.contract.root.start(handler, ROOT_KEY, None)
            # A nested run names its parent, so it is ordinary nesting rather
            # than new top-level work and must leave the open root alone.
            self.record_generation(handler, parent=ROOT_KEY)
            self.contract.root.end(handler, ROOT_KEY)

            self.assertEqual(len(self.events("trace")), 1)
            self.assertEqual([event for event in load_events() if event.metadata.get("abandoned")], [])


class BoundedRegistryTests(BridgeContractTestCase):
    """A handler cannot grow without limit when terminal callbacks stop arriving."""

    def open_run_count(self, handler: Any) -> int:
        """Count the runs a handler is holding, whichever shape it holds them in.

        A framework that correlates its callbacks is keyed by run id; one that
        does not (CrewAI) is stacked per thread by arrival order. Both are
        registries of open runs and both need the bound, so the case asserts on
        the total rather than on one shape.
        """

        keyed = len(getattr(handler, "_active_runs", ()))
        stacked = sum(len(stack) for stack in getattr(handler, "_call_stacks", {}).values())
        return keyed + stacked

    def test_open_runs_are_bounded_and_evicted_runs_are_written(self) -> None:
        with temporary_workdir():
            handler = self.build_handler()
            driver = self.separable_generation()

            def sequence() -> None:
                # Every one of these is abandoned, so they run in a copied
                # context for the same reason as the cases above.
                for index in range(_MAX_OPEN_RUNS + 50):
                    driver.start(handler, f"{CHILD_KEY}-{index}", None)

            contextvars.copy_context().run(sequence)

            self.assertLessEqual(self.open_run_count(handler), _MAX_OPEN_RUNS)
            # Evicting a run writes it: holding it forever was the alternative,
            # and dropping it silently would lose what was already recorded.
            written = load_events()
            self.assertTrue(written, "an evicted run must be written, not dropped")
            self.assertTrue(all(event.metadata.get("abandoned") == "evicted" for event in written))


class ReportedParentTests(BridgeContractTestCase):
    """Handlers whose framework names each run's parent follow that tree."""

    def test_overlapping_runs_stay_siblings_under_their_reported_parent(self) -> None:
        with temporary_workdir():
            handler = self.build_handler()

            generation_driver = self.separable_generation()

            self.contract.root.start(handler, ROOT_KEY, None)
            generation_driver.start(handler, "run-a", ROOT_KEY)
            generation_driver.start(handler, "run-b", ROOT_KEY)
            # End them in start order, the reverse of the order their Bir
            # contexts were opened, as a framework running two calls in parallel
            # under one parent would.
            generation_driver.end(handler, "run-a")
            generation_driver.end(handler, "run-b")
            self.contract.root.end(handler, ROOT_KEY)

            # Both runs named the root as their parent, so both are its
            # children. Parenting follows the tree the framework reported, not
            # the order the handler happened to open its Bir contexts in.
            root = self.single("trace")
            generations = self.events("generation")
            self.assertEqual(len(generations), 2)
            for event in generations:
                self.assertEqual(event.status, "success")
                self.assertEqual(event.trace_id, root.trace_id)
                self.assertEqual(event.parent_id, root.id)


class OrderedPairingTests(BridgeContractTestCase):
    """Handlers whose framework reports no parent pair runs by arrival order."""

    def test_overlapping_runs_pair_and_nest_in_arrival_order(self) -> None:
        with temporary_workdir():
            handler = self.build_handler()

            generation_driver = self.separable_generation()

            self.contract.root.start(handler, ROOT_KEY, None)
            generation_driver.start(handler, "run-a", ROOT_KEY)
            generation_driver.start(handler, "run-b", ROOT_KEY)
            generation_driver.end(handler, "run-a")
            generation_driver.end(handler, "run-b")
            self.contract.root.end(handler, ROOT_KEY)

            # The framework supplies nothing to correlate or parent by, so the
            # handler can only pair each end with the most recent open run and
            # nest by arrival. Both runs are still recorded, inside the trace,
            # and neither callback raises.
            root = self.single("trace")
            generations = self.events("generation")
            self.assertEqual(len(generations), 2)
            for event in generations:
                self.assertEqual(event.status, "success")
                self.assertEqual(event.trace_id, root.trace_id)


def build_bridge_test_case(contract: BridgeContract) -> type[BridgeContractTestCase]:
    """Return the conformance test case for one event-bridge handler.

    The case composes only what the declaration supports, so a handler is never
    asked to reconstruct a tree its framework never reported, nor to survive a
    callback sequence its framework cannot emit.
    """

    # Every mixin derives from BridgeContractTestCase, so listing the mixins
    # alone carries the shared cases; the base stands in when none applies.
    bases: list[type[BridgeContractTestCase]] = []
    if isinstance(contract.generation, RunDriver):
        bases.append(NestedWorkTests)
    if not contract.guarantees_paired_callbacks:
        # Only a callback stream can deliver an end that does not line up, or
        # two runs that overlap; a context manager or a single-call report
        # cannot, so those cases would test a sequence the framework never emits.
        bases.append(UnpairedCallbackTests)
        bases.append(ReportedParentTests if contract.reports_parents else OrderedPairingTests)
        # Only a callback stream can leave a run open forever, so only it needs
        # a bound on how many it may hold.
        bases.append(BoundedRegistryTests)
    if contract.reclaims_abandoned_roots:
        bases.append(AbandonedRunTests)

    class_name = "".join(part.title() for part in contract.id.replace(".", "_").split("_")) + "BridgeTests"
    return type(class_name, tuple(bases) or (BridgeContractTestCase,), {"contract": contract})
