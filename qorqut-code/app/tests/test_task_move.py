"""Move to project on the real host composition: authority, data preservation, blockers, atomicity and transports.

Every check runs the registered operations (``tasks_move_targets``, ``tasks_move_to_project_preview`` and
``tasks_move_to_project``) through the application, the dashboard HTTP routes or the MCP argument path, over the same frozen
composition a running installation uses: the Operations Task engine and its guarded relocation, the host adapter for private
conversations and Focus, the identity gate, policy overrides, idempotency and the audit. Synthetic data only; no model,
network, provider, deployment or live record.
"""

from __future__ import annotations

import sqlite3
import json
from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import InvocationOptions, Principal, new_id, plain
from starlette.testclient import TestClient

from bazarlyk.service.web import create_app
from bazarlyk.transport.auth import Authentication
from bazarlyk.transport.compatibility import DashboardCompatibility
from bazarlyk.transport.mcp import RegisteredAgentCaller, split_options, tool_schema
from host_foundation_support import set_agent_overrides
from test_project_records import (agent, as_principal, call, context, context_global, host, ok, person,  # noqa: F401
                                  seed_project)

TARGETS, PREVIEW, MOVE = "tasks_move_targets", "tasks_move_to_project_preview", "tasks_move_to_project"
OPERATIONS = (TARGETS, PREVIEW, MOVE)
UNREACHABLE = {"not_found", "forbidden", "scope_required"}


# -- helpers ------------------------------------------------------------------------------------------------------------

def scope_args(ctx):
    return {"scope": "global"} if ctx.business_id is None else {}


def make(host, ctx, title, parent=None, **extra):
    arguments = {"title": title, **scope_args(ctx), **({"parent_id": parent} if parent else {}), **extra}
    return ok(call(host, ctx, "tasks_create", arguments))["task"]


def current(host, task):
    with host.db.read() as uow:
        return dict(uow.connection.execute("SELECT * FROM tasks WHERE id=? ORDER BY business_id IS NULL, business_id", (task["id"],)).fetchone())


def revise(host, ctx, task, name, arguments):
    return ok(call(host, ctx, name, {"task_id": task["id"], **scope_args(ctx), **arguments}, revision=current(host, task)["revision"]))


def comment(host, ctx, task, text):
    """Task discussion belongs to the agents; the owner talks to Ordubashy in the private conversation instead."""
    return revise(host, agent(host, ctx.business_id), task, "tasks_comment", {"text": text, **scope_args(ctx)})


def select(source, destination, roots):
    return {"source_business_id": source, "destination_project_id": destination, "task_ids": [*roots]}


def preview(host, ctx, selection):
    return ok(call(host, ctx, PREVIEW, selection))["preview"]


def move(host, ctx, selection, token, key=None):
    return host.runtime.application.invoke(ctx, MOVE, {**selection, "preview_token": token},
                                           InvocationOptions(idempotency_key=key or new_id()))


def moved(host, ctx, selection):
    shown = preview(host, ctx, selection)
    assert shown["can_move"], shown["blockers"]
    result = move(host, ctx, selection, shown["token"])
    assert result.status == "ok", result.error
    return shown, plain(result.data)


def query(host, sql, *parameters):
    with host.db.read() as uow:
        return [dict(row) for row in uow.connection.execute(sql, parameters).fetchall()]


def marks(values):
    return ",".join("?" * len(values))


def where(host, ids):
    return {row["id"]: row for row in query(host, f"SELECT * FROM tasks WHERE id IN ({marks(ids)})", *ids)}


def state(host):
    """Everything a refused or failed move must leave exactly as it was."""
    tables = ("tasks", "thread_messages", "thread_follows", "item_tags", "relationships", "owner_conversations",
              "owner_conversation_messages", "focus_cards", "project_item_links", "task_moves", "task_snooze_wakes",
              "task_completions", "inbox_changes")
    return {table: query(host, f"SELECT * FROM {table} ORDER BY rowid") for table in tables}


def tree(host, ctx):
    """root -> done child -> grandchild, and an archived sibling child (the grandchild is blocked by the sibling)."""
    root = make(host, ctx, "Funding story", assignee_ids=[host.instance.owner], tags=["funding"])
    done = make(host, ctx, "Completed child", root["id"])
    revise(host, ctx, done, "tasks_complete", {})
    grandchild = make(host, ctx, "Grandchild", done["id"])
    sibling = make(host, ctx, "Archived child", root["id"])
    revise(host, ctx, sibling, "tasks_update", {"changes": {"archived": True}})
    revise(host, ctx, grandchild, "tasks_update", {"changes": {"blocked_by": [sibling["id"]]}})
    for task, text in ((root, "Root note"), (done, "Done note"), (grandchild, "Deep note")):
        comment(host, ctx, task, text)
    return {"root": root, "done": done, "grandchild": grandchild, "sibling": sibling}


def source_of(ctx, task):
    return {"type": "task", "id": task["id"], "business_id": ctx.business_id}


def drop_override(host, businesses):
    set_agent_overrides(host.instance, host.ordubashy, {}, businesses)


# -- success and preservation ---------------------------------------------------------------------------------------------

