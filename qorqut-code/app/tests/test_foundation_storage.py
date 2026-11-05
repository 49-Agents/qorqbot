from concurrent.futures import ThreadPoolExecutor
import gc
import hashlib
import os
import sqlite3
import weakref

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.storage import Database, TABLES
from foundation_support import AGENT, BUSINESS, HUMAN, OTHER, database_at


def test_all_named_repositories_and_sqlite_pragmas(tmp_path):
    database = database_at(tmp_path, real_initialization=True)
    with database.read() as uow:
        assert uow.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert uow.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert uow.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert uow.connection.execute("PRAGMA query_only").fetchone()[0] == 1
        for name in TABLES:
            assert database.columns(name)
        with pytest.raises(ValueError):
            uow.repository("arbitrary_table")
    assert database.path.stat().st_mode & 0o777 == 0o600


def test_pooled_connection_reused_and_query_only_toggles_before_reuse(tmp_path):
    database = database_at(tmp_path)
    with database.read() as uow:
        first_connection = uow.connection
        assert uow.connection.execute("PRAGMA query_only").fetchone()[0] == 1
    # Single-threaded and nothing else contending: the LIFO pool must hand
    # back the exact connection just released, not open a second one.
    with database.transaction() as uow:
        assert uow.connection is first_connection
        assert uow.connection.execute("PRAGMA query_only").fetchone()[0] == 0
        uow.repository("principals").update(HUMAN, {"display_name": "Reused"}, expected_revision=1)
    with database.read() as uow:
        assert uow.connection is first_connection
        assert uow.connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert uow.repository("principals").get(HUMAN)["display_name"] == "Reused"


def test_failed_unit_closes_its_connection_instead_of_pooling_it(tmp_path):
    database = database_at(tmp_path)
    with pytest.raises(RuntimeError):
        with database.transaction() as uow:
            broken = uow.connection
            raise RuntimeError("handler failed")
    assert broken not in database._pool
    with pytest.raises(sqlite3.ProgrammingError):
        broken.execute("SELECT 1")
    # A fresh connection replaces it; the database is otherwise unaffected.
    with database.read() as uow:
        assert uow.repository("principals").get(HUMAN)["display_name"] == "Owner"


def test_close_drains_the_pool(tmp_path):
    database = database_at(tmp_path)
    with database.read() as uow:
        pooled = uow.connection
    assert list(database._pool) == [pooled]
    database.close()
    assert list(database._pool) == []
    with pytest.raises(sqlite3.ProgrammingError):
        pooled.execute("SELECT 1")
    database.close()  # idempotent


def test_discarded_database_releases_its_pooled_connections(tmp_path):
    # SQLite holds registered functions in C, invisible to the cycle collector, so a
    # callback that strongly captured its unit kept the Database and its pool alive.
    database = database_at(tmp_path)
    path = str(database.path)
    with database.transaction() as uow:
        uow.actor_id = HUMAN
        assert uow.connection.execute("SELECT inbox_actor()").fetchone()[0] == HUMAN
    assert database._pool
    watcher = weakref.ref(database)
    del database, uow
    gc.collect()
    assert watcher() is None
    handles = [name for name in os.listdir("/proc/self/fd") if os.path.realpath(f"/proc/self/fd/{name}").startswith(path)]
    assert handles == []


def test_scoped_keys_and_cas_cannot_cross_business(tmp_path):
    database = database_at(tmp_path)
    with database.transaction(business_id=BUSINESS) as uow:
        tasks = uow.repository("tasks")
        task = tasks.insert({"id": "same", "title": "First", "status": "todo", "created_by": HUMAN})
        assert task["revision"] == 1
        with pytest.raises(DomainError, match="scope"):
            uow.repository("tasks", business_id=OTHER)
        with pytest.raises(DomainError):
            tasks.list(filters={"business_id": OTHER})
        assert tasks.update("same", {"title": "First"}, expected_revision=1)["revision"] == 1
        assert tasks.update("same", {"title": "Edited"}, expected_revision=1)["revision"] == 2
        with pytest.raises(DomainError) as conflict:
            tasks.update("same", {"title": "Stale"}, expected_revision=1)
        assert conflict.value.current_revision == 2
    with database.transaction(business_id=OTHER) as uow:
        assert uow.repository("tasks").get("same") is None
        uow.repository("tasks").insert({"id": "same", "title": "Independent", "status": "todo", "created_by": HUMAN})
    with database.read() as uow:
        with pytest.raises(DomainError, match="namespace"):
            uow.repository("tasks")


