"""CRM activity discussion permissions, delegated record boundaries and authentic actors.

Runs the real Domain and Host with current policy, exposure, Global capability and delegated assignment checks over a
synthetic SQLite store. Some tests explicitly wrap local_activity to model an event becoming unavailable between pages."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from qorqut_operations.contracts import InvocationContext, Principal, new_id

from bazarlyk import owner_sessions
from bazarlyk.agents.operational_delegation import OperationalDelegation
from bazarlyk.transport.auth import Authentication, OriginPolicy
from bazarlyk.transport.http import HTTPAPI
from context_source_status_support import ORIGIN, Http
from host_foundation_support import owner_global_context, set_agent_overrides
from test_crm_host_activity import (agent_ctx, call, comment, everything, ids, link, listing, make_task, ok, owner_ctx, pages,  # noqa: F401
                                    person_ctx, stamp, world)

BASE = {"tasks_get": "allow", "crm_activity_list": "allow", "signoffs_get": "allow", "actions_get": "allow", "tasks_update": "signoff"}


def grant(world, **modes):
    """The worker's explicit grants: every source read allowed, then the given changes (a discussion read denied, say)."""
    set_agent_overrides(world.instance, world.worker, {**BASE, **modes}, (world.first, world.second))


def of_type(page, kind):
    return [item for item in page["items"] if item["source"]["type"] == kind]


def linked_tasks(world):
    return {world.linked, world.incoming, world.global_task, world.referenced}


def walk(world, ctx, limit, task=None):
    """Every page of the listing, returned as one list of items; no page may be empty before the last."""
    collected = pages(world, ctx, limit, task=task)
    assert all(page["items"] for page in collected[:-1])
    return [item for page in collected for item in page["items"]]


# ----- the discussion read is its own authorization ---------------------------------------------------------------------


def test_a_denied_task_discussion_gives_no_text_label_count_or_paging_position_while_the_record_is_readable(world):
    worker = agent_ctx(world, world.worker, world.first)
    grant(world)
    full = everything(world, worker)
    mine = [item for item in full["items"] if item["source"]["type"] == "task" and item["source"]["id"] in linked_tasks(world)]
    assert {"Linked note one", "Incoming note one", "Global note", "Referenced note"} <= {item["summary"] for item in mine}

    grant(world, tasks_thread_get="deny")                      # the record read stays allowed; only the discussion read is denied
    page = everything(world, worker)
    assert not [item for item in page["items"] if item["source"]["type"] == "task" and item["source"]["id"] in linked_tasks(world)]
    text = json.dumps(page)
    for hidden in ("Linked note", "Incoming note", "Global note", "Referenced note", "Linked task", "Incoming from another business",
                   "Global task", "Only referenced"):
        assert hidden not in text, hidden
    # The record's own events and the other readable sources are unaffected, and the allowed sources are still there.
    assert {"Own note one", "Own note three", "Synthetic email received"} <= {item["summary"] for item in page["items"]}
    assert {item["source"]["type"] for item in page["items"]} >= {"task", "signoff", "action", "document"}
    # A denied discussion leaves no paging trace: bounded pages carry exactly the visible events, never an empty or extra page.
    for limit in (1, 2, 3):
        collected = pages(world, worker, limit)
        assert [item["id"] for page_ in collected for item in page_["items"]] == ids(page)
        assert len(collected) == -(-len(page["items"]) // limit)

    grant(world)                                               # restoration brings the identical listing back
    assert ids(everything(world, worker)) == ids(full)


def test_the_discussion_decision_is_current_on_every_page(world):
    worker = agent_ctx(world, world.worker, world.first)
    grant(world)
    first = ok(listing(world, worker, limit=3))
    assert first["next_cursor"]
    grant(world, tasks_thread_get="deny")
    rest, cursor = [], first["next_cursor"]
    while cursor:
        page = ok(listing(world, worker, limit=3, cursor=cursor))
        rest.extend(page["items"])
        cursor = page["next_cursor"]
    assert not [item for item in rest if item["source"]["type"] == "task" and item["source"]["id"] in linked_tasks(world)]
    assert not (set(ids(first)) & set(ids({"items": rest})))                       # nothing repeats
    grant(world)


def test_a_denied_signoff_notes_read_hides_the_notes_but_not_the_signoffs_own_developments(world):
    marker = "SIGNOFF-NOTE-TEXT"
    with world.database.transaction() as uow:
        uow.connection.execute("INSERT INTO signoff_notes(id,signoff_id,body,created_by,created_at,summary) VALUES (?,?,?,?,?,NULL)",
                               (new_id(), world.signoff, marker, world.instance.owner, stamp(18)))
    worker = agent_ctx(world, world.worker, world.first)
    grant(world, signoffs_notes_list="deny")
    page = everything(world, worker)
    assert marker not in json.dumps(page) and not [item for item in of_type(page, "signoff") if item["kind"] == "comment"]
    assert {item["change"]["state"] for item in of_type(page, "signoff")} >= {"proposed", "decided"}
    grant(world, signoffs_notes_list="allow")
    again = everything(world, worker)
    assert [item["body"] for item in of_type(again, "signoff") if item["kind"] == "comment"] == [marker]


def test_a_denied_action_discussion_hides_its_thread_but_not_the_actions_own_record(world):
    marker = "ACTION-THREAD-TEXT"
    with world.database.read() as uow:
        action = uow.connection.execute("SELECT id,business_id FROM action_logs WHERE action_key=?", (world.action,)).fetchone()
    with world.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,author_id,recorder_id,created_at) "
            "VALUES (?,?,'action',?,'comment',?,?,?,?)",
            (action["business_id"], "action-thread-1", action["id"], marker, world.instance.owner, world.instance.owner, stamp(18)))
    worker = agent_ctx(world, world.worker, world.first)
    grant(world, actions_thread_get="deny")
    page = everything(world, worker)
    assert marker not in json.dumps(page)
    assert [item["summary"] for item in of_type(page, "action")] == ["Action recorded"]
    grant(world, actions_thread_get="allow")
    assert marker in {item.get("body") for item in of_type(everything(world, worker), "action")}


