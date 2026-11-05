"""Migration 075 (dashboard read indexes): indexes only, no data or table change."""

from bazarlyk.storage import Database
from foundation_support import BUSINESS, HUMAN, database_at


def legacy_database(path, monkeypatch):
    migrations = Database.migrations
    with monkeypatch.context() as patch:
        patch.setattr(Database, "migrations", staticmethod(lambda: migrations()[:74]))
        db = database_at(path)
        with db.transaction() as uow:
            uow.repository("routines", business_id=BUSINESS).insert({"namespace": BUSINESS, "id": "filer", "name": "Filer",
                "scope": "business", "target": "business", "schedule": "*/5 * * * *", "timezone": "UTC",
                "status": "active", "job_kind": "context.file", "arguments_json": {}, "timeout_seconds": 600})
            uow.repository("routine_runs", business_id=BUSINESS).insert({"id": "existing-run", "namespace": BUSINESS,
                "routine_id": "filer", "trigger": "schedule", "state": "succeeded",
                "started_at": "2026-09-01T00:00:00Z", "finished_at": "2026-09-01T00:00:05Z",
                "result_json": {}, "cost_json": {}})
            uow.repository("jobs", business_id=BUSINESS).insert({"id": "existing-job", "target_namespace": BUSINESS,
                "kind": "context.file", "principal_id": HUMAN, "arguments_json": {"upload_id": "upload-1"},
                "dedupe_key": "upload-1", "state": "succeeded"})
    return db


def test_upgrade_adds_indexes_only_and_preserves_existing_rows(tmp_path, monkeypatch):
    old = legacy_database(tmp_path, monkeypatch)
    with old.read() as uow:
        before_run = uow.repository("routine_runs", business_id=BUSINESS).get("existing-run")
        before_job = uow.repository("jobs", business_id=BUSINESS).get("existing-job")
        before_tables = {row[0] for row in uow.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        # Confirm the "before" side really lacks the index, so the "after" SEARCH
        # assertions below prove something rather than reflecting a plan SQLite
        # would have chosen anyway.
        before_plan = " ".join(row[3] for row in uow.connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM routine_runs WHERE business_id IS ?", (BUSINESS,)))
        assert "SCAN" in before_plan

    current = Database(old.path).initialize()
    assert current.columns("routine_runs") == old.columns("routine_runs")
    assert current.columns("jobs") == old.columns("jobs")

    with current.read() as uow:
        assert uow.repository("routine_runs", business_id=BUSINESS).get("existing-run") == before_run
        assert uow.repository("jobs", business_id=BUSINESS).get("existing-job") == before_job
        after_tables = {row[0] for row in uow.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert after_tables == before_tables  # indexes only: no table added or removed

        indexes = {row[0] for row in uow.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name IN ('routine_runs','jobs')")}
        assert {"routine_runs_business_started", "routine_runs_namespace_started", "routine_runs_state_started",
                "jobs_business_kind_upload", "jobs_business_kind_created"} <= indexes

        plan = " ".join(row[3] for row in uow.connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM routine_runs WHERE business_id IS ?", (BUSINESS,)))
        assert "SEARCH" in plan and "SCAN" not in plan

        plan = " ".join(row[3] for row in uow.connection.execute(
            "EXPLAIN QUERY PLAN SELECT id,state,error_json,attempts,updated_at FROM jobs WHERE business_id=? "
            "AND kind='context.file' AND json_extract(arguments_json,'$.upload_id')=? "
            "ORDER BY CAST(json_extract(arguments_json,'$.expected_revision') AS INTEGER) DESC, created_at DESC, rowid DESC LIMIT 1",
            (BUSINESS, "upload-1")))
        assert "SEARCH" in plan and "SCAN" not in plan

        assert uow.connection.execute("PRAGMA user_version").fetchone()[0] == len(Database.migrations())
        assert uow.connection.execute("PRAGMA foreign_key_check").fetchone() is None
