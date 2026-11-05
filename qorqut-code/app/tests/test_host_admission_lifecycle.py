"""Mutation admission across the host lifetime, over disposable instances and the actual Runtime and Application boundaries.

Admission is refused from assembly and opened only by the owned startup path (ownership, then target validation); a
failed start refuses new mutations before it unwinds, while admitted receipts finish; starting again does nothing; the
recovery orchestrators (recovery administration and the nightly capture) drain first and keep every service, write
admission and SQLite beneath them when one cannot prove it stopped; shutdown's refusal survives a recovery window thawing
while the stop waits; a recovery window is serialized from its first step and judged by the ownership of the moment it is
entered. Services are inert recorders; no provider, terminal or native session is started."""

import threading

import pytest
from qorqut_operations.contracts import DomainError, InvocationOptions, new_id

from bazarlyk.service.lifetime import ShutdownIncomplete
from bazarlyk.service.runtime import build_runtime
from host_foundation_support import build_instance
from test_host_instance_ownership import SERVICES, Idle, quiet, record_steps
from test_host_recovery_inventory import CaptureOrchestrator, Idle_, compose, gate_state, owned  # noqa: F401  (the fixture)


def mutate(runtime, instance, title="Early"):
    """A real Application mutation by the owner; the result code ('ok' or the refusal's code)."""
    result = runtime.application.invoke(instance.human(), "tasks_create", {"title": title},
                                        InvocationOptions(idempotency_key=new_id()))
    return "ok" if result.status == "ok" else result.error["code"]


@pytest.fixture
def fresh(tmp_path):
    """An assembled, unowned instance: exactly what ``build_runtime`` returns, its services inert."""
    instance, _ = build_instance(tmp_path, owned=False)
    events = []
    quiet(instance.runtime, events)
    record_steps(instance.runtime, events)
    return instance, instance.runtime, events


class Failing(Idle):
    """A service whose start raises after it began work; its stop records what write admission looked like."""

    def __init__(self, name, events, runtime, error):
        super().__init__(name, events)
        self.runtime, self.error = runtime, error

    def start(self, *a, **k):
        super().start()
        raise self.error

    start_external_writer_ticker = start

    def stop(self, timeout=None):
        admission = self.runtime.application.writer_admission
        self.events.append(("stop", self.name, admission.refusing, admission.admitted))


def test_mutations_are_refused_from_assembly_until_the_owned_startup_path_admits_them(fresh):
    instance, runtime, events = fresh
    assert runtime.application.writer_admission.refusing and not runtime.ownership.held
    assert mutate(runtime, instance) == "maintenance"                              # a real mutation, before any start
    # Another backend owns the same instance: this one is still refused, and starting it fails before it admits anything.
    owner = build_runtime(instance.workspace, paths=instance.paths)
    quiet(owner, [])
    owner.start()
    try:
        assert mutate(owner, instance) == "ok"
        assert mutate(runtime, instance) == "maintenance"
        with pytest.raises(DomainError) as refused:
            runtime.start()
        assert refused.value.code == "instance_in_use"
        assert mutate(runtime, instance) == "maintenance" and events == []
    finally:
        assert owner.stop().clean
    # The owned startup path (here the next owner) admits them, and only after it owns and has validated the instance.
    assert runtime.start() is runtime
    assert [name for name, *_ in events if name == "validate"] == ["validate"] and mutate(runtime, instance) == "ok"
    assert runtime.stop().clean and mutate(runtime, instance) == "maintenance"


def test_target_validation_failing_leaves_mutations_refused_and_ownership_held(fresh):
    instance, runtime, events = fresh
    runtime.validate_target = lambda: (_ for _ in ()).throw(DomainError("authority_invalid", "not the selected target"))
    with pytest.raises(DomainError, match="not the selected target"):
        runtime.start()
    assert runtime.ownership.held and runtime.lifetime_state.phase == "failed_start"
    assert mutate(runtime, instance) == "maintenance"
    assert not any(kind == "reconcile" for kind, *_ in events)                      # nothing recovered before validation


