from concurrent.futures import ThreadPoolExecutor
import gc
import hashlib
import importlib.util
import logging
import os
import sqlite3
import weakref

import pytest

from qorqut_operations import background, inbox_nudges, local_time
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage import AssemblyError, Database, StorageContribution, TableSpec
from qorqut_operations.storage.sqlfunctions import install_sql_functions
from operational_support import OperationalHost
from support import BUSINESS, HUMAN, OTHER, Demo, database_at


def test_target_initialization_pragmas_and_identity(tmp_path):
    database = database_at(tmp_path)
    target = database.assembly.target
    with database.read() as uow:
        assert uow.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert uow.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert uow.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert uow.connection.execute("PRAGMA query_only").fetchone()[0] == 1
        for name in database.assembly.tables:
            assert database.columns(name)
        with pytest.raises(ValueError):
            uow.repository("arbitrary_table")
        assert uow.connection.execute("SELECT name, version, digest FROM target_identity").fetchall()[0][:] == (
            target.name, target.version, target.digest)
        assert uow.connection.execute("PRAGMA user_version").fetchone()[0] == target.version
        # A new application ID: a deployed v1 database is never mistaken for a target store.
        assert uow.connection.execute("PRAGMA application_id").fetchone()[0] == 0x514F5053
        assert uow.connection.execute("SELECT value FROM change_sequences WHERE name='signoffs'").fetchone()[0] == 0
    assert database.path.stat().st_mode & 0o777 == 0o600


def test_construction_and_open_never_create_or_migrate(tmp_path):
    demo = Demo()
    path = tmp_path / "nested" / "state.sqlite3"
    database = Database(path, assembly=demo.assembly())
    assert not path.exists() and not path.parent.exists()
    with pytest.raises(DomainError, match="Initialize"):
        database.read().__enter__()
    with pytest.raises(DomainError, match="missing"):
        database.open_existing()
    assert not path.parent.exists()
    with pytest.raises(TypeError, match="frozen Assembly"):
        Database(path, assembly=demo.builder())
    with pytest.raises(ValueError, match="explicit local file"):
        Database(":memory:", assembly=demo.assembly())


def test_cold_open_validates_without_initializing_or_changing_database(tmp_path, monkeypatch):
    original = database_at(tmp_path)
    original.close()
    sqlite3.connect(original.path).close()
    before = original.path.read_bytes()
    monkeypatch.setattr(Database, "initialize_target", lambda *args: pytest.fail("Open must not initialize"))
    monkeypatch.setattr(Database, "validate_snapshot", lambda *args: pytest.fail("Open must not scan records"))
    reopened = Database(original.path, assembly=original.demo.assembly()).open_existing()
    with reopened.read() as uow:
        assert uow.repository("principals").get(HUMAN)["display_name"] == "Owner"
    assert original.path.read_bytes() == before


def test_restored_delete_journal_stays_read_only_until_runtime_enables_wal(tmp_path):
    original = database_at(tmp_path)
    original.close()
    with sqlite3.connect(original.path) as connection:
        assert connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
    before = original.path.read_bytes()
    database = Database(original.path, assembly=original.demo.assembly(), busy_timeout=0.1).open_existing()
    assert original.path.read_bytes() == before
    with database.read() as reader:
        assert reader.connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    database.enable_wal()
    with database.read() as reader:
        assert reader.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert reader.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert reader.repository("principals").get(HUMAN)["display_name"] == "Owner"
        # A writer must commit while this reader holds an actual snapshot. In
        # DELETE mode this fails with busy/locked instead of completing.
        with database.transaction() as writer:
            writer.connection.execute("UPDATE principals SET display_name = 'Updated' WHERE id = ?", (HUMAN,))
        assert reader.repository("principals").get(HUMAN)["display_name"] == "Owner"
    with database.read() as reader:
        assert reader.repository("principals").get(HUMAN)["display_name"] == "Updated"
    database.close()


