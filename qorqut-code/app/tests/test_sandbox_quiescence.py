from contextlib import contextmanager
from multiprocessing import Event as ProcessEvent, Process
from threading import Event, Thread
from time import monotonic

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.sandbox.docker import Ownership
from qorqut_operations.storage.quiescence import BoundedWriter, QuiescenceProof, KnownZeroWriters, RecoveryQuiescer
from bazarlyk.sandbox.quiescence import DockerFreezer
from qorqut_operations.storage.admission import RecoveryAdmission


class Database:
    def __init__(self, claimed=False): self.claimed = claimed
    @contextmanager
    def read(self):
        database = self
        class Connection:
            def execute(self, sql): return self
            def fetchone(self): return (1,) if database.claimed else None
        yield type("Uow", (), {"connection": Connection()})()


class Writer:
    def __init__(self, *, timeout=False, running=True):
        self.calls, self.timeout, self.running = [], timeout, running
        self._thread = self
    def is_alive(self): return self.running
    def stop(self, *, timeout):
        self.calls.append(("stop", timeout))
        if self.timeout: raise TimeoutError()
        self.running = False
    def start(self):
        self.calls.append(("start",))
        self.running = True


class Lock:
    @contextmanager
    def exclusive(self, *, timeout):
        yield


class Containers:
    def freeze(self, *, timeout): return QuiescenceProof("containers", "frozen")
    def thaw(self): pass


class Gate:
    def __init__(self): self.closed = False
    def close(self): self.closed = True; return object()
    def restore(self, closure): self.closed = False


class Host:
    def __init__(self): self.calls = []
    def inventory_and_freeze(self, *, timeout):
        self.calls.append(("freeze", timeout)); return QuiescenceProof("host", "frozen")
    def thaw(self): self.calls.append(("thaw",))


class FakeDocker:
    def __init__(self, *, pause_second=True, refuse_unpause=False):
        self.calls, self.paused = [], set()
        self.pause_second, self.refuse_unpause = pause_second, refuse_unpause
        self.ids = ("a" * 64, "b" * 64)
    def call(self, *arguments, timeout=90):
        self.calls.append((arguments, timeout))
        if arguments[:2] == ("container", "ls"): return "\n".join(self.ids)
        if arguments[0] == "pause":
            if arguments[1] != self.ids[1] or self.pause_second: self.paused.add(arguments[1])
            return ""
        if arguments[0] == "unpause":
            if not self.refuse_unpause: self.paused.discard(arguments[1])
            return ""
        raise AssertionError(arguments)
    def owned(self, kind, identity, ownership, *, timeout=30):
        return {"State": {"Running": identity not in self.paused, "Paused": identity in self.paused}}


def ownership(): return Ownership("a" * 20, "123e4567-e89b-12d3-a456-426614174000")
def barrier(**kwargs):
    defaults = dict(host_writers=Host(), background=KnownZeroWriters("background"), signoffs=KnownZeroWriters("signoffs"), email_history=KnownZeroWriters("email_history"),
                    browser_writers=KnownZeroWriters("browser"), sandbox_writers=KnownZeroWriters("sandbox"),
                    container_writers=Containers(), maintenance_gate=Gate())
    defaults.update(kwargs)
    return RecoveryQuiescer(Database(), admission=RecoveryAdmission(), installation_lock=Lock(), **defaults)


def test_quiescer_requires_real_legacy_inventory_and_proof_returning_controllers():
    with pytest.raises(DomainError, match="legacy-writer inventory"):
        with RecoveryQuiescer(Database(), admission=RecoveryAdmission(), installation_lock=Lock()).quiesce(): pass
    with pytest.raises(DomainError, match="writer inventory is incomplete"):
        with RecoveryQuiescer(Database(), admission=RecoveryAdmission(), host_writers=Host(), installation_lock=Lock()).quiesce(): pass
    class BadHost:
        def inventory_and_freeze(self, *, timeout): return None
        def thaw(self): pass
    with pytest.raises(DomainError, match="no verified"):
        with barrier(host_writers=BadHost()).quiesce(): pass


