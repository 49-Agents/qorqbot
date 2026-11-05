"""The host's one ordered service lifetime, proven with bounded injected controllers and the real Signoff/background threads.

``Runtime.start`` takes the instance's ownership, validates the stored target, reconciles prepared effects and only then
starts the retained services in their fixed order; a failed start unwinds only the services that attempt started, with
proof. ``Runtime.stop`` refuses new work, drains the services, seals write admission, waits for admitted writers and
closes SQLite and releases ownership last; a drain that cannot be proved is a reported incomplete shutdown that leaves the
database open and keeps ownership. The controllers record calls and expose only the drain hooks the
lifetime is allowed to use, so a call to anything else (for example a native-session reset) raises. No real model,
terminal, provider or native product session is created."""

import pytest
from qorqut_operations.contracts import DomainError

import bazarlyk.service.lifetime as lifetime_module
from bazarlyk.background.integration import attach_background
from qorqut_operations.history_exports import ObserverStop
from bazarlyk.service.lifetime import ShutdownIncomplete
from host_foundation_support import build_instance

START_ORDER = ("native_chat", "inbox_nudges", "activity", "incidents", "signoffs", "background", "context", "terminal_control",
               "telegram", "broadcast_dispatcher", "sandbox", "previews", "recovery_admin", "email_history",
               "changes", "history", "maintenance_capture")
STOP_ORDER = ("recovery_admin", "maintenance_capture", "context", "history", "inbox_nudges", "native_chat",
              "terminal_control", "telegram", "broadcast_dispatcher", "activity", "incidents", "changes",
              "previews", "sandbox", "background", "signoffs", "email_history")
# Lane B's Context services and the history observer are read from these runtime attributes.
ATTRIBUTES = {"context": "context_services", "history": "history_observer"}


class Controller:
    """A bounded stand-in for one service: only the hooks ``Runtime.lifetime`` uses, each recording its call."""

    def __init__(self, name, events):
        self.name, self.events, self.fail_start, self.fail_stop = name, events, None, None

    def start(self):
        self.events.append(("start", self.name))
        if self.fail_start:
            raise self.fail_start

    def start_external_writer_ticker(self, connection_factory):
        assert callable(connection_factory)
        self.start()

    def stop(self, timeout=None):
        self.events.append(("stop", self.name))
        if self.fail_stop:
            raise self.fail_stop

    stop_observer = stop


class ContextController(Controller):
    """The Context loop's contract: ``stop`` reports ``{"stopped", "in_flight"}``; ``running`` and ``start()`` as well."""

    running = True

    def stop(self, timeout=None):
        super().stop(timeout)
        return {"stopped": True, "in_flight": False}


class HistoryController(Controller):
    """The history observer's contract: ``stop(timeout)`` reports ``ObserverStop``."""

    def stop(self, timeout=None):
        super().stop(timeout)
        return ObserverStop(True, False, False, None)

    def state(self):
        return type("State", (), {"started": True, "stopping": False})()


@pytest.fixture
def host(tmp_path):
    instance, _ = build_instance(tmp_path)
    runtime, events = instance.runtime, []
    kinds = {"context": ContextController, "history": HistoryController, "maintenance_capture": ContextController}
    controllers = {name: kinds.get(name, Controller)(name, events) for name in START_ORDER}
    for name, controller in controllers.items():
        setattr(runtime, ATTRIBUTES.get(name, name), controller)
    real_validate, real_reconcile = runtime.validate_target, runtime.application.file_executor.recover_pending
    runtime.validate_target = lambda: (events.append(("validate", None)), real_validate())[1]
    runtime.application.file_executor.recover_pending = lambda *a, **k: (events.append(("reconcile", None)), real_reconcile(*a, **k))[1]
    real_close = runtime.database.close
    runtime.database.close = lambda: (events.append(("close", "database")), real_close())[1]
    real_seal = runtime.application.writer_admission.seal
    runtime.application.writer_admission.seal = lambda **k: (events.append(("admission", "closed")), real_seal(**k))[1]
    return instance, runtime, controllers, events


def test_the_inventory_names_every_retained_service_in_start_order(host):
    _, runtime, _, _ = host
    assert tuple(name for name, _ in runtime.lifetime().inventory()) == START_ORDER


