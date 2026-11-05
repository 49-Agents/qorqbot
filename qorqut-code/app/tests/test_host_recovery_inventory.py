"""The host's recovery writer inventory and the shutdown ordering that lets admitted effects settle, over a synthetic instance.

The Operations barrier is complete or it refuses: every writer class the host composes (native observation/archive/input,
the nudges, activity, incidents, terminal control, broadcasts, the change feed, Outreach registrations, the shared Context
loop and the operational history observer, beside the engine's own) has a real controller over the very stop adapter the
lifetime uses, or a zero-writer inventory re-verified when the barrier runs; a class that is required but not composed gets
nothing and the barrier refuses. A stop is judged by its typed evidence, never by returning. Shutdown first refuses new
external work, lets admitted work finish its receipts, then seals admission and closes SQLite; the seal is never reopened.
The real Context driver loop and history observer are used with a blocked step; host terminals, Docker and providers are
absent, and the host/container freezers are bounded test controllers (the synthetic instance has no sandbox row)."""

import sqlite3
import threading
from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import DomainError, InvocationOptions, new_id
from qorqut_operations.storage.quiescence import KnownZeroWriters, QuiescenceProof, RecoveryBarrierProof

from qorqut_operations.history_exports import HistoryObserver
from bazarlyk.service.lifetime import ShutdownIncomplete
from qorqut_context.integration.loop import ContextDriverLoop
from host_foundation_support import build_instance
from test_host_instance_ownership import SERVICES, Blocked, Idle, bounded, quiet
from test_host_retained_operations import decide, host as retained_host, propose  # noqa: F401  (the fixture)


class FixtureHostWriters:
    """Test-only explicit known-zero host controller (no terminal or browser process exists in the synthetic instance)."""

    def inventory_and_freeze(self, *, timeout):
        return QuiescenceProof("fixture_host", "frozen")

    def thaw(self):
        pass


class Idle_(Idle):
    """An inert service that also lets the barrier ask whether it is running."""

    def __init__(self, name, events, running=True):
        super().__init__(name, events)
        self._thread = SimpleNamespace(is_alive=lambda: self.alive)
        self.alive = running

    def start(self, *a, **k):
        self.events.append(("start", self.name))
        self.alive = True

    def stop(self, timeout=None):
        self.events.append(("stop", self.name))
        self.alive = False


class Chat(Idle_):
    """The native Chat service's stop/running contract: its observer is stopped through ``stop_observer`` and its worker
    is the backend's thread."""

    def __init__(self, events, running=True):
        super().__init__("native_chat", events, running)
        self.backend = SimpleNamespace(_thread=self._thread)

    def stop_observer(self, timeout=None):
        return self.stop(timeout)


class IdleContext:
    """The Context loop's contract with nothing to run: stop reports a completed stop, ``running`` says whether."""

    def __init__(self, events, running=True):
        self.events, self.running = events, running

    def start(self):
        self.events.append(("start", "context"))
        self.running = True
        return {"status": "idle"}

    def stop(self, timeout=5.0):
        self.events.append(("stop", "context"))
        self.running = False
        return {"stopped": True, "in_flight": False}


