"""Instance ownership across the actual Runtime lifetime, over disposable synthetic instances.

One backend owns an instance for its whole lifetime. ``Runtime.start`` takes the exclusive OS-backed lifetime lock
(``instance-lifetime.lock`` beside the database, independent of the installation recovery lock) before it validates the
target, reconciles prepared effects, recovers native inputs or starts anything; a second backend on the same canonical
instance (another port, another process) is refused with nothing done, while a distinct instance owns its own lock. A
failed start or a stop whose drain cannot be proved keeps SQLite and the lock until a later *real* drain, and every
unproven drain is judged from the service's own typed evidence: the real Context driver loop and the real history
observer are used here with a blocked step in place of any model, snapshot or provider. Nothing is started that touches a
native session, terminal, network or operational data."""

import json
import os
import sqlite3
import subprocess
import sys
import threading

import pytest
from qorqut_operations.contracts import DomainError

import bazarlyk.native_chat.store as native_store
from qorqut_operations.history_exports import HistoryObserver
from bazarlyk.service.lifetime import ShutdownIncomplete
from bazarlyk.service.ownership import LOCK_NAME, InstanceOwnership
from bazarlyk.service.runtime import build_runtime
from qorqut_context.integration.loop import ContextDriverLoop
from host_foundation_support import build_instance

OWNED_BY_ANOTHER = "instance_in_use"
SERVICES = ("native_chat", "inbox_nudges", "activity", "incidents", "signoffs", "background", "terminal_control",
            "telegram", "broadcast_dispatcher", "sandbox", "previews", "recovery_admin", "email_history",
            "changes")


class Idle:
    """A service that records its calls and has nothing to drain: only the hooks the lifetime is allowed to use."""

    def __init__(self, name, events):
        self.name, self.events = name, events

    def start(self, *a, **k):
        self.events.append(("start", self.name))

    start_external_writer_ticker = start

    def stop(self, timeout=None):
        self.events.append(("stop", self.name))

    stop_observer = stop


def quiet(runtime, events):
    """Replace every thread-owning service by an inert recorder so starts and stops are bounded and observable."""
    for name in SERVICES:
        setattr(runtime, name, Idle(name, events))
    return runtime


def record_steps(runtime, events):
    """Record the validation and prepared-effect reconciliation, the first things a start does after ownership."""
    real_validate, real_reconcile = runtime.validate_target, runtime.application.file_executor.recover_pending
    runtime.validate_target = lambda: (events.append(("validate", None)), real_validate())[1]
    runtime.application.file_executor.recover_pending = lambda *a, **k: (events.append(("reconcile", None)), real_reconcile(*a, **k))[1]


class Blocked:
    """A driver whose step is stuck until released: the loop's thread stays alive and ``stop`` must say so."""

    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Event()

    def recover(self):
        return {"status": "idle"}

    def process_next(self):
        self.entered.set()
        self.release.wait(10)
        return {"status": "empty"}


def bounded(service, method="stop", seconds=0.2):
    """Cap the real stop's join so a stuck worker is judged in a fraction of a second; the real stop still runs."""
    real = getattr(service, method)
    setattr(service, method, lambda timeout=5.0: real(min(timeout, seconds)))
    return service


@pytest.fixture
def owner(tmp_path):
    instance, _ = build_instance(tmp_path, owned=False)
    events = []
    quiet(instance.runtime, events)
    record_steps(instance.runtime, events)
    return instance, instance.runtime, events


def second_backend(instance, events):
    """Another backend assembled over the very same canonical instance (as a second port or process would be)."""
    backend = build_runtime(instance.workspace, paths=instance.paths)
    quiet(backend, events)
    record_steps(backend, events)
    return backend


def test_assembling_a_backend_is_inert_and_takes_no_lock(owner):
    instance, runtime, events = owner
    lock = instance.paths.runtime / LOCK_NAME
    assert not lock.exists() and not runtime.ownership.held
    other = build_runtime(instance.workspace, paths=instance.paths)      # a second assembly is no refusal either
    assert not other.ownership.held and not lock.exists() and events == []
    runtime.start()
    assert lock.is_file() and runtime.ownership.held and oct(lock.stat().st_mode & 0o777) == "0o600"
    assert runtime.paths.locks != lock.parent                            # not the recovery lock's directory or file
    assert not (instance.paths.locks / LOCK_NAME).exists()
    runtime.stop()


