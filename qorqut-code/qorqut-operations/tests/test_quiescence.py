"""Generic recovery quiescence with bounded fake controllers: no Docker, terminals, services or live host."""

from contextlib import contextmanager
import os
import shutil
import threading
from threading import Event, Thread

import pytest

from qorqut_operations.application import Application
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage import (BoundedWriter, DatabaseMaintenanceGate, KnownZeroWriters, QuiescenceProof,
                                       RecoveryBarrierProof, RecoveryQuiescer, UnprovenStop)
from qorqut_operations.storage.admission import InstallationRecoveryLock, RecoveryAdmission
from support import database_at


class Order:
    """One shared, ordered record of every controller and gate step."""

    def __init__(self):
        self.steps = []

    def note(self, *step):
        self.steps.append(step)


class Writer:
    """A stop/start service in the shape of a SignoffService or BackgroundService worker."""

    def __init__(self, order, name, *, timeout=False, running=True, restart_error=None):
        self.order, self.name, self.timeout, self.running, self.restart_error = order, name, timeout, running, restart_error
        self._thread = self

    def is_alive(self):
        return self.running

    def stop(self, *, timeout):
        self.order.note("stop", self.name, timeout)
        if self.timeout:
            raise TimeoutError()
        self.running = False

    def start(self):
        self.order.note("start", self.name)
        if self.restart_error:
            raise self.restart_error
        self.running = True


class Database:
    def __init__(self, claimed=False):
        self.claimed = claimed

    @contextmanager
    def read(self):
        database = self

        class Connection:
            def execute(self, sql):
                return self

            def fetchone(self):
                return (1,) if database.claimed else None
        yield type("Uow", (), {"connection": Connection()})()


@pytest.fixture(autouse=True)
def stub_database_file(tmp_path, monkeypatch):
    """The stub database names a real file, because a window is bound to the file it froze."""
    path = tmp_path / "stub.sqlite3"
    path.write_bytes(b"")
    monkeypatch.setattr(Database, "path", path, raising=False)
    return path


class Lock:
    def __init__(self, order):
        self.order = order

    @contextmanager
    def exclusive(self, *, timeout):
        self.order.note("lock", timeout)
        try:
            yield
        finally:
            self.order.note("unlock")


class Gate:
    def __init__(self, order):
        self.order, self.closed = order, False

    def close(self):
        self.order.note("gate-close")
        self.closed = True
        return object()

    def restore(self, closure):
        self.order.note("gate-restore")
        self.closed = False


class Host:
    def __init__(self, order, *, proof=QuiescenceProof("host", "frozen")):
        self.order, self.proof = order, proof

    def inventory_and_freeze(self, *, timeout):
        self.order.note("host-freeze", timeout)
        return self.proof

    def thaw(self):
        self.order.note("host-thaw")


class Containers:
    def __init__(self, order, *, thaw_error=None):
        self.order, self.thaw_error = order, thaw_error

    def freeze(self, *, timeout):
        self.order.note("containers-freeze")
        return QuiescenceProof("containers", "frozen")

    def thaw(self):
        self.order.note("containers-thaw")
        if self.thaw_error:
            raise self.thaw_error


def barrier(order, database=None, **overrides):
    values = dict(background=KnownZeroWriters("background"), signoffs=KnownZeroWriters("signoffs"),
                  browser_writers=KnownZeroWriters("browser"),
                  sandbox_writers=KnownZeroWriters("sandbox"), container_writers=Containers(order),
                  host_writers=Host(order), maintenance_gate=Gate(order), installation_lock=Lock(order))
    values.update(overrides)
    return RecoveryQuiescer(database or Database(), admission=RecoveryAdmission(), **values)


def names(order):
    return [step[0] + (":" + step[1] if len(step) > 1 and isinstance(step[1], str) else "") for step in order.steps]


