"""Composed CRM integration: the reviewed Operations Domain inside the real host composition.

Nothing here is a double. The instance is the host's own assembled target (storage contributions, catalog, policy rules and
change feed), the CRM is ``qorqut_operations.crm`` as composed by ``bazarlyk.domains``, and every flow below runs through
``Application.invoke`` with the host's real prerequisite policy, Signoffs and idempotency receipts over a real SQLite store.
These are the cross-repository seams the source slices could only label as pending: the real Domain's operation envelopes
against the host's catalog/policy, membership atomicity and revisions, the shared Table discussion with the host's
``EventFinalizer``, the owner channel rule for recorded observations and the private/shared separation in the aggregate
activity read."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import InvocationOptions, Principal, new_id, plain

from bazarlyk.background.integration import attach_background
from host_foundation_support import (build_instance, create_registered_global, create_worker, owner_global_context,
                                     register_sources, select_coordinator, set_agent_overrides)

STAGES = {"stages": [{"id": "lead", "name": "Lead"}, {"id": "won", "name": "Won"}]}
COLUMNS = [{"id": "name", "name": "Name", "type": "text", "record_field": "title"},
           {"id": "email", "name": "Email", "type": "text", "record_field": "customer_profile.email"},
           {"id": "value", "name": "Value", "type": "number"},
           {"id": "notes", "name": "Notes", "type": "text"}]
# The host policy decisions an agent needs for the flows; the CRM names derive from the prerequisites (Amendment 002).
GRANTS = {"tables_get": "allow", "tables_rows_list": "allow", "tables_rows_write": "allow", "tasks_get": "allow",
          "tasks_create": "allow", "tasks_comment": "allow", "crm_entries_list": "allow", "crm_entries_add": "allow",
          "crm_entries_update": "allow", "crm_entries_remove": "allow", "tables_thread_get": "allow",
          "tables_comment": "allow", "tables_follow": "allow", "crm_activity_list": "allow"}


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    instance, ids = build_instance(tmp_path_factory.mktemp("crm-integration"), businesses=("first", "second"))
    first, second = ids["first"], ids["second"]
    register_sources(instance, first, "first")
    register_sources(instance, second, "second")
    coordinator = select_coordinator(instance, create_registered_global(instance, "ordubashy"))
    worker = create_worker(instance, "worker-a")
    for identity in (coordinator, worker):
        set_agent_overrides(instance, identity, GRANTS, (first, second))
    attach_background(instance.runtime, instance.runtime.programs)
    with instance.database.transaction() as uow:
        instance.runtime.authority.register_workers(uow, [first, second])
    instance.runtime.freeze_registry()
    state = SimpleNamespace(instance=instance, runtime=instance.runtime, database=instance.database, first=first, second=second,
                            coordinator=coordinator, worker=worker)
    try:
        yield state
    finally:
        instance.runtime.release_instance()
        instance.database.close()


# ----- helpers -----------------------------------------------------------------------------------------------------------


def invoke(host, ctx, name, arguments, *, revision=None, key=None):
    return host.runtime.application.invoke(ctx, name, arguments,
                                           InvocationOptions(idempotency_key=key or new_id(), expected_revision=revision))


def ok(result):
    assert result.status == "ok", result.error
    return plain(result.data)


def code(result):
    assert result.status == "error", result.data
    return result.error["code"]


def owner(host, business):
    return host.instance.human(business) if business else owner_global_context(host.instance)


def agent(host, identity, business):
    return host.runtime.resolver.invocation_context(Principal(identity, "agent"), business_id=business)


def scoped(business, **arguments):
    return {"scope": "business" if business else "global", **arguments}


def query(host, sql, *parameters):
    with host.database.read() as uow:
        return [tuple(row) for row in uow.connection.execute(sql, parameters)]


def count(host, table):
    return query(host, f"SELECT COUNT(*) FROM {table}")[0][0]


def revision_of(host, table_id, business):
    return query(host, "SELECT revision FROM data_tables WHERE business_id IS ? AND id=?", business, table_id)[0][0]


def make_crm(host, business, title="Pipeline"):
    return ok(invoke(host, owner(host, business), "tables_create",
                     scoped(business, title=title, table_kind="crm", columns=COLUMNS, crm_config=STAGES)))["table"]


def make_task(host, business, title="Record", **fields):
    return ok(invoke(host, owner(host, business), "tasks_create", scoped(business, title=title, **fields)))["task"]


def add(host, ctx, table, business, *, revision=None, key=None, **arguments):
    return invoke(host, ctx, "crm_entries_add", scoped(business, table_id=table["id"], **arguments),
                  revision=revision if revision is not None else table["revision"], key=key)


def ref(task, business):
    return {"type": "task", "id": task["id"] if isinstance(task, dict) else task, "business_id": business}


def entries(host, ctx, table, business, **arguments):
    return ok(invoke(host, ctx, "crm_entries_list", scoped(business, table_id=table["id"], **arguments)))


# ----- creation and the kind boundary --------------------------------------------------------------------------------------


def test_a_composed_crm_carries_its_kind_stages_and_bound_columns_and_refuses_ordinary_rows(host):
    business = host.first
    crm = make_crm(host, business, "Composed pipeline")
    assert crm["table_kind"] == "crm" and crm["crm_config"] == STAGES and crm["row_count"] == 0
    assert [(column["id"], column.get("record_field")) for column in crm["columns"]] == [
        ("name", "title"), ("email", "customer_profile.email"), ("value", None), ("notes", None)]
    general = ok(invoke(host, owner(host, business), "tables_create",
                        scoped(business, title="Ordinary", columns=[{"id": "name", "name": "Name", "type": "text"}])))["table"]
    assert general["table_kind"] == "general" and general["crm_config"] is None
    # The kind is fixed at creation, ordinary rows never become records, and the dedicated operation is named.
    assert code(invoke(host, owner(host, business), "tables_update",
                       scoped(business, table_id=general["id"], changes={"table_kind": "crm"}),
                       revision=general["revision"])) == "validation_error"
    refused = invoke(host, owner(host, business), "tables_rows_write",
                     scoped(business, table_id=crm["id"], rows=[{"cells": {"value": 1}}]), revision=crm["revision"])
    assert code(refused) == "validation_error" and "crm_entries_add" in json.dumps(plain(refused.error))
    assert ok(invoke(host, owner(host, business), "tables_rows_list", scoped(business, table_id=crm["id"])))["items"] == []
    listed = ok(invoke(host, owner(host, business), "tables_list", scoped(business, table_kind="crm")))["items"]
    assert [item["id"] for item in listed] == [crm["id"]]


def test_create_and_add_is_one_transaction_with_one_revision_and_an_exact_replay(host):
    business = host.first
    crm = make_crm(host, business, "Atomic")
    versions = count(host, "table_versions")
    key = new_id()
    first = ok(add(host, owner(host, business), crm, business,
                   create={"title": "Acme Ltd", "task_type": "customer", "customer_kind": "company"}, cells={"value": 1200},
                   stage_id="lead", key=key))
    assert first["created_record"] is True
    entry = first["entry"]
    assert (entry["table_id"], entry["business_id"], entry["stage_id"], entry["removed"]) == (crm["id"], business, "lead", False)
    assert entry["cells"]["name"] == "Acme Ltd" and entry["cells"]["value"] == 1200 and entry["record_ref"]["id"] == entry["record"]["id"]
    assert entry["record"]["task_type"] == "customer" and entry["record"]["customer_kind"] == "company"
    # One changing batch: one Table revision and one version, and the new Task keeps the Table's own scope.
    assert revision_of(host, crm["id"], business) == crm["revision"] + 1
    assert count(host, "table_versions") == versions + 1
    assert query(host, "SELECT business_id FROM tasks WHERE id=?", entry["record"]["id"]) == [(business,)]
    # A replay with the same key returns the first result and writes nothing at all.
    tables, tasks, versions = count(host, "data_tables"), count(host, "tasks"), count(host, "table_versions")
    again = add(host, owner(host, business), crm, business,
                create={"title": "Acme Ltd", "task_type": "customer", "customer_kind": "company"}, cells={"value": 1200},
                stage_id="lead", key=key)
    assert again.status == "ok" and plain(again.data)["entry"]["id"] == entry["id"]
    assert code(add(host, owner(host, business), crm, business, create={"title": "Different"}, key=key)) == "idempotency_conflict"
    assert (count(host, "data_tables"), count(host, "tasks"), count(host, "table_versions")) == (tables, tasks, versions)
    # The same record is not added twice: an active membership comes back unchanged, different initial values conflict.
    repeat = add(host, owner(host, business), {"id": crm["id"], "revision": revision_of(host, crm["id"], business)},
                 business, record=entry["record_ref"])
    assert repeat.status == "ok" and plain(repeat.data)["entry"]["id"] == entry["id"]
    assert code(add(host, owner(host, business), {"id": crm["id"], "revision": revision_of(host, crm["id"], business)},
                    business, record=entry["record_ref"], cells={"value": 9999})) == "conflict"


# ----- existing records ----------------------------------------------------------------------------------------------------


def test_existing_task_story_customer_and_historical_email_records_are_linked_without_conversion(host):
    business = host.first
    crm = make_crm(host, business, "All subtypes")
    plain_task = make_task(host, business, "Plain task")
    story = make_task(host, business, "A story", task_type="story")
    customer = make_task(host, business, "Acme", task_type="customer", customer_profile={"email": "ops@acme.test"})
    # A historical Email Task: a stored binding from an upgraded database, read-only here exactly as it is in core.
    identity = new_id()
    with host.database.transaction() as uow:
        uow.repository("tasks", business_id=business).insert({
            "id": identity, "title": "Old thread", "status": "open", "created_by": host.instance.owner,
            "fields_json": {"$task": {"task_type": "email", "overview": "", "email_conversation_id": new_id()}}})
    email = {"id": identity, "business_id": business, "title": "Old thread"}
    before = {task["id"]: query(host, "SELECT fields_json FROM tasks WHERE business_id IS ? AND id=?", business, task["id"])
              for task in (plain_task, story, customer, email)}
    revision = crm["revision"]
    for reference in (ref(plain_task, business), ref(story, business), ref(customer, business), ref(email, business)):
        added = ok(add(host, owner(host, business), {"id": crm["id"], "revision": revision}, business, record=reference))
        revision = added["table"]["revision"]
        assert added["created_record"] is False and added["entry"]["record_ref"] == reference
    items = entries(host, owner(host, business), {"id": crm["id"], "revision": revision}, business)["items"]
    assert [item["record"]["task_type"] for item in items] == ["task", "story", "customer", "email"]
    # Linking changed no Task field, parent, state or owner.
    assert {task_id: query(host, "SELECT fields_json FROM tasks WHERE business_id IS ? AND id=?", business, task_id)
            for task_id in before} == before
    assert query(host, "SELECT COUNT(*) FROM tasks WHERE parent_id IS NOT NULL")[0][0] == 0
    # A new Email Task still needs its supported binding: CRM is no route around the existing validation.
    assert code(invoke(host, owner(host, business), "tasks_create", scoped(business, title="Fresh thread", task_type="email"))) != "ok"


def test_shared_record_fields_are_read_live_and_local_cells_and_stages_stay_per_crm(host):
    business = host.first
    left = make_crm(host, business, "Left")
    right = make_crm(host, business, "Right")
    customer = make_task(host, business, "Shared Co", task_type="customer", customer_profile={"email": "hello@shared.test"})
    reference = ref(customer, business)
    one = ok(add(host, owner(host, business), left, business, record=reference, cells={"notes": "Left only"}, stage_id="lead"))["entry"]
    two = ok(add(host, owner(host, business), right, business, record=reference, cells={"notes": "Right only"}, stage_id="won"))["entry"]
    assert (one["id"], two["id"]) != (one["table_id"], two["table_id"]) and one["id"] != two["id"]
    assert (one["cells"]["name"], two["cells"]["name"]) == ("Shared Co", "Shared Co")
    assert (one["cells"]["notes"], two["cells"]["notes"]) == ("Left only", "Right only")
    assert (one["stage_id"], two["stage_id"]) == ("lead", "won")
    # The shared fields are the record's current values, never a copy: a Task edit shows in both CRM reads and writes no
    # Table revision, and neither membership's local cells move.
    table_revisions = (revision_of(host, left["id"], business), revision_of(host, right["id"], business))
    current = ok(invoke(host, agent(host, host.coordinator, business), "tasks_get", scoped(business, task_id=customer["id"])))["task"]
    ok(invoke(host, agent(host, host.coordinator, business), "tasks_update",
              scoped(business, task_id=customer["id"], changes={"title": "Shared Co renamed", "customer_profile": {"email": "new@shared.test"}}),
              revision=current["revision"]))
    assert (revision_of(host, left["id"], business), revision_of(host, right["id"], business)) == table_revisions
    items_left = {item["record_ref"]["id"]: item for item in entries(host, owner(host, business), left, business)["items"]}
    items_right = entries(host, owner(host, business), right, business)["items"]
    assert items_left[customer["id"]]["cells"] == {"name": "Shared Co renamed", "email": "new@shared.test", "notes": "Left only"}
    assert items_right[0]["cells"] == {"name": "Shared Co renamed", "email": "new@shared.test", "notes": "Right only"}
    assert items_left[customer["id"]]["stage_id"] == "lead" and items_right[0]["stage_id"] == "won"


# ----- removal, revival and structural edits -------------------------------------------------------------------------------


def test_removal_is_soft_revival_keeps_the_identity_and_the_record_and_thread_survive(host):
    business = host.first
    crm = make_crm(host, business, "Revive")
    added = ok(add(host, owner(host, business), crm, business, create={"title": "Acme"}, cells={"notes": "kept"}, stage_id="lead"))
    entry, record = added["entry"], added["entry"]["record"]
    thread_before = count(host, "thread_messages")
    removed = ok(invoke(host, owner(host, business), "crm_entries_remove",
                        scoped(business, table_id=crm["id"], entry_id=entry["id"]), revision=added["table"]["revision"]))
    assert removed["removed"] is True and removed["entry_id"] == entry["id"]
    # The Task and every message are untouched; the default listing hides the removed membership.
    assert query(host, "SELECT COUNT(*) FROM tasks WHERE id=?", record["id"])[0][0] == 1
    assert count(host, "thread_messages") == thread_before + 1          # the removal development itself
    assert query(host, "SELECT removed FROM crm_entries WHERE id=?", entry["id"]) == [(1,)]
    assert entries(host, owner(host, business), crm, business)["items"] == []
    archived = entries(host, owner(host, business), crm, business, include_removed=True)["items"]
    assert [item["id"] for item in archived] == [entry["id"]] and archived[0]["removed"] is True
    # Adding the same Task again revives that membership: same identity, local data unless explicitly replaced.
    revived = ok(add(host, owner(host, business), {"id": crm["id"], "revision": revision_of(host, crm["id"], business)},
                     business, record=entry["record_ref"]))
    restored = revived["entry"]
    assert restored["id"] == entry["id"] and restored["removed"] is False and restored["cells"]["notes"] == "kept"
    assert restored["stage_id"] == "lead"
    assert query(host, "SELECT COUNT(*) FROM crm_entries WHERE business_id IS ? AND table_id=?", business, crm["id"])[0][0] == 1


def test_stage_and_local_cell_edits_are_revision_checked_batches(host):
    business = host.first
    crm = make_crm(host, business, "Edits")
    added = ok(add(host, owner(host, business), crm, business, create={"title": "Acme"}, cells={"notes": "one"}))
    entry, table = added["entry"], added["table"]
    updated = ok(invoke(host, owner(host, business), "crm_entries_update",
                        scoped(business, table_id=crm["id"], entry_id=entry["id"], changes={"cells": {"notes": None, "value": 5}}),
                        revision=table["revision"]))
    assert updated["entry"]["cells"]["notes"] is None and updated["entry"]["cells"]["value"] == 5
    assert updated["entry"]["revision"] == entry["revision"] + 1 and updated["table"]["revision"] == table["revision"] + 1
    assert code(invoke(host, owner(host, business), "crm_entries_update",
                       scoped(business, table_id=crm["id"], entry_id=entry["id"], changes={"cells": {"nope": 1}}),
                       revision=updated["table"]["revision"])) == "validation_error"
    # A projected key is never written through the membership.
    assert code(invoke(host, owner(host, business), "crm_entries_update",
                       scoped(business, table_id=crm["id"], entry_id=entry["id"], changes={"cells": {"name": "Fake"}}),
                       revision=updated["table"]["revision"])) == "validation_error"
    assert code(invoke(host, owner(host, business), "crm_entries_update",
                       scoped(business, table_id=crm["id"], entry_id=entry["id"], changes={"stage_id": "no-such-stage"}),
                       revision=updated["table"]["revision"])) == "validation_error"
    # A stale Table revision is the batch conflict, and nothing was written.
    assert code(invoke(host, owner(host, business), "crm_entries_update",
                       scoped(business, table_id=crm["id"], entry_id=entry["id"], changes={"cells": {"value": 6}}),
                       revision=table["revision"])) == "revision_conflict"
    assert query(host, "SELECT cells_json FROM crm_entries WHERE id=?", entry["id"])[0][0] != json.dumps({"value": 6})


# ----- prepared Signoff and revalidation -----------------------------------------------------------------------------------


def test_a_staged_create_and_add_is_one_frozen_change_revalidated_at_approval(host):
    business = host.first
    crm = make_crm(host, business, "Staged")
    staged_agent = host.worker
    set_agent_overrides(host.instance, staged_agent, {**GRANTS, "tasks_create": "signoff"}, (host.first, host.second))
    try:
        tasks, memberships = count(host, "tasks"), count(host, "crm_entries")
        staged = add(host, agent(host, staged_agent, business), crm, business, create={"title": "Needs approval"})
        assert staged.status == "signoff_required", staged.error
        assert (count(host, "tasks"), count(host, "crm_entries")) == (tasks, memberships)
        owner_ctx = owner(host, business)
        proposal = ok(host.runtime.signoffs.get(owner_ctx, staged.signoff["id"]))["signoff"]
        targets = {item["resource"]["resource_type"] for item in proposal["expected_targets"]}
        assert {"data_tables"} <= targets                     # the frozen Table target is part of the change
        # Revalidated: with the creation right withdrawn, the approval is refused and neither half is written.
        set_agent_overrides(host.instance, staged_agent, {**GRANTS, "tasks_create": "deny"}, (host.first, host.second))
        refused = host.runtime.signoffs.review(owner_ctx, staged.signoff["id"], decision="approve",
                                               proposal_revision=proposal["proposal_revision"], payload_hash=proposal["payload_hash"],
                                               expected_status_revision=proposal["status_revision"], idempotency_key=new_id())
        assert refused.status == "error", refused
        assert (count(host, "tasks"), count(host, "crm_entries")) == (tasks, memberships)
        assert ok(host.runtime.signoffs.get(owner_ctx, proposal["id"]))["signoff"]["state"] == "conflicted"
        # A fresh proposal under restored authority is accepted, then the local Signoff executor applies both halves.
        set_agent_overrides(host.instance, staged_agent, {**GRANTS, "tasks_create": "signoff"}, (host.first, host.second))
        fresh = add(host, agent(host, staged_agent, business), crm, business, create={"title": "Needs approval"})
        assert fresh.status == "signoff_required", fresh.error
        proposal = fresh.signoff
        accepted = host.runtime.signoffs.review(owner_ctx, proposal["id"], decision="approve",
                                                proposal_revision=proposal["proposal_revision"], payload_hash=proposal["payload_hash"],
                                                expected_status_revision=proposal["status_revision"], idempotency_key=new_id())
        assert accepted.status == "accepted", accepted.error
        assert (count(host, "tasks"), count(host, "crm_entries")) == (tasks, memberships)
        applied = host.runtime.signoffs.run_one(proposal["id"])
        assert applied is not None and applied["state"] == "applied", applied
        assert (count(host, "tasks"), count(host, "crm_entries")) == (tasks + 1, memberships + 1)
        entry = applied["apply_result"]["data"]["entry"]
        assert entry["record"]["title"] == "Needs approval"
        assert host.runtime.signoffs.run_one(proposal["id"]) is None
        assert (count(host, "tasks"), count(host, "crm_entries")) == (tasks + 1, memberships + 1)
    finally:
        set_agent_overrides(host.instance, staged_agent, GRANTS, (host.first, host.second))


# ----- Table discussion, capture and the owner channel -----------------------------------------------------------------------


def test_a_table_comment_is_one_semantic_change_with_one_canonical_source_and_a_sticky_unfollow(host):
    business = host.first
    crm = make_crm(host, business, "Discussion")
    member = ok(add(host, owner(host, business), crm, business, create={"title": "Acme"}))["entry"]["record_ref"]
    reader = agent(host, host.coordinator, business)
    # Another authorized follower receives the comment; the commenter auto-follows without notifying itself.
    follower = agent(host, host.worker, business)
    ok(invoke(host, follower, "tables_follow", scoped(business, table_id=crm["id"], following=True)))
    events, deliveries = count(host, "inbox_events"), count(host, "inbox_deliveries")
    commented = ok(invoke(host, reader, "tables_comment", scoped(business, table_id=crm["id"], text="Team note")))
    assert commented["message"]["body"] == "Team note" and commented["thread_id"]
    # The real EventFinalizer drains transactional captures into canonical events and authorized deliveries.
    assert count(host, "inbox_changes") == 0
    # One event, its source the canonical Table identity (never a membership, a Task or a derived id) and its type the note.
    delivered = query(host, "SELECT source_type, source_id, event_type FROM inbox_events ORDER BY id DESC LIMIT 1")
    assert delivered == [("data_tables", crm["id"], "note_added")]
    assert count(host, "inbox_events") == events + 1
    assert count(host, "inbox_deliveries") == deliveries + 1
    assert query(host, "SELECT d.agent_id FROM inbox_deliveries d JOIN inbox_events e ON e.id=d.event_id "
                      "WHERE e.source_type='data_tables' AND e.source_id=? AND e.event_type='note_added'", crm["id"]) == [(host.worker,)]
    assert query(host, "SELECT COUNT(*) FROM inbox_events WHERE source_type='data_tables' AND source_id=?", crm["id"])[0][0] == 1
    # Sticky unfollow: an explicit false is not overwritten by the commenter rule and nothing is delivered again.
    ok(invoke(host, reader, "tables_follow", scoped(business, table_id=crm["id"], following=False)))
    ok(invoke(host, follower, "tables_follow", scoped(business, table_id=crm["id"], following=False)))
    assert ok(invoke(host, reader, "tables_thread_get", scoped(business, table_id=crm["id"])))["following"] is False
    deliveries = count(host, "inbox_deliveries")
    ok(invoke(host, reader, "tables_comment", scoped(business, table_id=crm["id"], text="Second note")))
    assert count(host, "inbox_deliveries") == deliveries
    thread = ok(invoke(host, reader, "tables_thread_get", scoped(business, table_id=crm["id"])))
    assert [message["body"] for message in thread["messages"] if message["kind"] == "comment"] == ["Team note", "Second note"]
    assert thread["following"] is False
    # The member record's own activity carries the membership development, read once, under current Table authority.
    activity = ok(invoke(host, reader, "crm_activity_list", scoped(business, task_id=member["id"])))
    developments = [item for item in activity["items"] if item["kind"] == "crm_membership"]
    assert len(developments) == 1 and developments[0]["change"]["operation"] == "added"
    assert developments[0]["source"] == {"type": "table", "id": crm["id"], "business_id": business, "label": "Discussion"}


def test_the_private_owner_channel_stays_out_of_the_shared_activity_and_the_owner_cannot_post_to_it(host):
    business = host.first
    crm = make_crm(host, business, "Private")
    member = ok(add(host, owner(host, business), crm, business, create={"title": "Acme"}))["entry"]["record_ref"]
    source = {"type": "table", "id": crm["id"], "business_id": business}
    ok(invoke(host, owner(host, business), "owner_conversation_send", {"source": source, "text": "Owner private sentence"}))
    reader = agent(host, host.coordinator, business)
    assert "Owner private sentence" not in json.dumps(ok(invoke(host, reader, "crm_activity_list",
                                                                scoped(business, task_id=member["id"]))))
    # The owner's shared Table comment is the team channel, not the private one: the existing channel rule refuses it.
    assert code(invoke(host, owner(host, business), "tables_comment",
                       scoped(business, table_id=crm["id"], text="Owner team text"))) == "channel_required"
    assert code(invoke(host, owner_global_context(host.instance), "tables_comment",
                       {"scope": "business", "table_id": crm["id"], "text": "Owner team text"})) == "scope_required"


def test_the_owner_records_an_observation_on_a_project_task_but_never_through_the_global_team_channel(host):
    business = host.first
    record = make_task(host, business, "Observed record")
    global_record = make_task(host, None, "Observed global record")
    body = {"kind": "email", "summary": "Mail received", "body": "Recorded body", "occurred_at": "2026-10-20T09:00:00.000000Z",
            "source": {"kind": "email", "title": "Synthetic mailbox"}, "event_key": "observed-1"}
    # The Global team channel is not the owner's: the same rule that refuses a Global team comment refuses the record.
    assert code(invoke(host, owner_global_context(host.instance), "crm_activity_record",
                       {"scope": "global", "task_id": global_record["id"], **body})) == "channel_required"
    # The Project Task keeps the existing exception: the owner records there, attributed to the owner, and the Task is untouched.
    before = query(host, "SELECT fields_json, status, revision FROM tasks WHERE business_id IS ? AND id=?", business, record["id"])
    recorded = ok(invoke(host, owner(host, business), "crm_activity_record", scoped(business, task_id=record["id"], **body)))
    assert recorded["duplicate"] is False and recorded["event"]["origin"] == "recorded"
    assert recorded["event"]["actor"]["id"] == host.instance.owner and recorded["event"]["source"]["label"] == "Synthetic mailbox"
    assert query(host, "SELECT fields_json, status, revision FROM tasks WHERE business_id IS ? AND id=?", business, record["id"]) == before
    again = ok(invoke(host, owner(host, business), "crm_activity_record", scoped(business, task_id=record["id"], **body)))
    assert again["duplicate"] is True and again["event"]["id"] == recorded["event"]["id"]
    assert count(host, "crm_activity_events") == 1
    # A different content under the same key is a conflict, and the read lists the observation exactly once.
    assert code(invoke(host, owner(host, business), "crm_activity_record",
                       scoped(business, task_id=record["id"], **{**body, "summary": "Different"}))) == "conflict"
    listed = ok(invoke(host, owner(host, business), "crm_activity_list", scoped(business, task_id=record["id"])))
    assert [item["summary"] for item in listed["items"]].count("Mail received") == 1


def test_the_existing_database_backup_and_restore_preserve_crm_records_and_recorded_bodies(host, tmp_path):
    business = host.first
    crm = make_crm(host, business, "Backup")
    member = ok(add(host, owner(host, business), crm, business, create={"title": "Preserved contact"}))["entry"]["record_ref"]
    ok(invoke(host, owner(host, business), "crm_activity_record", scoped(business, task_id=member["id"],
              kind="email", summary="Preserved observation", body="Synthetic body retained in full database backups.",
              occurred_at="2026-10-20T09:00:00.000000Z", source={"kind": "email", "title": "Synthetic mailbox"},
              event_key="backup-observation")))
    tables = ("crm_entries", "crm_activity_events", "thread_messages")
    def stored(database):
        with database.read() as uow:
            return {table: [tuple(row) for row in uow.connection.execute(f"SELECT * FROM {table} ORDER BY business_id,id")]
                    for table in tables}
    expected = stored(host.database)
    snapshot = tmp_path / "backup.sqlite3"
    host.database.backup(snapshot)
    restored = host.database.restore(snapshot, tmp_path / "restored.sqlite3")
    try:
        assert stored(restored) == expected
    finally:
        restored.close()