def test_initialization_is_explicit_and_refuses_an_existing_store(tmp_path):
    database = database_at(tmp_path)
    with pytest.raises(DomainError, match="already has tables"):
        Database(database.path, assembly=database.demo.assembly()).initialize_target()
    foreign = tmp_path / "foreign.sqlite3"
    connection = sqlite3.connect(foreign)
    connection.execute("CREATE TABLE other(id INTEGER)")
    connection.commit()
    connection.close()
    with pytest.raises(DomainError, match="already has tables"):
        Database(foreign, assembly=Demo().assembly()).initialize_target()


@pytest.mark.parametrize("kind", ["other_version", "other_composition", "v1_like", "missing_identity", "tampered_identity",
                                  "missing_table", "symlink"])
def test_open_refuses_a_store_that_is_not_exactly_this_target(tmp_path, kind):
    database = database_at(tmp_path)
    database.close()
    path, assembly = database.path, Demo().assembly()
    connection = sqlite3.connect(path)
    if kind == "other_version":
        assembly = Demo().assembly(version=2)
    elif kind == "other_composition":
        assembly = Demo().builder().add(__import__("qorqut_operations.storage", fromlist=["x"]).StorageContribution(
            "extra", {"extra": __import__("qorqut_operations.storage", fromlist=["x"]).TableSpec(("id",))},
            schema=("CREATE TABLE extra(id TEXT PRIMARY KEY)",))).freeze()
    elif kind == "v1_like":
        connection.execute("PRAGMA application_id = 0x425A4C4B")
    elif kind == "missing_identity":
        connection.execute("DROP TABLE target_identity")
    elif kind == "tampered_identity":
        connection.execute("UPDATE target_identity SET digest = 'wrong'")
    elif kind == "missing_table":
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE demo_history")
    elif kind == "symlink":
        link = tmp_path / "link.sqlite3"
        link.symlink_to(path)
        path = link
    connection.commit()
    connection.close()
    with pytest.raises(DomainError):
        Database(path, assembly=assembly).open_existing()


def tampered_store(tmp_path, statements):
    """A closed, checkpointed store with exactly these statements applied to its real schema."""
    database = database_at(tmp_path)
    database.close()
    connection = sqlite3.connect(database.path)
    connection.execute("PRAGMA foreign_keys = OFF")
    for statement in statements:
        connection.execute(statement)
    connection.commit()
    connection.close()
    return database


def test_expected_inventory_is_compiled_from_the_composition_and_matches_a_fresh_store(tmp_path):
    database = database_at(tmp_path)
    compiled = database.compiled()
    kinds = {kind for kind, _ in compiled.objects}
    assert kinds == {"table", "index", "trigger"}
    assert {("table", "target_identity"), ("table", "change_sequences"), ("table", "demo_history"),
            ("index", "agent_profile_active_names"), ("trigger", "signoffs_immutable_payload")} <= set(compiled.objects)
    assert not any(name.startswith("sqlite_") for _, name in compiled.objects)
    assert compiled.columns["items"][:2] == ("business_id", "id")
    database.close()
    before = database.path.read_bytes()
    Database(database.path, assembly=database.demo.assembly()).open_existing()  # exactly the compiled inventory
    assert database.path.read_bytes() == before