def test_ordered_drain_and_resume_restores_the_boundary_only_after_everything_is_verified():
    order = Order()
    signoffs, background = Writer(order, "signoffs"), Writer(order, "background")
    recovery = barrier(order, signoffs=BoundedWriter(signoffs, "signoffs"), background=BoundedWriter(background, "background"))
    with recovery.quiesce(timeout=7):
        order.note("body")
        # Inside the barrier: the gate is closed and local admission refuses new writers.
        with pytest.raises(DomainError) as refused:
            with recovery.admission.writer():
                pass
        assert refused.value.code == "maintenance"
    # Drains come first, while admitted work can still settle; the lock, the gate and the freezes follow them.
    assert names(order) == ["stop:background", "stop:signoffs", "lock", "gate-close", "host-freeze", "containers-freeze",
                            "body", "containers-thaw", "host-thaw", "start:signoffs", "start:background",
                            "gate-restore", "unlock"]
    assert all(1 <= step[2] <= 7 for step in order.steps if step[0] == "stop")   # every drain is bounded
    with recovery.admission.writer():                                           # admission is open again
        pass


def test_a_worker_that_was_already_stopped_is_not_started_by_recovery():
    order = Order()
    stopped = Writer(order, "background", running=False)
    with barrier(order, background=BoundedWriter(stopped, "background")).quiesce():
        assert not stopped.running
    assert not stopped.running and names(order).count("start:background") == 0


def test_failed_drain_leaves_the_boundary_closed_and_resumes_nothing():
    order = Order()
    slow, other = Writer(order, "background", timeout=True), Writer(order, "signoffs")
    gate = Gate(order)
    recovery = barrier(order, background=BoundedWriter(slow, "background"), signoffs=BoundedWriter(other, "signoffs"),
                       maintenance_gate=gate)
    with pytest.raises(DomainError) as failure:
        with recovery.quiesce():
            pytest.fail("a writer that did not drain must not yield a recovery window")
    assert failure.value.code == "recovery_not_quiescent" and failure.value.retryable
    assert gate.closed and "gate-restore" not in names(order)
    assert not any(step[0] == "start" for step in order.steps) and "host-freeze" not in names(order)
    with pytest.raises(DomainError):
        with recovery.admission.writer():
            pass


def test_unknown_thaw_or_failed_restart_keeps_gate_and_admission_closed():
    order = Order()
    gate = Gate(order)
    uncertain = barrier(order, maintenance_gate=gate, background=BoundedWriter(Writer(order, "background"), "background"),
                        container_writers=Containers(order, thaw_error=DomainError("environment_unavailable", "Unpause was not verified")))
    with pytest.raises(DomainError, match="Unpause"):
        with uncertain.quiesce():
            assert gate.closed
    assert gate.closed and not any(step[0] == "start" for step in order.steps)
    with pytest.raises(DomainError):
        with uncertain.admission.writer():
            pass

    order = Order()
    gate = Gate(order)
    failing = Writer(order, "background", restart_error=RuntimeError("worker restart failed"))
    restart = barrier(order, maintenance_gate=gate, background=BoundedWriter(failing, "background"))
    with pytest.raises(RuntimeError, match="restart failed"):
        with restart.quiesce():
            pass
    assert gate.closed and "gate-restore" not in names(order)
    with pytest.raises(DomainError):
        with restart.admission.writer():
            pass


def test_a_body_error_resumes_only_after_a_verified_thaw():
    order = Order()
    raw = Writer(order, "background")
    recovery = barrier(order, background=BoundedWriter(raw, "background"))
    with pytest.raises(ValueError, match="capture failed"):
        with recovery.quiesce():
            raise ValueError("capture failed")
    assert names(order)[-3:] == ["start:background", "gate-restore", "unlock"] and raw.running
    with recovery.admission.writer():
        pass