def test_move_preserves_actual_attention_workflow_and_task_history(host):
    destination = seed_project(host, name='Attention Destination', slug='attention-destination')
    owner = context_global(host)
    task = make(host, owner, 'Attention move', worker_ids=[host.instance.owner])
    revise(host, owner, task, 'tasks_complete', {})
    ref = source_of(owner, task)
    board = ok(call(host, owner, 'attention_add', {'scope': 'global', 'task': ref}))
    ok(call(host, owner, 'attention_focus_set', {'scope': 'global', 'task': ref, 'focus': ref,
                                                 'intent': 'Review', 'expected_revision': board['revision']}))
    with host.db.transaction() as uow:
        uow.connection.execute("INSERT INTO attention_workflows(agent_id,run_id,consumer_id,work_task_json,workflow_ref,"
                               "workflow_name,related_refs_json,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                               (host.ordubashy, 'synthetic-run', 'synthetic-consumer', json.dumps(ref), 'flow', 'Flow',
                                json.dumps([ref]), '2030-01-01T00:00:00Z'))
    old_task = current(host, task)
    audit = query(host, 'SELECT * FROM attention_membership_events WHERE agent_id=?', host.ordubashy)
    moved(host, owner, select(None, destination, [task['id']]))
    after = current(host, task)
    assert after['worker_ids_json'] == old_task['worker_ids_json']
    assert after['latest_completed_at'] == old_task['latest_completed_at']
    membership = query(host, 'SELECT * FROM attention_memberships WHERE agent_id=?', host.ordubashy)[0]
    assert membership['task_business_id'] == destination and membership['task_created_at'] == old_task['created_at']
    attention = query(host, 'SELECT * FROM agent_attention WHERE agent_id=?', host.ordubashy)[0]
    assert json.loads(attention['focus_json'])['business_id'] == destination
    assert json.loads(attention['work_task_json'])['business_id'] == destination
    workflow = query(host, 'SELECT * FROM attention_workflows WHERE agent_id=?', host.ordubashy)[0]
    assert json.loads(workflow['work_task_json'])['business_id'] == destination
    assert json.loads(workflow['related_refs_json'])[0]['business_id'] == destination
    assert query(host, 'SELECT * FROM attention_membership_events WHERE agent_id=?', host.ordubashy) == audit

def test_global_tree_moves_keeping_ids_parents_discussion_and_private_data(host):
    destination = seed_project(host, name="Fund Destination", slug="fund-destination")
    elsewhere = seed_project(host, name="Fund Elsewhere", slug="fund-elsewhere")
    owner = context_global(host)
    outside = make(host, owner, "Outside parent")
    nodes = tree(host, owner)
    root = nodes["root"]
    child = make(host, owner, "Child of the outside parent", outside["id"])
    ids = [nodes[name]["id"] for name in ("root", "done", "grandchild", "sibling")]
    # The root hangs under an outside parent, has a private owner conversation, a Focus card and a Project link.
    revise(host, owner, root, "tasks_update", {"changes": {"parent_id": outside["id"]}})
    conversation = ok(call(host, owner, "owner_conversation_send", {"scope": "global", "source": source_of(owner, root),
                                                                    "text": "Private about the funding story."}))
    ok(call(host, owner, "owner_conversation_send", {"scope": "global", "source": source_of(owner, nodes["done"]),
                                                      "text": "Private about the done child."}))
    card = ok(call(host, owner, "focus_present", {"scope": "global", "source": source_of(owner, nodes["grandchild"]),
                                                  "reason": "Watch", "requested_response": "Decide"}))["item"]
    link = ok(call(host, context(host, elsewhere), "project_items_link", {"project_id": elsewhere, "item": source_of(owner, root)}))["link"]
    before = {"tasks": where(host, ids), "messages": query(host, "SELECT * FROM thread_messages WHERE item_type='task' AND item_id IN "
                                                                 f"({marks(ids)}) ORDER BY id", *ids),
              "follows": query(host, f"SELECT * FROM thread_follows WHERE item_type='task' AND item_id IN ({marks(ids)}) ORDER BY item_id, principal_id", *ids),
              "tags": query(host, f"SELECT * FROM item_tags WHERE item_type='task' AND item_id IN ({marks(ids)}) ORDER BY item_id, tag_id", *ids),
              "edges": query(host, "SELECT * FROM relationships ORDER BY id"),
              "outside": current(host, outside), "unrelated": current(host, child),
              "conversations": query(host, "SELECT * FROM owner_conversations WHERE source_type='task' ORDER BY id"),
              "chat": query(host, "SELECT * FROM owner_conversation_messages ORDER BY sequence"),
              "card": query(host, "SELECT * FROM focus_cards WHERE id=?", card["id"])[0]}
    selection = select(None, destination, [root["id"]])
    shown, result = moved(host, owner, selection)

    assert shown["source"] == {"business_id": None, "name": "Global"} and shown["destination"]["id"] == destination
    assert shown["roots"] == [{"id": root["id"], "title": "Funding story", "previous_parent_id": outside["id"]}]
    assert {row["id"] for row in shown["tasks"]} == set(ids)
    assert {row["id"]: row["archived"] for row in shown["tasks"]}[nodes["sibling"]["id"]] is True
    assert shown["counts"]["tasks"] == 4 and shown["counts"]["comments"] == len(before["messages"]) >= 3
    assert shown["counts"]["private_messages"] == 2
    assert shown["counts"]["focus_cards"] == 1 and shown["counts"]["project_links"] == 1 and shown["counts"]["crm_entries"] == 0
    assert "destination" in shown["visibility"]["summary"] and "private" in shown["visibility"]["summary"]
    assert result["counts"] == shown["counts"] and result["destination"]["id"] == destination and len(result["move_id"]) == 36
    assert [row["id"] for row in result["moved"]] == [ids[0], *sorted(ids[1:], key=lambda item: [row["id"] for row in result["moved"]].index(item))]

    after = where(host, ids)
    for identity in ids:
        old, new = before["tasks"][identity], after[identity]
        assert new["business_id"] == destination and new["revision"] == old["revision"] + 1
        for column in ("id", "title", "status", "due", "archived", "created_by", "created_at", "snoozed_until", "snooze_generation", "kind"):
            assert new[column] == old[column], column
        if identity != root["id"]:
            assert new["parent_id"] == old["parent_id"]
    assert after[root["id"]]["parent_id"] is None and current(host, outside) == before["outside"] and current(host, child) == before["unrelated"]
    # Every byte of every message is kept; only the scope changes and the Global namespace is pinned beside the references.
    assert query(host, f"SELECT * FROM thread_messages WHERE item_type='task' AND item_id IN ({marks(ids)}) ORDER BY id", *ids) == [
        {**row, "business_id": destination, "reference_namespace": "global"} for row in before["messages"]]
    assert query(host, f"SELECT * FROM thread_follows WHERE item_type='task' AND item_id IN ({marks(ids)}) ORDER BY item_id, principal_id", *ids) == [
        {**row, "business_id": destination} for row in before["follows"]]
    assert query(host, f"SELECT * FROM item_tags WHERE item_type='task' AND item_id IN ({marks(ids)}) ORDER BY item_id, tag_id", *ids) == [
        {**row, "business_id": destination} for row in before["tags"]]
    for old, new in zip(before["edges"], query(host, "SELECT * FROM relationships ORDER BY id")):
        assert {key: value for key, value in new.items() if key != "business_id"} == {key: value for key, value in old.items() if key != "business_id"}
    # Private conversations keep identity, messages and read state; only their source scope and key follow the Task.
    after_conversations = query(host, "SELECT * FROM owner_conversations WHERE source_type='task' ORDER BY id")
    assert len(after_conversations) == len(before["conversations"]) == 2
    for old, new in zip(before["conversations"], after_conversations):
        assert new["id"] == old["id"] and new["business_id"] == destination and new["peer_agent_id"] == old["peer_agent_id"]
        assert new["source_key"] == f"source:{destination}:task:{old['source_id']}" and new["revision"] == old["revision"] + 1
        assert new["latest_sequence"] == old["latest_sequence"] and new["created_at"] == old["created_at"]
    assert query(host, "SELECT * FROM owner_conversation_messages ORDER BY sequence") == before["chat"]
    read = ok(call(host, context(host, destination), "owner_conversation_get", {"source": source_of(context(host, destination), root)}))
    assert read["conversation"]["id"] == conversation["conversation"]["id"] and [m["body"] for m in read["messages"]] == ["Private about the funding story."]
    # The Focus card keeps its identity, order and notes with its binding rewritten; the link follows without granting access.
    stored = query(host, "SELECT * FROM focus_cards WHERE id=?", card["id"])[0]
    assert stored["source_key"] == f"source:{destination}:task:{nodes['grandchild']['id']}" and '"business_id": "' + destination in stored["source_json"]
    assert stored["revision"] == before["card"]["revision"] + 1 and stored["position"] == before["card"]["position"]
    assert stored["created_at"] == before["card"]["created_at"] and stored["reason"] == before["card"]["reason"]
    assert query(host, "SELECT target_business_id, target_namespace, revision FROM project_item_links WHERE id=?", link["id"]) == [
        {"target_business_id": destination, "target_namespace": "business:" + destination, "revision": link["revision"] + 1}]
    # The audit records every moved Task once, and nothing else announced the relocation.
    audit = query(host, "SELECT task_id, source_business_id, destination_business_id, actor_id FROM task_moves WHERE move_id=? ORDER BY seq", result["move_id"])
    assert {row["task_id"] for row in audit} == set(ids) and {row["destination_business_id"] for row in audit} == {destination}
    assert {row["actor_id"] for row in audit} == {host.instance.owner}


