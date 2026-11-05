"""CRM tables: atomic create-and-add, exact scopes, current authority in both scopes, and prepared-change revalidation."""

from __future__ import annotations

from dataclasses import replace

import pytest

from crm_support import CRM_COLUMNS, STAGES, CRMHost, ref_of
from operational_support import AGENT, AGENT2, BUSINESS, COORDINATOR, HUMAN, OTHER
from qorqut_operations.contracts import DomainError, new_id, plain


@pytest.fixture
def host(tmp_path):
    return CRMHost(tmp_path)


def counts(host):
    return {"tasks": host.count("tasks"), "entries": host.count("crm_entries"), "versions": host.count("table_versions"),
            "messages": host.count("thread_messages"), "follows": host.count("thread_follows")}


def tick(host):
    host.later(seconds=1)


# -- create-and-add is one atomic change ----------------------------------------------------------------------------------------

def test_creating_the_record_and_its_entry_is_one_change(host):
    table = host.crm_table()
    base = counts(host)
    made = host.add(table, create={"title": "Newco", "customer_profile": {"company": "Newco Ltd"}, "tags": []}, stage_id="lead", cells={"value": 2})
    assert made["created_record"] is True and made["table"]["revision"] == 2
    record = made["entry"]["record"]
    assert (record["task_type"], record["title"], made["entry"]["stage_id"]) == ("customer", "Newco", "lead")
    assert made["entry"]["record_ref"] == {"type": "task", "id": record["id"], "business_id": BUSINESS}
    after = counts(host)
    assert (after["tasks"] - base["tasks"], after["entries"] - base["entries"], after["versions"] - base["versions"]) == (1, 1, 1)
    # The new Task is an ordinary Task: its creator follows its thread, and it is listed by the Task service.
    assert host.query("SELECT reason FROM thread_follows WHERE item_type='task' AND item_id=?", record["id"])[0]["reason"] == "creator"
    assert record["id"] in {task["id"] for task in host.read("tasks_list")["tasks"]}
    story = host.add(made["table"], create={"title": "As story", "task_type": "story"})
    assert story["entry"]["record"]["task_type"] == "story"
    # Scope and presentation are not the entry's to choose.
    for extra in ({"scope": "global"}, {"focus": {"card": "x"}}):
        assert host.error("crm_entries_add", {"table_id": table["id"], "create": {"title": "Bad", **extra}}, expected_revision=3) == "validation_error"


def test_any_failure_leaves_neither_the_task_nor_the_entry(host, monkeypatch):
    table = host.crm_table()
    base = counts(host)
    arguments = {"table_id": table["id"], "create": {"title": "Doomed"}}
    assert host.error("crm_entries_add", {**arguments, "stage_id": "bogus"}, expected_revision=1) == "validation_error"
    assert host.error("crm_entries_add", {**arguments, "create": {"title": "Doomed", "fields": {"unknown": 1}}}, expected_revision=1) == "validation_error"
    assert host.error("crm_entries_add", {**arguments, "cells": {"value": "text"}}, expected_revision=1) == "validation_error"
    assert host.error("crm_entries_add", arguments, expected_revision=7) == "revision_conflict"
    host.grants[AGENT] = {"tasks_create": "deny"}
    assert host.error("crm_entries_add", arguments, host.agent(AGENT), expected_revision=1) == "policy_denied"
    host.grants[AGENT] = {"crm_entries_add": "deny"}
    assert host.error("crm_entries_add", arguments, host.agent(AGENT), expected_revision=1) == "policy_denied"
    assert counts(host) == base and host.get(table)["revision"] == 1

    def fail(*args, **kwargs):
        raise DomainError("internal_error", "injected failure after the Task was written")
    monkeypatch.setattr(host.crm.threads, "record_membership", fail)
    assert host.error("crm_entries_add", arguments, expected_revision=1) == "internal_error"
    assert counts(host) == base and host.get(table)["revision"] == 1


def test_a_replayed_create_returns_the_first_result_and_creates_one_task(host):
    table = host.crm_table()
    key = new_id()
    arguments = {"table_id": table["id"], "create": {"title": "Once"}}
    first = host.data("crm_entries_add", arguments, expected_revision=1, idempotency_key=key)
    again = host.data("crm_entries_add", arguments, expected_revision=1, idempotency_key=key)
    assert again == first and host.count("tasks") == 1 and host.count("crm_entries") == 1
    assert host.error("crm_entries_add", arguments, expected_revision=1) == "revision_conflict"


