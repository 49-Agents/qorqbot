from types import SimpleNamespace
from uuid import uuid4

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.sandbox.lifecycle import EnvironmentResult, SandboxLifecycle
from bazarlyk.sandbox.worker import SandboxAdministration, SandboxWorker
from bazarlyk.sandbox.state import SandboxStateStore
from foundation_support import BUSINESS, database_at
from test_sandbox_lifecycle import CONFIG


@pytest.fixture(autouse=True)
def fixed_disk_capacity(monkeypatch):
    """Worker state tests use a fixed host capacity, independent of tmpfs size."""
    capacity = SimpleNamespace(f_frsize=1, f_bsize=1, f_bavail=64 * 1024 ** 3)
    monkeypatch.setattr("bazarlyk.sandbox.disk.os.statvfs", lambda _: capacity)


def fixture(tmp_path):
    database = database_at(tmp_path)
    (tmp_path / "first/hq").mkdir(parents=True)
    calls, fail_stop = [], [False]

    class Driver:
        def reconcile(self, business, paths, config, generation, *, heartbeat):
            calls.append(("run", generation))
            heartbeat()
            return EnvironmentResult("fixture-" + str(generation))

        def stop(self, business, paths, *, heartbeat):
            calls.append(("stop", None))
            heartbeat()
            if fail_stop[0]:
                raise DomainError("environment_unavailable", "private diagnostic must not be persisted")

    lifecycle = SandboxLifecycle(database, tmp_path, Driver(), config_loader=lambda _: CONFIG,
                                 adoption_inventory=lambda *_args, **_kwargs: None)
    return lifecycle, calls, fail_stop


def test_interrupted_operator_stop_retries_stop_and_never_restarts_writer(tmp_path):
    lifecycle, calls, fail_stop = fixture(tmp_path)
    admin = SandboxAdministration(lifecycle)
    first = admin.ensure(BUSINESS)
    requested = admin.request(BUSINESS, "stop", expected_revision=first["revision"])
    assert requested["state"] == "pending" and requested["desired_state"] == "stopped"
    # Operator disappears after committing intent. A restarted worker owns recovery.
    fail_stop[0] = True
    worker = SandboxWorker(lifecycle, require_authority=lambda: None)
    failed = worker.run_once()[0]
    assert failed["state"] == "failed" and failed["desired_state"] == "stopped"
    fail_stop[0] = False
    stopped = SandboxWorker(lifecycle, require_authority=lambda: None).run_once()[0]
    assert stopped["state"] == "stopped" and calls == [("run", 1), ("stop", None), ("stop", None)]
    assert stopped["environment_id"] == first["environment_id"]
    admin.request(BUSINESS, "start", expected_revision=stopped["revision"])
    assert worker.run_once()[0]["state"] == "ready"
    assert calls[-1] == ("run", 1)


def test_stop_unprovisioned_business_does_not_require_images_or_proxy(tmp_path):
    lifecycle, calls, _ = fixture(tmp_path)
    lifecycle.config_loader = lambda _: None
    pending = lifecycle.ensure_business_environment(BUSINESS)
    admin = SandboxAdministration(lifecycle)
    admin.request(BUSINESS, "stop", expected_revision=pending["revision"])
    stopped = lifecycle.ensure_business_environment(BUSINESS)
    assert stopped["state"] == "stopped" and stopped["environment_id"] is None
    assert calls == [("stop", None)]


def test_worker_uses_only_committed_intents_and_obeys_maintenance(tmp_path):
    lifecycle, calls, _ = fixture(tmp_path)
    clock = [0]
    worker = SandboxWorker(lifecycle, require_authority=lambda: None, clock=lambda: clock[0])
    assert worker.run_once() == [] and calls == []
    with lifecycle.database.transaction() as uow:
        lifecycle.state.insert_intent(uow, BUSINESS)
    assert worker.run_once()[0]["state"] == "ready"
    assert worker.run_once() == []
    clock[0] = 300
    assert worker.run_once()[0]["state"] == "ready"
    with lifecycle.database.transaction() as uow:
        row = uow.repository("maintenance_state").get("service")
        if row:
            uow.repository("maintenance_state").update("service", {"state": "closed"}, expected_revision=row["revision"])
        else:
            uow.repository("maintenance_state").insert({"id": "service", "state": "closed"})
    with pytest.raises(DomainError, match="maintenance"):
        worker.run_once()
    assert len(calls) == 2


def test_reconciliation_is_not_truncated_to_the_default_presentation_page(tmp_path):
    database = database_at(tmp_path)
    state, identities, called = SandboxStateStore(), [str(uuid4()) for _ in range(51)], []
    with database.transaction() as uow:
        for index, identity in enumerate(identities):
            uow.repository("businesses").insert({"id": identity, "slug": "fixture-" + str(index),
                "name": "Fixture", "workspace": str(tmp_path / identity), "hq_path": str(tmp_path / identity / "hq")})
            state.insert_intent(uow, identity)

    class Lifecycle:
        def ensure_business_environment(self, identity):
            called.append(identity)
            with database.read() as uow:
                return state.get(uow, identity)

    lifecycle = Lifecycle()
    lifecycle.database = database
    worker = SandboxWorker(lifecycle, require_authority=lambda: None)
    assert len(worker.run_once()) == 51 and set(called) == set(identities)


def test_existing_business_config_read_reports_scoped_sanitized_environment(tmp_path):
    from dataclasses import replace
    from foundation_support import OTHER, context
    from test_domain_documents import setup_documents
    database, app, _ = setup_documents(tmp_path)
    store = SandboxStateStore()
    with database.transaction() as uow:
        store.insert_intent(uow, BUSINESS)
        lease = store.claim(uow, BUSINESS)
        store.mark_pending(uow, lease)
    result = app.invoke(context(), "business_config_get", {})
    assert result.status == "ok"
    assert dict(result.data["environment"]) == {"mode": "sandbox", "state": "pending",
        "desired_state": "running", "desired_generation": 1, "observed_generation": None,
        "error_code": "configuration_missing"}
    foreign = app.invoke(replace(context(), business_id=OTHER, allowed_business_ids=frozenset({OTHER})),
                         "business_config_get", {})
    assert dict(foreign.data["environment"]) == {"mode": "host"}
