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
handed to the ordinary end callback.
"""

from __future__ import annotations

import contextvars
import inspect
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from integration_contract import REDACTED_TEXT, SECRET_TEXT, temporary_workdir

from bir import TraceEvent, configure, load_events, span, trace
from bir._sdk import _reset_config_for_tests

# Capture options every handler accepts, each defaulting to ``None`` so an unset
# option leaves the configured capture settings in charge.
HANDLER_CAPTURE_OPTIONS = ("capture_inputs", "capture_outputs")

ROOT_KEY = "run-root"
CHILD_KEY = "run-child"


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

    class_name = "".join(part.title() for part in contract.id.replace(".", "_").split("_")) + "BridgeTests"
    return type(class_name, tuple(bases) or (BridgeContractTestCase,), {"contract": contract})