def test_project_to_project_move_detaches_an_outside_parent_and_normalizes_overlapping_roots(host):
    source = seed_project(host, name="Move Source", slug="move-source")
    destination = seed_project(host, name="Move Destination", slug="move-destination")
    owner = context(host, source)
    outside = make(host, owner, "Outside parent")
    top = make(host, owner, "Top", outside["id"])
    middle = make(host, owner, "Middle", top["id"])
    leaf = make(host, owner, "Leaf", middle["id"])
    detached = make(host, owner, "Second root")
    # Overlapping roots (a Task and its own descendant) normalize to one move per record.
    selection = select(source, destination, [leaf["id"], top["id"], detached["id"]])
    shown, result = moved(host, context_global(host), selection)
    assert [row["id"] for row in shown["roots"]] == sorted([top["id"], detached["id"]], key=lambda item: [row["id"] for row in shown["roots"]].index(item))
    assert {row["id"]: row["previous_parent_id"] for row in shown["roots"]} == {top["id"]: outside["id"], detached["id"]: None}
    assert shown["counts"]["tasks"] == 4 and len(result["moved"]) == 4
    rows = where(host, [top["id"], middle["id"], leaf["id"], detached["id"], outside["id"]])
    assert rows[top["id"]]["parent_id"] is None and rows[middle["id"]]["parent_id"] == top["id"] and rows[leaf["id"]]["parent_id"] == middle["id"]
    assert {rows[item]["business_id"] for item in (top["id"], middle["id"], leaf["id"], detached["id"])} == {destination}
    assert rows[outside["id"]]["business_id"] == source and rows[outside["id"]]["revision"] == outside["revision"]
    # A second hop keeps the journey resolvable: the first source still points at the final location.
    third = seed_project(host, name="Move Third", slug="move-third")
    moved(host, context_global(host), select(destination, third, [top["id"]]))
    old = call(host, context(host, source), "tasks_get", {"task_id": top["id"]})
    assert old.error["code"] == "record_moved" and old.error["details"]["destination"]["id"] == third