def test_a_failed_start_refuses_before_unwinding_and_admitted_work_finishes(tmp_path):
    instance, _ = build_instance(tmp_path, owned=False)
    runtime, events = instance.runtime, []
    quiet(runtime, events)
    admission = runtime.application.writer_admission
    runtime.telegram = Failing("telegram", events, runtime, RuntimeError("injected start failure"))
    admitted, release, nested = threading.Event(), threading.Event(), []

    def receipt():
        with admission.writer():                                                    # admitted before the start fails
            admitted.set()
            release.wait(10)
            with admission.writer():                                                # its receipt completion
                nested.append("settled")

    # The owned entry first (a serving backend would already have admitted), then an admitted unit is in flight.
    runtime.own_instance()
    worker = threading.Thread(target=receipt)
    worker.start()
    assert admitted.wait(10)
    with pytest.raises(RuntimeError, match="injected start failure"):
        runtime.start()
    stops = [event for event in events if event[0] == "stop" and event[1] == "telegram"]
    # Refused and still counting the admitted unit by the time the failed service is unwound.
    assert stops == [("stop", "telegram", True, 1)]
    assert mutate(runtime, instance) == "maintenance" and runtime.lifetime_state.phase == "failed_start"
    release.set()
    worker.join(10)
    assert nested == ["settled"] and admission.admitted == 0                       # the admitted receipt finished its writes
    assert mutate(runtime, instance) == "maintenance"
    # A corrected retry admits again through the owned path.
    runtime.telegram = Idle("telegram", events)
    runtime.start()
    assert mutate(runtime, instance) == "ok"
    assert runtime.stop().clean


def test_starting_again_while_running_verifies_ownership_and_repeats_nothing(fresh):
    instance, runtime, events = fresh
    runtime.start()
    first = list(events)
    assert [kind for kind, *_ in first].count("validate") == 1 and [kind for kind, *_ in first].count("reconcile") == 1
    assert runtime.start() is runtime and runtime.start() is runtime
    assert events == first                                                         # no validation, recovery or service start
    runtime.ownership.release()                                                     # lost ownership is not hidden by the shortcut
    with pytest.raises(DomainError) as lost:
        runtime.start()
    assert lost.value.code == "ownership_lost"
    runtime.ownership.acquire()
    assert runtime.stop().clean
    with pytest.raises(DomainError) as closed:                                      # a stopped lifetime stays closed
        runtime.start()
    assert closed.value.code == "lifetime_closed"


class Unproven(Idle_):
    """A recovery orchestrator whose stop cannot be proved."""

    def stop(self, timeout=None):
        self.events.append(("stop", self.name))
        raise TimeoutError("the capture is still draining")


def test_a_failed_start_unwinds_the_orchestrators_first_and_holds_everything_beneath_an_unproven_one(owned):
    instance, runtime, events = owned
    runtime.changes = Failing("changes", events, runtime, RuntimeError("injected start failure"))
    runtime.changes.stop = lambda timeout=None: events.append(("stop", "changes"))
    runtime.recovery_admin = Idle_("recovery_admin", events)
    # Proved orchestrator: it is unwound before the services started ahead of it.
    with pytest.raises(RuntimeError, match="injected start failure") as failed:
        runtime.start()
    stops = [event[1] for event in events if event[0] == "stop"]
    assert stops[0] == "recovery_admin" and stops.index("recovery_admin") < stops.index("email_history")
    assert "unwound recovery_admin" in "\n".join(failed.value.__notes__)

    # Unproven orchestrator: nothing it can pause, capture or resume is stopped beneath it.
    events.clear()
    runtime.lifetime_state.incomplete.clear()
    runtime.lifetime_state.running.clear()
    runtime.recovery_admin = Unproven("recovery_admin", events)
    with pytest.raises(RuntimeError, match="injected start failure") as held:
        runtime.start()
    assert [event[1] for event in events if event[0] == "stop"] == ["recovery_admin"]
    incomplete = runtime.lifetime_state.incomplete
    assert "recovery_admin" in incomplete and "email_history" in incomplete and "native_chat" in incomplete
    assert "held beneath an unproven recovery orchestrator" in "\n".join(held.value.__notes__)
    assert runtime.ownership.held and mutate(runtime, instance) == "maintenance"
    with runtime.database.read() as uow:
        uow.connection.execute("SELECT 1").fetchone()                              # SQLite is still open beneath it
    with pytest.raises(DomainError) as refused:
        runtime.start()
    assert refused.value.code == "lifetime_incomplete"
    # Once the orchestrator can prove it, a stop drains them all.
    runtime.recovery_admin = Idle_("recovery_admin", events)
    report = runtime.stop()
    assert report.clean and "email_history" in report.stopped and not runtime.lifetime_state.incomplete