class CaptureOrchestrator:
    """A test-only stand-in for the nightly capture service (the real one is lane C's): the contract the lifetime relies on,
    ``start()``, ``stop(timeout=)`` reporting ``{stopped, in_flight}`` and ``running``, and the behavior that matters here, a
    recovery window entered from its own thread and held until released."""

    def __init__(self, runtime, events):
        self.runtime, self.events, self.proofs, self.errors = runtime, events, [], []
        self.requested, self.inside, self.release, self.finished = (threading.Event() for _ in range(4))
        self.stopping, self.in_window, self._thread = threading.Event(), False, None
        self.verified = self.verified_late = None

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        self.events.append(("start", "maintenance_capture"))
        self.stopping.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self.stopping.is_set():
            if not self.requested.wait(0.02):
                continue
            self.requested.clear()
            try:
                with self.runtime.recovery_quiescence(timeout=5) as proof:
                    self.proofs.append(proof)
                    proof.require_database(self.runtime.database)                           # used where it was created
                    self.verified, self.in_window = True, True
                    self.inside.set()
                    self.release.wait(15)
                    proof.verify_active()                                                    # still the same live window
                    self.verified_late = True
            except Exception as error:  # noqa: BLE001 - kept for the assertions
                self.errors.append(error)
            finally:
                self.in_window = False
                self.finished.set()

    def stop(self, timeout=5):
        self.events.append(("stop", "maintenance_capture"))
        self.stopping.set()
        if self._thread:
            self._thread.join(timeout)
        return {"stopped": not self.running, "in_flight": self.in_window}


@pytest.fixture
def owned(tmp_path):
    """A synthetic instance whose backend owns it, with the engine's browser/sandbox/host slots filled by bounded stand-ins."""
    instance, _ = build_instance(tmp_path)
    runtime, events = instance.runtime, []
    quiet(runtime, events)
    runtime.native_chat = Chat(events, running=True)
    runtime.background = None                                                                 # none attached: a verified zero class
    barrier = runtime.recovery_quiescer
    barrier.host_writers = FixtureHostWriters()
    barrier.browser_writers = KnownZeroWriters("browser")
    barrier.sandbox_writers = KnownZeroWriters("sandbox")
    runtime.ownership.acquire()
    return instance, runtime, events


def gate_state(instance):
    with instance.database.read() as uow:
        row = uow.repository("maintenance_state").get("service")
    return row and row["state"]


def test_the_barrier_refuses_while_a_required_writer_class_has_no_controller(owned):
    instance, runtime, events = owned
    with pytest.raises(DomainError) as incomplete:
        with runtime.recovery_quiescence(timeout=3):
            pytest.fail("an incomplete inventory must not yield a recovery window")
    assert incomplete.value.code == "recovery_not_quiescent"
    assert "inventory is incomplete: context, history" in incomplete.value.message
    # Nothing was refused, stopped or closed by asking: the inventory is checked before the first action.
    assert events == [] and not runtime.application.writer_admission.refusing and gate_state(instance) is None
    assert set(runtime.recovery_quiescer.writers) == {"native_chat", "inbox_nudges", "activity", "incidents", "terminal_control",
                                                       "broadcast_dispatcher", "changes"}


def test_the_composed_services_join_the_inventory_and_only_what_was_running_is_resumed(owned):
    instance, runtime, events = owned
    runtime.context_services = IdleContext(events)
    runtime.history_observer = bounded(HistoryObserver(lambda: None, interval=3600))            # composed but never started
    runtime.inbox_nudges = Idle_("inbox_nudges", events, running=True)
    runtime.activity = Idle_("activity", events, running=False)                                 # stopped before the window
    with runtime.recovery_quiescence(timeout=5):
        stops = [name for kind, name in events if kind == "stop"]
        # The lifetime's own drain order (producers of external effects first), every class drained exactly once.
        assert stops == ["context", "inbox_nudges", "native_chat", "terminal_control", "broadcast_dispatcher", "activity",
                         "incidents", "changes"]
        assert runtime.application.writer_admission.refusing and gate_state(instance) == "closed"
        assert not runtime.context_services.running and not runtime.inbox_nudges.alive and not runtime.native_chat.alive
        events.clear()
    # Only what was running before the window is started again, in reverse drain order, after the thaw.
    assert [name for kind, name in events if kind == "start"] == ["native_chat", "inbox_nudges", "context"]
    assert gate_state(instance) is None and not runtime.application.writer_admission.refusing
    assert runtime.recovery_controllers["inbox_nudges"][0] is runtime.inbox_nudges