def test_global_and_business_crms_create_records_in_their_own_scope(host):
    owner = host.owner(business=None)
    table = host.data("tables_create", {"title": "G", "table_kind": "crm", "columns": CRM_COLUMNS, "crm_config": STAGES, "scope": "global"}, owner)["table"]
    made = host.data("crm_entries_add", {"table_id": table["id"], "scope": "global", "create": {"title": "Global co"}}, owner, expected_revision=1)
    assert made["entry"]["business_id"] is None and made["entry"]["record_ref"]["business_id"] is None
    assert host.task(made["entry"]["record"]["id"], business=None)["title"] == "Global co"
    assert host.count("tasks", "business_id IS NULL") == 1 and host.count("tasks", "business_id IS NOT NULL") == 0


# -- exact scopes ------------------------------------------------------------------------------------------------------------

def test_the_same_task_id_in_two_scopes_is_two_different_records(host):
    owner = host.owner(business=None)
    shared = "shared-id"
    business = host.data("tasks_create", {"title": "Business copy", "id": shared})["task"]
    elsewhere = host.data("tasks_create", {"title": "Global copy", "id": shared, "scope": "global"}, owner)["task"]
    table = host.crm_table()
    one = host.data("crm_entries_add", {"table_id": table["id"], "record": ref_of(business)}, expected_revision=1)
    two = host.data("crm_entries_add", {"table_id": table["id"], "record": ref_of(elsewhere)}, expected_revision=2)
    assert one["entry"]["id"] != two["entry"]["id"] and one["entry"]["cells"]["name"] == "Business copy" and two["entry"]["cells"]["name"] == "Global copy"
    shown = {item["record_ref"]["business_id"]: item["cells"]["name"] for item in host.entries(host.get(table))["items"]}
    assert shown == {BUSINESS: "Business copy", None: "Global copy"}
    removed = host.data("crm_entries_remove", {"table_id": table["id"], "entry_id": two["entry"]["id"]}, expected_revision=3)
    restored = host.data("crm_entries_add", {"table_id": table["id"], "record": ref_of(elsewhere)}, expected_revision=removed["table"]["revision"])
    assert restored["entry"]["id"] == two["entry"]["id"] and host.count("crm_entries") == 2
    # A reference names its scope exactly: the right id with the wrong scope is not that Task.
    lost = {"type": "task", "id": "only-global", "business_id": BUSINESS}
    host.data("tasks_create", {"title": "Only global", "id": "only-global", "scope": "global"}, owner)
    assert host.error("crm_entries_add", {"table_id": table["id"], "record": lost}, expected_revision=removed["table"]["revision"] + 1) == "not_found"


def test_global_crms_hold_business_records_and_business_crms_hold_global_ones(host):
    owner = host.owner(business=None)
    global_task = host.data("tasks_create", {"title": "Global co", "scope": "global"}, owner)["task"]
    business_task = host.create_task("Biz co")
    other_task = host.create_task("Other co", ctx=host.owner(business=OTHER))
    table = host.data("tables_create", {"title": "G", "table_kind": "crm", "columns": CRM_COLUMNS, "crm_config": STAGES, "scope": "global"}, owner)["table"]
    revision = 1
    for task in (global_task, business_task, other_task):
        revision = host.data("crm_entries_add", {"table_id": table["id"], "scope": "global", "record": ref_of(task)}, owner, expected_revision=revision)["table"]["revision"]
    listed = host.read("crm_entries_list", {"table_id": table["id"], "scope": "global"}, owner)["items"]
    assert [item["record_ref"]["business_id"] for item in listed] == [None, BUSINESS, OTHER] and all(item["business_id"] is None for item in listed)
    business_crm = host.crm_table()
    host.data("crm_entries_add", {"table_id": business_crm["id"], "record": ref_of(global_task)}, expected_revision=1)
    assert host.entries(business_crm)["items"][0]["cells"]["name"] == "Global co"
    # An agent with one selected project cannot reach the other project's Task, nor the Global one without the capability.
    agent = host.agent(AGENT)
    assert host.error("crm_entries_add", {"table_id": business_crm["id"], "record": ref_of(other_task)}, agent, expected_revision=2) == "forbidden"
    assert host.error("crm_entries_add", {"table_id": business_crm["id"], "record": ref_of(global_task)}, agent, expected_revision=2) == "forbidden"
    assert host.count("crm_entries", "business_id IS ?", BUSINESS) == 1


