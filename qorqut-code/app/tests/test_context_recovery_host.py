"""The real BackupStore capture/verify boundary over a real Context repository and Operations database.

A generation holds the operational database and the Context pair under one proven writer barrier; verification
validates both; a missing or altered Context makes the generation invalid; an offline restore reproduces the
authoritative bytes at different paths without starting anything. The barrier proof is the real Operations
``RecoveryBarrierProof`` of a real ``RecoveryQuiescer`` window over the synthetic database (explicit synthetic external
writer controllers; see ``context_recovery_support``); the old method-shaped substitute is only a negative example.
"""

import json
import shutil
import sqlite3
import tarfile
import threading
import time

import pytest

from qorqut_context import recovery as component
from qorqut_context.engine import file_lock
from qorqut_operations.contracts import DomainError, new_id
from qorqut_operations.storage.admission import InstallationRecoveryLock, RecoveryAdmission
from qorqut_operations.storage.quiescence import RecoveryBarrierProof

import context_recovery_support as support
from bazarlyk.sandbox.recovery import FullRecoveryStore, ResticBinary
from qorqut_context.integration.driver import LOCK_NAME
from bazarlyk.storage import recovery as recovery_module
from qorqut_context.integration.recovery import CAPTURE_DIRECTORY, ProofBarrier
from bazarlyk.storage.recovery import BackupStore


@pytest.fixture
def host(tmp_path, monkeypatch):
    host = support.seed(support.build(tmp_path, monkeypatch))
    yield host
    host.quiescence.close()


def runs(host, generation):
    with host.database.read() as uow:
        return uow.repository("backup_runs").get(generation)


def work(host):
    """Everything an unwanted model call or generated operational work would change."""
    return (host.runner.calls[:], host.count("jobs"), host.count("context_inputs"), host.count("context_input_attempts"),
            host.count("context_completions"), host.count("context_filings"), host.count("documents"))


def refuse(host, generation, code, **options):
    with pytest.raises(DomainError) as raised:
        support.create(host, generation, **options)
    assert raised.value.code == code, raised.value.to_dict()
    assert not (host.backups.destination / generation).exists()
    assert not [path for path in host.backups.destination.iterdir() if path.name.startswith(".incomplete-")]
    assert runs(host, generation)["state"] == "failed"
    return raised.value


# ---- the generation holds both components under one barrier ---------------------------------------------------------

def test_a_generation_holds_the_database_and_the_context_under_one_barrier_and_verification_checks_both(host):
    generation, before = new_id(), support.authority(host.paths.context, host.paths.context_runtime)
    metadata = support.create(host, generation)
    context = metadata["context"]
    assert metadata["schema_version"] == 2 and context["complete"] and context["pending_event"]
    assert context["source_repository"] == str(host.paths.context) and context["source_runtime"] == str(host.paths.context_runtime)
    assert (host.backups.destination / generation / CAPTURE_DIRECTORY / "manifest.json").is_file()
    assert host.backups.verify(generation) == metadata
    assert runs(host, generation)["state"] == "complete"
    # The real source Context is untouched by the capture, and the barrier was proven for this very database around it.
    assert support.authority(host.paths.context, host.paths.context_runtime) == before
    proof = host.quiescence.proofs[0]
    assert type(proof) is RecoveryBarrierProof
    assert host.quiescence.checks(proof) >= 6 and host.quiescence.covered(proof) and all(
        database is host.database for database in host.quiescence.covered(proof))
    # The same generation again is only re-verified, never captured again; the proof is still checked first, once.
    with host.quiescence() as again:
        assert host.backups.create(generation, barrier=again) == metadata
    assert host.quiescence.covered(again) == [host.database] and host.quiescence.checks(again) == 2