def test_targets_list_accessible_active_projects_by_name_and_exclude_the_source(host):
    first = seed_project(host, name="Zeta Targets", slug="zeta-targets")
    second = seed_project(host, name="Alpha Targets", slug="alpha-targets")
    archived = seed_project(host, name="Aardvark Archived", slug="aardvark-archived", archived=True)
    hidden = seed_project(host, name="Hidden Targets", slug="hidden-targets", member=person(host, "Another owner"))
    listed = ok(call(host, context_global(host), TARGETS, {"source_business_id": first}))
    names = [item["name"] for item in listed["projects"]]
    assert listed["next_cursor"] is None and names == sorted(names) and "Zeta Targets" not in names and "Alpha Targets" in names
    assert archived not in {item["id"] for item in listed["projects"]} and hidden not in {item["id"] for item in listed["projects"]}
    assert all(set(item) == {"id", "name", "slug"} for item in listed["projects"])
    from_global = ok(call(host, context_global(host), TARGETS, {"source_business_id": None}))
    assert {first, second} <= {item["id"] for item in from_global["projects"]}
    # An unreachable source is refused by the engine's scope gate before anything about it is read.
    assert call(host, context_global(host), TARGETS, {"source_business_id": hidden}).error["code"] in UNREACHABLE


# -- stale previews and collisions --------------------------------------------------------------------------------------------

def test_a_changed_selection_conversation_or_link_after_the_preview_needs_a_new_preview(host):
    source = seed_project(host, name="Stale Source", slug="stale-source")
    destination = seed_project(host, name="Stale Destination", slug="stale-destination")
    other = seed_project(host, name="Stale Other", slug="stale-other")
    owner, root_ctx = context(host, source), context_global(host)
    root = make(host, owner, "Root")
    child = make(host, owner, "Child", root["id"])
    ok(call(host, owner, "owner_conversation_send", {"source": source_of(owner, child), "text": "First"}))
    selection = select(source, destination, [root["id"]])
    changes = (
        lambda: make(host, owner, "Late descendant", child["id"]),
        lambda: comment(host, owner, child, "A late comment"),
        lambda: ok(call(host, owner, "owner_conversation_send", {"source": source_of(owner, child), "text": "Late private"})),
        lambda: ok(call(host, context(host, other), "project_items_link", {"project_id": other, "item": source_of(owner, root)})),
        lambda: revise(host, owner, root, "tasks_update", {"changes": {"title": "Renamed root"}}),
    )
    for change in changes:
        shown = preview(host, root_ctx, selection)
        change()
        before = state(host)
        stale = move(host, root_ctx, selection, shown["token"])
        assert stale.status == "error" and stale.error["code"] == "revision_conflict", stale.error
        assert state(host) == before
    final = preview(host, root_ctx, selection)
    assert move(host, root_ctx, selection, final["token"]).status == "ok"


def test_destination_collisions_block_with_readable_reasons_and_change_nothing(host):
    source = seed_project(host, name="Collide Source", slug="collide-source")
    destination = seed_project(host, name="Collide Destination", slug="collide-destination")
    owner = context(host, source)
    root = make(host, owner, "Root")
    child = make(host, owner, "Child", root["id"])
    ok(call(host, owner, "owner_conversation_send", {"source": source_of(owner, child), "text": "Private"}))
    ok(call(host, context_global(host), "focus_present", {"scope": "global", "source": source_of(owner, child),
                                                           "reason": "Watch", "requested_response": "Decide"}))
    now = "2026-10-28T00:00:00Z"
    with host.db.transaction() as uow:
        uow.repository("tasks", business_id=destination).insert({"id": root["id"], "title": "Same bare id", "status": "open",
                                                                 "created_by": host.instance.owner})
        uow.connection.execute(
            "INSERT INTO owner_conversations(id,source_key,business_id,source_type,source_id,audience,revision,latest_sequence,created_at,updated_at)"
            " VALUES(?,?,?,'task',?,'owner_ordubashy',1,0,?,?)", (new_id(), f"source:{destination}:task:{child['id']}", destination, child["id"], now, now))
        uow.connection.execute(
            "INSERT INTO focus_cards(id,source_key,position,created_by,created_at,updated_at) VALUES(?,?,99,?,?,?)",
            (new_id(), f"source:{destination}:task:{child['id']}", host.instance.owner, now, now))
    selection = select(source, destination, [root["id"]])
    shown = preview(host, context_global(host), selection)
    codes = {item["code"] for item in shown["blockers"]}
    assert not shown["can_move"] and {"task_id_exists", "conversation_exists", "focus_card_exists"} <= codes
    assert all(item["message"] for item in shown["blockers"])
    before = state(host)
    blocked = move(host, context_global(host), selection, shown["token"])
    assert blocked.status == "error" and blocked.error["code"] == "move_blocked"
    assert {item["code"] for item in blocked.error["details"]["blockers"]} == codes
    assert state(host) == before


# -- authority --------------------------------------------------------------------------------------------------------------------