def test_unverified_proofs_and_missing_controllers_are_refusals_never_bypasses():
    order = Order()
    with pytest.raises(DomainError, match="legacy-writer inventory"):
        with RecoveryQuiescer(Database(), admission=RecoveryAdmission(), installation_lock=Lock(order)).quiesce():
            pass
    with pytest.raises(DomainError, match="writer inventory is incomplete"):
        with RecoveryQuiescer(Database(), admission=RecoveryAdmission(), host_writers=Host(order),
                              installation_lock=Lock(order)).quiesce():
            pass
    with pytest.raises(DomainError, match="admission is not installed"):
        with RecoveryQuiescer(Database(), host_writers=Host(order), installation_lock=Lock(order)).quiesce():
            pass
    with pytest.raises(DomainError, match="installation lock is not installed"):
        with RecoveryQuiescer(Database(), admission=RecoveryAdmission(), host_writers=Host(order)).quiesce():
            pass

    class NoProof:
        def inventory_and_freeze(self, *, timeout):
            return None

        def thaw(self):
            pass
    with pytest.raises(DomainError, match="no verified"):
        with barrier(order, host_writers=NoProof()).quiesce():
            pass
    with pytest.raises(DomainError, match="no verified"):
        with barrier(order, host_writers=Host(order, proof=QuiescenceProof("host", "drained"))).quiesce():
            pass
    with pytest.raises(DomainError, match="bounded"):
        with barrier(order).quiesce(timeout=0):
            pass


def test_resource_claims_are_an_additional_refusal_not_a_proof():
    order = Order()
    recovery = barrier(order, Database(claimed=True))
    with pytest.raises(DomainError, match="file effects"):
        with recovery.quiesce():
            pass
    assert recovery.admission._closed          # a refused window leaves the local boundary closed


def test_admission_waits_for_active_writer_then_rejects_new_writes():
    admission, entered, release = RecoveryAdmission(), Event(), Event()

    def hold():
        with admission.writer():
            entered.set()
            release.wait(1)
    thread = Thread(target=hold)
    thread.start()
    assert entered.wait(1)
    with pytest.raises(TimeoutError):
        admission.close_and_wait(timeout=0.01)
    release.set()
    thread.join(1)
    admission.close_and_wait(timeout=1)
    with pytest.raises(DomainError, match="unavailable during recovery"):
        with admission.writer():
            pass
    admission.open()
    with admission.writer():
        pass


# -- the real database maintenance gate ------------------------------------------------------------------------------------

def test_database_maintenance_gate_closes_writes_and_restores_the_previous_state(tmp_path):
    database = database_at(tmp_path)
    gate = DatabaseMaintenanceGate(database)
    closure = gate.close()
    with database.read() as uow:
        assert uow.repository("maintenance_state").get("service")["state"] == "closed"
    with pytest.raises(DomainError) as closed:
        with database.transaction() as uow:
            Application.ensure_writes_open(uow)
    assert closed.value.code == "maintenance" and "recovery" in closed.value.message
    # A restore from a different database identity is refused and the gate stays closed.
    moved = tmp_path / "state.moved"
    database.path.rename(moved)
    database.path.write_bytes(moved.read_bytes())
    with pytest.raises(DomainError, match="identity changed"):
        gate.restore(closure)
    database.path.unlink()
    moved.rename(database.path)
    gate.restore(closure)
    with database.read() as uow:
        assert uow.repository("maintenance_state").get("service") is None
    with database.transaction() as uow:
        Application.ensure_writes_open(uow)


def test_an_already_closed_maintenance_gate_is_left_as_the_operator_set_it(tmp_path):
    database = database_at(tmp_path)
    with database.transaction() as uow:
        uow.repository("maintenance_state").insert({"id": "service", "state": "closed", "message": "operator window"})
    gate = DatabaseMaintenanceGate(database)
    closure = gate.close()
    gate.restore(closure)               # this capture did not close it, so it does not reopen it
    with database.read() as uow:
        state = uow.repository("maintenance_state").get("service")
        assert state["state"] == "closed" and state["message"] == "operator window"