def test_the_authored_snapshot_never_walks_the_context_checkout_and_the_old_contextbase_capture_is_gone(host):
    generation = new_id()
    support.create(host, generation)
    graph = host.paths.context.relative_to(host.paths.operations).as_posix()
    assert (host.paths.context / ".git").is_dir() and any((host.paths.context).iterdir())
    names = tarfile.open(host.backups.destination / generation / "authored/sources.tar").getnames()
    assert not [name for name in names if name == graph or name.startswith(graph + "/")]
    names = {path.name for path in (host.backups.destination / generation).iterdir()}
    assert {"authored", "context", "generation.json", "state.sqlite3"} <= names
    assert not names & {"contextbase.sqlite3", "costs.sqlite3"}
    assert not hasattr(BackupStore, "context_base_path") and not hasattr(BackupStore, "_capture_context_base")
    assert not hasattr(BackupStore, "_capture_context_costs")


def test_capture_verification_and_restore_start_no_model_and_generate_no_operational_work(host):
    before = work(host)
    generation, destination = new_id(), host.tmp / "rehearsal"
    support.create(host, generation)
    host.backups.verify(generation)
    host.backups.restore(generation, destination)
    assert work(host) == before
    # The restored pair is only data: no driver lock, search index or run was created by restoring it.
    restored = destination / "context-graph", destination / ".runtime/context"
    assert not (restored[1] / LOCK_NAME).exists() and not (restored[1] / "search.sqlite3").exists()


# ---- offline restore ------------------------------------------------------------------------------------------------

def test_restore_into_other_paths_reproduces_the_authoritative_hashes_and_the_exact_pending_state(host):
    generation, destination = new_id(), host.tmp / "rehearsal"
    live = support.authority(host.paths.context, host.paths.context_runtime)
    live_rows = support.rows(host.paths.database)
    pending = (host.paths.context_runtime / "pending.json").read_bytes()
    metadata = support.create(host, generation)
    result = host.backups.restore(generation, destination)
    repository, runtime = destination / "context-graph", destination / ".runtime/context"
    assert result["state"] == "closed" and result["activation_root"] == str(host.paths.operations)
    assert result["context"]["status"] == "restored" and result["context"]["manifest_sha256"] == metadata["context"]["manifest_sha256"]
    assert (result["context"]["repository"], result["context"]["runtime"]) == (str(repository), str(runtime))
    # Identical refs, graph/event bytes, usage evidence and pending state, at paths unlike the originals.
    assert support.authority(repository, runtime) == live
    assert (runtime / "pending.json").read_bytes() == pending
    assert support.rows(destination / host.backups.database_relative) == live_rows       # queue, job and completion bindings
    # Only the two ownership markers name the new location; the engine accepts the pair as it stands.
    assert json.loads((repository / ".git/qorqut-context/runtime.json").read_bytes()) == {"runtime_root": str(runtime)}
    assert json.loads((runtime / "context.json").read_bytes()) == {"context_root": str(repository)}
    assert not (destination / ".runtime/restore-incomplete.json").exists()
    assert component.check_pair(repository, runtime, json.loads(
        (host.backups.destination / generation / CAPTURE_DIRECTORY / "manifest.json").read_bytes()))["published_commit"]
    with sqlite3.connect(destination / host.backups.database_relative) as connection:
        assert connection.execute("SELECT state FROM maintenance_state").fetchone() == ("closed",)
    # The live pair keeps naming itself.
    assert json.loads((host.paths.context_runtime / "context.json").read_bytes()) == {"context_root": str(host.paths.context)}