# -- current authority, recomputed on every read -------------------------------------------------------------------------------

def test_an_unreadable_task_is_a_bare_placeholder_that_no_filter_or_count_describes(host):
    owner = host.owner(business=None)
    secret = host.data("tasks_create", {"title": "Global secret", "scope": "global", "customer_profile": {"email": "x@y.test"}, "task_type": "customer"}, owner)["task"]
    visible = host.create_task("Visible Corp")
    table = host.crm_table()
    one = host.data("crm_entries_add", {"table_id": table["id"], "record": ref_of(secret), "cells": {"value": 4242}, "stage_id": "won"}, expected_revision=1)
    host.data("crm_entries_add", {"table_id": table["id"], "record": ref_of(visible), "cells": {"value": 5}}, expected_revision=2)
    agent = host.agent(AGENT)       # holds no Global capability
    items = host.entries(host.get(table), agent)["items"]
    assert items[0] == {"id": one["entry"]["id"], "revision": 1, "position": 0, "unavailable": True, "removed": False}
    assert items[1]["cells"]["name"] == "Visible Corp"
    text = repr(host.entries(host.get(table), agent))
    for leaked in ("Global secret", "x@y.test", "4242", secret["id"], "won"):
        assert leaked not in text, leaked
    # A filter or search can never select (or reveal by absence) a hidden target.
    for filters in ({"query": "secret"}, {"query": "Global"}, {"equals": {"value": 4242}}, {"stage_id": "won"}):
        assert host.entries(host.get(table), agent, **filters)["items"] == [], filters
    assert [item["id"] for item in host.entries(host.get(table), query="secret")["items"]] == [one["entry"]["id"]]
    # Sorting never lets a hidden entry's local value decide where it appears.
    for descending in (False, True):
        ordered = host.entries(host.get(table), agent, order_by="value", descending=descending)["items"]
        assert [bool(item.get("unavailable")) for item in ordered] == [False, True]


def test_revoking_and_restoring_task_access_changes_the_next_read(host):
    table = host.crm_table()
    task = host.create_task("Acme", task_type="customer", customer_profile={"email": "a@acme.test"})
    host.add(table, task)
    peer = host.agent(AGENT2)
    assert host.entries(host.get(table), peer)["items"][0]["cells"]["name"] == "Acme"
    host.grants[AGENT2] = {"tasks_get": "deny"}
    assert host.entries(host.get(table), peer)["items"][0].get("unavailable") is True
    host.grants[AGENT2] = {}
    assert host.entries(host.get(table), peer)["items"][0]["cells"]["email"] == "a@acme.test"


def test_writes_need_the_task_to_be_readable_except_removal(host):
    table = host.crm_table()
    task = host.create_task("Acme")
    added = host.add(table, task)
    revision = added["table"]["revision"]
    host.grants[AGENT2] = {"tasks_get": "deny"}
    peer = host.agent(AGENT2)
    other = host.create_task("Other")
    assert host.error("crm_entries_add", {"table_id": table["id"], "record": ref_of(other)}, peer, expected_revision=revision) == "policy_denied"
    assert host.error("crm_entries_update", {"table_id": table["id"], "entry_id": added["entry"]["id"], "changes": {"stage_id": "won"}},
                      peer, expected_revision=revision) == "policy_denied"
    assert host.get(table)["revision"] == revision
    # Taking something out of a Table needs no look at it.
    removed = host.data("crm_entries_remove", {"table_id": table["id"], "entry_id": added["entry"]["id"]}, peer, expected_revision=revision)
    assert removed["removed"] is True


def test_tables_of_other_projects_are_not_found(host):
    table = host.crm_table()
    task = host.create_task("Acme")
    elsewhere = host.owner(business=OTHER)
    assert host.error("crm_entries_list", {"table_id": table["id"]}, elsewhere) == "not_found"
    assert host.error("crm_entries_add", {"table_id": table["id"], "record": ref_of(task)}, elsewhere, expected_revision=1) == "not_found"
    assert host.error("tables_thread_get", {"table_id": table["id"]}, elsewhere) == "not_found"