def test_stop_asks_every_orchestrator_then_touches_nothing_beneath_one_that_is_unproven(owned):
    instance, runtime, events = owned
    compose(runtime, events)
    runtime.recovery_admin = Unproven("recovery_admin", events)
    capture = runtime.maintenance_capture = CaptureOrchestrator(runtime, events)
    capture.start()
    events.clear()
    with pytest.raises(ShutdownIncomplete) as incomplete:
        runtime.stop()
    report = incomplete.value.report
    assert [event[1] for event in events if event[0] == "stop"] == ["recovery_admin", "maintenance_capture"]   # both asked
    assert [name for name, _ in report.failed] == ["recovery_admin"] and "native_chat" in report.held
    assert not report.database_closed and not report.admission_closed and not runtime.application.writer_admission.sealed
    assert runtime.application.writer_admission.shutting_down and runtime.ownership.held
    runtime.recovery_admin = Idle_("recovery_admin", events)                         # a later stop proves it and completes
    assert runtime.stop().clean


def test_shutdown_refusal_survives_a_recovery_window_thawing_while_the_stop_waits(owned):
    instance, runtime, events = owned
    compose(runtime, events)
    admission = runtime.application.writer_admission
    capture = runtime.maintenance_capture = CaptureOrchestrator(runtime, events)
    capture.start()
    capture.requested.set()
    assert capture.inside.wait(10)
    outcome = {}
    stopper = threading.Thread(target=lambda: outcome.setdefault("report", runtime.stop()))
    stopper.start()
    for _ in range(500):
        if admission.shutting_down:
            break
        threading.Event().wait(0.01)
    assert admission.shutting_down and stopper.is_alive()                          # stop waits for the in-flight capture
    assert mutate(runtime, instance) == "maintenance"                              # nothing external is admitted meanwhile
    capture.release.set()
    assert capture.finished.wait(10) and not capture.errors and capture.verified_late
    # The window thawed and cleared its own refusal; shutdown's refusal is not a thaw failure and still holds.
    stopper.join(15)
    assert not stopper.is_alive() and outcome["report"].clean
    assert admission.refusing and admission.sealed
    assert mutate(runtime, instance) == "maintenance"


def test_a_window_cannot_start_once_shutdown_has_begun_and_open_leaves_shutdown_refused(owned):
    instance, runtime, events = owned
    compose(runtime, events)
    admission = runtime.application.writer_admission
    with runtime.recovery_quiescence(timeout=5):
        admission.begin_shutdown()                                                  # shutdown requested inside a window
        assert admission.refusing
    assert admission.refusing and mutate(runtime, instance) == "maintenance"       # the window's open() did not lift it
    with pytest.raises(DomainError, match="Shutdown has begun"):
        with runtime.recovery_quiescence(timeout=5):
            pytest.fail("no window may start once shutdown has begun")


def test_recovery_windows_are_serialized_before_any_side_effect_and_judged_at_entry(owned):
    instance, runtime, events = owned
    compose(runtime, events)
    synced = []
    real_sync = runtime.sync_recovery_writers
    runtime.sync_recovery_writers = lambda: (synced.append("sync"), real_sync())[1]
    with runtime.recovery_quiescence(timeout=5) as proof:
        before = (list(events), list(synced), dict(runtime.recovery_quiescer.writers))
        with pytest.raises(DomainError) as recursive:                               # inside its own window
            with runtime.recovery_quiescence(timeout=5):
                pytest.fail("a recursive window must be refused")
        assert recursive.value.code == "recovery_not_quiescent" and recursive.value.retryable
        assert (events, synced, runtime.recovery_quiescer.writers) == before        # not synced, refused, drained or resumed
        proof.verify_active()
    # A context manager obtained before a later stop is judged by the ownership of the moment it is entered.
    pending = runtime.recovery_quiescence(timeout=5)
    assert runtime.stop().clean
    with pytest.raises(DomainError, match="own the instance"):
        with pending:
            pytest.fail("a window must not be entered after the instance was given back")
