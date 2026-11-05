"""CRM storage: the declarations, their database guards, exact per-revision snapshots, history projection, exports and the
module surface a host composes."""

from __future__ import annotations

import json
import subprocess

import pytest

from crm_support import CRM_OPERATIONS, STAGES, CRMHost, ref_of
from operational_support import AGENT, BUSINESS, HUMAN, OTHER
from qorqut_operations import crm
from qorqut_operations.application import OperationRegistry
from qorqut_operations.contracts import canonical_hash, new_id, plain
from qorqut_operations.history_exports import PRIVATE_EXCLUDED, SEPARATE_HISTORY, ExportCatalog, HistoryContribution
from qorqut_operations.storage.assembly import AssemblyError
from qorqut_operations.tables import EXPORTS, HISTORY_REF, TableHistory
from qorqut_operations.tables import CLASSIFICATIONS as TABLE_CLASSIFICATIONS


@pytest.fixture
def host(tmp_path):
    return CRMHost(tmp_path)


def tick(host):
    host.later(seconds=1)


def refused(host, statement, *parameters):
    with host.database.transaction() as uow:
        with pytest.raises(Exception):
            uow.connection.execute(statement, parameters)


# -- the exact snapshots ----------------------------------------------------------------------------------------------------------

GENERAL_TABLE_KEYS = {"business_id", "id", "title", "description", "columns_json", "tags_json", "archived", "created_by", "revision",
                      "created_at", "updated_at"}


def test_general_snapshots_keep_their_original_shape_and_hash_inputs(host):
    table = host.make("Plain")["table"]
    host.write(table, [{"cells": {"name": "A"}}], revision=1)
    for version in host.versions():
        snapshot = version["snapshot_json"]
        assert set(snapshot) == {"schema_version", "table", "rows"} and snapshot["schema_version"] == 1
        assert set(snapshot["table"]) == GENERAL_TABLE_KEYS and "table_kind" not in json.dumps(snapshot)
        assert version["snapshot_hash"] == canonical_hash(snapshot)
    assert [row["position"] for row in host.versions()[-1]["snapshot_json"]["rows"]] == [0]


def test_crm_snapshots_hold_memberships_and_never_copy_the_task(host):
    table = host.crm_table()
    task = host.create_task("Confidential Corp", task_type="customer", customer_profile={"email": "private@corp.test"})
    tick(host)
    first = host.add(table, task, cells={"value": 4}, stage_id="lead")
    tick(host)
    second = host.add(first["table"], create={"title": "Second"})
    tick(host)
    host.data("crm_entries_remove", {"table_id": table["id"], "entry_id": first["entry"]["id"]}, expected_revision=second["table"]["revision"])
    versions = host.versions()
    assert [version["table_revision"] for version in versions] == [1, 2, 3, 4]
    assert [len(version["snapshot_json"]["entries"]) for version in versions] == [0, 1, 2, 2]
    snapshot = versions[-1]["snapshot_json"]
    assert snapshot["schema_version"] == 2 and snapshot["table_kind"] == "crm" and snapshot["table"]["crm_config_json"] == STAGES
    entries = sorted(snapshot["entries"], key=lambda e: e["position"])      # stored in id order, like data rows, so bytes are stable
    entry = entries[0]
    assert [(e["position"], e["removed"]) for e in entries] == [(0, 1), (1, 0)]
    assert entry["record_task_id"] == task["id"] and entry["record_business_id"] == BUSINESS and entry["record_namespace"] == "business:" + BUSINESS
    assert entry["cells_json"] == {"value": 4} and entry["stage_id"] == "lead"
    text = json.dumps(snapshot)
    assert "Confidential" not in text and "private@corp.test" not in text and "Second" not in text
    for version in versions:
        assert version["snapshot_hash"] == canonical_hash(version["snapshot_json"])
    # A rejected or unchanged write adds no revision and no version.
    host.error("crm_entries_add", {"table_id": table["id"], "create": {"title": "x"}, "stage_id": "bogus"}, expected_revision=4)
    host.data("crm_entries_add", {"table_id": table["id"], "record": ref_of(task)}, expected_revision=4)           # restores: a real change
    host.data("crm_entries_add", {"table_id": table["id"], "record": ref_of(task)}, expected_revision=5)           # now a member: unchanged
    assert [v["table_revision"] for v in host.versions()] == [1, 2, 3, 4, 5]


def test_crm_history_is_published_and_the_projection_matches_current_state(host):
    table = host.crm_table()
    task = host.create_task("Acme")
    added = host.add(table, task, cells={"notes": "n"}, stage_id="won")
    tick(host)
    removed = host.data("crm_entries_remove", {"table_id": table["id"], "entry_id": added["entry"]["id"]}, expected_revision=2)
    captured = {row["id"]: (row["snapshot_json"], row["path"]) for row in host.versions()}
    host.run(removed["job_id"])
    assert host.git("rev-list", "--count", HISTORY_REF) == "3"
    history = host.read("tables_history", {"table_id": table["id"]})["items"]
    assert [row["table_revision"] for row in history] == [3, 2, 1]
    for row in history:
        snapshot, path = captured[row["id"]]
        blob = subprocess.run(["git", "-C", str(host.root), "show", row["git_commit"] + ":" + path], check=True, capture_output=True).stdout
        assert blob == TableHistory.bytes(snapshot)
    projection = json.loads((host.root / "first" / "hq" / "tables" / (table["id"] + ".json")).read_text())
    assert projection["table"]["revision"] == 3 and projection["table_kind"] == "crm"
    assert [(e["id"], e["removed"], e["cells_json"]) for e in projection["entries"]] == [(added["entry"]["id"], 1, {"notes": "n"})]