def test_archived_unreachable_and_unknown_destinations_and_sources_are_refused_without_effects(host):
    source = seed_project(host, name="Refuse Source", slug="refuse-source")
    archived = seed_project(host, name="Refuse Archived", slug="refuse-archived", archived=True)
    foreign = seed_project(host, name="Refuse Foreign", slug="refuse-foreign", member=person(host, "Foreign owner"))
    owner = context(host, source)
    root = make(host, owner, "Root")
    before = state(host)
    for destination, codes in ((archived, {"business_archived"}), (foreign, {"not_found", "forbidden"}), (new_id(), {"not_found"})):
        for name in (PREVIEW, MOVE):
            arguments = {**select(source, destination, [root["id"]]), **({"preview_token": "x" * 64} if name == MOVE else {})}
            result = host.runtime.application.invoke(context_global(host), name, arguments, InvocationOptions(idempotency_key=new_id()))
            assert result.status == "error" and result.error["code"] in codes, (name, destination, result.error)
    assert call(host, context_global(host), PREVIEW, select(foreign, seed_project(host, name="Refuse Dest", slug="refuse-dest"),
                                                           [root["id"]])).error["code"] in UNREACHABLE
    same = call(host, context_global(host), PREVIEW, select(source, source, [root["id"]]))
    assert same.error["code"] == "validation_error"
    assert state(host) == before


def test_only_the_owner_and_the_configured_ordubashy_may_move_and_ordinary_agents_and_members_cannot(host):
    source = seed_project(host, name="Identity Source", slug="identity-source")
    destination = seed_project(host, name="Identity Destination", slug="identity-destination")
    owner = context(host, source)
    root = make(host, owner, "Root")
    selection = select(source, destination, [root["id"]])
    shown = preview(host, context_global(host), selection)
    member = person(host, "Plain member")
    with host.db.transaction() as uow:
        for project in (source, destination):
            uow.repository("business_memberships", business_id=project).insert({"principal_id": member, "is_owner": True})
    before = state(host)
    for who in (as_principal(host, source, member), as_principal(host, None, member)):
        for name in OPERATIONS:
            arguments = {**selection, **({"preview_token": shown["token"]} if name == MOVE else {})}
            if name == TARGETS:
                arguments = {"source_business_id": source}
            result = host.runtime.application.invoke(who, name, arguments, InvocationOptions(idempotency_key=new_id()))
            assert result.status == "error" and result.error["code"] in {"forbidden", "policy_denied"}, (name, result.error)
    assert state(host) == before
    # The exact configured Ordubashy is allowed; the same preview token is bound to its caller.
    ordubashy = agent(host, None)
    mine = preview(host, ordubashy, selection)
    assert mine["token"] != shown["token"]
    assert move(host, ordubashy, selection, shown["token"]).error["code"] == "revision_conflict"
    assert move(host, ordubashy, selection, mine["token"]).status == "ok"


def test_a_tool_override_or_a_missing_membership_refuses_the_move_with_no_effects(host):
    source = seed_project(host, name="Override Source", slug="override-source")
    destination = seed_project(host, name="Override Destination", slug="override-destination")
    owner = context(host, source)
    root = make(host, owner, "Root")
    selection = select(source, destination, [root["id"]])
    ordubashy = agent(host, None)
    shown = preview(host, ordubashy, selection)
    reach = (source, destination, host.first)
    for operation in ("tasks_update", "tasks_create", "tasks_get"):
        set_agent_overrides(host.instance, host.ordubashy, {operation: "deny"}, reach)
        try:
            before = state(host)
            for name, arguments in ((PREVIEW, selection), (MOVE, {**selection, "preview_token": shown["token"]})):
                result = host.runtime.application.invoke(agent(host, None), name, arguments, InvocationOptions(idempotency_key=new_id()))
                assert result.status == "error" and result.error["code"] in {"policy_denied", "forbidden"}, (operation, name, result.error)
            listed = host.runtime.application.invoke(agent(host, None), TARGETS, {"source_business_id": source}, InvocationOptions())
            if operation == "tasks_create":
                # A destination the caller cannot create in is not offered; the source stays readable.
                assert listed.status == "ok" and destination not in {item["id"] for item in plain(listed.data)["projects"]}
            else:
                assert listed.status == "error" and listed.error["code"] in {"policy_denied", "forbidden"}, (operation, listed.error)
            assert state(host) == before
        finally:
            drop_override(host, reach)
    # A move-specific explicit deny wins as well.
    set_agent_overrides(host.instance, host.ordubashy, {MOVE: "deny"}, reach)
    try:
        assert host.runtime.application.invoke(agent(host, None), MOVE, {**selection, "preview_token": shown["token"]},
                                               InvocationOptions(idempotency_key=new_id())).error["code"] == "policy_denied"
    finally:
        drop_override(host, reach)
    assert move(host, agent(host, None), selection, preview(host, agent(host, None), selection)["token"]).status == "ok"


def test_a_retry_with_the_same_key_returns_the_receipt_and_never_moves_twice_or_outlives_authority(host):
    source = seed_project(host, name="Replay Source", slug="replay-source")
    destination = seed_project(host, name="Replay Destination", slug="replay-destination")
    owner = context(host, source)
    root = make(host, owner, "Root")
    selection = select(source, destination, [root["id"]])
    shown = preview(host, context_global(host), selection)
    key = new_id()
    first = move(host, context_global(host), selection, shown["token"], key)
    assert first.status == "ok"
    audit = query(host, "SELECT * FROM task_moves")
    again = move(host, context_global(host), selection, shown["token"], key)
    assert again.status == "ok" and plain(again.data) == plain(first.data)
    assert query(host, "SELECT * FROM task_moves") == audit and current(host, root)["revision"] == root["revision"] + 1
    # A different key with the old token finds the Task already moved: a stale preview, never a second move.
    assert move(host, context_global(host), selection, shown["token"]).status == "error"
    # The saved receipt is re-authorized: once the destination is archived, even the same key is refused.
    with host.db.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at='2026-10-28T00:00:00Z' WHERE id=?", (destination,))
    refused = move(host, context_global(host), selection, shown["token"], key)
    assert refused.status == "error" and refused.error["code"] in {"business_archived", "not_found"}


