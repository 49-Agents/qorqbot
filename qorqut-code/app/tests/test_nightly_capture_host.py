"""The nightly job's asynchronous capture handoff: request, coordinator capture under quiescence, then continuation.

The real ``NightlyMaintenance`` runs as the registered ``maintenance.nightly`` program on the real ``BackgroundService`` over a
real Operations database and Context. The quiescence is a real Operations ``RecoveryQuiescer`` window and its proof the real
``RecoveryBarrierProof``; only the external writer controllers and the runtime port are explicit synthetic stand-ins
(``context_recovery_support``). The stock runtime's own composition of that window is not exercised here.
"""

import threading
import time
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.executor import application_claim
from qorqut_operations.storage.quiescence import RecoveryBarrierProof

import context_recovery_support as support
from bazarlyk.storage import maintenance_capture, recovery as storage_recovery
from bazarlyk.storage.versioning import PENDING_SECONDS


@pytest.fixture
def host(tmp_path, monkeypatch):
    host = support.seed(support.build(tmp_path, monkeypatch, nightly=True))
    host.runtime_port = SimpleNamespace(background=host.background, recovery_quiescence=host.quiescence, database=host.database)
    host.service = host.maintenance.bind_runtime(host.runtime_port)
    yield host
    host.service.stop(timeout=10)
    host.quiescence.close()


def nightly(host):
    """A queued ``maintenance.nightly`` job exactly as the scheduler prepares it (installation scope)."""
    with host.database.transaction() as uow:
        values = host.runtime.programs.job_values(host.human(None), "maintenance.nightly", {}, uow)
        return uow.repository("jobs", business_id=None).insert(values)["id"]


def receipt(host, job):
    return host.maintenance.read_receipt(job)


def state(host, job):
    return host.job(job)["state"]


def generation_exists(host, job):
    return (host.maintenance.backups.destination / job).exists()


def work(host):
    return (host.runner.calls[:], host.count("context_inputs"), host.count("context_input_attempts"), host.count("context_completions"))


def test_run_records_a_bounded_request_and_yields_without_a_generation_or_a_result(host):
    job, before = nightly(host), work(host)
    assert host.run_job(job) == {"job_id": job, "state": "queued"}
    saved = receipt(host, job)
    capture = saved["capture"]
    request = capture["request"]
    assert capture["state"] == "requested" and capture["attempts"] == 0 and "result" not in saved
    assert request["job_id"] == job and request["kind"] == "maintenance.nightly" and request["arguments"] == {}
    assert request["deadline_at"] > request["requested_at"] == host.wall.now
    row = host.job(job)
    assert row["state"] == "queued" and row["next_retry_at"] and not (row["result_json"] or {}).get("data")
    assert not generation_exists(host, job) and host.count("backup_runs") == 0
    assert not host.quiescence.entries and work(host) == before
    # Polling by the job neither succeeds nor resets anything: the same request, the same deadline.
    host.wall.advance(PENDING_SECONDS)
    assert host.run_job(job) == {"job_id": job, "state": "queued"} == host.run_job(job)
    assert receipt(host, job)["capture"] == capture
    assert not generation_exists(host, job) and state(host, job) == "queued"