def test_full_barrier_with_the_real_gate_database_and_recovery_lock(tmp_path):
    database = database_at(tmp_path)
    order = Order()
    lock = InstallationRecoveryLock(tmp_path / "locks" / "recovery.lock")
    admission = RecoveryAdmission(installation_lock=lock)
    recovery = RecoveryQuiescer(database, admission=admission, installation_lock=lock,
        background=BoundedWriter(Writer(order, "background"), "background"), signoffs=KnownZeroWriters("signoffs"),
        browser_writers=KnownZeroWriters("browser"),
        sandbox_writers=KnownZeroWriters("sandbox"), container_writers=Containers(order), host_writers=Host(order))
    with recovery.quiesce(timeout=5):
        with database.read() as uow:
            assert uow.repository("maintenance_state").get("service")["state"] == "closed"
        with pytest.raises(DomainError):
            with admission.writer():
                pass
    with database.read() as uow:
        assert uow.repository("maintenance_state").get("service") is None
    with admission.writer():
        pass
    assert names(order)[0] == "stop:background" and names(order)[-1] == "start:background"


# -- refuse new work first, drain admitted work, then seal --------------------------------------------------------------------

def test_admitted_work_settles_during_the_drain_without_reopening_admission_to_new_callers():
    """The drain runs while the lock and gate do not exist yet, so an admitted unit finishes its nested receipt writes."""
    order = Order()
    admission = RecoveryAdmission()
    admitted, may_finish, outcome = Event(), Event(), {}

    def receipt():
        with admission.writer():
            admitted.set()
            may_finish.wait(2)
            with admission.writer():           # the receipt completion nested in the admitted unit
                outcome["completed"] = True

    class DrainsAfterTheReceipt(Writer):
        def stop(self, *, timeout):
            may_finish.set()
            worker.join(2)                     # the drain proves the admitted effect finished
            assert outcome == {"completed": True}
            super().stop(timeout=timeout)

    worker = Thread(target=receipt)
    worker.start()
    assert admitted.wait(1)
    recovery = barrier(order, background=BoundedWriter(DrainsAfterTheReceipt(order, "background"), "background"))
    recovery.admission = admission
    with recovery.quiesce(timeout=5):
        pass
    assert outcome == {"completed": True}
    assert names(order)[:3] == ["stop:background", "lock", "gate-close"]


def test_new_external_work_is_refused_from_the_start_of_a_drain_and_admitted_work_is_not():
    admission, inside, release = RecoveryAdmission(), Event(), Event()
    refused = []

    def admitted_unit():
        with admission.writer():
            inside.set()
            release.wait(2)
            with admission.writer():
                refused.append("nested unit admitted")

    worker = Thread(target=admitted_unit)
    worker.start()
    assert inside.wait(1)
    admission.refuse_new()                                    # waits for nothing even though a unit is admitted
    assert admission.refusing and admission.admitted == 1
    with pytest.raises(DomainError) as new_caller:
        with admission.writer():
            pass
    assert new_caller.value.code == "maintenance"
    release.set()
    worker.join(2)
    assert refused == ["nested unit admitted"] and admission.admitted == 0 and admission.refusing
    admission.close_and_wait(timeout=1)
    admission.open()
    with admission.writer():
        pass


def test_a_seal_cannot_be_reopened_and_refuses_a_recovery_window():
    admission = RecoveryAdmission()
    admission.seal(timeout=1)
    with pytest.raises(DomainError, match="sealed"):
        admission.open()
    order = Order()
    recovery = barrier(order)
    recovery.admission = admission
    with pytest.raises(DomainError, match="sealed for shutdown"):
        with recovery.quiesce():
            pass
    assert order.steps == [] and admission.refusing

    held = RecoveryAdmission()
    release, inside = Event(), Event()

    def hold():
        with held.writer():
            inside.set()
            release.wait(2)
    worker = Thread(target=hold)
    worker.start()
    assert inside.wait(1)
    with pytest.raises(TimeoutError):
        held.seal(timeout=0.01)                              # a writer that does not drain leaves the seal taken
    assert held.sealed
    release.set()
    worker.join(2)
    with pytest.raises(DomainError, match="sealed"):
        held.open()