def test_a_source_whose_own_read_is_denied_contributes_nothing_at_all(world):
    worker = agent_ctx(world, world.worker, world.first)
    grant(world, signoffs_get="deny", actions_get="deny")
    page = everything(world, worker)
    assert not of_type(page, "signoff") and not of_type(page, "action")
    assert "Synthetic" not in {item["source"].get("label") for item in of_type(page, "signoff")}
    grant(world)
    assert of_type(everything(world, worker), "signoff") and of_type(everything(world, worker), "action")


def test_a_revoked_cross_scope_membership_removes_a_source_between_pages(world):
    wide = InvocationContext(Principal(world.person, "human"), business_id=world.first,
                             allowed_business_ids=frozenset({world.first, world.second}), source="dashboard")
    with world.database.transaction() as uow:
        uow.repository("business_memberships", business_id=world.second).insert({"principal_id": world.person, "is_owner": False})
    everything_before = everything(world, wide)
    assert "Incoming note one" in {item["summary"] for item in everything_before["items"]}
    first = ok(listing(world, wide, limit=3))
    with world.database.transaction() as uow:
        uow.connection.execute("UPDATE business_memberships SET active=0 WHERE business_id=? AND principal_id=?", (world.second, world.person))
    rest, cursor = [], first["next_cursor"]
    while cursor:
        page = ok(listing(world, wide, limit=3, cursor=cursor))
        rest.extend(page["items"])
        cursor = page["next_cursor"]
    assert not [item for item in rest if item["source"].get("business_id") == world.second]
    assert "Incoming" not in json.dumps(rest)


# ----- delegated assignments: the exact record boundary ----------------------------------------------------------------------


def iso(moment):
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def delegating_parent(world):
    """The registered Ordubashy, with the delegated-assignment capability granted once through the real ``agents_update``."""
    parent = world.coordinator
    with world.database.read() as uow:
        row = uow.connection.execute("SELECT revision, profile_json FROM agent_profiles WHERE agent_id=?", (parent,)).fetchone()
    scope = json.loads(row["profile_json"])["scope"]
    if "manage_delegated_assignments" not in scope["global_capabilities"]:
        result = call(world, owner_global_context(world.instance), "agents_update", {"agent_id": parent, "changes": {"scope": {
            **scope, "global_capabilities": sorted({*scope["global_capabilities"], "manage_delegated_assignments"})}}}, row["revision"])
        assert result.status == "ok", result.error
    return parent