def test_a_partial_restore_stays_incomplete_and_nonactive_and_resumes_only_the_same_pair(host, monkeypatch):
    generation, destination = new_id(), host.tmp / "rehearsal"
    live = support.authority(host.paths.context, host.paths.context_runtime)
    metadata = support.create(host, generation)

    class Stopped(BaseException):
        pass

    original = component._Restore.checkpoint

    def interrupt(self, name):
        if name == "bound":
            raise Stopped(name)
        return original(self, name)

    monkeypatch.setattr(component._Restore, "checkpoint", interrupt)
    with pytest.raises(Stopped):
        host.backups.restore(generation, destination)
    monkeypatch.setattr(component._Restore, "checkpoint", original)
    marker = json.loads((destination / ".runtime/restore-incomplete.json").read_text())
    assert marker == {"schema_version": 1, "generation_id": generation, "pending": "context"}
    assert not (destination / "context/.git").exists() and not (destination / ".runtime/context/context.json").exists()
    journal = json.loads((destination / ".context.context-restore.json").read_text())
    assert journal["phase"] == "bound" and journal["manifest_sha256"] == metadata["context"]["manifest_sha256"]
    # The rehearsal refuses a second host restore over the non-empty destination, and a different capture is refused.
    with pytest.raises(DomainError) as raised:
        host.backups.restore(generation, destination)
    assert raised.value.code == "invalid_restore"
    other = new_id()
    support.create(host, other)
    with pytest.raises(DomainError) as raised:
        host.backups.context.restore(host.backups.destination / other / CAPTURE_DIRECTORY, json.loads(
            (host.backups.destination / other / "generation.json").read_text())["context"], destination)
    assert raised.value.code == "invalid_restore" and raised.value.details["context_code"] == "restore_conflict"
    # The same capture finishes deterministically and clears the marker only after the pair passed its checks.
    finished = host.backups.context.restore(host.backups.destination / generation / CAPTURE_DIRECTORY, metadata["context"], destination,
                                            marker=destination / ".runtime/restore-incomplete.json")
    assert finished["status"] == "restored"
    assert not (destination / ".runtime/restore-incomplete.json").exists()
    assert support.authority(destination / "context-graph", destination / ".runtime/context") == live


# ---- the barrier proof ----------------------------------------------------------------------------------------------

def test_capture_requires_the_actual_proof_and_never_infers_one(host):
    store, generation = host.backups, new_id()
    with pytest.raises(TypeError):
        store.create(generation)
    half = type("Half", (), {"verify_active": lambda self: None})()
    for missing in (None, object(), True, lambda: None, {"verify_active": lambda: None}, half):
        with pytest.raises(DomainError) as raised:
            store.create(generation, barrier=missing)
        assert raised.value.code == "invalid_configuration"
    assert not store.destination.exists() or not (store.destination / generation).exists()
    with pytest.raises(DomainError):
        ProofBarrier(object(), host.database)


def test_a_method_shaped_substitute_or_a_forged_subclass_cannot_establish_the_host_boundary(host):
    """Only the exact Operations proof type is host evidence, even when it would answer every call correctly."""
    store, generation = host.backups, new_id()
    forged = type("Forged", (RecoveryBarrierProof,), {"verify_active": lambda self: None,
                                                       "require_database": lambda self, database: None})
    with host.quiescence() as real:
        subclass = forged(real.generation, real.process_id, real.thread_id, real.source, real.controllers, real.issuer)
        for imitation in (support.Substitute(host.database), subclass):
            with pytest.raises(DomainError) as raised:
                store.create(generation, barrier=imitation)
            assert raised.value.code == "invalid_configuration"
            with pytest.raises(DomainError):
                ProofBarrier(imitation, host.database)
        assert runs(host, generation) is None and not (store.destination / generation).exists()
        assert store.create(generation, barrier=real)["state"] == "complete"          # the real proof of the same window works


def test_the_real_window_issues_the_proof_only_once_established_and_revokes_it_on_exit(host):
    quiescence, generation = host.quiescence, new_id()
    external = quiescence.external
    assert external.running
    with quiescence() as proof:
        assert type(proof) is RecoveryBarrierProof and proof.process_id and proof.thread_id == threading.get_ident()
        assert not external.running and quiescence.admission.refusing                # drained and refused before the capture
        writes = external.writes
        assert quiescence.journal[:3] == [("drain", "external"), ("freeze", "hosts"), ("freeze", "containers")]
        with host.database.read() as uow:
            assert uow.repository("maintenance_state").get("service")["state"] == "closed"
        host.backups.create(generation, barrier=proof)
        time.sleep(0.05)
        assert external.writes == writes                                              # nothing external wrote during the capture
        proof.verify_active()
    with pytest.raises(DomainError) as raised:                                        # revoked on exit, before anything resumed
        proof.verify_active()
    assert raised.value.code == "recovery_not_quiescent"
    assert quiescence.journal[3:] == [("thaw", "containers"), ("thaw", "hosts"), ("resume", "external")]
    assert external.running and not quiescence.admission.refusing
    with host.database.read() as uow:
        assert uow.repository("maintenance_state").get("service") is None