@pytest.mark.parametrize("result", [False, 0, True, {"stopped": False}, {"stopped": True}, QuiescenceProof("x", "drained"), object()])
def test_a_stop_that_returns_anything_is_not_the_void_stop_evidence(result):
    order = Order()

    class Returns(Writer):
        def stop(self, *, timeout):
            super().stop(timeout=timeout)
            return result

    recovery = barrier(order, background=BoundedWriter(Returns(order, "background"), "background"))
    with pytest.raises(DomainError) as failure:
        with recovery.quiesce():
            pytest.fail("an unexamined stop result must not yield a recovery window")
    assert failure.value.code == "recovery_not_quiescent" and failure.value.retryable
    assert "lock" not in names(order) and "host-freeze" not in names(order)
    assert not any(step[0] == "start" for step in order.steps)
    with pytest.raises(DomainError):
        with recovery.admission.writer():
            pass


def test_an_explicit_stop_adapter_must_prove_the_stop_and_bounded_writers_say_what_was_running():
    order = Order()

    class Loop(Writer):
        """A service whose stop reports a result, like the Context loop's ``{"stopped", "in_flight"}``."""

        def __init__(self, order, name, report):
            super().__init__(order, name)
            self.report = report

        def stop(self, *, timeout):
            super().stop(timeout=timeout)
            return self.report

    def adapter(writer, timeout):
        report = writer.stop(timeout=timeout)
        if not (isinstance(report, dict) and report.get("stopped") is True and report.get("in_flight") is False):
            raise UnprovenStop(f"stop reported {report!r}")

    proven = Loop(order, "context", {"stopped": True, "in_flight": False})
    recovery = barrier(order, required=("context",))
    recovery.register_writer("context", BoundedWriter(proven, "context", stop=adapter, running=lambda writer: writer.running))
    with recovery.quiesce():
        assert not proven.running
    assert proven.running and names(order).count("start:context") == 1       # it was running before the barrier

    order = Order()
    unproven = Loop(order, "context", {"stopped": True, "in_flight": True})
    recovery = barrier(order, required=("context",))
    recovery.register_writer("context", BoundedWriter(unproven, "context", stop=adapter))
    with pytest.raises(DomainError):
        with recovery.quiesce():
            pass
    assert not any(step[0] == "start" for step in order.steps) and "lock" not in names(order)

    order = Order()
    idle = Loop(order, "context", {"stopped": True, "in_flight": False})
    idle.running = False
    recovery = barrier(order, required=("context",))
    recovery.register_writer("context", BoundedWriter(idle, "context", stop=adapter, running=lambda writer: writer.running))
    with recovery.quiesce():
        pass
    assert "start:context" not in names(order)                            # only work that was running is resumed


def test_the_writer_inventory_is_complete_or_the_barrier_refuses_before_stopping_anything():
    order = Order()
    live = Writer(order, "background")
    recovery = barrier(order, background=BoundedWriter(live, "background"), required=("history", "context"))
    with pytest.raises(DomainError, match="inventory is incomplete: context, history"):
        with recovery.quiesce():
            pass
    assert order.steps == [] and live.running and not recovery.admission.refusing

    recovery.register_writer("history", BoundedWriter(Writer(order, "history"), "history"))
    with pytest.raises(DomainError, match="inventory is incomplete: context$"):
        with recovery.quiesce():
            pass
    recovery.register_writer("context", object())                          # an object that can neither drain nor resume
    with pytest.raises(DomainError, match="inventory is incomplete: context$"):
        with recovery.quiesce():
            pass
    assert order.steps == [] and not recovery.admission.refusing
    with pytest.raises(DomainError):
        recovery.register_writer("history", object())                      # a class is registered once
    with pytest.raises(DomainError):
        recovery.register_writer("background", object())


def test_a_zero_writer_inventory_is_verified_when_the_barrier_runs():
    order = Order()
    present = []
    recovery = barrier(order, required=("context",))
    recovery.register_writer("context", KnownZeroWriters("context", verify=lambda: not present))
    with recovery.quiesce():
        pass
    present.append("a Context loop appeared")
    with pytest.raises(DomainError, match="context writer inventory is no longer empty"):
        with recovery.quiesce():
            pass
    assert recovery.admission.refusing                                       # an unproven barrier stays closed