def test_the_global_archive_floor_applies_to_crm_tables(host):
    owner = host.owner(business=None)
    table = host.data("tables_create", {"title": "G", "table_kind": "crm", "columns": CRM_COLUMNS, "crm_config": STAGES, "scope": "global"}, owner)["table"]
    coordinator = host.coordinator()
    result = host.invoke("tables_archive", {"table_id": table["id"], "scope": "global"}, coordinator, expected_revision=1)
    assert result.status == "signoff_required"
    # Membership writes on a Global CRM follow the same Global rule as its other operations: no grant, no access.
    task = host.data("tasks_create", {"title": "G co", "scope": "global"}, owner)["task"]
    assert host.error("crm_entries_add", {"table_id": table["id"], "scope": "global", "record": ref_of(task)}, host.eligible(), expected_revision=1) == "policy_denied"
    host.grants[COORDINATOR] = {}
    assert host.data("crm_entries_add", {"table_id": table["id"], "scope": "global", "record": ref_of(task)}, coordinator, expected_revision=1)["entry"]["business_id"] is None


# -- the new operations never open what their prerequisite decisions close ----------------------------------------------------------

def test_commenting_derives_from_the_table_write_decision_and_following_from_its_read_decision(host):
    table = host.crm_table()
    agent, comment = host.agent(AGENT), {"table_id": table["id"], "text": "Hello"}
    messages = host.count("thread_messages")
    assert host.data("tables_comment", comment, agent)["message"]["body"] == "Hello"
    # an allow on the new name never overrides a deny or a Signoff on the prerequisite
    host.grants[AGENT] = {"tables_comment": "allow", "tables_rows_write": "deny"}
    assert host.error("tables_comment", comment, agent) == "policy_denied"
    host.grants[AGENT] = {"tables_comment": "allow", "tables_rows_write": "signoff"}
    assert host.invoke("tables_comment", comment, agent).status == "signoff_required"
    assert host.count("thread_messages") == messages + 1
    # an explicit deny on the new name wins over an allowed prerequisite
    host.grants[AGENT] = {"tables_comment": "deny", "tables_rows_write": "allow"}
    assert host.error("tables_comment", comment, agent) == "policy_denied"
    # the discussion stays readable and followable without any right to change the Table's data
    host.grants[AGENT] = {"tables_rows_write": "deny", "crm_entries_add": "deny"}
    assert host.thread(table, agent)["messages"] and host.data("tables_follow", {"table_id": table["id"], "following": True}, agent)["following"] is True
    assert host.error("crm_entries_add", {"table_id": table["id"], "create": {"title": "No"}}, agent, expected_revision=1) == "policy_denied"
    # following is a read-authority preference: no read decision, no follow
    host.grants[AGENT] = {"tables_get": "deny"}
    assert host.error("tables_follow", {"table_id": table["id"], "following": False}, agent) == "policy_denied"


def test_a_staged_comment_is_applied_only_after_approval_and_still_works_on_an_archived_table(host):
    table = host.crm_table()
    host.data("tables_archive", {"table_id": table["id"]}, expected_revision=1)
    host.grants[AGENT] = {"tables_rows_write": "signoff"}
    agent = host.agent(AGENT)
    messages = host.count("thread_messages")
    # the prerequisite is a permission decision only: the archive does not close the discussion, the Signoff still gates it
    staged = host.stage("tables_comment", {"table_id": table["id"], "text": "Needs approval"}, agent)
    assert host.count("thread_messages") == messages
    assert host.apply_approved(staged)["state"] == "applied" and host.count("thread_messages") == messages + 1