def delegate(world, tmp_path, *, allowed, record_ids):
    """The invocation context of a live delegated worker of ``world.first`` holding exactly this record boundary."""
    parent = delegating_parent(world)
    store, now = OperationalDelegation(dispatch_reason=None), datetime.now(timezone.utc)
    with world.database.transaction() as uow:
        known = uow.connection.execute(
            "SELECT id FROM machine_registrations WHERE principal_id=? AND COALESCE(json_extract(compatibility_json,'$.kind'),'')"
            "!='delegated_worker'", (parent,)).fetchone()
        registration = known["id"] if known else new_id()
        revision = uow.connection.execute("SELECT revision FROM agent_profiles WHERE agent_id=?", (parent,)).fetchone()["revision"]
        if known:
            uow.connection.execute("UPDATE machine_registrations SET profile_revision=? WHERE id=?", (revision, registration))
        else:
            uow.repository("machine_registrations").insert({
                "id": registration, "principal_id": parent, "workspace": str(tmp_path / "parent-ws"),
                "service_url": "http://127.0.0.1:9876", "profile_revision": revision,
                "compatibility_json": {"durable_delegation_enabled": True}, "created_at": iso(now), "updated_at": iso(now)})
        assignment = store.create(
            uow, parent_principal_id=parent, business_id=world.first,
            scope={"kind": "business", "business_id": world.first, "record_ids": record_ids, "source_ids": {}},
            objective="Read the record's activity", plan={"steps": ["read"]}, allowed_operations=list(allowed), links={},
            resource_bounds={"max_turns": 20})
        running = store.claim(uow, assignment["id"], actor_principal_id=parent)
        issued = store.issue_worker_binding(
            uow, assignment["id"], parent_principal_id=parent, generation=running["execution_generation"], lease_id=running["lease_id"],
            expected_revision=running["revision"], workspace=str(tmp_path / f"worker-{new_id()[:8]}"), service_url="http://127.0.0.1:9876",
            expires_at=iso(now + timedelta(minutes=4)), parent_registration_id=registration)
    ctx = world.runtime.resolver.invocation_context(Principal(parent, "agent"), business_id=world.first,
                                                     session_registration_id=issued["registration_id"])
    return replace(ctx, delegation_binding_id=issued["binding"]["id"], delegation_assignment_id=assignment["id"],
                   delegation_generation=running["execution_generation"])


def events_of(page):
    return {(item["source"]["type"], item["source"]["id"]) for item in page["items"] if item["origin"] == "linked"}


def test_a_delegated_worker_reads_only_the_linked_records_inside_its_assignment_boundary(world, tmp_path):
    allowed = ("crm_activity_list", "tasks_get", "tasks_thread_get")
    inside = delegate(world, tmp_path, allowed=allowed, record_ids={"tasks": [world.record, world.linked]})
    seen = events_of(everything(world, inside))
    # The linked Task named by the assignment is read with its discussion; every other record is outside the boundary
    # (another Task of the same business, a Task of another business, a Global Task) and contributes nothing.
    assert seen == {("task", world.linked)}
    page = everything(world, inside)
    assert {"Linked note one", "Linked note two", "Linked note three"} <= {item["summary"] for item in page["items"]}
    assert not {"Incoming note one", "Referenced note", "Global note"} & {item["summary"] for item in page["items"]}

    wider = delegate(world, tmp_path, allowed=allowed, record_ids={"tasks": [world.record, world.linked, world.referenced]})
    assert events_of(everything(world, wider)) == {("task", world.linked), ("task", world.referenced)}

    none = delegate(world, tmp_path, allowed=allowed, record_ids={"tasks": [world.record]})
    assert events_of(everything(world, none)) == set()
    assert all(item["origin"] != "linked" for item in everything(world, none)["items"])


def test_a_delegated_worker_without_the_discussion_operation_gets_no_discussion_even_for_an_in_boundary_task(world, tmp_path):
    only_record = delegate(world, tmp_path, allowed=("crm_activity_list", "tasks_get"), record_ids={"tasks": [world.record, world.linked]})
    page = everything(world, only_record)
    assert events_of(page) == set() and "Linked note" not in json.dumps(page)
    assert {item["summary"] for item in page["items"]} >= {"Own note one"}


def test_a_delegated_worker_reaches_a_document_only_by_its_selector_and_never_a_signoff_or_action(world, tmp_path):
    operations = ("crm_activity_list", "tasks_get", "tasks_thread_get", "signoffs_get", "signoffs_notes_list", "actions_get",
                  "actions_thread_get", "context_get", "notes_get")
    # The Signoff and Action reads take no record selector, which a delegated read requires, so they are never reachable.
    both = delegate(world, tmp_path, allowed=operations, record_ids={"tasks": [world.record, world.linked], "documents": [world.document]})
    kinds = {item["source"]["type"] for item in everything(world, both)["items"] if item["origin"] == "linked"}
    assert kinds == {"task", "document"}
    # The document is inside the boundary only when the assignment names it.
    unnamed = delegate(world, tmp_path, allowed=operations, record_ids={"tasks": [world.record, world.linked]})
    assert {item["source"]["type"] for item in everything(world, unnamed)["items"] if item["origin"] == "linked"} == {"task"}
    # ... and only when the assignment may use that document's read operation.
    unread = delegate(world, tmp_path, allowed=("crm_activity_list", "tasks_get", "tasks_thread_get"),
                      record_ids={"tasks": [world.record, world.linked], "documents": [world.document]})
    assert {item["source"]["type"] for item in everything(world, unread)["items"] if item["origin"] == "linked"} == {"task"}