# -- database guards ---------------------------------------------------------------------------------------------------------------

def put(uow, built):
    uow.connection.execute(built[0], built[1:])


def insert_entry(table_id, task_id, business=BUSINESS, record_business=BUSINESS, stage=None, **extra):
    values = {"business_id": business, "id": new_id(), "table_id": table_id, "record_task_id": task_id, "record_business_id": record_business,
              "record_namespace": "global" if record_business is None else "business:" + record_business, "position": extra.pop("position", 0),
              "stage_id": stage, "cells_json": "{}", "removed": 0, "created_by": HUMAN, "updated_by": HUMAN,
              "created_at": "2026-10-12T12:00:00.000000Z", "updated_at": "2026-10-12T12:00:00.000000Z", **extra}
    keys = list(values)
    return (f"INSERT INTO crm_entries({','.join(keys)}) VALUES ({','.join('?' * len(keys))})", *values.values())


def test_the_database_refuses_inconsistent_memberships(host):
    crm_table, general = host.crm_table(), host.make("Plain")["table"]
    task = host.create_task("Acme")
    elsewhere = host.crm_table("Other", host.owner(business=OTHER))
    owner = host.owner(business=None)
    global_task = host.data("tasks_create", {"title": "G", "scope": "global"}, owner)["task"]
    with host.database.transaction() as uow:                      # the valid shapes are accepted, in both scopes
        put(uow, insert_entry(crm_table["id"], task["id"]))
        put(uow, insert_entry(crm_table["id"], global_task["id"], record_business=None, position=1))
    refused(host, *insert_entry(crm_table["id"], task["id"], position=2))                                    # one active membership per record
    refused(host, *insert_entry(crm_table["id"], global_task["id"], record_business=None, position=2))        # ... for Global records too
    refused(host, *insert_entry(crm_table["id"], task["id"], position=0, record_business=OTHER))              # position and a missing record
    refused(host, *insert_entry(general["id"], task["id"]))                                                  # a general table has no memberships
    refused(host, *insert_entry(elsewhere["id"], task["id"]))                                                # the Table must be in the entry's scope
    refused(host, *insert_entry(crm_table["id"], "no-such-task", position=3))
    refused(host, *insert_entry(crm_table["id"], task["id"], stage="bogus", position=3))
    refused(host, *insert_entry(crm_table["id"], global_task["id"], record_business=BUSINESS, position=3))     # scope label must match the record
    refused(host, "INSERT INTO data_rows(business_id,id,table_id,position,cells_json,created_by,updated_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            BUSINESS, new_id(), crm_table["id"], 0, "{}", HUMAN, HUMAN, "now", "now")                        # a CRM holds no data rows
    entry = host.query("SELECT id FROM crm_entries WHERE record_task_id=?", task["id"])[0]["id"]
    refused(host, "DELETE FROM crm_entries WHERE id=?", entry)                                              # removal is soft
    refused(host, "UPDATE crm_entries SET record_task_id='other' WHERE id=?", entry)
    refused(host, "UPDATE crm_entries SET table_id=? WHERE id=?", general["id"], entry)
    refused(host, "UPDATE crm_entries SET stage_id='bogus' WHERE id=?", entry)
    refused(host, "DELETE FROM tasks WHERE id=?", task["id"])                                               # a Task with memberships stays
    refused(host, "DELETE FROM data_tables WHERE id=?", crm_table["id"])
    refused(host, "UPDATE data_tables SET table_kind='general', crm_config_json=NULL WHERE id=?", crm_table["id"])
    refused(host, "UPDATE data_tables SET crm_config_json=NULL WHERE id=?", crm_table["id"])                 # kind and configuration agree
    refused(host, "UPDATE data_tables SET crm_config_json='{}' WHERE id=?", general["id"])
    refused(host, "INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,recorder_id,attribution_json,created_at) VALUES (?,?, 'table', ?, 'comment','x',?,'{}','now')",
            BUSINESS, new_id(), "no-such-table", HUMAN)                                                      # a thread cannot exist apart from its Table
    refused(host, "INSERT INTO thread_follows(business_id,item_type,item_id,principal_id,following,explicit,reason,created_at,updated_at) VALUES (?,?,?,?,1,1,'explicit','now','now')",
            BUSINESS, "table", "no-such-table", HUMAN)


def test_the_declarations_describe_exactly_the_two_crm_tables(host):
    assert crm.TABLE_NAMES == ("crm_entries", "crm_activity_events") and set(crm.TABLES) == set(crm.TABLE_NAMES)
    assert set(crm.IMMUTABLE_FIELDS) == {"crm_entries"} and {"table_id", "record_task_id"} <= set(crm.IMMUTABLE_FIELDS["crm_entries"])
    contribution = crm.storage_contribution()
    assert contribution.owner == "crm" and set(contribution.tables) == set(crm.TABLES)
    assert {"data_tables", "tasks", "thread_messages", "thread_follows", "inbox_changes"} <= set(contribution.required_tables)
    assert tuple(crm.SCHEMA) == tuple(contribution.schema)
    host.add(host.crm_table(), host.create_task("Acme"))
    assert "crm_entries" in {row["source_type"] for row in host.query("SELECT DISTINCT source_type FROM inbox_changes")}


def test_history_exports_classify_both_tables_explicitly(host):
    kinds = {item.table: item.kind for item in crm.CLASSIFICATIONS}
    assert kinds == {"crm_entries": SEPARATE_HISTORY, "crm_activity_events": PRIVATE_EXCLUDED}
    assert next(item for item in crm.CLASSIFICATIONS if item.table == "crm_entries").canonical == "qorqut-tables"
    table = host.crm_table("Exported")
    task = host.create_task("Acme")
    host.add(table, task)
    host.data("crm_activity_record", {"task_id": task["id"], "kind": "email", "summary": "s", "body": "private body", "occurred_at": "2026-10-11T10:00:00Z",
                                      "source": {"kind": "manual"}, "event_key": "e"}, host.agent(AGENT))
    catalog = ExportCatalog((HistoryContribution("tables", exports=EXPORTS, classifications=TABLE_CLASSIFICATIONS),
                             HistoryContribution("crm", classifications=crm.CLASSIFICATIONS)))
    with host.database.read() as uow:
        exported = catalog.export(uow)
    assert "table_kind" in exported.tables["data_tables"].columns and [row["table_kind"] for row in exported.tables["data_tables"].rows] == ["crm"]
    assert "crm_entries" not in exported.tables and "crm_activity_events" not in exported.tables and "private body" not in json.dumps(plain(exported.tables), default=str)


# -- the module surface a host composes --------------------------------------------------------------------------------------------------

def test_the_stable_module_surface_and_registration(host):
    assert set(crm.__all__) == {"CATALOG_ENTRIES", "CLASSIFICATIONS", "CRMOperations", "IMMUTABLE_FIELDS", "MUTATION_NAMES", "READ_NAMES", "SCHEMA",
                                "TABLES", "TABLE_NAMES", "storage_contribution"}
    assert crm.READ_NAMES == ("crm_entries_list", "tables_thread_get")
    assert set(crm.MUTATION_NAMES) == {"crm_entries_add", "crm_entries_update", "crm_entries_remove", "crm_activity_record", "tables_comment", "tables_follow"}
    entries = {entry.name: (entry.effect_kind, entry.supports_signoff) for entry in crm.CATALOG_ENTRIES}
    assert entries["crm_entries_list"] == ("read", False) and entries["tables_follow"] == ("records", False) and entries["tables_comment"] == ("records", True)
    assert "crm_activity_list" not in entries            # the unified activity page belongs to the host
    registry = host.registry
    registered = {name for name in CRM_OPERATIONS}
    assert all(registry.get(name) for name in registered)
    add, activity = registry.get("crm_entries_add"), registry.get("crm_activity_record")
    assert add.derived_effects == ("tables_rows_write",) and activity.derived_effects == ("tasks_comment",)
    # Amendment 002: commenting derives from the Table's row-write decision, following only from its read decision.
    assert registry.get("tables_comment").derived_effects == ("tables_rows_write",)
    assert registry.get("tables_follow").derived_effects == ("tables_get",)
    assert {"focus", "scope", "rationale"}.isdisjoint(add.input_schema["properties"]["create"]["properties"]) and "title" in add.input_schema["properties"]["create"]["properties"]
    assert registry.get("crm_entries_list").input_schema["properties"]["limit"]["maximum"] == 100
    assert registry.get("tables_thread_get").effect_kind == "read" and registry.get("tables_follow").supports_signoff is False
    assert host.crm.register(OperationRegistry()) is host.crm


def test_composition_is_explicit_and_type_checked(host):
    good = (host.app, host.tables, host.tasks, host.items)
    for index, wrong in enumerate((object(), object(), object(), object())):
        arguments = list(good)
        arguments[index] = wrong
        with pytest.raises(AssemblyError):
            crm.CRMOperations(*arguments)
    with pytest.raises(AssemblyError):
        crm.CRMOperations(*good, clock=None)
    from qorqut_operations.tables import TableOperations
    fresh = TableOperations(host.database, host.root, host.project_hq, clock=host.clock, job_programs=host.programs)
    with pytest.raises(AssemblyError):
        fresh.bind_crm(object())
    # Until a CRM is bound, a CRM table cannot be created or read through the Table service.
    with pytest.raises(Exception):
        fresh.crm_service()
