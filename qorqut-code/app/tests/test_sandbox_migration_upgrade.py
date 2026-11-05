"""Regression coverage for upgrading pre-sandbox operational databases."""

import base64
import hashlib
import sqlite3

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.storage import Database
from bazarlyk.storage import history as shared_history


OWNER = "00000000-0000-4000-8000-000000000001"
AGENT = "00000000-0000-4000-8000-000000000002"
BUSINESS = "00000000-0000-4000-8000-000000000003"
DOCUMENT = "00000000-0000-4000-8000-000000000004"
MEMORY = b"Existing committed working memory.\n"
MEMORY_HASH = hashlib.sha256(MEMORY).hexdigest()


def _legacy_database(path, monkeypatch, version):
    """Create an actual historical schema with its original migration receipts."""
    migrations = Database.migrations()
    with monkeypatch.context() as legacy:
        legacy.setattr(Database, "migrations", staticmethod(lambda: migrations[:version]))
        database = Database(path).initialize()
        with database.transaction() as uow:
            uow.repository("principals").insert({"id": OWNER, "kind": "human", "display_name": "Existing owner"})
            uow.repository("principals").insert({"id": AGENT, "kind": "agent", "display_name": "Existing agent"})
            uow.repository("businesses").insert({
                "id": BUSINESS,
                "slug": "existing-business",
                "workspace": "/private/existing-business",
                "hq_path": "/private/existing-business/hq",
                "name": "Existing business",
            })
            uow.repository("business_memberships", business_id=BUSINESS).insert({"principal_id": OWNER, "is_owner": 1})
            uow.repository("agent_profiles", business_id=BUSINESS).insert({
                "agent_id": AGENT,
                "principal_id": AGENT,
                "home_business_id": BUSINESS,
                "namespace": BUSINESS,
                "name": "Existing agent",
                "workspace": "/private/existing-business/agents/existing-agent",
                "profile_json": {"role": "coder"},
            })
            uow.repository("documents", business_id=BUSINESS).insert({
                "id": DOCUMENT,
                "namespace": BUSINESS,
                "owning_agent_id": AGENT,
                "kind": "note",
                "path": "context/existing.md",
                "content_hash": "a" * 64,
            })
            if version >= 43:
                memory = uow.repository("documents", business_id=BUSINESS).insert({
                    "id": "00000000-0000-4000-8000-000000000005",
                    "namespace": BUSINESS,
                    "owning_agent_id": AGENT,
                    "kind": "working_memory",
                    "path": "context/agent-memory/existing-agent.md",
                    "content_hash": MEMORY_HASH,
                })
                uow.repository("working_memory_versions", business_id=BUSINESS).insert({
                    "document_id": memory["id"],
                    "version": 1,
                    "file_bytes": base64.b64encode(MEMORY).decode(),
                    "content_hash": MEMORY_HASH,
                    "source_refs_json": [],
                    "author_id": AGENT,
                })
    return database, migrations