# ----- authentic actors -------------------------------------------------------------------------------------------------


def test_a_linked_system_development_carries_the_principal_whose_change_produced_it(world):
    items = everything(world, owner_ctx(world, world.first))["items"]
    developments = [item for item in items if item["origin"] == "linked" and item["kind"] == "development" and item["source"]["type"] == "task"]
    assert developments, "the owner's real link edits wrote system entries on the linked Tasks"
    assert all(item["actor"] is not None and item["actor"]["id"] == world.instance.owner for item in developments)
    # A change recorded by a known agent keeps that agent, whatever the thread's author column says (a system entry has none).
    with world.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,recorder_id,attribution_json,change_json,created_at) "
            "VALUES (?,?,'task',?,'system','Status changed to working',?,'{\"kind\":\"system\"}','{\"status\":\"working\"}',?)",
            (world.first, "sys-by-agent", world.linked, world.worker, stamp(19)))
    shown = [item for item in everything(world, owner_ctx(world, world.first))["items"] if item["summary"] == "Status changed to working"]
    assert len(shown) == 1 and shown[0]["actor"]["id"] == world.worker and shown[0]["actor"]["kind"] == "agent"
    assert shown[0]["kind"] == "development" and shown[0]["change"] == {"status": "working"}


def test_a_relayed_comment_keeps_its_speaker_and_the_recording_agent_without_copying_the_relayed_text(world):
    statement = "PRIVATE-RELAYED-STATEMENT"
    relay = {"kind": "explicit_owner_statement", "owner_id": world.instance.owner, "statement_text": statement,
             "source": {"provider": "codex", "session_id": "session-1", "message_id": "message-1"}}
    with world.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,author_id,recorder_id,attribution_json,created_at) "
            "VALUES (?,?,'task',?,'comment',?,?,?,?,?)",
            (world.first, "relayed-1", world.linked, "Relayed owner decision", world.instance.owner, world.coordinator, json.dumps(relay), stamp(19, 12)))
    page = everything(world, owner_ctx(world, world.first))
    [shown] = [item for item in page["items"] if item["summary"] == "Relayed owner decision"]
    assert shown["actor"]["id"] == world.instance.owner and shown["recorded_by"]["id"] == world.coordinator
    assert shown["attribution"] == {"kind": "explicit_owner_statement", "owner_id": world.instance.owner}
    text = json.dumps(page)
    assert statement not in text and "session-1" not in text and "message-1" not in text
    # An ordinary comment carries no attribution fields.
    [plain_comment] = [item for item in page["items"] if item["summary"] == "Linked note one"]
    assert "attribution" not in plain_comment and "recorded_by" not in plain_comment


# ----- the Global activity-record route ----------------------------------------------------------------------------------


def test_the_global_operation_route_carries_crm_activity_record_with_the_same_restrictions_as_the_other_global_operations(world):
    api = HTTPAPI(world.runtime, Authentication(world.database, world.instance.paths, signing_key=b"synthetic-dispatch-key-not-a-secret-000"),
                  OriginPolicy([ORIGIN]))

    def post(operation, body):
        return asyncio.run(api(Http.request("POST", f"/api/root/operations/{operation}", body)))

    unknown = post("crm_activity_not_an_operation", {"scope": "global"})
    assert unknown.status_code == 404 and json.loads(unknown.body)["error"]["message"] == "Endpoint is not registered"
    routed = post("crm_activity_record", {"scope": "global", "task_id": world.global_task, "idempotency_key": new_id()})
    error = json.loads(routed.body)["error"]
    # This incomplete request reaches operation validation; the full Global owner channel guard is exercised
    # through the real HTTP/browser path in test_crm_integration_browser.
    assert routed.status_code != 404 and error["message"] != "Endpoint is not registered" and error["code"] != "not_found"
    # Business options are refused exactly as on the other Global operations.
    refused = post("crm_activity_record", {"scope": "global", "task_id": world.global_task, "business_id": world.first})
    assert refused.status_code in {400, 403, 422} and json.loads(refused.body)["error"]["code"] in {"forbidden", "validation_error"}
    # Reading stays on the same route, and the record write is not an owner-session capability.
    listed = post("crm_activity_list", {"scope": "global", "task_id": world.global_task})
    assert json.loads(listed.body).get("status") == "ok"
    assert "crm_activity_record" not in owner_sessions.READ_OPERATIONS | owner_sessions.WRITE_OPERATIONS