@pytest.mark.parametrize("kind, statements, message", [
    ("missing_required_column", ["ALTER TABLE items DROP COLUMN revision"], "changed table items"),
    ("missing_immutable_trigger", ["DROP TRIGGER signoffs_immutable_payload"], "missing trigger signoffs_immutable_payload"),
    ("missing_index", ["DROP INDEX agent_profile_active_names"], "missing index agent_profile_active_names"),
    ("changed_index", ["DROP INDEX agent_profile_active_names",
                       "CREATE INDEX agent_profile_active_names ON agent_profiles(namespace, name)"],
     "changed index agent_profile_active_names"),
    ("unexpected_object", ["CREATE INDEX stray ON items(title)"], "unexpected index stray"),
    ("missing_engine_sequence_table", ["DROP TABLE change_sequences"], "missing table change_sequences"),
])
def test_open_refuses_a_store_whose_actual_schema_differs_and_changes_nothing(tmp_path, kind, statements, message):
    database = tampered_store(tmp_path, statements)
    before_bytes = database.path.read_bytes()
    for check in (lambda: Database(database.path, assembly=database.demo.assembly()).open_existing(),
                  lambda: Database(tmp_path / "other.sqlite3", assembly=database.demo.assembly()).validate_snapshot(database.path)):
        with pytest.raises(DomainError, match=message) as refused:
            check()
        assert refused.value.code in {"invalid_database", "invalid_backup"}
    # Validation neither repaired nor wrote anything: the database file is byte-identical (SQLite's own, empty WAL
    # index sidecars for a read-only WAL open are not content) and no other file was created.
    assert database.path.read_bytes() == before_bytes
    assert {path.name for path in tmp_path.iterdir()} <= {"state.sqlite3", "state.sqlite3-wal", "state.sqlite3-shm"}
    assert not (tmp_path / "state.sqlite3-wal").exists() or (tmp_path / "state.sqlite3-wal").stat().st_size == 0


# Everything the retained engine domains own, with the synthetic host/demo tables of the composition left out.
ENGINE_TABLES = frozenset({
    "action_businesses", "action_logs", "action_revisions", "agent_profiles", "apply_receipts", "business_memberships",
    "businesses", "change_sequences", "context_review_states", "context_reviews", "idempotency_receipts", "inbox_deliveries",
    "inbox_events", "inbox_nudge_attempts", "inbox_nudge_evidence", "inbox_nudge_needs", "inbox_subscriptions", "installation",
    "item_tags", "jobs", "maintenance_state", "principals", "relationships", "resource_claims", "routine_runs", "routine_slots",
    "routines", "signoff_attention", "signoff_notes", "signoff_notification_schedule", "signoff_notifications", "signoffs",
    "tags", "target_identity", "task_completions", "task_moves", "task_snooze_wakes", "tasks", "thread_follows", "thread_messages"})
HOST_OR_DEMO_TABLES = frozenset({"admitted_revisions", "business_configs", "demo_history", "documents", "inbox_changes", "items", "messages", "tickets",
                                 "topic_files", "topics"})