# -- blockers ---------------------------------------------------------------------------------------------------------------------

def test_a_crossing_dependency_blocks_and_never_removes_or_retargets_it(host):
    source = seed_project(host, name="Cross Source", slug="cross-source")
    destination = seed_project(host, name="Cross Destination", slug="cross-destination")
    owner = context(host, source)
    root = make(host, owner, "Root")
    inside = make(host, owner, "Inside", root["id"])
    outside = make(host, owner, "Outside")
    revise(host, owner, inside, "tasks_update", {"changes": {"blocked_by": [outside["id"]]}})
    shown = preview(host, context_global(host), select(source, destination, [root["id"]]))
    assert not shown["can_move"] and {item["code"] for item in shown["blockers"]} == {"crossing_relationship"}
    assert shown["blockers"][0]["task_id"] in {root["id"], inside["id"], outside["id"]}
    before = state(host)
    assert move(host, context_global(host), select(source, destination, [root["id"]]), shown["token"]).error["code"] == "move_blocked"
    assert state(host) == before


def test_unsupported_active_bindings_fail_the_whole_move(host):
    source = seed_project(host, name="Bound Source", slug="bound-source")
    destination = seed_project(host, name="Bound Destination", slug="bound-destination")
    owner = context(host, source)
    root = make(host, owner, "Root")
    child = make(host, owner, "Child", root["id"])
    selection = select(source, destination, [root["id"]])
    now = "2026-10-28T00:00:00Z"
    with host.db.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO routines(namespace,business_id,id,name,scope,target,schedule,timezone,status,job_kind,arguments_json,"
            "timeout_seconds,metadata_json,revision,created_at,updated_at) VALUES(?,?,?,?,'business','business','0 0 * * *','UTC',"
            "'active','test.fixture',?,900,'{}',1,?,?)", (source, source, new_id(), "Names the child", f'{{"task_id": "{child["id"]}"}}', now, now))
    shown = preview(host, context_global(host), selection)
    assert not shown["can_move"] and any(item["code"] == "unsupported_binding" and "routines" in item["message"] for item in shown["blockers"])
    before = state(host)
    assert move(host, context_global(host), selection, shown["token"]).error["code"] == "move_blocked"
    assert state(host) == before
    with host.db.transaction() as uow:
        uow.connection.execute("DELETE FROM routines WHERE business_id=?", (source,))
    assert preview(host, context_global(host), selection)["can_move"]


# -- atomicity and the guard -------------------------------------------------------------------------------------------------

def test_a_failure_in_the_middle_of_the_move_rolls_everything_back_and_clears_the_guard(host):
    source = seed_project(host, name="Rollback Source", slug="rollback-source")
    destination = seed_project(host, name="Rollback Destination", slug="rollback-destination")
    owner = context(host, source)
    root = make(host, owner, "Root")
    child = make(host, owner, "Child", root["id"])
    ok(call(host, owner, "owner_conversation_send", {"source": source_of(owner, child), "text": "Private"}))
    ok(call(host, context_global(host), "focus_present", {"scope": "global", "source": source_of(owner, child),
                                                           "reason": "Watch", "requested_response": "Decide"}))
    selection = select(source, destination, [root["id"]])
    applied = []

    class Failing:
        """Runs after the Task rows, discussion, links and the host adapter's rewrites, inside the armed guard."""

        name = "failing"

        @staticmethod
        def inspect(uow, relocation):
            from qorqut_operations.tasks.relocation import AdapterReport
            return AdapterReport()

        @staticmethod
        def apply(uow, relocation, at):
            applied.append(uow.relocation is not None)
            assert uow.connection.execute("SELECT business_id FROM tasks WHERE id=?", (root["id"],)).fetchone()[0] == destination
            raise RuntimeError("injected failure after the Task rows moved")

    relocation = host.runtime.domains.tasks.relocation
    original = relocation.adapters
    relocation.adapters = (*original, Failing)
    try:
        # The token is taken with the same adapters, so the apply passes every check and then fails mid-transaction.
        shown = preview(host, context_global(host), selection)
        before = state(host)
        with pytest.raises(RuntimeError, match="injected failure"):
            move(host, context_global(host), selection, shown["token"])
    finally:
        relocation.adapters = original
    assert applied == [True]
    assert state(host) == before
    assert query(host, "SELECT business_id FROM tasks WHERE id IN (?, ?) ORDER BY id", root["id"], child["id"]) == [{"business_id": source}] * 2
    with host.db.transaction() as uow:
        assert uow.relocation is None
        with pytest.raises(sqlite3.DatabaseError):
            uow.connection.execute("UPDATE tasks SET business_id=? WHERE id=?", (destination, root["id"]))
    assert state(host) == before
    final = preview(host, context_global(host), selection)
    assert move(host, context_global(host), selection, final["token"]).status == "ok"
    with host.db.transaction() as uow:
        assert uow.relocation is None
        with pytest.raises(sqlite3.DatabaseError):
            uow.connection.execute("UPDATE tasks SET business_id=? WHERE id=?", (source, root["id"]))


def test_a_destination_membership_lost_after_the_preview_refuses_the_move(host):
    source = seed_project(host, name="Lost Source", slug="lost-source")
    destination = seed_project(host, name="Lost Destination", slug="lost-destination")
    root = make(host, context(host, source), "Root")
    selection = select(source, destination, [root["id"]])
    shown = preview(host, context_global(host), selection)
    with host.db.transaction() as uow:
        uow.connection.execute("UPDATE business_memberships SET active=0 WHERE business_id=? AND principal_id=?",
                               (destination, host.instance.owner))
    before = state(host)
    result = move(host, context_global(host), selection, shown["token"])
    assert result.status == "error" and result.error["code"] in UNREACHABLE, result.error
    assert state(host) == before


