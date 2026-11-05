"""CRM upgrades the exact deployed v1.3 schemas without importing live data or contacting providers."""
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

BASELINES = json.loads((Path(__file__).parent / "fixtures/crm_release_schemas.json").read_text())["baselines"]


@pytest.mark.parametrize("index", [0, 1], ids=["core-with-overseer", "deployed-extensions"])
def test_upgrade_the_frozen_deployed_release_schema_and_preserve_all_rows(index, tmp_path, monkeypatch):
    definition = BASELINES[index]
    monkeypatch.setattr(store, "definition", lambda: definition)
    source = tmp_path / "source.sqlite3"
    connection = store.create(source)
    store.populate(connection)
    names = definition["extensions"]
    configuration = {"extensions": [
        {"factory": f"qorqut_{name}.extension:create", **({"config": {
            "engine_url": "http://127.0.0.1:1", "admin_token_file": str(tmp_path / "absent-token")}}
            if name == "fastmail" else {})} for name in names]}
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
    config_path = tmp_path / "synthetic-installation.json"
    config_path.write_text(json.dumps(configuration))
    command = [sys.executable, "-m", "bazarlyk.migration.crm_upgrade", "--source", str(source),
               "--destination", str(destination), "--installation-config", str(config_path)]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["new_tables"] == ["crm_activity_events", "crm_entries", "task_moves"]
    assert report["source_target"] == definition["target"] and report["target"]["digest"] == target.target.digest
    assert report["checks"] == {"source_recognized": True, "schema": "equal", "rows": "identical",
                                "foreign_keys": "ok", "integrity": "ok"}
    assert crm_upgrade.file_sha256(source) == before_hash
    with sqlite3.connect(destination) as upgraded:
        assert read_objects(upgraded) == compile_target(target).objects
        after_rows = store.tables(upgraded)
        # The existing Table gains two default-valued columns; all other carried rows are byte-for-byte equal.
        for name, rows in before_rows.items():
            if name == "thread_messages":      # the pin of a moved note's reference scope is the one new column: NULL for every old note
                assert rows and [row[:-1] for row in after_rows[name]] == rows and {row[-1] for row in after_rows[name]} == {None}
            elif name not in {"target_identity", "data_tables"}:
                assert after_rows[name] == rows, name
        assert upgraded.execute("SELECT table_kind,crm_config_json FROM data_tables").fetchall() == [("general", None)] * 2
    if names:
        # Omitting extension selection cannot silently remove their data or produce a core-only output.
        with pytest.raises(DomainError) as error:
            crm_upgrade.upgrade(source, tmp_path / "wrong-selection.sqlite3")
        assert error.value.code == "upgrade_source_unrecognized"
        assert not (tmp_path / "wrong-selection.sqlite3").exists()
    # A changed schema is not accepted merely because its stored identity still matches a frozen release.
    with sqlite3.connect(source) as changed:
        changed.execute("CREATE TABLE unexpected_private_data(value TEXT)")
    with pytest.raises(DomainError) as error:
        crm_upgrade.upgrade(source, tmp_path / "unknown.sqlite3", configuration=configuration)
    assert error.value.code == "upgrade_source_unrecognized"
    assert not (tmp_path / "unknown.sqlite3").exists()