@pytest.mark.parametrize("legacy_version", [42, 43, 50])
def test_explicit_upgrade_from_pre_sandbox_schema_preserves_authority_and_does_not_adopt(tmp_path, monkeypatch, legacy_version):
    path = tmp_path / f"state-{legacy_version}.sqlite3"
    database, migrations = _legacy_database(path, monkeypatch, legacy_version)

    connection = sqlite3.connect(path)
    before_history = connection.execute("SELECT version, checksum FROM schema_migrations ORDER BY version").fetchall()
    before_version = connection.execute("PRAGMA user_version").fetchone()[0]
    connection.close()
    assert before_version == legacy_version
    assert before_history == [(version, checksum) for version, checksum, _ in migrations[:legacy_version]]

    # Startup is deliberately non-migrating. An operator must explicitly run initialize().
    with pytest.raises(DomainError, match="explicit reviewed migration"):
        Database(path).open_existing()
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == legacy_version
    assert connection.execute("SELECT version, checksum FROM schema_migrations ORDER BY version").fetchall() == before_history
    connection.close()

    database.initialize()
    reopened = Database(path).open_existing()
    with reopened.read() as uow:
        assert uow.repository("principals").get(OWNER)["display_name"] == "Existing owner"
        assert uow.repository("businesses").get(BUSINESS)["workspace"] == "/private/existing-business"
        assert uow.repository("documents", business_id=BUSINESS).get(DOCUMENT)["path"] == "context/existing.md"
        assert uow.repository("business_sandboxes").get(BUSINESS) is None
        assert uow.repository("business_previews", business_id=BUSINESS).list() == []
        assert uow.connection.execute("SELECT COUNT(*) FROM business_sandboxes").fetchone()[0] == 0
        assert uow.connection.execute("PRAGMA foreign_key_check").fetchone() is None
        if legacy_version >= 43:
            version = uow.repository("working_memory_versions", business_id=BUSINESS).get(
                ("00000000-0000-4000-8000-000000000005", 1)
            )
            assert base64.b64decode(version["file_bytes"]) == MEMORY
            # Migration 100 imported the complete legacy chain into shared history.
            shared = shared_history.working_memory_version(uow.connection, BUSINESS, "00000000-0000-4000-8000-000000000005", 1)
            assert base64.b64decode(shared["file_bytes"]) == MEMORY and shared["author_id"] == AGENT

    connection = sqlite3.connect(path)
    history = connection.execute("SELECT version, checksum FROM schema_migrations ORDER BY version").fetchall()
    columns = {row[1] for row in connection.execute("PRAGMA table_info(business_sandboxes)")}
    assert connection.execute("PRAGMA user_version").fetchone()[0] == len(migrations)
    assert history == [(version, checksum) for version, checksum, _ in migrations]
    assert history[:legacy_version] == before_history
    assert {"disk_used_bytes", "disk_budget_bytes", "disk_reserved_bytes", "disk_available_bytes", "disk_host_floor_bytes", "disk_checked_at", "disk_error_code"} <= columns
    connection.close()


def test_session_identity_upgrade_preserves_legacy_impacts_and_fault_runs(tmp_path, monkeypatch):
    """Migration 072 keeps 071 incident history while adding session identity."""
    migrations = Database.migrations()
    path = tmp_path / "state-71.sqlite3"
    with monkeypatch.context() as patch:
        patch.setattr(Database, "migrations", staticmethod(lambda: migrations[:71]))
        database, _ = _legacy_database(path, monkeypatch, 71)

    ticket = "00000000-0000-4000-8000-000000000006"
    incident = "00000000-0000-4000-8000-000000000007"
    fingerprint = "f" * 64
    now = "2026-09-16T00:00:00+00:00"
    with database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO dev_tickets(id,title,details,kind,status,reporter_id,business_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (ticket, "Preserved incident", "fixture", "bug", "open", OWNER, BUSINESS, now, now),
        )
        uow.connection.execute(
            "INSERT INTO system_incidents(id,fingerprint,ticket_id,title,status,classification,first_seen,last_seen,"
            "occurrence_count,recovery_count,business_id,representative_observation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (incident, fingerprint, ticket, "Preserved incident", "open", "infrastructure_fault", now, now, 1, 0,
             BUSINESS, None),
        )
        uow.connection.execute(
            "INSERT INTO system_incident_impacts(incident_id,business_id,agent_id,task_id,operation,job_id,receipt_id,"
            "episode,recovered_at,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (incident, BUSINESS, AGENT, None, "metrics.sync", "job-legacy", "receipt-legacy", 2, None, now, now),
        )
        uow.connection.execute(
            "INSERT INTO system_incident_fault_runs(fingerprint,agent_key,business_id,operation,component,call_site,"
            "first_seen,last_seen,failures) VALUES(?,?,?,?,?,?,?,?,?)",
            (fingerprint, "agent-key", BUSINESS, "metrics.sync", "background", "run_one", now, now, 3),
        )

    database.initialize()
    with database.read() as uow:
        impact = uow.connection.execute(
            "SELECT business_id,agent_id,job_id,receipt_id,episode,session_id FROM system_incident_impacts"
        ).fetchone()
        fault = uow.connection.execute(
            "SELECT fingerprint,agent_key,business_id,failures,session_id FROM system_incident_fault_runs"
        ).fetchone()
        assert tuple(impact) == (BUSINESS, AGENT, "job-legacy", "receipt-legacy", 2, None)
        assert tuple(fault) == (fingerprint, "agent-key", BUSINESS, 3, "")
        assert uow.connection.execute("PRAGMA foreign_key_check").fetchone() is None