def test_a_required_class_stays_refused_when_its_service_disappears_and_an_optional_one_is_verified_empty(owned):
    instance, runtime, events = owned
    runtime.context_services = IdleContext(events)
    runtime.history_observer = HistoryObserver(lambda: None, interval=3600)
    with runtime.recovery_quiescence(timeout=5):
        pass                                                                                  # broadcast: verified empty
    del runtime.context_services                                                              # the composition no longer has it
    with pytest.raises(DomainError, match="inventory is incomplete: context"):
        with runtime.recovery_quiescence(timeout=5):
            pass
    runtime.context_services = IdleContext(events)
    runtime.background = object()                                                             # a writer the zero inventory missed
    with pytest.raises(DomainError, match="background writer inventory is no longer empty"):
        with runtime.recovery_quiescence(timeout=5):
            pass
    assert runtime.application.writer_admission.refusing                                      # an unproven window stays closed


def test_a_false_stop_is_not_a_drain_and_the_barrier_stays_closed(owned):
    instance, runtime, events = owned
    driver = Blocked()
    runtime.context_services = bounded(ContextDriverLoop(driver, poll_seconds=0.05))
    runtime.history_observer = HistoryObserver(lambda: None, interval=3600)
    runtime.context_services.start()
    assert driver.entered.wait(5)
    with pytest.raises(DomainError) as unproven:
        with runtime.recovery_quiescence(timeout=5):
            pytest.fail("a loop with a step in flight must not yield a recovery window")
    assert unproven.value.code == "recovery_not_quiescent" and unproven.value.retryable
    # Closed, durably and in process; nothing was frozen and the other writers were not resumed behind the failure.
    assert runtime.application.writer_admission.refusing and gate_state(instance) == "closed"
    assert runtime.context_services.running and not any(kind == "start" for kind, _ in events)
    with pytest.raises(DomainError) as refused:
        runtime.require_authority()
    assert refused.value.code == "maintenance"

    driver.release.set()
    runtime.context_services.stop(5)
    assert not runtime.context_services.running


def test_a_stop_that_returns_without_evidence_is_no_drain_for_any_service(owned):
    instance, runtime, events = owned
    runtime.context_services = IdleContext(events)
    runtime.history_observer = HistoryObserver(lambda: None, interval=3600)

    class Returns(Idle_):
        def stop(self, timeout=None):
            super().stop(timeout)
            return True                                                                      # a truthy return is no evidence

    runtime.activity = Returns("activity", events)
    with pytest.raises(DomainError) as unproven:
        with runtime.recovery_quiescence(timeout=5):
            pytest.fail("a stop that only returned must not yield a recovery window")
    assert unproven.value.code == "recovery_not_quiescent"
    assert not any(kind == "start" for kind, _ in events) and runtime.application.writer_admission.refusing
    # The lifetime judges the very same service the same way.
    with pytest.raises(ShutdownIncomplete) as incomplete:
        runtime.stop()
    assert [name for name, _ in incomplete.value.report.failed] == ["activity"]


