"""The relocated agent-maintained tables over the synthetic tables host: scopes, rows, revisions, conflicts, Global
authority, snapshot bytes and the Git history publisher with its failure reconciliation."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess

import pytest

from operational_support import AGENT, AGENT2, BUSINESS, COORDINATOR, OTHER
from qorqut_operations.contracts import DomainError, canonical_hash, new_id, plain
from qorqut_operations.history_exports import ExportCatalog, HistoryContribution
from qorqut_operations.policy import PolicyRules
from qorqut_operations.policy.rules import resolve_operation_policy
from qorqut_operations.tables import (CLASSIFICATIONS, EXPORTS, HISTORY_REF, IMMUTABLE_FIELDS, TABLES, TableHistory, global_tables)
from tables_support import COLUMNS, ELIGIBLE, TablesHost


@pytest.fixture
def host(tmp_path):
    return TablesHost(tmp_path)


def global_owner(host):
    return host.owner(business=None)


# -- project tables: rows, revisions, conflicts ---------------------------------------------------------------------------

def test_project_rows_revisions_conflicts_and_idempotent_replay(host):
    made = host.make()
    table = made["table"]
    assert (table["revision"], table["row_count"], table["versioning"]["state"]) == (1, 0, "pending")
    rows = [{"cells": {"name": "A <client>", "amount": 12.5}}, {"cells": {"name": "B", "details": {"channels": ["email"]}}}]
    key = new_id()
    saved = host.data("tables_rows_write", {"table_id": table["id"], "rows": rows}, expected_revision=1, idempotency_key=key)
    # Row-only edit: the table record takes a new revision although none of its own fields changed.
    assert (saved["table"]["revision"], saved["table"]["row_count"]) == (2, 2)
    assert host.data("tables_rows_write", {"table_id": table["id"], "rows": rows}, expected_revision=1, idempotency_key=key) == saved
    listed = host.read("tables_rows_list", {"table_id": table["id"]})
    assert [row["cells"]["name"] for row in listed["items"]] == ["A <client>", "B"] and listed["table_revision"] == 2
    first = saved["rows"][0]
    patched = host.write(table, [{"id": first["id"], "cells": {"amount": None}}], revision=2)
    assert patched["rows"][0]["cells"] == {"name": "A <client>", "amount": None} and patched["table"]["revision"] == 3
    assert host.error("tables_rows_write", {"table_id": table["id"], "rows": rows}, expected_revision=1) == "revision_conflict"
    for bad, code in (([{"cells": {"name": "ok"}}, {"cells": {"amount": True}}], "validation_error"),
                      ([{"id": new_id(), "cells": {"name": "missing"}}], "not_found")):
        assert host.error("tables_rows_write", {"table_id": table["id"], "rows": bad}, expected_revision=3) == code
    assert host.error("tables_rows_write", {"table_id": table["id"]}, expected_revision=3) == "validation_error"
    removed = host.write(table, delete=[first["id"]], revision=3)
    assert removed["deleted_ids"] == [first["id"]] and removed["table"]["row_count"] == 1 and removed["table"]["revision"] == 4
    assert [version["table_revision"] for version in host.versions()] == [1, 2, 3, 4]


def test_columns_types_filters_and_archive(host):
    table = host.make("Ledger", tags={"topics": ["sales"]})["table"]
    first = host.write(table, [{"cells": {"name": "00123", "amount": 1.0}}, {"cells": {"name": "B", "amount": 7}}], revision=1)
    ids = [row["id"] for row in first["rows"]]
    assert host.read("tables_rows_list", {"table_id": table["id"], "equals": {"amount": 1}})["items"][0]["cells"]["name"] == "00123"
    assert host.read("tables_rows_list", {"table_id": table["id"], "order_by": "amount", "descending": True})["items"][0]["id"] == ids[1]
    assert host.error("tables_rows_list", {"table_id": table["id"], "order_by": "details"}) == "validation_error"
    # Removing a column deletes its cells; re-adding the ID does not resurrect them. A type that no longer fits is refused.
    kept = [COLUMNS[0], COLUMNS[2]]
    host.data("tables_update", {"table_id": table["id"], "changes": {"columns": kept}}, expected_revision=2)
    host.data("tables_update", {"table_id": table["id"], "changes": {"columns": COLUMNS}}, expected_revision=3)
    assert host.read("tables_rows_list", {"table_id": table["id"]})["items"][0]["cells"] == {"name": "00123"}
    as_number = [{**COLUMNS[0], "type": "number"}, *COLUMNS[1:]]
    assert host.error("tables_update", {"table_id": table["id"], "changes": {"columns": as_number}}, expected_revision=4) == "validation_error"
    duplicate = [COLUMNS[0], {**COLUMNS[1], "name": " name "}]
    assert host.error("tables_update", {"table_id": table["id"], "changes": {"columns": duplicate}}, expected_revision=4) == "validation_error"
    found = host.read("tables_list", {"topic": "sales"})["items"]
    assert [item["id"] for item in found] == [table["id"]] and host.read("tables_list", {"query": "nothing"})["items"] == []
    archived = host.data("tables_archive", {"table_id": table["id"]}, expected_revision=4)["table"]
    assert archived["archived"] is True and archived["revision"] == 5
    assert host.error("tables_archive", {"table_id": table["id"]}, expected_revision=5) == "validation_error"
    assert host.error("tables_rows_write", {"table_id": table["id"], "rows": [{"cells": {"name": "x"}}]}, expected_revision=5) == "validation_error"
    assert host.read("tables_list", {})["items"] == [] and len(host.read("tables_list", {"include_archived": True})["items"]) == 1
    restored = host.data("tables_update", {"table_id": table["id"], "changes": {"archived": False}}, expected_revision=5)["table"]
    assert restored["archived"] is False


def test_pagination_binds_the_table_revision_and_scope(host):
    table = host.make()["table"]
    host.write(table, [{"cells": {"name": f"row {number}"}} for number in range(5)], revision=1)
    page = host.read("tables_rows_list", {"table_id": table["id"], "limit": 2})
    assert len(page["items"]) == 2 and page["next_cursor"]
    follow = host.read("tables_rows_list", {"table_id": table["id"], "limit": 2, "cursor": page["next_cursor"]})
    assert [row["cells"]["name"] for row in follow["items"]] == ["row 2", "row 3"]
    host.write(table, [{"cells": {"name": "late"}}], revision=2)
    assert host.error("tables_rows_list", {"table_id": table["id"], "limit": 2, "cursor": page["next_cursor"]}) in {"invalid_cursor", "validation_error"}


def test_task_links_on_create_and_scope_isolation(host):
    task = host.create_task("Quarter close")
    made = host.make("Linked", task_ids=[task["id"]])
    links = host.query("SELECT * FROM relationships WHERE role='concerns'")
    assert len(links) == 1 and made["table"]["title"] == "Linked"
    assert host.error("tables_create", {"title": "Bad", "columns": COLUMNS, "task_ids": [new_id()]}) == "not_found"
    other = host.make("Elsewhere", ctx=host.owner(business=OTHER))
    assert host.error("tables_get", {"table_id": other["table"]["id"]}) == "not_found"
    assert host.error("tables_get", {"table_id": other["table"]["id"]}, ctx=host.owner(business=BUSINESS)) == "not_found"
    assert host.read("tables_get", {"table_id": other["table"]["id"]}, ctx=host.owner(business=OTHER))["table"]["title"] == "Elsewhere"
    assert host.error("tables_get", {"table_id": made["table"]["id"], "scope": "global"}, ctx=global_owner(host)) == "not_found"


# -- Global tables ------------------------------------------------------------------------------------------------------

def test_global_tables_are_a_separate_namespace_with_their_own_program(host):
    ctx = global_owner(host)
    business = host.make("Business")
    shared = host.data("tables_create", {"title": "Shared", "columns": COLUMNS, "scope": "global"}, ctx)
    table = shared["table"]
    assert table["business_id"] is None
    assert host.error("tables_create", {"title": "G", "columns": COLUMNS, "scope": "global", "task_ids": [new_id()]}, ctx) == "validation_error"
    assert host.error("tables_rows_write", {"table_id": table["id"], "scope": "global",
                                            "rows": [{"cells": {"name": "x"}, "source_upload_id": new_id()}]}, ctx, expected_revision=1) == "validation_error"
    wrote = host.data("tables_rows_write", {"table_id": table["id"], "scope": "global", "rows": [{"cells": {"name": "G"}}]}, ctx, expected_revision=1)
    assert wrote["table"]["revision"] == 2 and wrote["rows"][0]["business_id"] is None
    assert [item["title"] for item in host.read("tables_list", {"scope": "global"}, ctx)["items"]] == ["Shared"]
    assert [item["title"] for item in host.read("tables_list", {})["items"]] == ["Business"]
    assert host.error("tables_get", {"table_id": table["id"]}) == "not_found"
    assert host.error("tables_get", {"table_id": business["table"]["id"], "scope": "global"}, ctx) == "not_found"
    # A project is never asked for its headquarters while Global history is located.
    assert BUSINESS in host.hq_calls
    kinds = {row["id"]: (row["kind"], row["business_id"]) for row in host.query("SELECT id, kind, business_id FROM jobs")}
    assert kinds[business["job_id"]] == ("tables.version", BUSINESS) and kinds[shared["job_id"]] == ("tables.global-version", None)
    host.run(shared["job_id"])
    pending = {(row["business_id"], row["id"]) for row in host.query("SELECT business_id, id FROM table_versions WHERE git_commit IS NULL")}
    assert pending == {(BUSINESS, business["table"]["versioning"]["version_id"]), (None, wrote["table"]["versioning"]["version_id"])}
    host.run(wrote["job_id"])
    assert host.git("rev-list", "--count", HISTORY_REF) == "2"
    assert (host.root / "files" / "tables" / (table["id"] + ".json")).is_file()
    host.run(business["job_id"])
    assert host.git("rev-list", "--count", HISTORY_REF) == "3"


def test_global_access_follows_registered_authority(host):
    ctx = global_owner(host)
    args = {"title": "Shared", "columns": COLUMNS, "scope": "global"}
    # Ordubashy holds the direct Global defaults.
    assert host.data("tables_create", args, host.coordinator())["table"]["business_id"] is None
    # Another eligible Global agent needs an explicit grant; its business defaults never carry over.
    assert host.error("tables_create", args, host.eligible()) == "policy_denied"
    host.grants[ELIGIBLE] = {"tables_create": "allow"}
    assert host.data("tables_create", {**args, "title": "Granted"}, host.eligible())["table"]["title"] == "Granted"
    # An agent bound to a project, or without the Global capability, has no Global access.
    assert host.error("tables_create", args, host.agent(AGENT)) in {"forbidden", "policy_denied"}
    assert host.error("tables_list", {"scope": "global"}, host.agent(AGENT2)) in {"forbidden", "policy_denied"}
    # The owner has direct authority; an explicit deny override on Ordubashy stays effective.
    host.grants[COORDINATOR] = {"tables_list": "deny"}
    assert host.error("tables_list", {"scope": "global"}, host.coordinator()) == "policy_denied"
    assert len(host.read("tables_list", {"scope": "global"}, ctx)["items"]) == 2


def test_global_archive_needs_the_owner_through_every_entry_point(host):
    ctx = global_owner(host)
    coordinator = host.coordinator()
    table = host.data("tables_create", {"title": "Shared", "columns": COLUMNS, "scope": "global"}, ctx)["table"]
    proposed = host.stage("tables_archive", {"table_id": table["id"], "scope": "global"}, coordinator, expected_revision=1)
    assert host.query("SELECT archived FROM data_tables WHERE id=?", table["id"])[0]["archived"] == 0
    applied = host.apply_approved(proposed, ctx)
    assert applied["state"] == "applied" and host.query("SELECT archived FROM data_tables WHERE id=?", table["id"])[0]["archived"] == 1
    # The same effect rides on a mixed update, and on creating an already archived table.
    again = host.data("tables_create", {"title": "Two", "columns": COLUMNS, "scope": "global"}, ctx)["table"]
    host.stage("tables_update", {"table_id": again["id"], "scope": "global", "changes": {"archived": True, "title": "Two!"}}, coordinator, expected_revision=1)
    host.stage("tables_create", {"title": "Born archived", "columns": COLUMNS, "scope": "global", "archived": True}, coordinator)
    # Restoring is an ordinary allowed update; the owner archives directly.
    restored = host.data("tables_update", {"table_id": table["id"], "scope": "global", "changes": {"archived": False}}, coordinator, expected_revision=2)
    assert restored["table"]["archived"] is False
    assert host.data("tables_archive", {"table_id": again["id"], "scope": "global"}, ctx, expected_revision=1)["table"]["archived"] is True


@pytest.mark.parametrize("role, scope, defaults, overrides, name, expected", [
    ("ordubashy", "global", {}, {}, "tables_get", ("allow", "global_table_role")),
    ("ordubashy", "global", {}, {}, "tables_archive", ("signoff", "global_table_archive_owner_approval")),
    ("ordubashy", "global", {}, {"tables_archive": "allow"}, "tables_archive", ("signoff", "global_table_archive_owner_approval")),
    ("ordubashy", "global", {}, {"tables_update": "deny"}, "tables_archive", ("deny", "global_table_archive_owner_approval")),
    ("ordubashy", "global", {}, {"tables_archive": "deny"}, "tables_archive", ("deny", "global_table_archive_owner_approval")),
    ("ordubashy", "global", {}, {"tables_update": "deny"}, "tables_update", ("deny", "agent_override")),
    ("global-worker", "global", {"tables_get": "allow", "tables_archive": "allow"}, {}, "tables_get", ("deny", "global_table_grant_required")),
    ("global-worker", "global", {"tables_archive": "allow"}, {}, "tables_archive", ("deny", "global_table_grant_required")),
    ("global-worker", "global", {}, {"tables_get": "allow"}, "tables_get", ("allow", "agent_override")),
    ("global-worker", "global", {}, {"tables_update": "allow"}, "tables_archive", ("signoff", "global_table_archive_owner_approval")),
    ("global-worker", "global", {}, {"tables_archive": "signoff"}, "tables_archive", ("signoff", "global_table_archive_owner_approval")),
    ("global-worker", "global", {}, {"tables_update": "deny", "tables_archive": "allow"}, "tables_archive", ("deny", "global_table_archive_owner_approval")),
    ("global-worker", "global", {}, {"tables_update": "allow", "tables_archive": "deny"}, "tables_archive", ("deny", "global_table_archive_owner_approval")),
    ("global-worker", "business", {"tables_update": "signoff"}, {}, "tables_archive", ("signoff", "role_default")),
    ("global-worker", "business", {"tables_update": "allow"}, {"tables_update": "deny"}, "tables_archive", ("deny", "agent_override")),
    ("global-worker", "business", {"tables_update": "deny"}, {"tables_archive": "allow"}, "tables_archive", ("allow", "agent_override")),
    ("global-worker", "business", {"tables_get": "allow"}, {}, "tables_get", ("allow", "role_default")),
])
def test_global_table_rule_decisions(host, role, scope, defaults, overrides, name, expected):
    rules = PolicyRules(before_override=(global_tables,))
    decision = resolve_operation_policy(name, defaults, overrides, host.registry, rules=rules, role_id=role, scope=scope)
    assert (decision.mode, decision.provenance) == expected


def test_the_rule_declines_every_other_operation(host):
    assert global_tables("tasks_get", {}, {}, host.registry, role_id="ordubashy", scope="global", resolve=None) is None
    assert global_tables("tables_get", {}, {}, host.registry, role_id="ordubashy", scope="business", resolve=None) is None


# -- stored plans stay scoped ------------------------------------------------------------------------------------------------

def test_a_tampered_plan_is_refused_by_its_own_scope_check(host):
    ctx = global_owner(host)
    options = type("Options", (), {"expected_revision": None})()
    with host.database.read() as uow:
        business_plan = host.tables.prepare("tables_create", host.owner(), {"title": "T", "columns": COLUMNS}, options, uow)
        global_plan = host.tables.prepare("tables_create", ctx, {"title": "T", "columns": COLUMNS, "scope": "global", "archived": True}, options, uow)
        with pytest.raises(DomainError) as crossed:
            host.tables.validate_scope(ctx, replace(business_plan, target_namespace="global"), uow)
        assert crossed.value.code == "invalid_plan"
        stripped = replace(global_plan, changes=tuple(change for change in global_plan.changes if change.action != "policy_effect"))
        with pytest.raises(DomainError) as unapproved:
            host.tables.validate_scope(ctx, stripped, uow)
        assert unapproved.value.code == "invalid_plan" and "archive approval" in str(unapproved.value)
        host.tables.validate_scope(ctx, global_plan, uow)


# -- history: snapshot bytes, retry and receipt reconciliation --------------------------------------------------------------------

def test_every_revision_is_committed_with_its_exact_snapshot_bytes(host):
    made = host.make()
    table = made["table"]
    one = host.write(table, [{"cells": {"name": "One"}}], revision=1)
    two = host.write(table, [{"id": one["rows"][0]["id"], "cells": {"name": "Two", "details": {"é": "ü"}}}], revision=2)
    captured = {row["id"]: (row["snapshot_json"], row["snapshot_hash"], row["path"]) for row in host.versions()}
    assert len(captured) == 3 and all(snapshot for snapshot, _, _ in captured.values())
    host.run(two["job_id"])   # the newest job publishes every earlier pending revision first, one commit each
    assert host.git("rev-list", "--count", HISTORY_REF) == "3"
    history = host.read("tables_history", {"table_id": table["id"]})["items"]
    assert [row["table_revision"] for row in history] == [3, 2, 1]
    for row in history:
        snapshot, digest, path = captured[row["id"]]
        blob = subprocess.run(["git", "-C", str(host.root), "show", row["git_commit"] + ":" + path], check=True, capture_output=True).stdout
        assert blob == TableHistory.bytes(snapshot) and canonical_hash(snapshot) == digest == row["snapshot_hash"]
        assert path == Path(host.query("SELECT hq_path FROM businesses WHERE id=?", BUSINESS)[0]["hq_path"]).relative_to(host.root).as_posix() + "/tables/" + table["id"] + ".json"
    host.run(made["job_id"])
    host.run(one["job_id"])
    assert host.git("rev-list", "--count", HISTORY_REF) == "3"
    assert not any(row["snapshot_json"] for row in host.versions())
    projection = json.loads((host.root / "first" / "hq" / "tables" / (table["id"] + ".json")).read_text())
    assert projection["rows"][0]["cells_json"] == {"name": "Two", "details": {"é": "ü"}} and projection["table"]["revision"] == 3
    # The publisher neither staged a file nor touched HEAD, the working index or any branch of the owner's repository.
    assert not (host.root / ".git" / "index").exists()
    assert host.git("symbolic-ref", "HEAD") != HISTORY_REF


def test_failed_git_keeps_the_pending_snapshot_and_retry_does_not_duplicate(host, monkeypatch):
    made = host.make()
    history = host.tables.history
    runner = history.runner
    def broken(*args, **kwargs):
        raise OSError("test Git failure")
    monkeypatch.setattr(history, "runner", broken)
    failed = host.background.run_one(made["job_id"])
    assert failed["error"]["code"] == "versioning_failed"
    version = host.versions()[0]
    assert version["snapshot_json"] and version["git_commit"] is None
    monkeypatch.setattr(history, "runner", runner)
    for _ in range(2):
        retry = host.data("tables_history_retry", {"version_id": version["id"]})
        host.run(retry["job_id"])
    assert host.git("rev-list", "--count", HISTORY_REF) == "1"
    assert host.versions()[0]["snapshot_json"] is None and host.versions()[0]["git_commit"]


def test_git_saved_but_sql_receipt_missing_is_reconciled_without_a_second_commit(host, monkeypatch):
    owner = host.root / "owner.txt"
    owner.write_text("Owner committed content\n")
    host.git("add", "owner.txt")
    host.git("-c", "user.name=Owner", "-c", "user.email=owner@example.test", "commit", "-qm", "Owner baseline")
    head = host.git("rev-parse", "HEAD")
    owner.write_text("Owner working content\n")
    made = host.make()
    history, receipt = host.tables.history, {}
    commit = history.commit
    def crash(version, control):
        receipt["commit"] = commit(version, control)
        raise OSError("Simulated stop after Git ref update, before the SQLite receipt")
    monkeypatch.setattr(history, "commit", crash)
    with pytest.raises(OSError, match="Simulated stop"):
        host.background.run_one(made["job_id"])
    assert host.query("SELECT state FROM jobs WHERE id=?", made["job_id"])[0]["state"] == "failed"
    assert host.versions()[0]["git_commit"] is None and host.versions()[0]["snapshot_json"]
    monkeypatch.setattr(history, "commit", commit)
    for _ in range(2):
        host.run(host.data("tables_history_retry", {"version_id": made["table"]["versioning"]["version_id"]})["job_id"])
    assert host.git("rev-list", "--count", HISTORY_REF) == "1" and host.git("rev-parse", HISTORY_REF) == receipt["commit"]
    assert host.versions()[0]["git_commit"] == receipt["commit"] and host.versions()[0]["snapshot_json"] is None
    assert host.git("rev-parse", "HEAD") == head and owner.read_text() == "Owner working content\n"


def test_a_marker_with_different_bytes_is_refused(host):
    made = host.make()
    host.run(made["job_id"])
    version = host.versions()[0]
    other = host.make("Other")
    forged = {**host.versions(BUSINESS)[1], "id": version["id"], "snapshot_json": {"schema_version": 1, "table": {}, "rows": []}}
    forged["snapshot_hash"] = canonical_hash(forged["snapshot_json"])
    control = type("Control", (), {"check": lambda self: None, "remaining_seconds": lambda self: 30})()
    with pytest.raises(DomainError) as refused:
        host.tables.history.commit(forged, control)
    assert refused.value.code == "versioning_failed" and other["job_id"]


def test_the_publisher_never_initializes_a_repository(tmp_path):
    host = TablesHost(tmp_path)
    made = host.make()
    # Remove the repository the fixture created: the publisher must fail, not create one or fall back to a parent.
    subprocess.run(["rm", "-rf", str(host.root / ".git")], check=True)
    parent = tmp_path / ".git"
    failed = host.background.run_one(made["job_id"])
    assert failed["error"]["code"] in {"versioning_failed", "versioning_unavailable"}
    assert not (host.root / ".git").exists() and not parent.exists()
    assert host.versions()[0]["snapshot_json"] and host.versions()[0]["git_commit"] is None


def test_an_oversized_snapshot_rolls_back_the_whole_batch(host, monkeypatch):
    table = host.make()["table"]
    host.write(table, [{"cells": {"name": "00123", "amount": 1.0}}], revision=1)
    monkeypatch.setattr("qorqut_operations.tables.operations.MAX_SNAPSHOT", 500)
    assert host.error("tables_rows_write", {"table_id": table["id"], "rows": [{"cells": {"name": "Oversized"}}]}, expected_revision=2) == "validation_error"
    current = host.read("tables_get", {"table_id": table["id"]})["table"]
    assert (current["revision"], current["row_count"]) == (2, 1) and len(host.read("tables_history", {"table_id": table["id"]})["items"]) == 2


# -- declarations the host splices ------------------------------------------------------------------------------------------------

def test_declarations_form_a_valid_schema_with_enforced_immutable_fields(host):
    assert set(TABLES) == {"data_tables", "data_rows", "table_versions"} and set(IMMUTABLE_FIELDS) <= set(TABLES)
    table = host.make()["table"]
    row = host.write(table, [{"cells": {"name": "A"}}], revision=1)["rows"][0]
    with host.database.transaction() as uow:
        with pytest.raises(DomainError):
            uow.repository("data_rows", business_id=BUSINESS).update(row["id"], {"table_id": new_id()})
    assert host.query("SELECT value FROM change_sequences WHERE name='tables'")[0]["value"] == 2
    assert {"data_tables", "data_rows"} <= {event["source_type"] for event in host.query("SELECT DISTINCT source_type FROM inbox_changes")}


def test_history_coverage_exports_only_the_declared_scalar_columns(host):
    made = host.make("Exported", tags={"topics": ["x"]})
    host.write(made["table"], [{"cells": {"name": "secret row"}}], revision=1)
    assert [export.table for export in EXPORTS] == ["data_tables"]
    assert {item.table for item in CLASSIFICATIONS} == {"data_rows", "table_versions"}
    catalog = ExportCatalog((HistoryContribution("tables", exports=EXPORTS, classifications=CLASSIFICATIONS),))
    with host.database.read() as uow:
        exported = catalog.export(uow)
    table = exported.tables["data_tables"]
    assert set(table.columns) == set(EXPORTS[0].columns) and "columns_json" not in table.columns
    assert [row["title"] for row in table.rows] == ["Exported"] and "secret row" not in json.dumps(plain(exported.tables), default=str)
    # Rows and revisions are classified as separately versioned, never exported.
    assert "data_rows" not in exported.tables and "table_versions" not in exported.tables