@pytest.mark.parametrize("entry", ["start", "own_instance"])
def test_owned_start_restores_wal_before_admitting_work(owner, entry):
    instance, original, _ = owner
    original.database.close()
    with sqlite3.connect(instance.paths.database) as connection:
        assert connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
    runtime = second_backend(instance, [])
    with runtime.database.read() as uow:
        assert uow.connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    enable_wal = runtime.database.enable_wal
    calls = []

    def prepare():
        runtime.ownership.verify()
        with pytest.raises(DomainError):
            with runtime.application.writer_admission.writer():
                pytest.fail("Mutations must not be admitted before WAL is ready")
        calls.append("owned, not admitted")
        return enable_wal()

    runtime.database.enable_wal = prepare
    getattr(runtime, entry)()
    assert calls == ["owned, not admitted"]
    with runtime.database.read() as uow:
        assert uow.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert runtime.stop().clean


def test_a_second_backend_on_the_same_instance_is_refused_before_it_validates_recovers_or_starts_anything(owner, monkeypatch):
    instance, runtime, events = owner
    runtime.start()
    events.clear()
    recovered = []
    monkeypatch.setattr(native_store, "recover_claimed", lambda connection: recovered.append("native inputs") or 0)
    other_events = []
    other = second_backend(instance, other_events)
    with pytest.raises(DomainError) as refused:
        other.start()                                                     # e.g. the same instance behind another port
    assert refused.value.code == OWNED_BY_ANOTHER
    # Nothing a start does came first: no validation, no prepared-effect or native-input recovery, no service.
    assert other_events == [] and recovered == [] and not other.ownership.held and other.running == set()
    assert other.lifetime_state.phase == "new"
    with pytest.raises(DomainError) as unrecovered:
        with other.recovery_quiescence(timeout=1):                        # a non-owner cannot open a recovery window either
            pytest.fail("a backend that does not own the instance must not enter a recovery window")
    assert unrecovered.value.code == "recovery_not_quiescent"
    # The owner is untouched and still owns the instance.
    assert events == [] and runtime.ownership.held
    runtime.ownership.verify()
    assert runtime.stop().clean and not runtime.ownership.held

    # Once the owner has drained and released, the instance can be owned again, by a new backend.
    assert other.start() is other and other.ownership.held
    assert [name for kind, name in other_events if kind == "start"] == list(SERVICES)
    assert other.stop().clean


def test_a_distinct_instance_owns_its_own_lock_at_the_same_time(owner, tmp_path):
    instance, runtime, events = owner
    other, _ = build_instance(tmp_path / "elsewhere", owned=False)
    quiet(other.runtime, [])
    runtime.start()
    other.runtime.start()                                                 # a different canonical instance: no conflict
    assert runtime.ownership.path != other.runtime.ownership.path
    assert runtime.ownership.held and other.runtime.ownership.held
    assert other.runtime.stop().clean and runtime.stop().clean


def test_the_lock_is_refused_when_its_location_is_unsafe_replaced_or_foreign_and_a_live_file_is_never_unlinked(owner):
    instance, runtime, _ = owner
    lock = instance.paths.runtime / LOCK_NAME

    lock.symlink_to(instance.paths.runtime / "elsewhere.lock")             # a symlinked lock file
    with pytest.raises(DomainError) as symlinked:
        runtime.ownership.acquire()
    assert symlinked.value.code == "ownership_unavailable" and not runtime.ownership.held
    assert not (instance.paths.runtime / "elsewhere.lock").exists()        # the link was not followed or created through
    lock.unlink()

    lock.write_text("")                                                    # a file other users can write
    lock.chmod(0o666)
    with pytest.raises(DomainError, match="writable by other users"):
        runtime.ownership.acquire()
    lock.unlink()

    lock.write_text("")                                                    # a hard-linked file shared with another name
    os.link(lock, instance.paths.runtime / "alias.lock")
    with pytest.raises(DomainError, match="not a plain file"):
        runtime.ownership.acquire()
    lock.unlink()
    (instance.paths.runtime / "alias.lock").unlink()

    runtime.start()
    inode = lock.stat().st_ino
    assert runtime.stop().clean and lock.stat().st_ino == inode            # released, never unlinked

    # A held lock whose file was removed (or replaced) is detected, not trusted.
    fresh = InstanceOwnership(instance.paths)
    fresh.acquire()
    lock.unlink()
    with pytest.raises(DomainError) as lost:
        fresh.verify()
    assert lost.value.code == "ownership_lost"
    fresh.release()
    lock.write_text("replaced")
    lock.chmod(0o600)
    held = InstanceOwnership(instance.paths).acquire()
    lock.unlink()
    lock.write_text("another file")
    lock.chmod(0o600)
    with pytest.raises(DomainError, match="replaced"):
        held.verify()
    held.release()


