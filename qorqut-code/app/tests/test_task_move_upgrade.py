"""Offline upgrade of the current CRM schema to the Move to project target.

The sources are frozen, synthetic stores generated from the unmodified host composition before this feature (``fixtures/
crm_release_schemas.json`` entries 2 and 3: core only, and the deployed Paper, Fastmail and Content set); they hold no row of any
live installation. The upgrade reads one explicit stopped source, writes one new destination and must preserve every carried
row. What changes is only what the relocation needs: the append-only ``task_moves`` audit and the scope-immutability triggers
that now recognize the guarded relocation. A fresh store carries the same objects, and neither an upgraded nor a fresh store
lets plain SQL relocate a Task.
"""

import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

import crm_baseline_store as store
from bazarlyk.migration import crm_upgrade
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.inventory import compile_target, read_objects
from qorqut_operations.storage.sqlfunctions import install_sql_functions

BASELINES = json.loads((Path(__file__).parent / "fixtures/crm_release_schemas.json").read_text())["baselines"]
GUARDED_TRIGGERS = ["crm_activity_events_immutable_update", "crm_entries_identity", "task_snooze_wakes_immutable",
                    "tasks_scope_identity", "thread_messages_immutable_update"]


def add_crm_rows(connection):
    """A CRM Table, one membership of a Task and one recorded observation, as the Domain stores them."""
    stamp = store.STAMP
    connection.execute("INSERT INTO data_tables(business_id,id,title,columns_json,created_by,created_at,updated_at,table_kind,crm_config_json) "
                       "VALUES (?,?,?,?,?,?,?,?,?)", (store.BUSINESS, "crm-a", "Synthetic CRM", "[]", store.OWNER, stamp, stamp, "crm",
                                                      json.dumps({"stages": [{"id": "lead", "label": "Lead"}]})))
    connection.execute("INSERT INTO crm_entries(business_id,id,table_id,record_task_id,record_business_id,record_namespace,position,"
                       "stage_id,cells_json,created_by,updated_by,created_at,updated_at) VALUES (?,?,?,?,?,?,0,'lead','{}',?,?,?,?)",
                       (store.BUSINESS, "entry-a", "crm-a", "task-a", store.BUSINESS, "business:" + store.BUSINESS,
                        store.OWNER, store.OWNER, stamp, stamp))
    connection.execute("INSERT INTO crm_activity_events(business_id,id,task_id,event_key,kind,summary,body,occurred_at,recorded_at,"
                       "recorded_by,source_json,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (store.BUSINESS, "event-a", "task-a", "synthetic-1", "message", "Synthetic call", "Synthetic body", stamp, stamp,
                        store.OWNER, "{}", "0" * 64))


def configuration_for(names, tmp_path):
    return {"extensions": [
        {"factory": f"qorqut_{name}.extension:create", **({"config": {
            "engine_url": "http://127.0.0.1:1", "admin_token_file": str(tmp_path / "absent-token")}}
            if name == "fastmail" else {})} for name in names]}