def test_the_coordinator_captures_under_quiescence_and_the_job_then_returns_its_one_final_result(host):
    job = nightly(host)
    host.run_job(job)
    assert host.service.poll_once() is True
    saved = receipt(host, job)["capture"]
    assert saved["state"] == "complete" and saved["attempts"] == 1
    proof = host.quiescence.proofs[0]
    assert len(host.quiescence.entries) == 1 and type(proof) is RecoveryBarrierProof and host.quiescence.checks(proof) >= 6
    with pytest.raises(DomainError) as ended:                             # the real proof was revoked when the window exited
        proof.verify_active()
    assert ended.value.code == "recovery_not_quiescent" and proof.thread_id == threading.get_ident()
    assert host.quiescence.journal == [("drain", "external"), ("freeze", "hosts"), ("freeze", "containers"),
                                       ("thaw", "containers"), ("thaw", "hosts"), ("resume", "external")]
    metadata = host.maintenance.backups.verify(job)
    assert saved["result"] == {"checksum": metadata["database"]["checksum"], "context": metadata["context"]["manifest_sha256"]}
    assert state(host, job) == "queued"                                   # the capture alone completes nothing
    outcome = host.run_job(job)
    assert outcome == {"job_id": job, "state": "succeeded"}
    data = host.job(job)["result_json"]["data"]
    assert data["generation"] == job and data["checksum"] == metadata["database"]["checksum"] and data["versioning"] == {"enabled": False}
    assert receipt(host, job)["result"] == data
    # Exactly one sealed generation, one recorded run, and nothing left to capture or run.
    assert host.count("backup_runs") == 1 and host.count("backup_runs", "state='complete'") == 1
    assert host.service.poll_once() is False and host.run_job(job) is None
    assert len(host.quiescence.entries) == 1 and host.maintenance.backups.verify(job) == metadata


def test_the_capture_runs_on_the_coordinators_own_thread_and_never_on_the_background_worker(host):
    job = nightly(host)
    host.run_job(job)
    names = []
    host.quiescence.on_enter = lambda proof: names.append((threading.current_thread().name, proof.thread_id == threading.get_ident()))
    host.service.interval = 0.05
    assert host.service.start() is host.service and host.service.running
    deadline = time.monotonic() + 60
    while receipt(host, job)["capture"]["state"] != "complete" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert receipt(host, job)["capture"]["state"] == "complete"
    assert names == [("maintenance-capture", True)]                        # the real proof is bound to the coordinator's thread
    assert host.service.stop(timeout=10) == {"stopped": True, "in_flight": False} and not host.service.running
    assert host.run_job(job) == {"job_id": job, "state": "succeeded"}


def test_a_service_stopped_and_started_again_keeps_working_and_stop_reports_its_state(host):
    assert host.service.stop() == {"stopped": True, "in_flight": False}      # never started
    host.service.interval = 0.05
    for _ in range(2):                                                         # no permanent disable, a fresh start each time
        host.service.start()
        host.service.start()                                                   # idempotent while running
        assert host.service.running
        assert host.service.stop(timeout=10) == {"stopped": True, "in_flight": False}
        assert not host.service.running
    job = nightly(host)
    host.run_job(job)
    host.service.start()
    host.service.wake()
    deadline = time.monotonic() + 60
    while receipt(host, job)["capture"]["state"] != "complete" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert host.service.stop(timeout=10)["stopped"] and receipt(host, job)["capture"]["state"] == "complete"


def test_a_drain_that_asks_the_coordinator_to_stop_from_its_own_thread_cannot_join_itself_or_abort_the_capture(host):
    job = nightly(host)
    host.run_job(job)
    results = []
    host.quiescence.on_enter = lambda proof: results.append(host.service.stop(timeout=1))
    host.service.interval = 0.05
    host.service.start()
    deadline = time.monotonic() + 60
    while receipt(host, job)["capture"]["state"] != "complete" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert results == [{"stopped": False, "in_flight": True}]
    assert receipt(host, job)["capture"]["state"] == "complete" and generation_exists(host, job)
    assert host.service.stop(timeout=10)["stopped"]


def test_binding_is_inert_idempotent_and_single_runtime(tmp_path, monkeypatch):
    fresh = support.build(tmp_path, monkeypatch, nightly=True)
    maintenance = fresh.maintenance
    directory = fresh.paths.runtime / "maintenance"
    assert not directory.exists()
    threads = threading.active_count()
    service = maintenance.bind_runtime(fresh.runtime)
    assert service is maintenance.maintenance_capture is maintenance.bind_runtime(fresh.runtime)
    assert service.runtime is fresh.runtime and not service.running and threading.active_count() == threads
    assert not directory.exists() and not fresh.count("backup_runs")
    with pytest.raises(ValueError):
        maintenance.bind_runtime(SimpleNamespace())