def test_another_process_cannot_take_the_lock_of_a_running_backend(owner):
    instance, runtime, _ = owner
    script = ("import json,sys\nfrom pathlib import Path\nfrom bazarlyk.instance_paths import InstancePaths\n"
              "from bazarlyk.service.ownership import InstanceOwnership\nfrom qorqut_operations.contracts import DomainError\n"
              "value=json.loads(sys.argv[1]);paths=InstancePaths(Path(value['root']),{k:Path(v) for k,v in value['components'].items()})\n"
              "try:\n    InstanceOwnership(paths).acquire();print('acquired')\nexcept DomainError as error:\n    print(error.code)\n")
    paths = instance.paths
    argument = json.dumps({"root": str(paths.instance_root), "components": {k: str(v) for k, v in paths.components.items()}})

    def attempt():
        return subprocess.run([sys.executable, "-c", script, argument], capture_output=True, text=True, timeout=60, cwd=str(paths.instance_root),
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    runtime.start()
    refused = attempt()
    assert refused.stdout.strip() == OWNED_BY_ANOTHER, refused.stderr
    assert runtime.stop().clean
    assert attempt().stdout.strip() == "acquired"                          # the OS released it with the clean stop


def test_a_failed_start_with_a_loop_that_cannot_prove_its_stop_keeps_sqlite_and_ownership_until_a_real_drain(owner):
    instance, runtime, events = owner
    driver = Blocked()
    runtime.context_services = bounded(ContextDriverLoop(driver, poll_seconds=0.05))
    runtime.telegram.start = lambda: (_ for _ in ()).throw(RuntimeError("injected start failure"))   # fails after Context started
    with pytest.raises(RuntimeError, match="injected start failure") as failed:
        runtime.start()
    assert driver.entered.wait(5)
    # The original error is the one raised, annotated with what the cleanup could and could not prove.
    notes = "\n".join(failed.value.__notes__)
    assert "unproven context" in notes and "SQLite stays open" in notes and "ownership is held" in notes
    assert "context" in runtime.lifetime_state.incomplete and runtime.lifetime_state.phase == "failed_start"
    assert runtime.context_services.running and runtime.ownership.held
    # A stop that merely returned is no unwind: the database is open, the lock held, another backend still refused.
    with instance.database.read() as uow:
        assert uow.connection.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(DomainError) as other:
        second_backend(instance, []).start()
    assert other.value.code == OWNED_BY_ANOTHER
    # A new start is refused while an unproven service remains; stopping now is incomplete for the same reason.
    with pytest.raises(DomainError) as refused:
        runtime.start()
    assert refused.value.code == "lifetime_incomplete"
    with pytest.raises(ShutdownIncomplete) as incomplete:
        runtime.stop()
    assert [name for name, _ in incomplete.value.report.failed] == ["context"]
    assert not incomplete.value.report.database_closed and runtime.ownership.held

    driver.release.set()                                                   # the step ends: a real drain can now be proved
    report = runtime.stop()
    assert report.clean and report.database_closed and report.ownership_released
    assert not runtime.context_services.running and not runtime.ownership.held and runtime.lifetime_state.incomplete == {}
    successor = second_backend(instance, [])
    assert successor.start().ownership.held and successor.stop().clean


def test_a_history_observer_that_cannot_prove_its_stop_is_an_incomplete_shutdown_until_its_snapshot_ends(owner):
    instance, runtime, events = owner
    release, running = threading.Event(), threading.Event()

    def snapshot():
        running.set()
        release.wait(10)
    runtime.history_observer = bounded(HistoryObserver(snapshot, interval=0.05))
    runtime.start()
    assert running.wait(5)
    with pytest.raises(ShutdownIncomplete) as incomplete:
        runtime.stop()
    report = incomplete.value.report
    assert [name for name, _ in report.failed] == ["history"] and "did not report a completed stop" in report.failed[0][1]
    assert not report.database_closed and not report.ownership_released and runtime.ownership.held
    with pytest.raises(DomainError) as closed:                              # new work stays refused; admission is sealed
        with runtime.application.writer_admission.writer():
            pass
    assert closed.value.code == "maintenance"
    with pytest.raises(DomainError):
        runtime.application.writer_admission.open()

    release.set()
    assert runtime.stop().clean and not runtime.ownership.held
    successor = second_backend(instance, [])
    assert successor.start().ownership.held and successor.stop().clean


def test_repeated_start_and_stop_are_bounded_and_say_what_is_true(owner):
    instance, runtime, events = owner
    assert runtime.stop().clean is True                                    # never started: nothing to drain, nothing held
    with pytest.raises(DomainError) as ended:
        runtime.start()
    assert ended.value.code == "lifetime_closed"

    other = second_backend(instance, [])
    other.start()
    other.start()                                                          # a repeated start starts nothing twice
    first = other.stop()
    again = other.stop()                                                   # a repeated clean stop answers the first report
    assert first.clean and again is first
    with pytest.raises(DomainError) as stopped:
        other.start()
    assert stopped.value.code == "lifetime_closed"