@pytest.mark.parametrize("index", [2, 3], ids=["current-crm-core", "current-crm-extensions"])
def test_the_current_crm_schema_upgrades_offline_keeping_every_row_and_adding_only_the_relocation_objects(index, tmp_path, monkeypatch):
    definition = BASELINES[index]
    monkeypatch.setattr(store, "definition", lambda: definition)
    source = tmp_path / "source.sqlite3"
    connection = store.create(source)
    store.populate(connection)
    add_crm_rows(connection)
    names = definition["extensions"]
    configuration = configuration_for(names, tmp_path)
    if names:
        connection.execute("INSERT INTO content_session_invocations(principal_id,business_id,operation_name,request_id,"
                           "operation_version,normalized_request_sha256,recorder_operation_id,session_id,prepared_plan_json,"
                           "created_at,updated_at) VALUES (?,?,?,?,1,?,?,?,?,?,?)",
                           (store.WORKER, store.BUSINESS, "synthetic", "request", "a" * 64, "record", "session", "{}",
                            store.STAMP, store.STAMP))
    before_rows = store.tables(connection)
    connection.close()
    before_hash = crm_upgrade.file_sha256(source)
    target = crm_upgrade.current_assembly(configuration)
    destination = tmp_path / "upgraded.sqlite3"
    dry = crm_upgrade.upgrade(source, destination, dry_run=True, configuration=configuration)
    assert not destination.exists() and dry["source_target"] == definition["target"]
    # The one new column is the pin of the scope a moved note's references were written in; every old note keeps NULL.
    assert dry["new_tables"] == ["task_moves"] and dry["rebuilt_tables"] == ["thread_messages"]
    assert dry["added_columns"] == {"thread_messages": ["reference_namespace"]}
    config_path = tmp_path / "synthetic-installation.json"
    config_path.write_text(json.dumps(configuration))
    completed = subprocess.run([sys.executable, "-m", "bazarlyk.migration.crm_upgrade", "--source", str(source),
                                "--destination", str(destination), "--installation-config", str(config_path)],
                               capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["new_tables"] == ["task_moves"] and report["target"]["digest"] == target.target.digest
    assert report["source_target"] == definition["target"]
    assert report["checks"] == {"source_recognized": True, "schema": "equal", "rows": "identical", "foreign_keys": "ok", "integrity": "ok"}
    assert report["changed_triggers"] == GUARDED_TRIGGERS and report["changed_indexes"] == []
    assert set(report["new_triggers"]) == {"task_moves_immutable_update", "task_moves_keep"}
    assert set(report["new_indexes"]) == {"task_moves_source", "task_moves_destination"}
    assert "Synthetic" not in json.dumps(report)
    assert crm_upgrade.file_sha256(source) == before_hash
    with sqlite3.connect(destination) as upgraded:
        assert read_objects(upgraded) == compile_target(target).objects
        after_rows = store.tables(upgraded)
        assert set(after_rows) == set(before_rows) | {"task_moves"}
        for name, rows in before_rows.items():
            if name == "thread_messages":
                assert rows and [row[:-1] for row in after_rows[name]] == rows and {row[-1] for row in after_rows[name]} == {None}
            elif name != "target_identity":
                assert after_rows[name] == rows, name
        assert after_rows["task_moves"] == []
        assert upgraded.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []
    # Default deny survives the upgrade: with the engine's callbacks installed, and without them, plain SQL cannot relocate a
    # Task, its discussion, its CRM membership or its observation, and the audit cannot be rewritten or deleted.
    for registered in (True, False):
        raw = sqlite3.connect(destination, isolation_level=None)
        if registered:
            install_sql_functions(raw)
        try:
            for statement in ("UPDATE tasks SET business_id=NULL WHERE id='task-a'",
                              "UPDATE thread_messages SET business_id=NULL WHERE item_id='task-a'",
                              "UPDATE crm_entries SET record_business_id=NULL, record_namespace='global' WHERE id='entry-a'",
                              "UPDATE crm_activity_events SET business_id=NULL WHERE id='event-a'"):
                with pytest.raises(sqlite3.DatabaseError):
                    raw.execute(statement)
            assert raw.execute("SELECT business_id FROM tasks WHERE id='task-a'").fetchone() == (store.BUSINESS,)
        finally:
            raw.close()
    if names:
        # Omitting the extension selection cannot silently remove their data or produce a core-only output.
        with pytest.raises(DomainError) as error:
            crm_upgrade.upgrade(source, tmp_path / "wrong-selection.sqlite3")
        assert error.value.code == "upgrade_source_unrecognized" and not (tmp_path / "wrong-selection.sqlite3").exists()
    # A changed source schema is not accepted merely because its stored identity still matches the frozen current store, and
    # an existing destination is never overwritten.
    with pytest.raises(DomainError) as error:
        crm_upgrade.upgrade(source, destination, configuration=configuration)
    assert error.value.code != "upgrade_source_unrecognized" and destination.exists()
    with sqlite3.connect(source) as changed:
        changed.execute("CREATE TABLE unexpected_private_data(value TEXT)")
    with pytest.raises(DomainError) as error:
        crm_upgrade.upgrade(source, tmp_path / "unknown.sqlite3", configuration=configuration)
    assert error.value.code == "upgrade_source_unrecognized" and not (tmp_path / "unknown.sqlite3").exists()


def test_a_store_already_at_the_target_is_not_upgraded_again(tmp_path, monkeypatch):
    """The target itself is never a recognized source: a second upgrade of the output is refused."""
    definition = BASELINES[2]
    monkeypatch.setattr(store, "definition", lambda: definition)
    source = tmp_path / "source.sqlite3"
    connection = store.create(source)
    store.populate(connection)
    connection.close()
    upgraded = tmp_path / "upgraded.sqlite3"
    crm_upgrade.upgrade(source, upgraded)
    with pytest.raises(DomainError) as error:
        crm_upgrade.upgrade(upgraded, tmp_path / "again.sqlite3")
    assert error.value.code == "upgrade_already_current" and not (tmp_path / "again.sqlite3").exists()