def test_admitted_receipt_completion_settles_while_new_external_work_is_refused_and_admission_stays_sealed(retained_host):
    host = retained_host
    runtime = host.runtime
    for name in SERVICES:                                                                     # the real Signoff, background and
        if name not in ("signoffs", "background", "email_history"):                           # email-history services stay; no other
            setattr(runtime, name, Idle(name, []))                                            # service has a thread to run
    signoff = propose(host)
    assert decide(host, signoff).status == "accepted"
    admission = runtime.application.writer_admission
    entered, proceed, outcome = threading.Event(), threading.Event(), {}
    real_apply = runtime.application.apply_prepared

    def apply_after_shutdown_began(*arguments, **keywords):
        entered.set()
        assert proceed.wait(15)
        return real_apply(*arguments, **keywords)
    runtime.application.apply_prepared = apply_after_shutdown_began
    worker = threading.Thread(target=lambda: outcome.setdefault("applied", runtime.signoffs.run_pending()))
    worker.start()
    assert entered.wait(10)                                                                   # admitted, mid-receipt

    runtime.ownership.acquire()
    stopper = threading.Thread(target=lambda: outcome.setdefault("report", runtime.stop()))
    stopper.start()
    for _ in range(500):
        if admission.refusing:
            break
        threading.Event().wait(0.01)
    assert admission.refusing and admission.admitted == 1
    # New external work is refused now, while the admitted receipt has not finished.
    try:
        refused = runtime.application.invoke(host.agent_context, "tasks_create", {"title": "Late"},
                                             InvocationOptions(idempotency_key=new_id()))
        code = refused.error["code"] if refused.status == "error" else refused.status
    except DomainError as error:
        code = error.code
    assert code == "maintenance"
    assert stopper.is_alive()                                                                 # the stop waits for the admitted unit

    proceed.set()
    worker.join(15)
    stopper.join(15)
    assert not worker.is_alive() and not stopper.is_alive()
    assert outcome["applied"] == 1 and outcome["report"].clean
    # The receipt settled: applied exactly once, the Task exists, and the late request left nothing behind.
    connection = sqlite3.connect(f"file:{host.instance.paths.database}?mode=ro", uri=True)
    try:
        assert connection.execute("SELECT state FROM signoffs").fetchone()[0] == "applied"
        assert [row[0] for row in connection.execute("SELECT state FROM apply_receipts WHERE signoff_id=?",
                                                     (signoff["id"],))] == ["applied"]
        assert [row[0] for row in connection.execute("SELECT title FROM tasks")] == ["Widget"]
    finally:
        connection.close()
    # Settling never reopened external mutation admission: it is sealed and cannot be opened.
    assert admission.sealed and admission.refusing
    with pytest.raises(DomainError):
        admission.open()
    with pytest.raises(DomainError) as after:
        with admission.writer():
            pass
    assert after.value.code == "maintenance"


def compose(runtime, events):
    """Every required writer class composed with an inert service (the Context loop and the history observer included)."""
    runtime.context_services = IdleContext(events)
    runtime.history_observer = HistoryObserver(lambda: None, interval=3600)
    runtime.inbox_nudges = Idle_("inbox_nudges", events, running=True)


def revoked(proof, action="verify_active", *arguments):
    with pytest.raises(DomainError) as error:
        getattr(proof, action)(*arguments)
    assert error.value.code == "recovery_not_quiescent"


def test_the_runtime_window_yields_the_actual_operations_proof_bound_to_this_instance_database(owned, tmp_path):
    instance, runtime, events = owned
    compose(runtime, events)
    other, _ = build_instance(tmp_path / "other")
    with runtime.recovery_quiescence(timeout=5) as proof:
        assert isinstance(proof, RecoveryBarrierProof) and proof.issuer is runtime.recovery_quiescer
        proof.verify_active()
        proof.require_database(runtime.database)
        revoked(proof, "require_database", other.database)                                   # another instance's database
        assert {item.controller for item in proof.controllers} >= {"native_chat", "context", "history", "fixture_host"}
    revoked(proof)
    with pytest.raises(ValueError):
        with runtime.recovery_quiescence(timeout=5) as proof:
            proof.verify_active()
            raise ValueError("the capture failed")
    revoked(proof)
    with runtime.recovery_quiescence(timeout=5) as later:                                    # and never valid in a later window
        later.verify_active()
        revoked(proof)


def test_a_failed_barrier_yields_no_proof_to_the_runtime_caller(owned):
    instance, runtime, events = owned                                                        # context and history not composed
    with pytest.raises(DomainError):
        with runtime.recovery_quiescence(timeout=3) as proof:
            pytest.fail(f"an incomplete inventory must not yield a proof: {proof!r}")
    assert runtime.recovery_quiescer._active is None
    assert gate_state(instance) is None                                                      # nothing was closed to establish one