def test_a_busy_installation_lock_after_proved_drains_restores_the_running_services_and_admission():
    order = Order()

    class Busy(Lock):
        @contextmanager
        def exclusive(self, *, timeout):
            self.order.note("lock-busy")
            raise TimeoutError("installation recovery lock is busy")
            yield

    background, signoffs = Writer(order, "background"), Writer(order, "signoffs")
    gate = Gate(order)
    recovery = barrier(order, background=BoundedWriter(background, "background"), signoffs=BoundedWriter(signoffs, "signoffs"),
                       installation_lock=Busy(order), maintenance_gate=gate)
    with pytest.raises(DomainError, match="installation lock") as busy:
        with recovery.quiesce():
            pass
    assert busy.value.retryable
    assert names(order) == ["stop:background", "stop:signoffs", "lock-busy", "start:signoffs", "start:background"]
    assert background.running and signoffs.running and not gate.closed and not recovery.admission.refusing
    with recovery.admission.writer():
        pass


# -- the active barrier proof --------------------------------------------------------------------------------------------------

def real_barrier(tmp_path, order=None, **overrides):
    """A quiescer over a real database, a real installation lock and the real gate; only the writers are bounded stand-ins."""
    database = database_at(tmp_path)
    order = order or Order()
    lock = InstallationRecoveryLock(tmp_path / "locks" / "recovery.lock")
    values = dict(background=BoundedWriter(Writer(order, "background"), "background"), signoffs=KnownZeroWriters("signoffs"),
                  browser_writers=KnownZeroWriters("browser"),
                  sandbox_writers=KnownZeroWriters("sandbox"), container_writers=Containers(order), host_writers=Host(order))
    values.update(overrides)
    return RecoveryQuiescer(database, admission=RecoveryAdmission(installation_lock=lock), installation_lock=lock, **values), database, order


def refused(proof, action="verify_active", *arguments):
    with pytest.raises(DomainError) as error:
        getattr(proof, action)(*arguments)
    assert error.value.code == "recovery_not_quiescent"


class FreezesNothing(Containers):
    def freeze(self, *, timeout):
        return None                                                                    # no positive evidence


@pytest.mark.parametrize("failing", ["slow_writer", "false_host_proof", "no_container_proof"])
def test_no_proof_exists_unless_every_drain_and_freeze_succeeded(tmp_path, failing):
    order = Order()
    overrides = {"slow_writer": lambda: {"background": BoundedWriter(Writer(order, "background", timeout=True), "background")},
                 "false_host_proof": lambda: {"host_writers": Host(order, proof=QuiescenceProof("host", "drained"))},
                 "no_container_proof": lambda: {"container_writers": FreezesNothing(order)}}[failing]()
    recovery, database, _ = real_barrier(tmp_path, order, **overrides)
    with pytest.raises(DomainError):
        with recovery.quiesce(timeout=5) as proof:
            pytest.fail(f"no window may be yielded after {failing}: {proof!r}")
    assert recovery._active is None
    # The closed gate and the clear claims that remain are not a proof, and they are not reopened.
    with database.read() as uow:
        assert uow.repository("maintenance_state").get("service")["state"] == "closed"
        assert not uow.connection.execute("SELECT 1 FROM resource_claims LIMIT 1").fetchone()


def test_the_yielded_proof_is_active_only_inside_its_window_and_carries_read_only_evidence(tmp_path):
    recovery, database, order = real_barrier(tmp_path)
    with recovery.quiesce(timeout=5) as proof:
        assert isinstance(proof, RecoveryBarrierProof) and proof.generation == 1
        proof.verify_active()
        proof.require_database(database)
        assert proof.source.path == database.path.resolve() and (proof.source.device, proof.source.inode) == (
            os.stat(database.path).st_dev, os.stat(database.path).st_ino)
        assert [item.controller for item in proof.controllers] == ["background", "signoffs", "browser", "sandbox",
                                                                  "host", "containers"]
        with pytest.raises(AttributeError):
            proof.generation = 2                                                       # evidence is read-only
    refused(proof)
    refused(proof, "require_database", database)