def test_the_generic_row_read_of_a_crm_obeys_the_dedicated_operations_policy(host):
    crm_table, plain_table = host.crm_table(), host.make("Plain")["table"]
    task = host.create_task("Acme")
    host.add(crm_table, task)
    agent = host.agent(AGENT)
    assert len(host.read("tables_rows_list", {"table_id": crm_table["id"]}, agent)["items"]) == 1
    host.grants[AGENT] = {"crm_entries_list": "deny"}
    assert host.error("crm_entries_list", {"table_id": crm_table["id"]}, agent) == "policy_denied"
    assert host.error("tables_rows_list", {"table_id": crm_table["id"]}, agent) == "policy_denied"
    # an ordinary table has no dedicated operation and is unaffected
    assert host.read("tables_rows_list", {"table_id": plain_table["id"]}, agent)["items"] == []
    host.grants[AGENT] = {"tables_rows_list": "deny"}
    assert host.error("tables_rows_list", {"table_id": crm_table["id"]}, agent) == "policy_denied"


def test_create_and_add_needs_the_real_task_creation_decision_only_when_it_creates(host):
    table = host.crm_table()
    task = host.create_task("Existing")
    agent = host.agent(AGENT)
    # an allow on the membership never grants the new Task's creation: Signoff there stages the whole composed change
    host.grants[AGENT] = {"crm_entries_add": "allow", "tasks_create": "signoff"}
    base = {"tasks": host.count("tasks"), "entries": host.count("crm_entries")}
    staged = host.stage("crm_entries_add", {"table_id": table["id"], "create": {"title": "Staged"}}, agent, expected_revision=1)
    assert {"tasks": host.count("tasks"), "entries": host.count("crm_entries")} == base
    assert host.apply_approved(staged)["state"] == "applied"
    assert {"tasks": host.count("tasks"), "entries": host.count("crm_entries")} == {"tasks": base["tasks"] + 1, "entries": base["entries"] + 1}
    # pinning an existing Task needs no creation decision at all, even where creating one is denied
    host.grants[AGENT] = {"crm_entries_add": "allow", "tasks_create": "deny"}
    assert host.data("crm_entries_add", {"table_id": table["id"], "record": ref_of(task)}, agent, expected_revision=2)["entry"]["removed"] is False
    assert host.error("crm_entries_add", {"table_id": table["id"], "create": {"title": "Denied"}}, agent, expected_revision=3) == "policy_denied"
    assert host.count("tasks") == base["tasks"] + 1


# -- a saved receipt never outlives the access its result shows ----------------------------------------------------------------

def replayed(host, operation, arguments, ctx, key, revision):
    return host.invoke(operation, arguments, ctx, expected_revision=revision, idempotency_key=key)


def test_a_saved_add_or_update_receipt_is_refused_once_its_task_is_no_longer_readable(host):
    table = host.crm_table()
    task = host.create_task("Acme Secret Co")
    peer = host.agent(AGENT2)
    add, key = {"table_id": table["id"], "record": ref_of(task), "cells": {"notes": "Private note"}}, new_id()
    first = replayed(host, "crm_entries_add", add, peer, key, 1)
    assert first.status == "ok"
    # unchanged authority: exactly the first result, and nothing is written again
    base = counts(host)
    again = replayed(host, "crm_entries_add", add, peer, key, 1)
    assert again.status == "ok" and plain(again.data) == plain(first.data) and counts(host) == base
    entry_id = plain(first.data)["entry"]["id"]
    update, update_key = {"table_id": table["id"], "entry_id": entry_id, "changes": {"cells": {"notes": "Changed"}}}, new_id()
    changed = replayed(host, "crm_entries_update", update, peer, update_key, 2)
    assert changed.status == "ok" and replayed(host, "crm_entries_update", update, peer, update_key, 2).status == "ok"
    remove, remove_key = {"table_id": table["id"], "entry_id": entry_id}, new_id()
    assert replayed(host, "crm_entries_remove", remove, peer, remove_key, 3).status == "ok"
    base = counts(host)
    # the caller's Task read is revoked: every receipt that shows the Task is refused, never re-executed
    host.grants[AGENT2] = {"tasks_get": "deny"}
    for operation, arguments, number, saved in (("crm_entries_add", add, 1, key), ("crm_entries_update", update, 2, update_key)):
        refused = replayed(host, operation, arguments, peer, saved, number)
        assert refused.status == "error" and refused.error["code"] == "policy_denied"
        assert "Acme Secret Co" not in repr(refused) and "Private note" not in repr(refused) and "Changed" not in repr(refused)
    # a removal's result names no Task data, so it needs no Task read
    assert replayed(host, "crm_entries_remove", remove, peer, remove_key, 3).status == "ok"
    assert counts(host) == base
    host.grants[AGENT2] = {}
    assert replayed(host, "crm_entries_add", add, peer, key, 1).status == "ok"          # access restored: the receipt is served again