def test_an_ended_proof_refuses_before_any_effect_even_for_a_sealed_generation_and_verify_stays_read_only(host):
    store, sealed, fresh = host.backups, new_id(), new_id()
    metadata = support.create(host, sealed)
    with host.quiescence() as ended:
        pass
    before_run, before_directory = runs(host, sealed), sorted(path.name for path in store.destination.iterdir())
    for generation in (sealed, fresh):
        with pytest.raises(DomainError) as raised:
            store.create(generation, barrier=ended)
        assert raised.value.code == "recovery_not_quiescent"
    assert runs(host, sealed) == before_run and runs(host, fresh) is None             # not even a re-recorded sealed generation
    assert sorted(path.name for path in store.destination.iterdir()) == before_directory
    assert not (store.destination / fresh).exists()
    entered = len(host.quiescence.entries)
    assert store.verify(sealed) == metadata                                           # verification needs no window or proof
    assert runs(host, sealed) == before_run and len(host.quiescence.entries) == entered


def test_a_proof_of_another_database_is_refused_before_any_effect(host, tmp_path, monkeypatch):
    store, sealed, fresh = host.backups, new_id(), new_id()
    support.create(host, sealed)
    copy = host.database.backup(tmp_path / "other-copy.sqlite3")
    other = host.database.restore(tmp_path / "other-copy.sqlite3", tmp_path / "other" / "state.sqlite3", expected_checksum=copy["checksum"])
    lock = InstallationRecoveryLock(tmp_path / "other" / "recovery.lock")
    foreign = support.Quiescence(host, monkeypatch, database=other, admission=RecoveryAdmission(installation_lock=lock), lock=lock)
    before_run = runs(host, sealed)
    try:
        with foreign() as proof:
            proof.verify_active()                                                    # a perfectly good proof, of the other file
            for generation in (sealed, fresh):
                with pytest.raises(DomainError) as raised:
                    store.create(generation, barrier=proof)
                assert raised.value.code == "recovery_not_quiescent" and "froze" in raised.value.to_dict()["message"]
    finally:
        foreign.close()
    assert runs(host, sealed) == before_run and runs(host, fresh) is None and not (store.destination / fresh).exists()
    assert not [path for path in store.destination.iterdir() if path.name.startswith(".incomplete-")]


def test_a_proof_used_from_another_thread_is_refused_before_any_effect(host):
    store, generation, outcome = host.backups, new_id(), {}
    with host.quiescence() as proof:
        def elsewhere():
            try:
                store.create(generation, barrier=proof)
            except DomainError as error:
                outcome["error"] = error

        thread = threading.Thread(target=elsewhere)
        thread.start()
        thread.join(60)
    assert outcome["error"].code == "recovery_not_quiescent"
    assert runs(host, generation) is None and not (store.destination / generation).exists()


def test_a_proof_of_an_ended_window_is_not_revived_by_a_later_window(host):
    with host.quiescence() as first:
        pass
    with host.quiescence() as second:
        with pytest.raises(DomainError) as raised:
            host.backups.create(new_id(), barrier=first)
        assert raised.value.code == "recovery_not_quiescent"
        assert host.backups.create(new_id(), barrier=second)["state"] == "complete"