def test_raw_sql_and_ordinary_updates_cannot_relocate_a_task(host):
    source = seed_project(host, name="Raw Source", slug="raw-source")
    destination = seed_project(host, name="Raw Destination", slug="raw-destination")
    owner = context(host, source)
    root = make(host, owner, "Root")
    comment(host, owner, root, "A note")
    before = state(host)
    assert call(host, owner, "tasks_update", {"task_id": root["id"], "changes": {"business_id": destination}},
                revision=current(host, root)["revision"]).status == "error"
    with host.db.transaction() as uow:
        for statement in ("UPDATE tasks SET business_id=? WHERE id=?", "UPDATE thread_messages SET business_id=? WHERE item_id=?"):
            with pytest.raises(sqlite3.DatabaseError):
                uow.connection.execute(statement, (destination, root["id"]))
    assert state(host) == before


# -- old references -----------------------------------------------------------------------------------------------------------------

def test_an_old_reference_reports_where_the_task_went_only_to_a_caller_who_may_read_it(host):
    source = seed_project(host, name="Old Source", slug="old-source")
    destination = seed_project(host, name="Old Destination", slug="old-destination")
    outsider = person(host, "Source-only member")
    with host.db.transaction() as uow:
        uow.repository("business_memberships", business_id=source).insert({"principal_id": outsider, "is_owner": True})
    owner = context(host, source)
    root = make(host, owner, "Root")
    moved(host, context_global(host), select(source, destination, [root["id"]]))
    for name, arguments in (("tasks_get", {"task_id": root["id"]}), ("tasks_thread_get", {"task_id": root["id"]}),
                            ("tasks_notes_list", {"task_id": root["id"]})):
        result = call(host, owner, name, arguments)
        assert result.status == "error" and result.error["code"] == "record_moved", (name, result.error)
        assert result.error["details"]["destination"] == {"id": destination, "name": "Old Destination", "slug": "old-destination"}
        assert result.error["details"]["task"] == {"type": "task", "id": root["id"], "business_id": destination}
    for name, arguments in (("tasks_update", {"task_id": root["id"], "changes": {"title": "Edit"}}),
                            ("tasks_comment", {"task_id": root["id"], "text": "Late"}),
                            ("tasks_complete", {"task_id": root["id"]})):
        result = call(host, owner, name, arguments, revision=1)
        assert result.status == "error" and result.error["code"] == "record_moved", (name, result.error)
    hidden = call(host, as_principal(host, source, outsider), "tasks_get", {"task_id": root["id"]})
    assert hidden.error["code"] == "not_found" and "details" not in hidden.error or not hidden.error.get("details")
    assert current(host, root)["business_id"] == destination


def test_pending_inbox_events_follow_the_task_for_a_recipient_who_may_read_it_now(host):
    source = seed_project(host, name="Inbox Source", slug="inbox-source")
    destination = seed_project(host, name="Inbox Destination", slug="inbox-destination")
    owner = context(host, source)
    root = make(host, owner, "Root")
    revise(host, owner, root, "tasks_assign", {"assignee_ids": [host.ordubashy]})
    recipient = agent(host, None)

    def pending():
        listed = ok(call(host, agent(host, None), "inbox_list", {"limit": 50}))
        return [item for item in listed["items"] if item["source_type"] == "tasks" and item["source_id"] == root["id"]]

    before = pending()
    assert before and {item["business_id"] for item in before} == {source}
    ids = query(host, "SELECT id FROM inbox_events WHERE source_type='tasks' AND source_id=? ORDER BY id", root["id"])
    moved(host, context_global(host), select(source, destination, [root["id"]]))
    # The recorded events keep their original scope and payload; they stay deliverable because the recipient may read the Task
    # where it is now, and the relocation itself announced nothing.
    assert query(host, "SELECT id FROM inbox_events WHERE source_type='tasks' AND source_id=? ORDER BY id", root["id"]) == ids
    assert [item["id"] for item in pending()] == [item["id"] for item in before]
    assert {item["business_id"] for item in pending()} == {source}
    # Delivery follows destination authority: with the Task read denied there, the old event is no longer shown.
    reach = (source, destination, host.first)
    set_agent_overrides(host.instance, host.ordubashy, {"tasks_get": "deny"}, reach)
    try:
        assert all(item["source_id"] != root["id"] for item in pending())
    finally:
        drop_override(host, reach)
    assert [item["id"] for item in pending()] == [item["id"] for item in before]


# -- transports ---------------------------------------------------------------------------------------------------------------------

def web(host, tmp_path):
    dashboard = tmp_path / "dashboard"
    (dashboard / "js").mkdir(parents=True)
    (dashboard / "css").mkdir()
    return TestClient(create_app(host.runtime, dashboard_root=dashboard, origins=["http://testserver"],
                                 authentication=Authentication(host.db, host.runtime.paths),
                                 compatibility=DashboardCompatibility(host.runtime), manage_lifecycle=False),
                      base_url="http://testserver")


def post(client, path, body):
    return client.post(path, headers={"Origin": "http://testserver"}, json=body)