def test_a_saved_create_and_add_receipt_needs_the_created_task_and_its_composed_authority(host):
    table = host.crm_table()
    agent, key = host.agent(AGENT), new_id()
    arguments = {"table_id": table["id"], "create": {"title": "Fresh Lead"}}
    first = replayed(host, "crm_entries_add", arguments, agent, key, 1)
    assert first.status == "ok" and plain(first.data)["created_record"] is True
    base = counts(host)
    assert plain(replayed(host, "crm_entries_add", arguments, agent, key, 1).data) == plain(first.data) and counts(host) == base
    for grants in ({"tasks_get": "deny"}, {"tasks_create": "deny"}, {"tables_rows_write": "deny"}):
        host.grants[AGENT] = grants
        refused = replayed(host, "crm_entries_add", arguments, agent, key, 1)
        assert refused.status == "error" and refused.error["code"] == "policy_denied" and "Fresh Lead" not in repr(refused)
        assert counts(host) == base         # never a second Task and never a second entry
    host.grants[AGENT] = {}
    assert replayed(host, "crm_entries_add", arguments, agent, key, 1).status == "ok"


def test_a_saved_receipt_of_a_foreign_scope_task_is_refused_once_that_scope_is_lost_or_archived(host):
    table = host.crm_table()
    elsewhere = host.create_task("Other Scope Co", ctx=host.owner(business=OTHER))
    coordinator, key = host.coordinator(business=BUSINESS), new_id()
    arguments = {"table_id": table["id"], "record": ref_of(elsewhere)}
    first = replayed(host, "crm_entries_add", arguments, coordinator, key, 1)
    assert first.status == "ok" and plain(first.data)["entry"]["record"]["title"] == "Other Scope Co"
    # the caller loses access to the Task's project
    reduced = replace(coordinator, allowed_business_ids=frozenset({BUSINESS}))
    lost = replayed(host, "crm_entries_add", arguments, reduced, key, 1)
    assert lost.status == "error" and "Other Scope Co" not in repr(lost)
    # the Task's project is archived
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at='2026-10-13T00:00:00Z', archived_by=? WHERE id=?", (HUMAN, OTHER))
    archived = replayed(host, "crm_entries_add", arguments, coordinator, key, 1)
    assert archived.status == "error" and archived.error["code"] in {"forbidden", "business_archived"} and "Other Scope Co" not in repr(archived)
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at=NULL, archived_by=NULL WHERE id=?", (OTHER,))
    assert replayed(host, "crm_entries_add", arguments, coordinator, key, 1).status == "ok"
    assert host.count("crm_entries") == 1


# -- prepared changes are re-derived at approval ----------------------------------------------------------------------------------

def test_staged_changes_are_revalidated_when_approved(host):
    host.grants[AGENT] = {"crm_entries_add": "signoff", "crm_entries_update": "signoff"}
    table = host.crm_table()
    agent = host.agent(AGENT)
    task = host.create_task("Acme")
    # create-and-add, approved untouched: one Task, one entry
    staged = host.stage("crm_entries_add", {"table_id": table["id"], "create": {"title": "Staged"}}, agent, expected_revision=1)
    assert host.count("tasks") == 1
    assert host.apply_approved(staged)["state"] == "applied" and host.count("tasks") == 2 and host.count("crm_entries") == 1
    # the Table moved on after staging: the proposal is stale and writes nothing
    stale = host.stage("crm_entries_add", {"table_id": table["id"], "record": ref_of(task)}, agent, expected_revision=2)
    host.data("tables_update", {"table_id": table["id"], "changes": {"description": "moved"}}, expected_revision=2)
    assert host.approve(stale).status == "error" and host.row(stale)["state"] == "conflicted" and host.count("crm_entries") == 1
    # the Task changed after staging: the pinned Task revision no longer matches
    pinned = host.stage("crm_entries_add", {"table_id": table["id"], "record": ref_of(task)}, agent, expected_revision=3)
    host.data("tasks_update", {"task_id": task["id"], "changes": {"title": "Renamed"}}, expected_revision=task["revision"])
    assert host.approve(pinned).status == "error" and host.count("crm_entries") == 1
    # the proposer lost permission to read the Task before approval
    lost = host.stage("crm_entries_add", {"table_id": table["id"], "record": ref_of(task)}, agent, expected_revision=3)
    host.grants[AGENT] = {"crm_entries_add": "signoff", "tasks_get": "deny"}
    assert host.approve(lost).status == "error" and host.count("crm_entries") == 1
    # the target Task vanished from the plan's scope
    assert host.count("tasks") == 2