@pytest.mark.parametrize("boundary", ["context_copy", "components_done", "before_seal"])
def test_a_barrier_lost_at_any_boundary_never_seals_a_generation(host, monkeypatch, boundary):
    generation, store = new_id(), host.backups
    with host.quiescence() as proof:
        def lose(owner, name, after=False):
            original = getattr(owner, name)

            def wrapper(*args, **kwargs):
                if not after:
                    host.quiescence.admission.open()                            # an outsider reopens write admission mid-window
                result = original(*args, **kwargs)
                host.quiescence.admission.open()
                return result

            monkeypatch.setattr(owner, name, wrapper)

        # Inside the Context copy, after all components are captured, and after the final verification before sealing.
        {"context_copy": lambda: lose(component, "_copy_stable"),
         "components_done": lambda: lose(recovery_module, "build_exports"),
         "before_seal": lambda: lose(BackupStore, "_verify_directory", after=True)}[boundary]()
        with pytest.raises(DomainError) as raised:
            store.create(generation, barrier=proof)
    assert raised.value.code == "recovery_not_quiescent", raised.value.to_dict()
    assert not (store.destination / generation).exists()
    assert not [path for path in store.destination.iterdir() if path.name.startswith(".incomplete-")]
    assert runs(host, generation)["state"] == "failed"


# ---- refused sources ------------------------------------------------------------------------------------------------

def test_a_still_running_context_writer_refuses_and_the_same_capture_succeeds_once_it_stopped(host):
    generation = new_id()
    with file_lock(host.paths.context_runtime / "writer.lock"):
        assert refuse(host, generation, "backup_incomplete").retryable
    with file_lock(host.paths.context_runtime / LOCK_NAME):
        error = refuse(host, new_id(), "backup_incomplete")
        assert error.details["context_code"] == "writer_active"
    assert support.create(host, new_id())["context"]["complete"]


def test_missing_runtime_or_git_objects_refuse_without_a_partial_capture(host):
    context = host.context
    blob = next(line.split()[0] for line in context.git.run("rev-list", "--objects", "--all").decode().splitlines()
                if line.split()[-1].startswith("graph/events/"))
    loose = host.paths.context / ".git/objects" / blob[:2] / blob[2:]
    saved = loose.read_bytes()
    loose.chmod(0o644)
    loose.unlink()
    assert refuse(host, new_id(), "backup_incomplete").details["context_code"] == "object_missing"
    loose.write_bytes(saved)
    job = json.loads((host.paths.context_runtime / "pending.json").read_bytes())
    pending_input = host.paths.context_runtime / "events" / job["id"] / "input.bin"
    original = pending_input.read_bytes()
    pending_input.write_bytes(b"tampered input")
    assert refuse(host, new_id(), "backup_incomplete").details["context_code"] == "source_invalid"
    pending_input.write_bytes(original)
    assert support.create(host, new_id())["context"]["complete"]


def test_an_unclassified_file_in_the_context_runtime_is_refused_not_dropped(host):
    stray = host.paths.context_runtime / "operator-note.txt"
    stray.write_text("keep me")
    error = refuse(host, new_id(), "backup_incomplete")
    assert error.details["context_code"] == "unclassified_authority" and list(error.details["paths"]) == ["runtime/operator-note.txt"]
    stray.unlink()
    assert support.create(host, new_id())["context"]["complete"]


def test_an_invalid_ownership_pair_refuses(host):
    marker = host.paths.context_runtime / "context.json"
    original = marker.read_bytes()
    marker.write_text(json.dumps({"context_root": str(host.tmp / "elsewhere")}))
    assert refuse(host, new_id(), "backup_incomplete").details["context_code"] == "invalid_ownership"
    marker.write_bytes(original)
    assert support.create(host, new_id())["context"]["complete"]


# ---- the Context is a required part of the generation ---------------------------------------------------------------

def damaged(host, mutate, code="invalid_backup"):
    generation = new_id()
    support.create(host, generation)
    directory = host.backups.destination / generation
    mutate(directory)
    with pytest.raises(DomainError) as raised:
        host.backups.verify(generation)
    assert raised.value.code == code
    return generation