def test_the_real_dashboard_routes_serve_all_three_operations_and_map_conflicts(host, tmp_path):
    source = seed_project(host, name="Web Source", slug="web-source")
    destination = seed_project(host, name="Web Destination", slug="web-destination")
    owner = context(host, source)
    root = make(host, owner, "Root")
    child = make(host, owner, "Child", root["id"])
    selection = select(source, destination, [root["id"]])
    with web(host, tmp_path) as client:
        listed = post(client, "/api/root/operations/tasks_move_targets", {"source_business_id": source})
        assert listed.status_code == 200 and destination in {item["id"] for item in listed.json()["data"]["projects"]}
        shown = post(client, "/api/root/operations/tasks_move_to_project_preview", selection)
        assert shown.status_code == 200, shown.text
        token = shown.json()["data"]["preview"]["token"]
        assert shown.json()["data"]["preview"]["can_move"]
        # A business route must agree with the explicit source; a Global body on a project route is refused.
        wrong = post(client, "/api/biz/web-destination/operations/tasks_move_to_project_preview", selection)
        assert wrong.status_code in {400, 403, 422} and wrong.json()["error"]["code"] in {"validation_error", "forbidden"}, wrong.text
        right = post(client, "/api/biz/web-source/operations/tasks_move_to_project_preview", selection)
        assert right.status_code == 200, right.text
        assert right.json()["data"]["preview"]["token"] == token
        # The mutation needs its idempotency key; a stale token is a conflict, and the real move is a plain 200.
        missing = post(client, "/api/root/operations/tasks_move_to_project", {**selection, "preview_token": token})
        assert missing.status_code in {400, 422} and missing.json()["error"]["code"] == "validation_error", missing.text
        stale = post(client, "/api/root/operations/tasks_move_to_project", {**selection, "preview_token": "0" * 64, "idempotency_key": new_id()})
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "revision_conflict", stale.text
        key = new_id()
        done = post(client, "/api/root/operations/tasks_move_to_project", {**selection, "preview_token": token, "idempotency_key": key})
        assert done.status_code == 200, done.text
        assert {row["id"] for row in done.json()["data"]["moved"]} == {root["id"], child["id"]}
        again = post(client, "/api/root/operations/tasks_move_to_project", {**selection, "preview_token": token, "idempotency_key": key})
        assert again.status_code == 200 and again.json()["data"] == done.json()["data"]
        old = post(client, "/api/biz/web-source/operations/tasks_get", {"task_id": root["id"]})
        assert old.status_code == 409 and old.json()["error"]["code"] == "record_moved", old.text
        assert old.json()["error"]["details"]["destination"]["id"] == destination
        # An Email-bound Task is a readable blocker over the same route, and the mutation reports move_blocked as a conflict.
        blocked_source = seed_project(host, name="Web Blocked", slug="web-blocked")
        email = make(host, context(host, blocked_source), "Old thread")
        with host.db.transaction() as uow:
            row = uow.repository("tasks", business_id=blocked_source).get(email["id"])
            uow.repository("tasks", business_id=blocked_source).update(
                email["id"], {"fields_json": {"$task": {"task_type": "email", "overview": "", "email_conversation_id": "thread-1"}}},
                expected_revision=row["revision"])
        blocked = select(blocked_source, destination, [email["id"]])
        sample = post(client, "/api/root/operations/tasks_move_to_project_preview", blocked)
        assert sample.status_code == 200 and sample.json()["data"]["preview"]["can_move"] is False
        assert [item["code"] for item in sample.json()["data"]["preview"]["blockers"]] == ["email_binding"]
        refused = post(client, "/api/root/operations/tasks_move_to_project",
                       {**blocked, "preview_token": sample.json()["data"]["preview"]["token"], "idempotency_key": new_id()})
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "move_blocked", refused.text
        assert refused.json()["error"]["details"]["blockers"][0]["code"] == "email_binding"


def test_the_operations_are_exposed_to_the_configured_ordubashy_through_the_mcp_catalog_and_wire(host):
    caller = RegisteredAgentCaller(host.runtime, None)
    actor = SimpleNamespace(principal=Principal(host.ordubashy, "agent", "Ordubashy"), delegation_assignment_id=None,
                            delegation_binding_id=None, delegation_generation=None, registration_id=None)
    rows = {row["canonical_operation"]: row for row in caller.inventory(actor)}
    for name in OPERATIONS:
        assert rows[name]["available"], name
        assert not host.runtime.registry.get(name).owner_only and "mcp" in host.runtime.registry.get(name).exposure
        schema = tool_schema(host.runtime.registry, name)
        assert schema["properties"]["source_business_id"] and "source_business_id" in schema["required"]
    assert "idempotency_key" in tool_schema(host.runtime.registry, MOVE)["required"] and "idempotency_key" not in tool_schema(host.runtime.registry, PREVIEW)["required"]
    source = seed_project(host, name="Wire Source", slug="wire-source")
    destination = seed_project(host, name="Wire Destination", slug="wire-destination")
    root = make(host, context(host, source), "Root")
    selection = select(source, destination, [root["id"]])
    options = SimpleNamespace(business_id=None, review_business_id=None)
    wire, wire_options = split_options(dict(selection))
    ctx = caller.invocation_context(SimpleNamespace(**{**actor.__dict__}), canonical_name=PREVIEW, options=options)
    assert wire == selection and wire_options.idempotency_key is None
    shown = host.runtime.application.invoke(ctx, PREVIEW, wire, wire_options)
    assert shown.status == "ok", shown.error
    wire, wire_options = split_options({**selection, "preview_token": plain(shown.data)["preview"]["token"], "idempotency_key": new_id()})
    assert "idempotency_key" not in wire and wire_options.idempotency_key
    result = host.runtime.application.invoke(ctx, MOVE, wire, wire_options)
    assert result.status == "ok", result.error
    assert current(host, root)["business_id"] == destination