def test_a_tampered_plan_is_refused_by_the_crm_validator(host):
    table = host.crm_table()
    task, other = host.create_task("Acme"), host.create_task("Other")
    options = type("Options", (), {"expected_revision": 1})()
    ctx = host.owner()
    with host.database.read() as uow:
        add = host.crm.entries.prepare("crm_entries_add", ctx, {"table_id": table["id"], "record": ref_of(task)}, options, uow)
        create = host.crm.entries.prepare("crm_entries_add", ctx, {"table_id": table["id"], "create": {"title": "New"}}, options, uow)
        host.crm.entries.validate(ctx, add, uow)
        host.crm.entries.validate(ctx, create, uow)
        pointed = replace(add, arguments={**add.arguments, "record": ref_of(other)})
        with pytest.raises(DomainError) as moved:
            host.crm.entries.validate(ctx, pointed, uow)
        assert moved.value.code == "invalid_plan"
        smuggled = replace(add, changes=(*add.changes, *create.changes[:1]))
        with pytest.raises(DomainError) as extra:
            host.crm.entries.validate(ctx, smuggled, uow)
        assert extra.value.code == "invalid_plan"
        crossed = replace(create, changes=tuple(change for change in create.changes if change.resource.resource_type != "crm_entries"))
        with pytest.raises(DomainError) as headless:
            host.crm.entries.validate(ctx, crossed, uow)
        assert headless.value.code == "invalid_plan"
        elsewhere = replace(create, target_namespace="business:" + OTHER)
        with pytest.raises(DomainError):
            host.crm.entries.validate(ctx, elsewhere, uow)


def test_a_task_of_an_archived_project_is_unavailable_not_an_error(host):
    table = host.crm_table()
    elsewhere = host.create_task("Other co", ctx=host.owner(business=OTHER))
    mine = host.create_task("Mine")
    host.data("crm_entries_add", {"table_id": table["id"], "record": ref_of(elsewhere)}, expected_revision=1)
    host.data("crm_entries_add", {"table_id": table["id"], "record": ref_of(mine)}, expected_revision=2)
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at='2026-10-13T00:00:00Z', archived_by=? WHERE id=?", (HUMAN, OTHER))
    items = host.entries(host.get(table))["items"]
    assert [bool(item.get("unavailable")) for item in items] == [True, False] and "Other co" not in repr(items)
    messages = host.thread(table)["messages"]
    assert sorted(bool(m["change"].get("unavailable")) for m in messages) == [False, True]
    assert host.error("crm_entries_add", {"table_id": table["id"], "record": ref_of(elsewhere)}, expected_revision=3) == "business_archived"
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at=NULL, archived_by=NULL WHERE id=?", (OTHER,))
    assert [item.get("cells", {}).get("name") for item in host.entries(host.get(table))["items"]] == ["Other co", "Mine"]


def test_a_stage_change_and_a_task_state_change_are_independent(host):
    table = host.crm_table()
    task = host.create_task("Acme", task_type="customer", customer_state="prospect")
    added = host.add(table, task, stage_id="lead")
    before = host.task(task["id"])
    moved = host.data("crm_entries_update", {"table_id": table["id"], "entry_id": added["entry"]["id"], "changes": {"stage_id": "won"}},
                      expected_revision=added["table"]["revision"])
    assert moved["entry"]["stage_id"] == "won" and host.task(task["id"]) == before
    host.data("tasks_update", {"task_id": task["id"], "changes": {"customer_state": "customer"}}, expected_revision=task["revision"])
    shown = host.entries(host.get(table))["items"][0]
    assert shown["stage_id"] == "won" and shown["record"]["customer_state"] == "customer" and host.get(table)["revision"] == moved["table"]["revision"]