@pytest.mark.parametrize("change, code", [
    ("cancel_requested", "cancelled"),
    ("configuration", "configuration_changed"),
    ("definition", "policy_denied"),
    ("principal", "policy_denied"),
    ("installation", "policy_denied"),
    ("tampered", "invalid_plan"),
    ("other_job", "invalid_plan"),
    ("terminal", "policy_denied"),
])
def test_a_request_is_validated_against_the_persisted_job_authority_and_configuration_before_any_quiescence(host, change, code):
    job = nightly(host)
    host.run_job(job)
    saved = receipt(host, job)
    request = saved["capture"]["request"]
    if change == "cancel_requested":
        with host.database.transaction() as uow:
            repo = uow.repository("jobs", business_id=None)
            row = repo.get(job)
            repo.update(job, {"state": "cancel_requested"}, expected_revision=row["revision"])
    elif change == "configuration":
        saved["configuration"]["git_enabled"] = True
    elif change == "definition":
        request["definition_digest"] = "0" * 64
    elif change == "principal":
        request["principal_id"] = host.owner
    elif change == "installation":
        request["installation"] = "00000000-0000-4000-8000-000000000000"
    elif change == "tampered":
        request["deadline_at"] = "never"
    elif change == "other_job":
        request["job_id"] = "00000000-0000-4000-8000-000000000001"
    elif change == "terminal":
        with host.database.transaction() as uow:
            repo = uow.repository("jobs", business_id=None)
            row = repo.get(job)
            repo.update(job, {"state": "failed"}, expected_revision=row["revision"])
    host.maintenance.write_receipt(job, saved)
    assert host.service.poll_once() is True
    failed = receipt(host, job)["capture"]
    assert failed["state"] == "failed" and failed["error"]["code"] == code, failed
    assert not host.quiescence.entries and not generation_exists(host, job) and host.count("backup_runs") == 0
    assert host.service.poll_once() is False                              # a resolved request is never retried or duplicated
    host.run_job(job)                                                      # the job ends with the saved failure, never a result
    assert state(host, job) == ("cancelled" if change == "cancel_requested" else "failed") and not generation_exists(host, job)
    assert "result" not in receipt(host, job)


def test_the_exact_failure_is_saved_and_the_job_fails_with_it_without_a_second_capture(host):
    job = nightly(host)
    host.run_job(job)
    host.quiescence.fail = DomainError("recovery_not_quiescent", "A writer did not drain before recovery", retryable=True)
    assert host.service.poll_once() is True
    failed = receipt(host, job)["capture"]
    assert failed["state"] == "failed" and failed["attempts"] == 1
    assert failed["error"] == {"code": "recovery_not_quiescent", "message": "A writer did not drain before recovery", "retryable": True}
    host.quiescence.fail = None
    assert host.service.poll_once() is False and not host.quiescence.entries     # not retried behind the job's back
    host.run_job(job)
    row = host.job(job)
    assert row["state"] == "failed" and row["error_json"]["code"] == "recovery_not_quiescent" and row["error_json"]["retryable"]
    assert not generation_exists(host, job) and "result" not in receipt(host, job)


def test_a_proof_lost_during_capture_is_the_exact_failure_and_nothing_is_sealed(host, monkeypatch):
    job = nightly(host)
    host.run_job(job)

    original = storage_recovery.build_exports

    def release(*args, **kwargs):
        host.quiescence.admission.open()                                    # an outsider reopens write admission mid-window
        return original(*args, **kwargs)

    monkeypatch.setattr(storage_recovery, "build_exports", release)
    assert host.service.poll_once() is True
    failed = receipt(host, job)["capture"]
    assert failed["state"] == "failed" and failed["error"]["code"] == "recovery_not_quiescent"
    assert not generation_exists(host, job) and host.count("backup_runs", "state='failed'") == 1