@pytest.mark.parametrize("writer_name", ["background", "signoffs", "email_history"])
def test_quiescer_stops_joins_and_resumes_registered_writers(writer_name):
    raw, host = Writer(), Host()
    with barrier(host_writers=host, **{writer_name: BoundedWriter(raw, writer_name)}).quiesce(timeout=7):
        assert raw.calls[0][0] == "stop" and 1 <= raw.calls[0][1] <= 7
    assert raw.calls[-1] == ("start",)
    assert host.calls[0][0] == "freeze" and 1 <= host.calls[0][1] <= 7
    assert host.calls[-1] == ("thaw",)


def test_quiescer_preserves_a_worker_that_was_already_stopped():
    raw = Writer(running=False)
    with barrier(background=BoundedWriter(raw, "background")).quiesce():
        assert not raw.running
    assert not raw.running and [call[0] for call in raw.calls] == ["stop"]


def test_failed_worker_restart_does_not_reopen_maintenance():
    class FailedRestart(Writer):
        def start(self):
            raise RuntimeError("worker restart failed")
    gate = Gate()
    recovery = barrier(maintenance_gate=gate, background=BoundedWriter(FailedRestart(), "background"))
    with pytest.raises(RuntimeError, match="restart failed"):
        with recovery.quiesce():
            pass
    assert gate.closed
    with pytest.raises(DomainError):
        with recovery.admission.writer():
            pass


def test_quiescer_sanitizes_timeout_and_keeps_claims_as_additional_check():
    with pytest.raises(DomainError, match="did not drain"):
        with barrier(background=BoundedWriter(Writer(timeout=True), "background")).quiesce(): pass
    with pytest.raises(DomainError, match="file effects"):
        with RecoveryQuiescer(Database(claimed=True), admission=RecoveryAdmission(), host_writers=Host(), installation_lock=Lock(),
                         background=KnownZeroWriters("background"), signoffs=KnownZeroWriters("signoffs"), email_history=KnownZeroWriters("email_history"),
                         browser_writers=KnownZeroWriters("browser"), sandbox_writers=KnownZeroWriters("sandbox"), container_writers=Containers(), maintenance_gate=Gate()).quiesce(): pass


def test_uncertain_thaw_keeps_maintenance_and_write_admission_closed():
    class FailedThaw(Containers):
        def thaw(self):
            raise DomainError("environment_unavailable", "Unpause was not verified")
    gate, raw = Gate(), Writer()
    recovery = barrier(maintenance_gate=gate, container_writers=FailedThaw(),
                       background=BoundedWriter(raw, "background"))
    with pytest.raises(DomainError, match="Unpause"):
        with recovery.quiesce():
            assert gate.closed
    assert gate.closed and not any(call[0] == "start" for call in raw.calls)
    with pytest.raises(DomainError):
        with recovery.admission.writer():
            pass


def test_capture_error_resumes_only_after_verified_thaw():
    gate, raw = Gate(), Writer()
    recovery = barrier(maintenance_gate=gate, background=BoundedWriter(raw, "background"))
    with pytest.raises(ValueError, match="capture failed"):
        with recovery.quiesce():
            raise ValueError("capture failed")
    assert not gate.closed and raw.calls[-1] == ("start",)
    with recovery.admission.writer():
        pass


def test_recovery_admission_waits_for_active_writer_then_rejects_new_writes():
    admission, entered, release = RecoveryAdmission(), Event(), Event()
    thread = Thread(target=lambda: _write(admission, entered, release)); thread.start(); assert entered.wait(1)
    with pytest.raises(TimeoutError): admission.close_and_wait(timeout=0.01)
    release.set(); thread.join(1); admission.close_and_wait(timeout=1)
    with pytest.raises(DomainError, match="unavailable during recovery"):
        with admission.writer(): pass
    admission.open()


def _write(admission, entered, release):
    with admission.writer(): entered.set(); release.wait(1)


def test_docker_freezer_rolls_back_timeout_candidate_and_preserves_existing_pause():
    docker = FakeDocker(pause_second=False); freezer = DockerFreezer(docker)
    candidates = []
    with pytest.raises(DomainError, match="paused state"):
        freezer.freeze(ownership(), deadline=monotonic() + 2, on_candidate=candidates.append)
    assert not docker.paused
    assert candidates == [docker.ids[0], docker.ids[1]]
    assert any(c[0][0] == "unpause" and c[0][1] == docker.ids[0] for c in docker.calls)


