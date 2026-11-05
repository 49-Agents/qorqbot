import json

from bazarlyk.application import Application
from bazarlyk.application.catalog import builtin_registry
from bazarlyk.domains.learnings import LearningOperations
from bazarlyk.storage import Database
from bazarlyk.storage.exports import build_exports
from foundation_support import BUSINESS, HUMAN, FixedPolicy, context, database_at


def test_upgrade_preserves_accepted_learning_and_exports_new_work_history(tmp_path, monkeypatch):
    migrations = Database.migrations()
    with monkeypatch.context() as legacy:
        legacy.setattr(Database, "migrations", staticmethod(lambda: migrations[:8]))
        db = database_at(tmp_path)
        with db.transaction() as uow:
            original = uow.repository("learning_candidates", business_id=BUSINESS).insert({"id": "historical",
                "candidate_json": {"text": "Existing rule", "bucket": "delta", "tag": "rule", "scope": "global",
                                   "evidence": {"owner_quote": "Keep this evidence"}}})
            decision = uow.repository("learning_decisions", business_id=BUSINESS).insert({"candidate_id": "historical",
                "state": "accepted", "feedback": "Existing correction", "decided_by": HUMAN})

    db.initialize()
    with db.read() as uow:
        upgraded = uow.repository("learning_candidates", business_id=BUSINESS).get("historical")
        assert all(upgraded[key] == value for key, value in original.items())
        assert upgraded["work_state"] is None and upgraded["agent_note"] == "" and upgraded["filing_json"] == {}
        assert uow.repository("learning_decisions", business_id=BUSINESS).get("historical") == decision
        assert uow.repository("learning_history", business_id=BUSINESS).list() == []
        assert uow.repository("jobs", business_id=BUSINESS).list() == []

    app = Application(db, builtin_registry(), policy=FixedPolicy())
    LearningOperations(app, None).register()
    result = app.invoke(context(), "learnings_work_start", {"learning_id": "historical", "agent_note": "Owner selected this"},
        {"expected_revision": 1, "idempotency_key": "upgrade-work"})
    assert result.status == "ok", result.to_dict()
    saved = tmp_path / "snapshot.sqlite3"
    db.backup(saved)
    bundle = build_exports(saved, tmp_path)
    candidates = json.loads(bundle.files["first/hq/exports/learning_candidates.json"])["records"]
    history = json.loads(bundle.files["first/hq/exports/learning_history.json"])["records"]
    assert candidates[0]["work_state"] == "in-progress"
    assert candidates[0]["agent_note"] == "Owner selected this"
    assert len(history) == 1 and history[0]["before_json"]["state"] == "accepted"
    assert history[0]["after_json"]["decision"]["note"] == "Existing correction"
    assert not any("context-search.sqlite3" in name for name in bundle.files)