def test_stopping_the_service_mid_capture_is_not_a_failure_and_the_next_start_completes_the_same_request(host):
    job = nightly(host)
    host.run_job(job)
    run = maintenance_capture._Run()
    host.quiescence.on_enter = lambda proof: run.stop.set()
    assert host.service.poll_once(run) is False
    paused = receipt(host, job)["capture"]
    assert paused["state"] == "requested" and paused["attempts"] == 1 and "error" not in paused
    assert not generation_exists(host, job)
    host.quiescence.on_enter = None
    assert host.service.poll_once() is True
    done = receipt(host, job)["capture"]
    assert done["state"] == "complete" and done["attempts"] == 2 and done["request"] == paused["request"]
    assert host.count("backup_runs", "state='complete'") == 1
    assert host.run_job(job)["state"] == "succeeded"


def test_cancelling_the_job_during_capture_stops_it_without_a_generation(host):
    job = nightly(host)
    host.run_job(job)

    def cancel(proof):
        with host.database.transaction() as uow:
            repo = uow.repository("jobs", business_id=None)
            row = repo.get(job)
            repo.update(job, {"state": "cancel_requested"}, expected_revision=row["revision"])

    host.quiescence.on_enter = cancel
    assert host.service.poll_once() is True
    assert receipt(host, job)["capture"]["error"]["code"] == "cancelled" and not generation_exists(host, job)
    host.run_job(job)
    assert state(host, job) == "cancelled"


def test_a_generation_sealed_before_its_result_was_recorded_is_verified_not_captured_again(host):
    job = nightly(host)
    host.run_job(job)
    metadata = support.create(host, job)                                    # the capture sealed, then the process was lost
    assert receipt(host, job)["capture"]["state"] == "requested"
    entered = len(host.quiescence.entries)
    assert host.service.poll_once() is True
    assert len(host.quiescence.entries) == entered
    assert receipt(host, job)["capture"]["state"] == "complete"
    assert host.run_job(job) == {"job_id": job, "state": "succeeded"}
    assert host.job(job)["result_json"]["data"]["checksum"] == metadata["database"]["checksum"]
    assert host.count("backup_runs") == 1


def test_a_corrupt_sealed_generation_never_continues_to_exports_or_a_result(host):
    job = nightly(host)
    host.run_job(job)
    host.service.poll_once()
    (host.maintenance.backups.destination / job / "context/git/objects.pack").write_bytes(b"corrupt")
    host.run_job(job)
    row = host.job(job)
    assert row["state"] == "failed" and row["error_json"]["code"] == "invalid_backup" and "result" not in receipt(host, job)


def test_a_job_whose_service_never_runs_expires_at_its_one_fixed_deadline_and_never_succeeds(host):
    job = nightly(host)
    host.run_job(job)
    deadline = receipt(host, job)["capture"]["request"]["deadline_at"]
    for _ in range(3):
        host.wall.advance(60)
        assert host.run_job(job)["state"] == "queued"
        assert receipt(host, job)["capture"]["request"]["deadline_at"] == deadline
    host.wall.now = deadline
    host.run_job(job)
    row = host.job(job)
    assert row["state"] == "failed" and row["error_json"]["code"] == "timeout"
    assert receipt(host, job)["capture"]["state"] == "failed" and not generation_exists(host, job)
    assert host.service.poll_once() is False and not host.quiescence.entries


def test_a_request_is_left_alone_while_the_job_still_holds_its_claim(host):
    job = nightly(host)
    host.run_job(job)
    with application_claim(host.maintenance.locks, job):
        assert host.service.poll_once() is False
    assert not host.quiescence.entries and receipt(host, job)["capture"]["state"] == "requested"
    assert host.service.poll_once() is True


def test_a_receipt_that_is_not_a_request_or_belongs_to_another_job_is_ignored_by_the_coordinator(host):
    job = nightly(host)
    host.run_job(job)
    directory = host.maintenance.receipt_directory()
    (directory / "not-a-receipt.json").write_text("{}")
    (directory / "00000000-0000-4000-8000-000000000009.json").write_text("not json")
    assert host.service.poll_once() is True                                 # the real request is still served
    assert receipt(host, job)["capture"]["state"] == "complete"
    assert sorted(path.name for path in directory.iterdir()) == sorted(
        ["not-a-receipt.json", "00000000-0000-4000-8000-000000000009.json", job + ".json"])