def test_prepare_is_sqlite_read_only_and_transaction_rolls_back(tmp_path):
    database = database_at(tmp_path)
    with pytest.raises(DomainError):
        with database.read() as uow:
            uow.connection.execute("UPDATE principals SET active=0")
    with pytest.raises(RuntimeError):
        with database.transaction() as uow:
            uow.repository("principals").update(HUMAN, {"active": False}, expected_revision=1)
            raise RuntimeError("handler failed")
    with database.read() as uow:
        assert uow.repository("principals").get(HUMAN)["active"] == 1


def test_cross_business_assignee_rejected_by_task_workflow(tmp_path):
    from bazarlyk.domains.tasks import TaskOperations
    from foundation_support import context

    database = database_at(tmp_path)
    with database.transaction() as uow:
        uow.repository("principals").insert({"id": AGENT, "kind": "agent"})
        uow.repository("business_memberships", business_id=OTHER).insert({"principal_id": AGENT})
    with pytest.raises(DomainError):
        with database.read() as uow:
            TaskOperations.assignee(context(), AGENT, uow)


def test_concurrent_revision_checks_admit_one_writer(tmp_path):
    database = database_at(tmp_path)
    def update(name):
        try:
            with database.transaction() as uow:
                uow.repository("principals").update(HUMAN, {"display_name": name}, expected_revision=1)
            return "ok"
        except DomainError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, ["One", "Two"])) == ["ok", "revision_conflict"]


def test_online_backup_restore_validates_and_never_overwrites(tmp_path):
    database = database_at(tmp_path, real_initialization=True)
    backup = tmp_path / "backups" / "nightly.sqlite3"
    metadata = database.backup(backup)
    assert metadata["record_counts"]["businesses"] == 2
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == metadata["checksum"]
    with database.transaction() as uow:
        uow.repository("principals").update(HUMAN, {"display_name": "Later"}, expected_revision=1)
    restored = Database.restore(backup, tmp_path / "restored.sqlite3", expected_checksum=metadata["checksum"])
    with restored.read() as uow:
        assert uow.repository("principals").get(HUMAN)["display_name"] == "Owner"
    with pytest.raises(DomainError, match="already exists"):
        database.backup(backup)
    with pytest.raises(DomainError, match="already exists"):
        Database.restore(backup, database.path)
    with pytest.raises(DomainError, match="checksum"):
        Database.restore(backup, tmp_path / "wrong.sqlite3", expected_checksum="0" * 64)
    assert not (tmp_path / "wrong.sqlite3").exists()


def test_cold_open_validates_without_initializing_or_changing_database(tmp_path, monkeypatch):
    original = database_at(tmp_path, real_initialization=True)
    before = original.path.read_bytes()
    monkeypatch.setattr(Database, "initialize", lambda *args: pytest.fail("Startup must not run migrations"))
    monkeypatch.setattr(Database, "validate_snapshot", lambda *args: pytest.fail("Startup must not scan operational records"))
    reopened = Database(original.path).open_existing()
    with reopened.read() as uow:
        assert uow.repository("principals").get(HUMAN)["display_name"] == "Owner"
    assert original.path.read_bytes() == before
    missing = tmp_path / "missing/state.sqlite3"
    with pytest.raises(DomainError):
        Database(missing).open_existing()
    assert not missing.parent.exists()


def test_cold_open_requires_current_unchanged_migrations(tmp_path):
    database = database_at(tmp_path, real_initialization=True)
    connection = sqlite3.connect(database.path)
    connection.execute("UPDATE schema_migrations SET checksum='tampered' WHERE version=1")
    connection.commit()
    connection.close()
    with pytest.raises(DomainError, match="migration history"):
        Database(database.path).open_existing()


@pytest.mark.parametrize("kind", ["unrelated", "future", "changed_migration", "missing_migration"])
def test_database_refuses_unknown_or_tampered_schema(tmp_path, kind):
    path = tmp_path / "state.sqlite3"
    if kind != "unrelated":
        Database(path).initialize()
    connection = sqlite3.connect(path)
    if kind == "unrelated":
        connection.execute("CREATE TABLE other(id INTEGER)")
    elif kind == "future":
        connection.execute("INSERT INTO schema_migrations VALUES(999,'future','now')")
    elif kind == "changed_migration":
        connection.execute("UPDATE schema_migrations SET checksum='wrong' WHERE version=1")
    else:
        connection.execute("DELETE FROM schema_migrations WHERE version=1")
    connection.commit()
    connection.close()
    with pytest.raises(DomainError):
        Database(path).initialize()