def test_the_proof_is_revoked_before_anything_is_thawed_resumed_or_unlocked_on_every_exit(tmp_path):
    for fail in (False, True):
        seen = {}

        class Observed:
            def __init__(self, label):
                self.label = label

            def check(self):
                try:
                    holder["proof"].verify_active()
                    seen[self.label] = "active"
                except DomainError:
                    seen[self.label] = "revoked"

        holder = {}

        class WatchedHost(Host):
            def thaw(self):
                Observed("thaw").check()
                super().thaw()

        class WatchedWriter(Writer):
            def start(self):
                Observed("resume").check()
                super().start()

        order = Order()
        recovery, database, _ = real_barrier(tmp_path / str(fail), order, host_writers=WatchedHost(order),
            background=BoundedWriter(WatchedWriter(order, "background"), "background"))
        try:
            with recovery.quiesce(timeout=5) as proof:
                holder["proof"] = proof
                Observed("inside").check()
                if fail:
                    raise ValueError("the capture failed")
        except ValueError:
            assert fail
        assert seen == {"inside": "active", "thaw": "revoked", "resume": "revoked"}
        refused(proof)


def test_a_proof_is_not_valid_in_a_later_window_and_a_recreated_one_is_never_the_issued_one(tmp_path):
    recovery, database, order = real_barrier(tmp_path)
    with recovery.quiesce(timeout=5) as first:
        pass
    with recovery.quiesce(timeout=5) as second:
        assert second is not first and second.generation == first.generation + 1
        second.verify_active()
        refused(first)                                                                 # stale while a new generation is active
        lookalike = RecoveryBarrierProof(second.generation, os.getpid(), threading.get_ident(), second.source,
                                         second.controllers, recovery)
        refused(lookalike)                                                             # same fields, never issued
        other, _, _ = real_barrier(tmp_path / "other")
        refused(RecoveryBarrierProof(second.generation, os.getpid(), threading.get_ident(), second.source, (), other))
    refused(first)
    refused(second)


def test_the_proof_is_refused_for_another_thread_a_foreign_database_and_a_replaced_file(tmp_path):
    recovery, database, order = real_barrier(tmp_path)
    foreign = database_at(tmp_path / "foreign")
    # Leaving a window whose file was replaced cannot restore the gate; maintenance stays closed.
    with pytest.raises(DomainError, match="Database identity changed"), recovery.quiesce(timeout=5) as proof:
        outcome = {}

        def elsewhere():
            try:
                proof.verify_active()
            except DomainError as error:
                outcome["code"] = error.code
        worker = Thread(target=elsewhere)
        worker.start()
        worker.join(5)
        assert outcome == {"code": "recovery_not_quiescent"}
        refused(proof, "require_database", foreign)                                    # a different database
        refused(proof, "require_database", type("Stub", (), {"path": None})())
        proof.require_database(database)
        # The same path now names a different file: the window no longer covers the database that was frozen.
        replacement = database.path.with_name("replacement.sqlite3")
        shutil.copy2(database.path, replacement)
        os.replace(replacement, database.path)
        refused(proof)
        refused(proof, "require_database", database)


def test_a_database_replaced_while_the_barrier_is_established_issues_no_proof_and_leaves_maintenance_closed(tmp_path):
    class Replacing(Host):
        def inventory_and_freeze(self, *, timeout):
            replacement = database.path.with_name("replacement.sqlite3")
            shutil.copy2(database.path, replacement)
            os.replace(replacement, database.path)
            return super().inventory_and_freeze(timeout=timeout)

    order = Order()
    recovery, database, _ = real_barrier(tmp_path, order, host_writers=Replacing(order))
    with pytest.raises(DomainError, match="replaced"):
        with recovery.quiesce(timeout=5):
            pytest.fail("a replaced source database must not yield a window")
    assert recovery._active is None and not any(step[0] == "start" for step in order.steps)
    with database.read() as uow:
        assert uow.repository("maintenance_state").get("service")["state"] == "closed"
    with pytest.raises(DomainError):                                                   # and admission stays closed
        with recovery.admission.writer():
            pass


def test_a_proof_does_not_survive_admission_being_reopened_beneath_it(tmp_path):
    recovery, database, order = real_barrier(tmp_path)
    with recovery.quiesce(timeout=5) as proof:
        proof.verify_active()
        recovery.admission.open()
        refused(proof)
        recovery.admission.refuse_new()


