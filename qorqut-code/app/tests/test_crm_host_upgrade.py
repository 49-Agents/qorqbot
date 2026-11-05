"""Offline pre-CRM -> CRM-capable store upgrade: acceptance on a populated synthetic store, and every refusal.

The pre-CRM store is built from the frozen base schema (``crm_baseline_store``); the target is the host's current composition
with the reviewed Operations Domain present, so the CRM tables, the widened shared sources and the ``data_tables`` columns and
capture triggers are the Domain's own DDL, imported by ``crm_upgrade`` and never copied here."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat

import pytest

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.database import APPLICATION_ID, Database
from qorqut_operations.storage.inventory import compile_target, read_objects
from qorqut_operations.storage.sqlfunctions import install_sql_functions

import crm_baseline_store as store
from bazarlyk.migration import crm_upgrade

REBUILT = ["data_tables", "owner_conversations", "thread_follows", "thread_messages"]
NEW_TABLES = sorted(["crm_activity_events", "crm_entries", "overseer_development_task_links",
    "overseer_development_task_requests", "overseer_development_tasks", "overseer_due_occurrences",
    "overseer_due_request_occurrences", "overseer_due_requests", "overseer_invocations", "overseer_owner_inputs",
    "overseer_retained_links", "overseer_retained_materials", "overseer_retention_events", "overseer_signoffs",
    "overseer_telegram_associations", "overseer_workflow_interpretations", "overseer_workflow_request_ops",
    "overseer_workflow_requests", "task_moves"])


def connect(path, *, foreign_keys=True):
    connection = sqlite3.connect(path, isolation_level=None)
    install_sql_functions(connection)
    connection.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")
    return connection


def make_source(directory, name="source.sqlite3", *, populated=True):
    path = directory / name
    connection = store.create(path)
    if populated:
        store.populate(connection)
    connection.close()
    return path


def refusal(source, destination, **options):
    with pytest.raises(DomainError) as caught:
        crm_upgrade.upgrade(source, destination, **options)
    return caught.value


def leftovers(directory):
    return sorted(path.name for path in directory.iterdir() if path.name.startswith(".crm-upgrade-"))


@pytest.fixture(scope="module")
def assembly():
    return crm_upgrade.current_assembly()


@pytest.fixture(scope="module")
def upgraded(tmp_path_factory, assembly):
    """One populated source, its dry-run report, and the real upgrade of it (module-wide: the upgrade is a heavy, read-only subject)."""
    directory = tmp_path_factory.mktemp("crm-upgrade")
    source = make_source(directory)
    before = crm_upgrade.file_sha256(source)
    destination = directory / "out" / "upgraded.sqlite3"
    dry = crm_upgrade.upgrade(source, destination, dry_run=True, assembly=assembly)
    dry_wrote = destination.parent.exists()
    report = crm_upgrade.upgrade(source, destination, assembly=assembly)
    return {"directory": directory, "source": source, "before": before, "destination": destination, "report": report, "dry": dry,
            "dry_wrote": dry_wrote}


# ----- recognition data -----------------------------------------------------------------------------------------------


def test_the_frozen_fingerprint_describes_the_committed_baseline_schema():
    """The schema fixture the tests replay and the fingerprint the upgrade trusts are one and the same pre-CRM store."""
    frozen, data = crm_upgrade.baseline(), store.definition()
    memory = sqlite3.connect(":memory:")
    install_sql_functions(memory)
    memory.execute(data["identity_ddl"])
    for _owner, statements in data["schema"]:
        for statement in statements:
            memory.execute(statement)
    actual = {key: (owner, hashlib.sha256(sql.encode()).hexdigest()) for key, (owner, sql) in read_objects(memory).items()}
    assert actual == frozen["objects"]
    assert frozen["target"] == data["target"]
    assert len(frozen["objects"]) > 400


def test_the_current_target_is_the_same_named_target_with_a_new_digest(assembly):
    assert assembly.target.digest != crm_upgrade.baseline()["target"]["digest"]
    assert (assembly.target.name, assembly.target.version) == (crm_upgrade.baseline()["target"]["name"], crm_upgrade.baseline()["target"]["version"])


# ----- populated acceptance -------------------------------------------------------------------------------------------


def test_the_upgrade_publishes_a_valid_new_store_and_leaves_the_source_byte_identical(upgraded, assembly):
    report, destination = upgraded["report"], upgraded["destination"]
    assert crm_upgrade.file_sha256(upgraded["source"]) == upgraded["before"] == report["source"]["sha256"]
    assert report["source"]["unchanged"] is True
    assert destination.is_file() and stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert report["destination"]["path"] == str(destination) and report["destination"]["sha256"] == crm_upgrade.file_sha256(destination)
    Database(destination, assembly=assembly).open_existing()
    connection = connect(destination)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        assert connection.execute("PRAGMA user_version").fetchone()[0] == assembly.target.version
        assert connection.execute("SELECT name, version, digest FROM target_identity").fetchall() == [
            (assembly.target.name, assembly.target.version, assembly.target.digest)]
    finally:
        connection.close()
    assert leftovers(upgraded["directory"] / "out") == []


def test_the_upgrade_report_names_exactly_the_expected_changes_and_holds_no_content(upgraded):
    report = upgraded["report"]
    assert report["new_tables"] == NEW_TABLES
    assert report["rebuilt_tables"] == REBUILT
    assert report["added_columns"] == {"data_tables": ["table_kind", "crm_config_json"], "thread_messages": ["reference_namespace"]}
    # The rewritten triggers are the three ``data_tables`` capture triggers (the two new columns are part of the row shape
    # they publish) and the three scope-immutability triggers that now recognize the guarded Task relocation. No index of the
    # source store changes.
    assert report["changed_indexes"] == []
    assert report["changed_triggers"] == ["inbox_capture_data_tables_delete", "inbox_capture_data_tables_insert",
                                         "inbox_capture_data_tables_update", "task_snooze_wakes_immutable",
                                         "tasks_scope_identity", "thread_messages_immutable_update"]
    # Every new index is the Domain's: the memberships' identity/position/revival keys, the recorded events' keys, and the
    # Table-discussion index that finds a record's membership developments.
    assert set(report["new_indexes"]) == {
        "crm_entries_record", "crm_entries_global_record", "crm_entries_position", "crm_entries_global_position",
        "crm_entries_by_record", "crm_entries_global_key", "crm_activity_events_event_key", "crm_activity_events_global_event_key",
        "crm_activity_events_global_key", "crm_activity_events_task", "thread_messages_crm_record",
        "overseer_due_membership", "overseer_due_occurrence_created", "overseer_due_request_pending",
        "overseer_due_request_target", "overseer_retained_event", "overseer_retained_links_target",
        "overseer_signoffs_application", "overseer_signoffs_batch", "overseer_signoffs_delta",
        "task_moves_source", "task_moves_destination"}
    # The target's constraints arrive with it: kind immutability, rows-vs-memberships, the scoped parents and the guards that
    # keep a Task or Table with memberships/history from being deleted.
    assert {"data_tables_kind_immutable", "data_rows_general_only", "data_tables_keep_crm", "tasks_keep_crm_entry",
            "tasks_keep_crm_activity", "thread_messages_table_scope", "thread_follows_table_scope",
            "inbox_capture_crm_entries_insert", "inbox_capture_crm_entries_update",
            "inbox_capture_crm_activity_events_insert", "task_moves_immutable_update", "task_moves_keep"} <= set(report["new_triggers"])
    assert report["checks"] == {"source_recognized": True, "schema": "equal", "rows": "identical", "foreign_keys": "ok", "integrity": "ok"}
    assert report["rows"]["tasks"] == 3 and report["rows"]["owner_conversation_messages"] == 4 and report["rows"]["thread_messages"] == 4
    text = json.dumps(report)
    assert "Synthetic" not in text  # counts, names and hashes only - never a row, title or message body


def test_a_dry_run_reports_the_same_upgrade_and_writes_nothing(upgraded):
    dry, report = dict(upgraded["dry"]), dict(upgraded["report"])
    assert upgraded["dry_wrote"] is False
    assert dry["mode"] == "dry-run" and dry["destination"] is None
    for key in ("mode", "destination"):
        dry.pop(key), report.pop(key)
    assert dry == report


def test_every_baseline_row_is_carried_with_its_id_and_every_new_column_takes_its_default(upgraded):
    before, after = connect(upgraded["source"], foreign_keys=False), connect(upgraded["destination"], foreign_keys=False)
    try:
        carried = store.tables(before)
        migrated = store.tables(after)
        assert set(migrated) == set(carried) | set(NEW_TABLES)
        for name, rows in carried.items():
            if name in ("target_identity", "data_tables"):
                continue
            if name == "thread_messages":      # the pin of a moved note's reference scope is the one new column: NULL for every old note
                assert rows and [row[:-1] for row in migrated[name]] == rows and {row[-1] for row in migrated[name]} == {None}
                continue
            assert migrated[name] == rows, name
        baseline_columns = "rowid, business_id, id, title, description, columns_json, tags_json, archived, created_by, revision, created_at, updated_at"
        assert (after.execute(f"SELECT {baseline_columns} FROM data_tables ORDER BY rowid").fetchall()
                == before.execute(f"SELECT {baseline_columns} FROM data_tables ORDER BY rowid").fetchall())
        assert after.execute("SELECT DISTINCT table_kind FROM data_tables").fetchall() == [("general",)]
        assert all(migrated[name] == [] for name in NEW_TABLES)
    finally:
        before.close()
        after.close()


def test_the_copy_fires_no_trigger_and_the_change_feed_and_id_counters_are_carried(upgraded):
    before, after = connect(upgraded["source"], foreign_keys=False), connect(upgraded["destination"])
    try:
        assert after.execute("SELECT COUNT(*) FROM inbox_changes").fetchone() == before.execute("SELECT COUNT(*) FROM inbox_changes").fetchone() == (14,)
        assert before.execute("SELECT * FROM sqlite_sequence ORDER BY name").fetchall() == after.execute("SELECT * FROM sqlite_sequence ORDER BY name").fetchall()
        # The retired private message id is not reused: the next sequence follows the carried counter, not the highest row.
        after.execute("INSERT INTO owner_conversation_messages(id,conversation_id,created_by,author_role,body,created_at) VALUES "
                      "('private-new','conversation-a',?,'owner','Synthetic new body','2026-03-02T10:00:00Z')", (store.OWNER,))
        assert after.execute("SELECT sequence FROM owner_conversation_messages WHERE id='private-new'").fetchone() == (41,)
    finally:
        before.close()
        after.close()


def test_triggers_and_immutability_survive_the_upgrade(upgraded):
    connection = connect(upgraded["destination"])
    try:
        count = connection.execute("SELECT COUNT(*) FROM inbox_changes").fetchone()[0]
        connection.execute("INSERT INTO tasks(business_id,id,title,status,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                           (store.BUSINESS, "task-new", "Synthetic after upgrade", "open", store.OWNER, store.STAMP, store.STAMP))
        assert connection.execute("SELECT COUNT(*) FROM inbox_changes").fetchone()[0] == count + 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE owner_conversation_messages SET body='changed' WHERE id='private-1'")
    finally:
        connection.close()


def test_the_widened_shared_sources_admit_a_table_in_the_new_store_and_not_in_the_old_one(upgraded, tmp_path):
    copy = tmp_path / "old-copy.sqlite3"
    shutil.copy(upgraded["source"], copy)
    message = ("INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,author_id,recorder_id,created_at) "
               "VALUES (?, 'table-comment', 'table', 'table-a', 'comment', 'Synthetic table comment', ?, ?, '2026-03-02T10:00:00Z')")
    follow = ("INSERT INTO thread_follows(business_id,item_type,item_id,principal_id,following,explicit,reason,created_at,updated_at) "
              "VALUES (?, 'table', 'table-a', ?, 1, 0, 'commenter', '2026-03-02T10:00:00Z', '2026-03-02T10:00:00Z')")
    conversation = ("INSERT INTO owner_conversations(id,source_key,business_id,source_type,source_id,audience,created_at,updated_at) "
                    "VALUES ('conversation-table','table:business-one:table-a',?,'table','table-a','owner_ordubashy','2026-03-02T10:00:00Z','2026-03-02T10:00:00Z')")
    for path, admitted in ((copy, False), (upgraded["destination"], True)):
        connection = connect(path)
        try:
            for statement, arguments in ((message, (store.BUSINESS, store.OWNER, store.OWNER)), (follow, (store.BUSINESS, store.OWNER)),
                                         (conversation, (store.BUSINESS,))):
                if admitted:
                    connection.execute(statement, arguments)
                else:
                    with pytest.raises(sqlite3.IntegrityError):
                        connection.execute(statement, arguments)
            if admitted:
                connection.execute("INSERT INTO owner_conversation_messages(id,conversation_id,created_by,author_role,body,created_at) VALUES "
                                   "('private-table','conversation-table',?,'owner','Synthetic table chat','2026-03-02T10:00:00Z')", (store.OWNER,))
        finally:
            connection.close()
    # The pre-existing task thread rows are unchanged next to the new Table source.
    connection = connect(upgraded["destination"])
    try:
        assert connection.execute("SELECT COUNT(*) FROM thread_messages WHERE item_type='task'").fetchone() == (4,)
    finally:
        connection.close()


def test_the_upgraded_store_is_the_store_a_fresh_core_instance_creates(upgraded, assembly, tmp_path):
    fresh = tmp_path / "fresh.sqlite3"
    Database(fresh, assembly=assembly).initialize_target()
    Database(fresh, assembly=assembly).open_existing()
    one, two = connect(fresh), connect(upgraded["destination"])
    try:
        assert read_objects(one) == read_objects(two) == dict(compile_target(assembly).objects)
        for table in (*NEW_TABLES, "data_tables"):
            assert [row[1] for row in one.execute(f'PRAGMA table_info("{table}")')] == [row[1] for row in two.execute(f'PRAGMA table_info("{table}")')]
        assert one.execute("SELECT * FROM change_sequences ORDER BY name").fetchall() == [("focus_board", 0), ("signoffs", 0), ("tables", 0)]
    finally:
        one.close()
        two.close()


def test_an_empty_pre_crm_store_upgrades_too(tmp_path, assembly):
    source = make_source(tmp_path, populated=False)
    report = crm_upgrade.upgrade(source, tmp_path / "empty-out.sqlite3", assembly=assembly)
    assert sum(report["rows"].values()) == 3  # only the schema seeds
    Database(tmp_path / "empty-out.sqlite3", assembly=assembly).open_existing()


# ----- refusals -------------------------------------------------------------------------------------------------------


def test_an_existing_destination_is_refused_and_never_touched(tmp_path, assembly):
    source = make_source(tmp_path)
    before = crm_upgrade.file_sha256(source)
    for suffix in ("", "-wal", "-journal"):
        destination = tmp_path / f"existing.sqlite3{suffix}"
        target = tmp_path / "existing.sqlite3"
        destination.write_bytes(b"owner file")
        error = refusal(source, target, assembly=assembly)
        assert error.code == "upgrade_destination_exists"
        assert destination.read_bytes() == b"owner file"
        destination.unlink()
    link = tmp_path / "dangling.sqlite3"
    link.symlink_to(tmp_path / "nowhere.sqlite3")
    assert refusal(source, link, assembly=assembly).code == "upgrade_destination_exists"
    assert refusal(source, link, dry_run=True, assembly=assembly).code == "upgrade_destination_exists"
    assert crm_upgrade.file_sha256(source) == before and leftovers(tmp_path) == []


def test_upgrading_twice_is_refused_and_an_upgraded_store_is_never_a_source(tmp_path, assembly):
    source = make_source(tmp_path)
    destination = tmp_path / "once.sqlite3"
    crm_upgrade.upgrade(source, destination, assembly=assembly)
    again = refusal(source, destination, assembly=assembly)
    assert again.code == "upgrade_destination_exists"
    hashed = crm_upgrade.file_sha256(destination)
    chained = refusal(destination, tmp_path / "twice.sqlite3", assembly=assembly)
    assert chained.code == "upgrade_already_current"
    assert not (tmp_path / "twice.sqlite3").exists() and crm_upgrade.file_sha256(destination) == hashed and leftovers(tmp_path) == []


def alter_identity(connection):
    connection.execute("UPDATE target_identity SET digest = 'sha256:other'")


def add_table(connection):
    connection.execute("CREATE TABLE stray_table (id TEXT PRIMARY KEY)")


def drop_index(connection):
    name = connection.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL LIMIT 1").fetchone()[0]
    connection.execute(f'DROP INDEX "{name}"')


def replace_trigger(connection):
    name, sql = connection.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name LIMIT 1").fetchone()
    connection.execute(f'DROP TRIGGER "{name}"')
    connection.execute(sql.replace("BEGIN", "BEGIN SELECT 1;", 1))


def wrong_application(connection):
    connection.execute("PRAGMA application_id = 1111")


def wrong_version(connection):
    connection.execute("PRAGMA user_version = 7")


def add_column(connection):
    connection.execute("ALTER TABLE tasks ADD COLUMN stray TEXT")


@pytest.mark.parametrize("damage", [alter_identity, add_table, drop_index, replace_trigger, wrong_application, wrong_version, add_column])
def test_an_unknown_or_edited_source_schema_is_refused_without_output(tmp_path, assembly, damage):
    source = make_source(tmp_path)
    connection = connect(source, foreign_keys=False)
    damage(connection)
    connection.close()
    before = crm_upgrade.file_sha256(source)
    error = refusal(source, tmp_path / "out" / "new.sqlite3", assembly=assembly)
    assert error.code == "upgrade_source_unrecognized"
    assert not (tmp_path / "out").exists() and leftovers(tmp_path) == []
    assert crm_upgrade.file_sha256(source) == before


def test_a_file_that_is_not_a_database_and_a_damaged_store_are_refused(tmp_path, assembly):
    junk = tmp_path / "junk.sqlite3"
    junk.write_bytes(b"not a database" * 100)
    assert refusal(junk, tmp_path / "junk-out.sqlite3", assembly=assembly).code == "upgrade_source_invalid"
    broken = make_source(tmp_path, "broken.sqlite3")
    connection = connect(broken, foreign_keys=False)
    connection.execute("UPDATE businesses SET archived_by = 'principal-missing'")
    connection.close()
    assert refusal(broken, tmp_path / "broken-out.sqlite3", assembly=assembly).code == "upgrade_source_invalid"
    assert not (tmp_path / "junk-out.sqlite3").exists() and not (tmp_path / "broken-out.sqlite3").exists()


def test_a_missing_symlinked_or_non_regular_source_is_refused(tmp_path, assembly):
    source = make_source(tmp_path)
    link = tmp_path / "linked.sqlite3"
    link.symlink_to(source)
    (tmp_path / "directory.sqlite3").mkdir()
    for candidate in (tmp_path / "absent.sqlite3", link, tmp_path / "directory.sqlite3"):
        assert refusal(candidate, tmp_path / "x.sqlite3", assembly=assembly).code == "upgrade_source_invalid"
    behind = tmp_path / "behind"
    behind.symlink_to(tmp_path)
    assert refusal(behind / "source.sqlite3", tmp_path / "x.sqlite3", assembly=assembly).code == "upgrade_source_invalid"
    assert not (tmp_path / "x.sqlite3").exists()


def test_a_destination_beneath_a_symbolic_link_is_refused(tmp_path, assembly):
    source = make_source(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real)
    assert refusal(source, tmp_path / "alias" / "new.sqlite3", assembly=assembly).code == "upgrade_destination_invalid"
    assert list(real.iterdir()) == []


def test_an_active_wal_or_hot_journal_makes_the_source_unusable(tmp_path, assembly):
    source = make_source(tmp_path, "live.sqlite3")
    holder = connect(source)
    try:
        assert holder.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        holder.execute("INSERT INTO principals(id,kind,display_name,created_at,updated_at) VALUES ('principal-late','human','Synthetic Late',?,?)",
                       (store.STAMP, store.STAMP))
        assert (tmp_path / "live.sqlite3-wal").exists()
        before = crm_upgrade.file_sha256(source)
        error = refusal(source, tmp_path / "wal-out.sqlite3", assembly=assembly)
        assert error.code == "upgrade_source_active" and "-wal" in error.message
        assert refusal(source, tmp_path / "wal-out.sqlite3", dry_run=True, assembly=assembly).code == "upgrade_source_active"
        assert crm_upgrade.file_sha256(source) == before
    finally:
        holder.close()
    # A cleanly closed WAL store leaves no sidecar and is acceptable.
    assert not (tmp_path / "live.sqlite3-wal").exists()
    assert crm_upgrade.upgrade(source, tmp_path / "wal-out.sqlite3", assembly=assembly)["ok"] is True

    hot = make_source(tmp_path, "hot.sqlite3")
    (tmp_path / "hot.sqlite3-journal").write_bytes(b"\x01" * 512)
    assert refusal(hot, tmp_path / "hot-out.sqlite3", assembly=assembly).code == "upgrade_source_active"
    (tmp_path / "hot.sqlite3-journal").write_bytes(b"")
    (tmp_path / "hot.sqlite3-shm").write_bytes(b"")
    assert refusal(hot, tmp_path / "hot-out.sqlite3", assembly=assembly).code == "upgrade_source_active"
    assert not (tmp_path / "hot-out.sqlite3").exists()


def test_a_target_that_would_lose_source_tables_or_columns_is_refused(tmp_path, assembly):
    source = make_source(tmp_path)
    frozen, compiled = crm_upgrade.baseline(), compile_target(assembly)
    connection = crm_upgrade.open_source(source)
    try:
        retired = {**frozen, "objects": {**frozen["objects"], ("table", "retired_store"): ("retired_store", "0" * 64)}}
        with pytest.raises(DomainError) as dropped:
            crm_upgrade.plan(connection, retired, compiled, assembly)
        assert dropped.value.code == "upgrade_incompatible" and "retired_store" in dropped.value.message
    finally:
        connection.close()
    wider = connect(source, foreign_keys=False)
    wider.execute("ALTER TABLE tasks ADD COLUMN retired_column TEXT")
    try:
        with pytest.raises(DomainError) as lost:
            crm_upgrade.plan(wider, frozen, compiled, assembly)
        assert lost.value.code == "upgrade_incompatible" and "tasks.retired_column" in lost.value.message
    finally:
        wider.close()


def test_a_failed_build_leaves_nothing_behind(tmp_path, assembly, monkeypatch):
    source = make_source(tmp_path)
    before = crm_upgrade.file_sha256(source)

    def fail(*_args, **_kwargs):
        raise DomainError("upgrade_failed", "Injected verification failure")

    monkeypatch.setattr(crm_upgrade, "verify_rows", fail)
    destination = tmp_path / "failed" / "new.sqlite3"
    assert refusal(source, destination, assembly=assembly).code == "upgrade_failed"
    assert not destination.exists() and leftovers(destination.parent) == []
    assert crm_upgrade.file_sha256(source) == before


def test_a_destination_that_appears_during_the_build_is_never_replaced(tmp_path, assembly, monkeypatch):
    source = make_source(tmp_path)
    destination = tmp_path / "raced.sqlite3"
    real_build = crm_upgrade.build

    def racing(*args, **kwargs):
        result = real_build(*args, **kwargs)
        destination.write_bytes(b"owner file")
        return result

    monkeypatch.setattr(crm_upgrade, "build", racing)
    assert refusal(source, destination, assembly=assembly).code == "upgrade_destination_exists"
    assert destination.read_bytes() == b"owner file" and leftovers(tmp_path) == []


def test_a_source_that_changes_during_the_upgrade_is_reported(tmp_path, assembly, monkeypatch):
    source = make_source(tmp_path)
    real_build = crm_upgrade.build

    def changing(*args, **kwargs):
        result = real_build(*args, **kwargs)
        os.utime(source, None)
        with source.open("r+b") as handle:
            handle.seek(0, os.SEEK_END)
            handle.write(b"\0" * 8)
        return result

    monkeypatch.setattr(crm_upgrade, "build", changing)
    assert refusal(source, tmp_path / "changed.sqlite3", dry_run=True, assembly=assembly).code == "upgrade_failed"


def test_a_source_that_changes_after_the_build_publishes_nothing(tmp_path, assembly, monkeypatch):
    """The source check is the last step before the exclusive link, so a failed upgrade can never leave a usable output."""
    source = make_source(tmp_path)
    destination = tmp_path / "out" / "changed.sqlite3"
    real_build, changed = crm_upgrade.build, {}

    def changing(*args, **kwargs):
        result = real_build(*args, **kwargs)
        with source.open("r+b") as handle:
            handle.seek(0, os.SEEK_END)
            handle.write(b"\0" * 8)
        changed["hash"] = crm_upgrade.file_sha256(source)
        return result

    monkeypatch.setattr(crm_upgrade, "build", changing)
    assert refusal(source, destination, assembly=assembly).code == "upgrade_failed"
    assert not destination.exists() and leftovers(destination.parent) == []
    assert crm_upgrade.file_sha256(source) == changed["hash"]            # the upgrader never wrote the source


def test_a_source_that_is_only_touched_after_the_build_is_also_not_published(tmp_path, assembly, monkeypatch):
    source = make_source(tmp_path)
    destination = tmp_path / "touched.sqlite3"
    real_build = crm_upgrade.build

    def touching(*args, **kwargs):
        result = real_build(*args, **kwargs)
        os.utime(source, ns=(1, 1))
        return result

    monkeypatch.setattr(crm_upgrade, "build", touching)
    assert refusal(source, destination, assembly=assembly).code == "upgrade_failed"
    assert not destination.exists() and leftovers(tmp_path) == []


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_an_active_sidecar_that_appears_during_the_build_publishes_nothing(tmp_path, assembly, monkeypatch, suffix):
    source = make_source(tmp_path)
    before = crm_upgrade.file_sha256(source)
    destination = tmp_path / "out" / "active.sqlite3"
    real_build = crm_upgrade.build

    def activating(*args, **kwargs):
        result = real_build(*args, **kwargs)
        (tmp_path / ("source.sqlite3" + suffix)).write_bytes(b"x" * 16)
        return result

    monkeypatch.setattr(crm_upgrade, "build", activating)
    assert refusal(source, destination, assembly=assembly).code == "upgrade_source_active"
    assert not destination.exists() and leftovers(destination.parent) == []
    assert crm_upgrade.file_sha256(source) == before


def test_a_store_that_fails_validation_before_publication_leaves_no_output(tmp_path, assembly, monkeypatch):
    source = make_source(tmp_path)
    destination = tmp_path / "out" / "invalid.sqlite3"

    def invalid(self):
        raise DomainError("invalid_store", "Injected validation failure")

    monkeypatch.setattr(Database, "open_existing", invalid)
    assert refusal(source, destination, assembly=assembly).code == "invalid_store"
    assert not destination.exists() and leftovers(destination.parent) == []


def test_a_link_that_cannot_be_made_is_a_failed_upgrade_without_output(tmp_path, assembly, monkeypatch):
    source = make_source(tmp_path)
    destination = tmp_path / "out" / "unlinked.sqlite3"

    def refuse(*_args, **_kwargs):
        raise PermissionError("injected")

    monkeypatch.setattr(crm_upgrade.os, "link", refuse)
    assert refusal(source, destination, assembly=assembly).code == "upgrade_failed"
    assert not destination.exists() and leftovers(destination.parent) == []


def test_a_failure_after_the_link_removes_exactly_the_file_this_run_created(tmp_path, assembly, monkeypatch):
    source = make_source(tmp_path)
    destination = tmp_path / "out" / "durable.sqlite3"

    def failing(_directory):
        assert destination.is_file()                      # it was linked ...
        raise OSError("injected directory sync failure")

    monkeypatch.setattr(crm_upgrade, "_fsync_directory", failing)
    assert refusal(source, destination, assembly=assembly).code == "upgrade_failed"
    assert not destination.exists() and leftovers(destination.parent) == []      # ... and is gone again with the failure


def test_a_failure_after_the_link_never_removes_a_file_another_actor_put_there(tmp_path, assembly, monkeypatch):
    source = make_source(tmp_path)
    destination = tmp_path / "out" / "replaced.sqlite3"

    def failing(_directory):
        destination.unlink()
        destination.write_bytes(b"another actor's file")
        raise OSError("injected directory sync failure")

    monkeypatch.setattr(crm_upgrade, "_fsync_directory", failing)
    assert refusal(source, destination, assembly=assembly).code == "upgrade_failed"
    assert destination.read_bytes() == b"another actor's file" and leftovers(destination.parent) == []


# ----- command line ---------------------------------------------------------------------------------------------------


def test_the_command_line_prints_a_report_and_a_refusal_code(tmp_path, capsys, monkeypatch):
    source = make_source(tmp_path)
    destination = tmp_path / "cli-out.sqlite3"
    assert crm_upgrade.main(["--source", str(source), "--destination", str(destination), "--dry-run"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["mode"] == "dry-run" and not destination.exists()
    assert crm_upgrade.main(["--source", str(source), "--destination", str(destination)]) == 0
    done = json.loads(capsys.readouterr().out)
    assert done["mode"] == "upgrade" and destination.is_file() and done["destination"]["sha256"] == crm_upgrade.file_sha256(destination)
    assert crm_upgrade.main(["--source", str(source), "--destination", str(destination)]) == 2
    refused = json.loads(capsys.readouterr().err)
    assert refused["ok"] is False and refused["error"]["code"] == "upgrade_destination_exists"
    with pytest.raises(SystemExit):
        crm_upgrade.main(["--source", str(source)])  # an explicit destination is required: no default path exists


def test_the_real_upgrade_module_runs_in_a_separate_process_with_the_composed_domain(tmp_path):
    import subprocess
    import sys

    source = make_source(tmp_path)
    before = crm_upgrade.file_sha256(source)
    destination = tmp_path / "subprocess.sqlite3"
    command = [sys.executable, "-m", "bazarlyk.migration.crm_upgrade", "--source", str(source),
               "--destination", str(destination)]
    dry = subprocess.run([*command, "--dry-run"], capture_output=True, text=True, timeout=60)
    assert dry.returncode == 0, dry.stderr
    assert json.loads(dry.stdout)["mode"] == "dry-run" and not destination.exists()
    upgraded = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert upgraded.returncode == 0, upgraded.stderr
    report = json.loads(upgraded.stdout)
    assert report["checks"]["integrity"] == "ok" and report["checks"]["foreign_keys"] == "ok"
    assert crm_upgrade.file_sha256(source) == before
    refused = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert refused.returncode == 2
    assert json.loads(refused.stderr)["error"]["code"] == "upgrade_destination_exists"
    assert crm_upgrade.file_sha256(destination) == report["destination"]["sha256"]