def test_docker_freezer_skips_preexisting_pause_and_verifies_resume():
    docker = FakeDocker(); docker.paused.add(docker.ids[0])
    freezer, candidates = DockerFreezer(docker), []
    freezer.freeze(ownership(), deadline=monotonic() + 2, on_candidate=candidates.append)
    assert candidates == [docker.ids[1]]
    assert docker.ids[0] in docker.paused
    assert not any(call[0][:2] == ("pause", docker.ids[0]) for call in docker.calls)
    freezer.thaw(ownership(), candidates, deadline=monotonic() + 2)
    assert docker.ids[0] in docker.paused and docker.ids[1] not in docker.paused


def _hold_shared_recovery_lock(root, entered, release):
    from qorqut_operations.storage.admission import InstallationRecoveryLock
    with InstallationRecoveryLock(root).shared(timeout=1):
        entered.set()
        release.wait(5)


def test_installation_lock_excludes_recovery_until_reconciliation_releases(tmp_path):
    from qorqut_operations.storage.admission import InstallationRecoveryLock
    entered, release = ProcessEvent(), ProcessEvent()
    process = Process(target=_hold_shared_recovery_lock, args=(str(tmp_path), entered, release))
    process.start()
    assert entered.wait(2)
    with pytest.raises(TimeoutError):
        with InstallationRecoveryLock(tmp_path).exclusive(timeout=1): pass
    release.set()
    process.join(2)
    assert process.exitcode == 0
    with InstallationRecoveryLock(tmp_path).exclusive(timeout=1): pass


def test_sandbox_container_freeze_retains_candidate_after_failed_rollback():
    from bazarlyk.sandbox.quiescence import DockerSandboxWriters
    business = "123e4567-e89b-12d3-a456-426614174000"
    class ContainerDatabase:
        @contextmanager
        def read(self):
            class Connection:
                def execute(self, sql): return self
                def fetchall(self): return [(business,)]
            yield type("Uow", (), {"connection": Connection()})()
    class Freezer:
        class docker: pass
        def freeze(self, ownership, *, deadline, on_candidate):
            on_candidate("candidate")
            raise DomainError("environment_unavailable", "pause outcome uncertain", retryable=True)
        def thaw(self, ownership, identities, *, deadline):
            raise DomainError("environment_unavailable", "resume failed", retryable=True)
    writers = DockerSandboxWriters(ContainerDatabase(), "/installation", freezer=Freezer())
    with pytest.raises(DomainError, match="pause outcome"):
        writers.freeze(timeout=2)
    assert writers._frozen and writers._frozen[0][1] == ("candidate",)


def _hold_admitted_writer(root, entered, release):
    from qorqut_operations.storage.admission import InstallationRecoveryLock, RecoveryAdmission
    admission = RecoveryAdmission(installation_lock=InstallationRecoveryLock(root))
    with admission.writer():
        entered.set()
        release.wait(5)


def test_admission_shared_lock_excludes_recovery_across_processes(tmp_path):
    from qorqut_operations.storage.admission import InstallationRecoveryLock, RecoveryAdmission
    entered, release = ProcessEvent(), ProcessEvent()
    process = Process(target=_hold_admitted_writer, args=(str(tmp_path), entered, release))
    process.start()
    assert entered.wait(2)
    with pytest.raises(TimeoutError):
        with InstallationRecoveryLock(tmp_path).exclusive(timeout=1): pass
    release.set()
    process.join(2)
    assert process.exitcode == 0
    admission = RecoveryAdmission(installation_lock=InstallationRecoveryLock(tmp_path))
    with InstallationRecoveryLock(tmp_path).exclusive(timeout=1):
        with pytest.raises(DomainError) as blocked:
            with admission.writer():
                pytest.fail("Recovery must reject new writes without blocking worker shutdown")
        assert blocked.value.code == "maintenance"
    with admission.writer():
        pass


def _enter_admission(admission, entered):
    with admission.writer():
        entered.set()
