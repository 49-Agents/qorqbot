"""CRM tables under a delegated assignment: the assignment's exact Task and Table record boundary on every embedded read.

The engine and its delegation authorization are the real ones. The trusted host's stored delegation rows and vocabulary are
``FakeDelegation`` from ``test_delegation`` (a labeled test provider), given the selector names the Task and Table
operations carry. A CRM row, a membership development or a saved receipt must never grant what a direct ``tasks_get`` of
the same Task refuses."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from crm_support import CRMHost, ref_of
from operational_support import AGENT2, BUSINESS
from qorqut_operations.contracts import DomainError, new_id, plain
from qorqut_operations.crm.access import ScopeCache
from test_delegation import ASSIGNMENT, BINDING, ROLE, FakeDelegation

READS = ("tasks_get", "tables_get", "tables_thread_get", "tables_rows_list", "crm_entries_list")
WRITES = ("crm_entries_add", "crm_entries_update", "tables_rows_write")
OUTSIDE, RESTRICTED = "Synthetic outside assignment", "Synthetic restricted cell"


@pytest.fixture
def host(tmp_path):
    return CRMHost(tmp_path)


def delegate(host, *, tables, tasks, allowed=READS):
    """Install the labeled delegation provider and return it with the delegated context of the parent agent."""
    provider = FakeDelegation(business_id=BUSINESS, allowed=list(allowed), record_ids={"data_tables": list(tables), "tasks": list(tasks)})
    provider.binding_row["parent_principal_id"] = AGENT2
    provider.selector_kinds = lambda operation: {"task_id": "tasks", "table_id": "data_tables"}
    host.app.delegation = provider
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET profile_json=json_set(profile_json, '$.scope.global_capabilities', json(?)) WHERE agent_id=?",
                               (json.dumps(["manage_delegated_assignments"]), AGENT2))
    ctx = replace(host.agent(AGENT2), role_artifact_ref=ROLE, delegation_binding_id=BINDING, delegation_assignment_id=ASSIGNMENT,
                  delegation_generation=1, global_capabilities=frozenset({"manage_delegated_assignments"}))
    return provider, ctx


def mixed(host):
    """One CRM holding an assigned Task and one outside the assignment (with a restricted local cell)."""
    table = host.crm_table("Assigned CRM")
    inside, outside = host.create_task("Synthetic assigned record"), host.create_task(OUTSIDE)
    first = host.add(table, inside)
    second = host.add(host.get(table), outside, cells={"notes": RESTRICTED})
    return table, inside, outside, first["entry"], second["entry"]


def listed(host, operation, table, ctx, **arguments):
    result = host.invoke(operation, {"table_id": table["id"], **arguments}, ctx)
    assert result.status == "ok", result.error
    return plain(result.data)


def test_a_delegated_read_of_a_crm_returns_only_the_task_records_the_assignment_names(host):
    table, inside, outside, first, second = mixed(host)
    provider, ctx = delegate(host, tables=[table["id"]], tasks=[inside["id"]])
    direct = host.invoke("tasks_get", {"task_id": outside["id"]}, ctx)
    assert direct.status == "error" and direct.error["code"] == "forbidden"
    assert host.invoke("tasks_get", {"task_id": inside["id"]}, ctx).status == "ok"
    # the dedicated read and its generic alias agree: the in-boundary Task is shown, the other is a bare placeholder
    for operation in ("crm_entries_list", "tables_rows_list"):
        page = listed(host, operation, table, ctx)
        assert [item.get("unavailable", False) for item in page["items"]] == [False, True]
        assert page["items"][0]["record"]["title"] == "Synthetic assigned record" and page["items"][0]["cells"]["name"] == "Synthetic assigned record"
        assert set(page["items"][1]) == {"id", "revision", "position", "unavailable", "removed"}
        assert OUTSIDE not in repr(page) and RESTRICTED not in repr(page) and outside["id"] not in repr(page)
    # a hidden row matches no search, no local-value filter and is not counted by either
    assert listed(host, "crm_entries_list", table, ctx, query="outside")["items"] == []
    assert listed(host, "crm_entries_list", table, ctx, query=RESTRICTED.lower())["items"] == []
    assert listed(host, "crm_entries_list", table, ctx, equals={"notes": RESTRICTED})["items"] == []
    assert [item["record"]["title"] for item in listed(host, "tables_rows_list", table, ctx, query="assigned")["items"]] == ["Synthetic assigned record"]
    # widening the assignment is honored on the next read, and narrowing it again hides the row again
    provider.assignment_row["scope_json"]["record_ids"]["tasks"].append(outside["id"])
    page = listed(host, "crm_entries_list", table, ctx)
    assert [item["record"]["title"] for item in page["items"]] == ["Synthetic assigned record", OUTSIDE]
    assert [item["local_cells"] for item in listed(host, "crm_entries_list", table, ctx, query=RESTRICTED.lower())["items"]] == [{"notes": RESTRICTED}]
    provider.assignment_row["scope_json"]["record_ids"]["tasks"].remove(outside["id"])
    assert OUTSIDE not in repr(listed(host, "crm_entries_list", table, ctx))


def test_the_table_thread_redacts_a_development_of_a_task_outside_the_assignment(host):
    table, inside, outside, first, second = mixed(host)
    provider, ctx = delegate(host, tables=[table["id"]], tasks=[inside["id"]])
    messages = listed(host, "tables_thread_get", table, ctx)["messages"]
    changes = {message["change"]["entry_id"]: message["change"] for message in messages if message.get("change")}
    assert changes[first["id"]]["record_label"] == "Synthetic assigned record"
    assert changes[second["id"]] == {"kind": "crm_membership", "entry_id": second["id"], "operation": "added", "unavailable": True}
    assert OUTSIDE not in repr(messages) and outside["id"] not in repr(messages)
    provider.assignment_row["scope_json"]["record_ids"]["tasks"].append(outside["id"])
    assert OUTSIDE in repr(listed(host, "tables_thread_get", table, ctx)["messages"])


def test_record_resolution_and_the_local_activity_follow_the_same_boundary(host):
    table, inside, outside, first, second = mixed(host)
    unassigned = host.crm_table("Unassigned CRM")           # also holds the assigned Task, but is not one of the assignment's Tables
    host.add(unassigned, inside)
    provider, ctx = delegate(host, tables=[table["id"]], tasks=[inside["id"]])
    with host.database.read() as uow:
        assert host.crm.resolve_record(ctx, ref_of(inside), uow)["title"] == "Synthetic assigned record"
        shown = host.crm.local_activity(ctx, inside["id"], {}, uow)["items"]
        assert [item["origin"] for item in shown] == ["crm_membership"] and shown[0]["source"]["id"] == table["id"]
        assert "Unassigned CRM" not in repr(shown)
        for stage in (lambda: host.crm.resolve_record(ctx, ref_of(outside), uow), lambda: host.crm.local_activity(ctx, outside["id"], {}, uow),
                      lambda: host.crm.access.require(ctx, ref_of(outside), uow)):
            with pytest.raises(DomainError) as refused:
                stage()
            assert refused.value.code == "forbidden"
    # a record-specific allow never carries over to the other Tasks of the scope, in one call that reads both
    with host.database.read() as uow:
        cache = ScopeCache()
        assert host.crm.access.readable(ctx, BUSINESS, inside["id"], uow, cache) and not host.crm.access.readable(ctx, BUSINESS, outside["id"], uow, cache)


def test_a_saved_receipt_is_refused_once_its_task_leaves_the_assignment(host):
    table = host.crm_table("Assigned CRM")
    inside, outside = host.create_task("Synthetic assigned record"), host.create_task(OUTSIDE)
    entry = host.add(table, inside)["entry"]
    # the parent agent made these calls before the assignment narrowed; the delegated call replays the same saved receipts
    parent = host.agent(AGENT2)
    add_key, update_key = new_id(), new_id()
    add = {"table_id": table["id"], "record": ref_of(inside)}
    update = {"table_id": table["id"], "entry_id": entry["id"], "changes": {"cells": {"notes": RESTRICTED}}}
    first = host.invoke("crm_entries_add", add, parent, expected_revision=2, idempotency_key=add_key)
    changed = host.invoke("crm_entries_update", update, parent, expected_revision=2, idempotency_key=update_key)
    assert first.status == changed.status == "ok"
    provider, ctx = delegate(host, tables=[table["id"]], tasks=[inside["id"]], allowed=(*READS, *WRITES))

    def replay(operation, arguments, key):
        return host.invoke(operation, arguments, ctx, expected_revision=2, idempotency_key=key)

    # the Task is in the assignment: exactly the saved receipts are returned, nothing is executed again
    assert plain(replay("crm_entries_add", add, add_key).data) == plain(first.data)
    assert plain(replay("crm_entries_update", update, update_key).data) == plain(changed.data)
    base = host.count("crm_entries"), host.count("table_versions"), host.count("thread_messages")
    # the Task is removed from the assignment's record_ids: both receipts are refused and show no Task data
    provider.assignment_row["scope_json"]["record_ids"]["tasks"].remove(inside["id"])
    for operation, arguments, key in (("crm_entries_add", add, add_key), ("crm_entries_update", update, update_key)):
        refused = replay(operation, arguments, key)
        assert refused.status == "error" and refused.error["code"] == "forbidden"
        assert "Synthetic assigned record" not in repr(refused) and RESTRICTED not in repr(refused)
    assert (host.count("crm_entries"), host.count("table_versions"), host.count("thread_messages")) == base
    provider.assignment_row["scope_json"]["record_ids"]["tasks"].append(inside["id"])
    assert replay("crm_entries_add", add, add_key).status == "ok"
    assert outside["id"] not in repr(first.data)
