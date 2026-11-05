import pytest

from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.storage import Database
from bazarlyk.storage.routine_cost_repair import preview, apply
from foundation_support import BUSINESS, HUMAN, database_at


def legacy_database(path, monkeypatch):
    migrations = Database.migrations
    with monkeypatch.context() as patch:
        patch.setattr(Database, "migrations", staticmethod(lambda: migrations()[:6]))
        db = database_at(path)
        with db.transaction() as uow:
            uow.repository("installation").insert({"id": "service", "installation_id": new_id(), "owner_principal_id": HUMAN})
            uow.repository("maintenance_state").insert({"id": "service", "state": "closed"})
            uow.repository("routines", business_id=None).insert({"namespace": "global", "id": "evolution-extraction", "name": "evolution-extraction",
                "scope": "global", "target": "each-business", "schedule": "0 0 * * *", "timezone": "America/New_York",
                "status": "active", "job_kind": "learnings.extract", "arguments_json": {}, "timeout_seconds": 7200, "skip_if_live": True})
            uow.repository("routine_slots").insert({"namespace": "global", "routine_id": "evolution-extraction", "target_namespace": BUSINESS,
                "schedule_revision": 1, "slot": "2026-09-08T04:00:00Z", "wall_slot": "2026-09-08T00:00", "state_json": {"done": True}})
            uow.repository("routine_runs", business_id=BUSINESS).insert({"id": "old-run", "namespace": "global", "routine_id": "evolution-extraction",
                "trigger": "schedule", "slot": "2026-09-08T04:00:00Z", "state": "succeeded", "started_at": "2026-09-08T04:00:22Z",
                "finished_at": "2026-09-08T04:00:44Z", "cost_json": {"usd": 0, "calls": None},
                "result_json": {"legacy": {"history": ["preserved"]}, "stdout_tail": "run learning-1\nextracted 1"}})
            uow.repository("learning_runs", business_id=BUSINESS).insert({"id": "learning-1", "result_json": {
                "start": "2026-09-08T04:00:22Z", "end": "2026-09-08T04:00:44Z", "usd": .00462121, "errors": [], "model": "test-model", "usage": {"out": 123}}})
    return db


def test_upgrade_keeps_slots_revisions_history_and_original_cost_evidence(tmp_path, monkeypatch):
    old = legacy_database(tmp_path, monkeypatch)
    plan = preview(old.path)
    assert len(plan["changes"]) == 1
    with old.read() as uow:
        slots = uow.repository("routine_slots").list()
        run = uow.repository("routine_runs", business_id=BUSINESS).get("old-run")
        routine = uow.repository("routines", business_id=None).get({"namespace": "global", "id": "evolution-extraction"})
    current = Database(old.path).initialize()
    assert "skip_if_live" not in current.columns("routines")
    with current.read() as uow:
        assert uow.repository("routine_slots").list() == slots
        assert uow.repository("routine_runs", business_id=BUSINESS).get("old-run") == run
        assert uow.repository("routines", business_id=None).get({"namespace": "global", "id": "evolution-extraction"}) == {k:v for k,v in routine.items() if k != "skip_if_live"}
    assert apply(current, plan) == {"repaired": 1}
    assert preview(old.path)["changes"] == []
    with current.read() as uow:
        repaired = uow.repository("routine_runs", business_id=BUSINESS).get("old-run")
        assert repaired["cost_json"]["usd"] == .00462121
        assert repaired["result_json"]["legacy"] == run["result_json"]["legacy"]
        assert repaired["result_json"]["cost_reconciliation"]["original_cost"] == run["cost_json"]
        assert uow.repository("routine_slots").list() == slots
    with pytest.raises(DomainError, match="sources changed"):
        apply(current, plan)


def test_repair_rejects_open_gate_and_tampered_preview(tmp_path, monkeypatch):
    db = Database(legacy_database(tmp_path, monkeypatch).path).initialize()
    plan = preview(db.path)
    with db.transaction() as uow:
        uow.repository("maintenance_state").update("service", {"state": "open"}, expected_revision=1)
    with pytest.raises(DomainError, match="Close maintenance"):
        apply(db, plan)
    with db.transaction() as uow:
        uow.repository("maintenance_state").update("service", {"state": "closed"}, expected_revision=2)
    plan["changes"][0]["after"]["usd"] = 99
    with pytest.raises(DomainError, match="sources changed"):
        apply(db, plan)