def test_the_retained_engine_composition_owns_only_general_tables_and_no_department_grouping(tmp_path):
    host = OperationalHost(tmp_path, extra=(inbox_nudges.storage_contribution(), background.storage_contribution()))
    with host.database.read() as uow:
        tables = {row[0] for row in uow.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'")}
        columns = {name: {row[1] for row in uow.connection.execute(f'PRAGMA table_info("{name}")')} for name in tables}
        assert uow.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    # Finance, metrics, subscriptions, campaign routing/attribution, reports, milestones, setup, standalone Story/Idea/Request
    # storage and the Paper and content-session products are not engine storage: this is the complete engine-owned set.
    assert tables - HOST_OR_DEMO_TABLES == ENGINE_TABLES
    assert not any("department" in column for names in columns.values() for column in names)
    assert importlib.util.find_spec("qorqut_operations.departments") is None


def test_a_store_from_the_previous_schema_with_department_grouping_is_refused_and_not_converted(tmp_path):
    database = tampered_store(tmp_path, ["ALTER TABLE routines ADD COLUMN departments_json TEXT NOT NULL DEFAULT '[]'"])
    before = database.path.read_bytes()
    with pytest.raises(DomainError, match="changed table routines") as refused:
        Database(database.path, assembly=database.demo.assembly()).open_existing()
    assert refused.value.code == "invalid_database" and database.path.read_bytes() == before


def test_schema_inventory_is_checked_only_when_initializing_or_validating(tmp_path):
    database = tampered_store(tmp_path, ["DROP TRIGGER signoffs_immutable_payload"])
    candidate = Database(database.path, assembly=database.demo.assembly())  # construction reads nothing and compiles nothing
    assert candidate._compiled is None
    with pytest.raises(DomainError, match="missing trigger"):
        candidate.open_existing()
    assert candidate._compiled is not None
    with pytest.raises(DomainError, match="already has tables"):  # never executes DDL against an existing target
        candidate.initialize_target()
    connection = sqlite3.connect(database.path)
    assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='signoffs_immutable_payload'").fetchone() is None
    connection.close()


def test_declared_columns_must_exist_in_the_compiled_schema_before_any_store_exists(tmp_path):
    from support import DEMO_SCHEMA
    path = tmp_path / "state.sqlite3"
    for table, spec in (("items", TableSpec(("business_id", "id"), "business_id", revision_column="version")),
                        ("items", TableSpec(("business_id", "id"), "tenant")),
                        ("items", TableSpec(("business_id", "ident"), "business_id"))):
        builder = Demo().builder().add(StorageContribution("more", {"extra_items": spec},
            schema=("CREATE TABLE extra_items(business_id TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL, "
                    "PRIMARY KEY (business_id, id))",)))
        with pytest.raises(AssemblyError, match="lacks declared columns"):
            Database(path, assembly=builder.freeze()).initialize_target()
    builder = Demo().builder().add(StorageContribution("more", {"missing": TableSpec(("id",))}))
    with pytest.raises(AssemblyError, match="does not create table missing"):
        Database(path, assembly=builder.freeze()).initialize_target()
    builder = Demo().builder().add(StorageContribution("more", schema=("CREATE TABLE change_sequences(id TEXT)",)))
    with pytest.raises(AssemblyError, match="does not compile"):  # the engine's own table cannot be redefined by DDL either
        Database(path, assembly=builder.freeze()).initialize_target()
    assert not path.exists() and DEMO_SCHEMA


def test_declared_immutable_columns_must_exist_in_the_compiled_schema(tmp_path):
    builder = Demo().builder().add(StorageContribution("more", {"extra_items": TableSpec(("id",), revision_column=None)},
        {"extra_items": frozenset({"id", "frozen_at"})}, schema=("CREATE TABLE extra_items(id TEXT PRIMARY KEY)",)))
    with pytest.raises(AssemblyError, match="lacks declared columns: frozen_at"):
        Database(tmp_path / "state.sqlite3", assembly=builder.freeze()).initialize_target()


def test_sql_helper_functions_are_installed_by_one_shared_helper_with_unchanged_semantics():
    connection = sqlite3.connect(":memory:")
    install_sql_functions(connection)
    for instant in ("2026-01-01T03:30:00Z", "2026-07-01T03:30:00+00:00", "not a time", None):
        assert connection.execute("SELECT san_juan_date(?), san_juan_hour(?)", (instant, instant)).fetchone() == (
            local_time.local_date(instant), local_time.local_hour(instant))
    assert connection.execute("SELECT inbox_actor()").fetchone() == (None,)
    assert connection.execute("SELECT san_juan_date('2026-01-01T03:30:00Z')").fetchone() == ("2025-12-31",)
    connection.close()


def test_store_with_an_expression_index_snapshots_validates_and_reopens_read_only(tmp_path):
    daily = StorageContribution("daily", schema=(
        "CREATE INDEX principals_by_day ON principals(san_juan_date(created_at), san_juan_hour(created_at))",))
    database = database_at(tmp_path, extra=(daily,))
    snapshot = tmp_path / "snapshots" / "daily.sqlite3"
    metadata = database.backup(snapshot)
    assert metadata["record_counts"]["principals"] == 1
    before = snapshot.read_bytes()
    assert database.validate_snapshot(snapshot)["record_counts"]["principals"] == 1  # integrity_check evaluates the index
    assert snapshot.read_bytes() == before  # read-only function setup does not change the source
    restored = database.restore(snapshot, tmp_path / "restored.sqlite3")
    database.close()
    reopened = Database(database.path, assembly=database.assembly).open_existing()
    with reopened.read() as uow:
        assert uow.connection.execute("SELECT san_juan_date(created_at) FROM principals").fetchone()[0] is not None
    restored.close()
    reopened.close()


def test_pooled_connection_reused_and_query_only_toggles_before_reuse(tmp_path):
    database = database_at(tmp_path)
    with database.read() as uow:
        first_connection = uow.connection
        assert uow.connection.execute("PRAGMA query_only").fetchone()[0] == 1
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
        items = uow.repository("items")
        item = items.insert({"id": "same", "title": "First", "status": "todo", "created_by": HUMAN})
        assert item["revision"] == 1
        with pytest.raises(DomainError, match="scope"):
            uow.repository("items", business_id=OTHER)
        with pytest.raises(DomainError):
            items.list(filters={"business_id": OTHER})
        assert items.update("same", {"title": "First"}, expected_revision=1)["revision"] == 1
        assert items.update("same", {"title": "Edited"}, expected_revision=1)["revision"] == 2
        with pytest.raises(DomainError) as conflict:
            items.update("same", {"title": "Stale"}, expected_revision=1)
        assert conflict.value.current_revision == 2
        with pytest.raises(DomainError, match="expected_revision"):
            items.update("same", {"title": "No pin"})
    with database.transaction(business_id=OTHER) as uow:
        assert uow.repository("items").get("same") is None
        uow.repository("items").insert({"id": "same", "title": "Independent", "status": "todo", "created_by": HUMAN})
    with database.read() as uow:
        with pytest.raises(DomainError, match="namespace"):
            uow.repository("items")


def test_prepare_is_sqlite_read_only_and_transaction_rolls_back(tmp_path):
    database = database_at(tmp_path)
    with pytest.raises(DomainError):
        with database.read() as uow:
            uow.connection.execute("UPDATE principals SET active=0")
    with pytest.raises(DomainError, match="Preparation"):
        with database.read() as uow:
            uow.repository("principals").update(HUMAN, {"active": False}, expected_revision=1)
    with pytest.raises(RuntimeError):
        with database.transaction() as uow:
            uow.repository("principals").update(HUMAN, {"active": False}, expected_revision=1)
            raise RuntimeError("handler failed")
    with database.read() as uow:
        assert uow.repository("principals").get(HUMAN)["active"] == 1


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


def test_engine_immutability_triggers_and_append_only_receipts(tmp_path):
    database = database_at(tmp_path)
    with database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO apply_receipts(id, principal_id, target_namespace, operation_name, payload_hash, change_set,"
            "state, created_at, updated_at) VALUES ('r1', ?, 'global', 'op', ?, x'7b7d', 'prepared', 'now', 'now')",
            (HUMAN, "a" * 64))
    with pytest.raises(DomainError):
        with database.transaction() as uow:
            uow.connection.execute("UPDATE apply_receipts SET change_set=x'00' WHERE id='r1'")
    with pytest.raises(DomainError, match="Immutable"):
        with database.transaction() as uow:
            uow.repository("apply_receipts").update("r1", {"payload_hash": "0" * 64}, expected_revision=1)
    with database.transaction() as uow:
        uow.repository("idempotency_receipts").insert({"principal_id": HUMAN, "target_namespace": "global",
            "operation_name": "op", "idempotency_key": "k", "input_hash": "h", "result_json": {"status": "ok"}})
    with pytest.raises(DomainError):
        with database.transaction() as uow:
            uow.connection.execute("UPDATE idempotency_receipts SET input_hash='x'")
    with pytest.raises(DomainError, match="append-only"):
        with database.transaction() as uow:
            uow.repository("idempotency_receipts").delete(
                {"principal_id": HUMAN, "target_namespace": "global", "operation_name": "op", "idempotency_key": "k"})


def test_online_backup_restore_validates_and_never_overwrites(tmp_path):
    database = database_at(tmp_path)
    backup = tmp_path / "backups" / "nightly.sqlite3"
    metadata = database.backup(backup)
    assert metadata["record_counts"]["businesses"] == 2 and metadata["schema_version"] == 1
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == metadata["checksum"]
    with database.transaction() as uow:
        uow.repository("principals").update(HUMAN, {"display_name": "Later"}, expected_revision=1)
    restored = database.restore(backup, tmp_path / "restored.sqlite3", expected_checksum=metadata["checksum"])
    with restored.read() as uow:
        assert uow.repository("principals").get(HUMAN)["display_name"] == "Owner"
    with pytest.raises(DomainError, match="already exists"):
        database.backup(backup)
    with pytest.raises(DomainError, match="already exists"):
        database.restore(backup, database.path)
    with pytest.raises(DomainError, match="checksum"):
        database.restore(backup, tmp_path / "wrong.sqlite3", expected_checksum="0" * 64)
    assert not (tmp_path / "wrong.sqlite3").exists()
    # A snapshot of another composition is not restorable here.
    other = Database(tmp_path / "other.sqlite3", assembly=Demo().assembly(version=2))
    with pytest.raises(DomainError):
        other.restore(backup, tmp_path / "foreign.sqlite3")
    assert not (tmp_path / "foreign.sqlite3").exists()


def test_same_transaction_finalizers_roll_back_with_the_unit_and_publisher_runs_after_commit(tmp_path, caplog):
    database = database_at(tmp_path)
    demo = database.demo
    with database.transaction(business_id=BUSINESS, operation="demo.write") as uow:
        uow.repository("items").insert({"id": "a", "title": "A", "created_by": HUMAN})
    with database.read() as uow:
        # Finalizers share the writer's transaction (the seeding unit left one row); capture ran just before
        # COMMIT and the publisher only after it.
        assert [row[0] for row in uow.connection.execute("SELECT kind FROM demo_history")] == ["finalizer"] * 2
    assert demo.publisher.log == ["capture", ("publish", "demo.write", True)]
    demo.publisher.log.clear()
    with database.read():
        pass
    assert demo.publisher.log == []  # reads never publish
    # A unit that fails after its body publishes nothing and keeps no finalizer row.
    with pytest.raises(RuntimeError):
        with database.transaction(business_id=BUSINESS) as uow:
            uow.repository("items").insert({"id": "b", "title": "B", "created_by": HUMAN})
            raise RuntimeError("boom")
    assert demo.publisher.log == []
    # A finalizer or capture failure is part of the transaction: the whole unit rolls back, nothing publishes.
    def failing_capture(connection):
        raise RuntimeError("capture failed")
    demo.publisher.capture = failing_capture
    with pytest.raises(RuntimeError, match="capture failed"):
        with database.transaction(business_id=BUSINESS) as uow:
            uow.repository("items").insert({"id": "c", "title": "C", "created_by": HUMAN})
    with database.read(business_id=BUSINESS) as uow:
        assert [row["id"] for row in uow.repository("items").list()] == ["a"]
    assert demo.publisher.log == []


def test_post_commit_publication_failure_cannot_fail_or_undo_a_committed_unit(tmp_path, caplog):
    database = database_at(tmp_path)
    database.demo.publisher.fail_publish = True
    with caplog.at_level(logging.ERROR, logger="qorqut_operations.storage.database"):
        with database.transaction(business_id=BUSINESS, operation="demo.write") as uow:
            uow.repository("items").insert({"id": "kept", "title": "Kept", "created_by": HUMAN})
    assert any("change_publish_failed" in record.getMessage() for record in caplog.records)
    with database.read(business_id=BUSINESS) as uow:
        assert uow.repository("items").get("kept")["title"] == "Kept"


def test_publisher_is_told_whether_a_write_unit_changed_anything(tmp_path):
    database = database_at(tmp_path, finalize=False)
    publisher = database.demo.publisher
    publisher.log.clear()
    with database.transaction(operation="demo.noop"):
        pass
    with database.transaction(operation="demo.write", business_id=BUSINESS) as uow:
        uow.repository("items").insert({"id": "x", "title": "X", "created_by": HUMAN})
    assert publisher.log == ["capture", ("publish", "demo.noop", False), "capture", ("publish", "demo.write", True)]