def test_removing_or_altering_the_captured_context_makes_the_generation_invalid(host):
    damaged(host, lambda directory: shutil.rmtree(directory / CAPTURE_DIRECTORY))
    damaged(host, lambda directory: (directory / CAPTURE_DIRECTORY / "manifest.json").unlink())
    damaged(host, lambda directory: (directory / CAPTURE_DIRECTORY / "git/objects.pack").write_bytes(b"corrupt"))
    runtime_file = lambda directory: next((directory / CAPTURE_DIRECTORY / "runtime").rglob("input.bin"))
    damaged(host, lambda directory: runtime_file(directory).write_bytes(runtime_file(directory).read_bytes() + b"!"))
    damaged(host, lambda directory: runtime_file(directory).unlink())
    damaged(host, lambda directory: (directory / CAPTURE_DIRECTORY / "runtime/extra.json").write_text("{}"))
    damaged(host, lambda directory: (directory / CAPTURE_DIRECTORY).rename(directory / "elsewhere"))


def test_a_generation_without_its_recorded_context_or_with_another_instances_context_is_invalid(host):
    def edit(directory, change):
        path = directory / "generation.json"
        metadata = json.loads(path.read_text())
        change(metadata)
        path.write_text(json.dumps(metadata))

    damaged(host, lambda directory: edit(directory, lambda metadata: metadata.pop("context")))
    damaged(host, lambda directory: edit(directory, lambda metadata: metadata.update(schema_version=1)))
    damaged(host, lambda directory: edit(directory, lambda metadata: metadata["context"].update(source_repository="/other/context")))
    damaged(host, lambda directory: edit(directory, lambda metadata: metadata["context"].update(manifest_sha256="0" * 64)))
    damaged(host, lambda directory: edit(directory, lambda metadata: metadata["context"].update(complete=False)))


def test_a_corrupt_context_is_never_restored(host):
    generation, destination = new_id(), host.tmp / "rehearsal"
    support.create(host, generation)
    (host.backups.destination / generation / CAPTURE_DIRECTORY / "git/objects.pack").write_bytes(b"corrupt")
    with pytest.raises(DomainError) as raised:
        host.backups.restore(generation, destination)
    assert raised.value.code == "invalid_backup" and not destination.exists()


# ---- full installation recovery -------------------------------------------------------------------------------------

def test_full_recovery_captures_under_the_yielded_proof_and_restores_the_context_at_its_final_paths(host):
    for slug in host.ids:
        host.paths.business_code(slug).mkdir(parents=True, exist_ok=True)
    restic, digest = support.write_restic(host.tmp)
    password = host.tmp / "restic-password"
    password.write_text("synthetic\n")
    password.chmod(0o600)
    store = FullRecoveryStore(host.database, host.paths, repository=host.tmp / "encrypted", password_file=password,
                              authority_staging=host.tmp / "staging", restic=ResticBinary(restic, digest), initialize_repository=True)
    generation, live = new_id(), support.authority(host.paths.context, host.paths.context_runtime)
    created = store.create(generation, quiescence=host.quiescence())
    proof = host.quiescence.proofs[0]
    assert created["state"] == "complete" and type(proof) is RecoveryBarrierProof and host.quiescence.checks(proof) >= 6
    with pytest.raises(DomainError) as ended:
        proof.verify_active()
    assert ended.value.code == "recovery_not_quiescent"
    assert not (host.tmp / "staging" / generation).exists()
    # A plain context manager that yields no proof cannot capture.
    class Plain:
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    with pytest.raises(DomainError) as raised:
        store.create(new_id(), quiescence=Plain())
    assert raised.value.code == "invalid_configuration"
    destination = host.tmp / "full-rehearsal"
    result = store.restore(generation, destination)
    assert result["state"] == "closed" and result["context"]["status"] == "restored"
    assert support.authority(destination / "context-graph", destination / ".runtime/context") == live
    assert not (destination / ".runtime/restore-incomplete.json").exists()
    assert json.loads((destination / ".runtime/context/context.json").read_bytes()) == {
        "context_root": str(destination / "context-graph")}