def test_a_second_window_is_refused_without_touching_the_running_one(tmp_path):
    recovery, database, order = real_barrier(tmp_path)
    with recovery.quiesce(timeout=5) as proof:
        steps = list(order.steps)
        outcome = {}

        def concurrent():
            try:
                with recovery.quiesce(timeout=5):
                    outcome["entered"] = True
            except DomainError as error:
                outcome["error"] = (error.code, error.retryable)
        for attempt in (concurrent, lambda: concurrent()):                             # from another thread, then nested
            if attempt is concurrent:
                worker = Thread(target=concurrent)
                worker.start()
                worker.join(5)
            else:
                concurrent()
            assert outcome == {"error": ("recovery_not_quiescent", True)}
            outcome.clear()
        assert order.steps == steps                                                    # nothing drained, resumed or reopened
        proof.verify_active()
        with pytest.raises(DomainError):
            with recovery.admission.writer():
                pass
    assert names(order).count("start:background") == 1
    with recovery.admission.writer():
        pass


# -- the start, recovery and shutdown refusals are independent --------------------------------------------------------------

def admitted(admission):
    try:
        with admission.writer():
            return True
    except DomainError as error:
        assert error.code == "maintenance"
        return False


def test_a_withheld_admission_is_ended_only_by_admit_and_never_by_a_recovery_window_ending():
    admission = RecoveryAdmission(serving=False)
    assert admission.refusing and not admission.serving and not admitted(admission)
    admission.open()                                              # a recovery window's reopening is not the start
    assert not admitted(admission)
    admission.admit()
    assert admission.serving and not admission.refusing and admitted(admission)
    admission.refuse_new()                                        # a recovery refusal is its own reason
    assert not admitted(admission)
    admission.open()
    assert admitted(admission)
    admission.withhold()                                          # a failed start refuses again, waiting for nothing
    assert not admitted(admission)
    admission.refuse_new()
    admission.admit()
    assert not admitted(admission)                                # admit() leaves an unproven recovery refusal alone
    admission.open()
    assert admitted(admission)


def test_begin_shutdown_refuses_new_units_without_waiting_and_lets_admitted_receipts_finish():
    admission = RecoveryAdmission()
    inside, proceed, outcome = Event(), Event(), {}

    def receipt():
        with admission.writer():
            inside.set()
            proceed.wait(5)
            with admission.writer():                              # the receipt completion nested in the admitted unit
                outcome["settled"] = True

    worker = Thread(target=receipt)
    worker.start()
    assert inside.wait(5)
    admission.begin_shutdown()                                    # returns at once although a unit is admitted
    assert admission.shutting_down and admission.refusing and admission.admitted == 1 and not admission.sealed
    assert not admitted(admission)
    proceed.set()
    worker.join(5)
    assert outcome == {"settled": True} and admission.admitted == 0
    # Neither a recovery reopening nor the startup path lifts it; only the seal's own no-reopen rule is separate.
    admission.open()
    assert admission.refusing and not admitted(admission)
    with pytest.raises(DomainError):
        admission.admit()
    admission.seal(timeout=1)
    assert admission.sealed
    with pytest.raises(DomainError, match="sealed"):
        admission.open()


def test_a_window_thawing_after_shutdown_began_leaves_admission_refused_and_no_new_window_starts(tmp_path):
    recovery, database, order = real_barrier(tmp_path)
    admission = recovery.admission
    with recovery.quiesce(timeout=5) as proof:
        admission.begin_shutdown()                                # shutdown is requested while the window is open
        proof.verify_active()                                     # the window itself is unaffected
    assert admission.refusing and not admitted(admission)         # its open() cleared only its own refusal; no thaw failure
    assert names(order).count("start:background") == 1           # the thaw and the restart completed normally
    with pytest.raises(DomainError, match="Shutdown has begun"):
        with recovery.quiesce(timeout=5):
            pytest.fail("no window starts after shutdown began")
    assert names(order).count("start:background") == 1