def test_start_validates_and_reconciles_before_the_ordered_starts_and_stop_closes_sqlite_last(host):
    instance, runtime, _, events = host
    assert runtime.start() is runtime
    assert events[:2] == [("validate", None), ("reconcile", None)]
    assert [name for kind, name in events[2:] if kind == "start"] == list(START_ORDER)
    assert runtime.running == set(START_ORDER)

    events.clear()
    report = runtime.stop()
    assert [name for kind, name in events if kind == "stop"] == list(STOP_ORDER)
    # Admission closes after every service drained, and SQLite closes after admission.
    assert [kind for kind, _ in events][-2:] == ["admission", "close"]
    assert report.clean and report.database_closed and report.admission_closed and not report.failed
    assert set(report.stopped) == set(STOP_ORDER) and runtime.running == set()


def test_a_failed_start_unwinds_only_the_services_that_attempt_started(host):
    instance, runtime, controllers, events = host
    runtime.running.add("native_chat")  # left running by an earlier completed start; this attempt does not own it
    controllers["telegram"].fail_start = RuntimeError("injected start failure")
    with pytest.raises(RuntimeError, match="injected start failure"):
        runtime.start()
    started = [name for kind, name in events if kind == "start"]
    stopped = [name for kind, name in events if kind == "stop"]
    assert started == ["inbox_nudges", "activity", "incidents", "signoffs", "background", "context", "terminal_control",
                       "telegram"]
    # Newest first, including the failing step (it may have begun before it raised); never the earlier attempt's
    # service and never a service that was not started.
    assert stopped == ["telegram", "terminal_control", "context", "background", "signoffs", "incidents", "activity",
                       "inbox_nudges"]
    assert runtime.running == {"native_chat"}
    # The database stays open for a corrected retry, which starts only what is not already running.
    with instance.database.read() as uow:
        uow.connection.execute("SELECT 1").fetchone()
    assert ("close", "database") not in events
    controllers["telegram"].fail_start = None
    events.clear()
    runtime.start()
    assert [name for kind, name in events if kind == "start"] == [name for name in START_ORDER if name != "native_chat"]
    assert runtime.running == set(START_ORDER)


def test_a_drain_that_cannot_be_proved_is_an_incomplete_shutdown_that_leaves_sqlite_open(host):
    instance, runtime, controllers, events = host
    runtime.start()
    events.clear()
    controllers["signoffs"].fail_stop = TimeoutError("the Signoff worker did not stop")
    with pytest.raises(ShutdownIncomplete) as incomplete:
        runtime.stop()
    report = incomplete.value.report
    assert [name for name, _ in report.failed] == ["signoffs"] and not report.clean
    # The other services were still asked to drain, admission was closed, and the database was NOT closed.
    assert {name for kind, name in events if kind == "stop"} == set(STOP_ORDER)
    assert report.admission_closed and not report.database_closed
    assert ("close", "database") not in events and runtime.running == set(START_ORDER)
    with instance.database.read() as uow:
        assert uow.connection.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(DomainError) as closed:
        with runtime.application.writer_admission.writer():
            pass
    assert closed.value.code == "maintenance"

    # Once the drain can be proved, the same stop completes and closes SQLite.
    controllers["signoffs"].fail_stop = None
    assert runtime.stop().clean
    assert events[-1] == ("close", "database") and runtime.running == set()


def test_an_admitted_writer_that_does_not_drain_blocks_the_database_close(host, monkeypatch):
    instance, runtime, _, events = host
    monkeypatch.setattr(lifetime_module, "ADMISSION_DRAIN_SECONDS", 0.05)
    runtime.start()
    events.clear()
    with runtime.application.writer_admission.writer():
        with pytest.raises(ShutdownIncomplete) as incomplete:
            runtime.stop()
    assert [name for name, _ in incomplete.value.report.failed] == ["write admission"]
    assert not incomplete.value.report.database_closed and ("close", "database") not in events
    assert runtime.stop().database_closed


def test_the_real_signoff_and_background_services_start_and_drain_under_the_lifetime(tmp_path):
    instance, _ = build_instance(tmp_path)
    runtime, events = instance.runtime, []
    background = attach_background(runtime, runtime.programs)
    for name in START_ORDER:
        if name not in ("signoffs", "background"):
            setattr(runtime, name, (ContextController if name == "maintenance_capture" else Controller)(name, events))
    runtime.freeze_registry()
    runtime.start()
    threads = (runtime.signoffs._thread, background._thread)
    try:
        assert all(thread.is_alive() for thread in threads)
    finally:
        report = runtime.stop()
    assert report.clean and report.database_closed
    assert not any(thread.is_alive() for thread in threads)