def test_the_capture_orchestrator_enters_the_barrier_from_its_own_thread_and_is_never_joined_by_it(owned):
    instance, runtime, events = owned
    compose(runtime, events)
    capture = runtime.maintenance_capture = CaptureOrchestrator(runtime, events)
    runtime.lifetime().steps[-1].start()                                                     # started as the lifetime would
    capture.requested.set()
    assert capture.inside.wait(10) and not capture.errors
    # The window was established without draining its own caller, and the caller is not a drained writer class.
    assert "maintenance_capture" not in runtime.recovery_quiescer.writers and capture.running
    assert ("stop", "maintenance_capture") not in events
    assert gate_state(instance) == "closed" and runtime.application.writer_admission.refusing
    proof = capture.proofs[0]
    assert capture.verified                                                                  # verified in the creating thread
    revoked(proof)                                                                           # and refused anywhere else

    # Another orchestrator (recovery administration) serializes behind the held window instead of joining or reopening it.
    outcome = {}

    def second_orchestrator():
        try:
            with runtime.recovery_quiescence(timeout=5):
                outcome["entered"] = True
        except DomainError as error:
            outcome["error"] = (error.code, error.retryable)
    before = list(events)
    other = threading.Thread(target=second_orchestrator)
    other.start()
    other.join(10)
    assert outcome == {"error": ("recovery_not_quiescent", True)} and events == before
    assert gate_state(instance) == "closed"

    capture.release.set()
    assert capture.finished.wait(10) and not capture.errors and capture.verified_late
    assert gate_state(instance) is None and not runtime.application.writer_admission.refusing
    revoked(proof)
    assert capture.running                                                                   # still alive for the next request
    capture.stopping.set()
    capture._thread.join(5)


def test_shutdown_drains_the_recovery_orchestrators_before_any_service_they_quiesce(owned):
    instance, runtime, events = owned
    compose(runtime, events)
    runtime.maintenance_capture = CaptureOrchestrator(runtime, events)
    runtime.maintenance_capture.start()
    events.clear()
    report = runtime.stop()
    stops = [name for kind, name in events if kind == "stop"]
    assert report.clean and not report.held
    assert stops[:2] == ["recovery_admin", "maintenance_capture"] and {"context", "native_chat", "signoffs"} <= set(stops[2:])
    assert not runtime.maintenance_capture.running


def test_an_orchestrator_with_a_capture_in_flight_holds_back_every_service_admission_and_sqlite(owned):
    instance, runtime, events = owned
    compose(runtime, events)
    capture = runtime.maintenance_capture = CaptureOrchestrator(runtime, events)
    capture.start()
    capture.requested.set()
    assert capture.inside.wait(10)
    events.clear()
    with pytest.raises(ShutdownIncomplete) as incomplete:
        runtime.stop()
    report = incomplete.value.report
    assert [name for name, _ in report.failed] == ["maintenance_capture"] and not report.clean
    assert "UnprovenStop" in report.failed[0][1]
    # Only the orchestrators were asked; nothing they quiesce was stopped, admission was not sealed, SQLite stayed open.
    assert [name for kind, name in events if kind == "stop"] == ["recovery_admin", "maintenance_capture"]
    assert {"native_chat", "context", "signoffs", "email_history"} <= set(report.held)
    assert not report.database_closed and not report.admission_closed and not runtime.application.writer_admission.sealed
    assert runtime.ownership.held and capture.in_window
    with runtime.database.read() as uow:
        uow.connection.execute("SELECT 1").fetchone()

    # The capture finishes and thaws through the barrier's normal exit; a later stop then completes.
    capture.release.set()
    assert capture.finished.wait(10) and not capture.errors and capture.verified_late      # the window was untouched
    final = runtime.stop()
    assert final.clean and final.database_closed and not final.held
    with pytest.raises(DomainError):
        runtime.application.writer_admission.open()
