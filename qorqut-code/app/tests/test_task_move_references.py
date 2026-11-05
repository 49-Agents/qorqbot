"""Move to project on the real host: what historical references mean after a move, and which active work really names a Task.

Part one proves that an immutable note keeps its meaning. Task, Action, Signoff and Focus note readers resolve a stored
reference in the scope it was written in (a destination record that only shares the bare id is never mistaken for it), follow a
Task that moved only for a reader authorized to read it where it is now, and never rewrite a payload. Part two proves the
binding scan is typed, scoped and complete: queued jobs, routines, delegated assignments and pending approvals block a move
only when they name the selected Task in its scope, and a scan that cannot be completed refuses instead of granting the move.

Synthetic data only; the composition, authorization, triggers and operations are the product's own."""

from __future__ import annotations

import json

import pytest
from qorqut_operations.contracts import new_id, plain

from bazarlyk.domains.tasks import relocation
from host_foundation_support import set_agent_overrides
from test_project_records import agent, call, context, context_global, host, ok, seed_project  # noqa: F401
from test_task_move import PREVIEW, current, drop_override, make, move, moved, preview, query, revise, select, state

NOW = "2026-10-28T00:00:00Z"


def notes_of(host, ctx, holder):
    return ok(call(host, ctx, "tasks_notes_list", {"task_id": holder["id"], **({"scope": "global"} if ctx.business_id is None else {})}))["notes"]


def cite(host, ctx, holder, target, text="See the other task"):
    scope = {"scope": "global"} if ctx.business_id is None else {}
    return revise(host, agent(host, ctx.business_id), holder, "tasks_comment",
                  {"text": text, "note_refs": [{"type": "task", "id": target["id"]}], **scope})


def membership(host, project, *, active):
    with host.db.transaction() as uow:
        uow.connection.execute("UPDATE business_memberships SET active=? WHERE business_id=? AND principal_id=?",
                               (active, project, host.instance.owner))


def stored_notes(host, holder):
    return query(host, "SELECT id, body, refs_json, author_id, recorder_id, created_at, event_id FROM thread_messages "
                       "WHERE item_type='task' AND item_id=? ORDER BY id", holder["id"])


# -- Task, Action and CRM discussion readers --------------------------------------------------------------------------------


def test_a_note_about_a_task_that_moved_keeps_it_navigable_for_a_reader_who_may_read_it(host):
    source = seed_project(host, name="Cited Source", slug="cited-source")
    destination = seed_project(host, name="Cited Destination", slug="cited-destination")
    owner = context(host, source)
    holder, target = make(host, owner, "Holder"), make(host, owner, "Referenced original")
    cite(host, owner, holder, target)
    written = stored_notes(host, holder)
    assert notes_of(host, owner, holder)[0]["refs"][0]["label"] == "Referenced original"
    moved(host, context_global(host), select(source, destination, [target["id"]]))
    shown = notes_of(host, owner, holder)[0]["refs"][0]
    assert shown["label"] == "Referenced original" and shown["id"] == target["id"] and shown["type"] == "task"
    assert shown["business_id"] == destination and shown["business_slug"] == "cited-destination" and shown["business_name"] == "Cited Destination"
    # every reader of the same note agrees (the Task detail and the whole thread), and the stored payload never changed
    assert ok(call(host, owner, "tasks_get", {"task_id": holder["id"]}))["task"]["notes"][0]["refs"][0]["business_id"] == destination
    assert ok(call(host, owner, "tasks_thread_get", {"task_id": holder["id"]}))["notes"][-1]["refs"][0]["business_id"] == destination
    assert stored_notes(host, holder) == written
    # A new note that cites the moved Task from the old scope is validated strictly and refused: writes never follow a move
    late = call(host, agent(host, source), "tasks_comment", {"task_id": holder["id"], "text": "About it again",
                                                              "note_refs": [{"type": "task", "id": target["id"]}]},
                revision=current(host, holder)["revision"])
    assert late.status == "error" and late.error["code"] in {"not_found", "record_moved"}, late
    assert stored_notes(host, holder) == written


def test_a_note_never_retargets_a_same_id_task_and_keeps_pointing_at_the_one_left_behind(host):
    source = seed_project(host, name="Collide Source", slug="collide-source")
    destination = seed_project(host, name="Collide Destination", slug="collide-destination")
    owner = context(host, source)
    holder, target = make(host, owner, "Holder that moves"), make(host, owner, "Correct source target")
    cite(host, owner, holder, target, "About the correct source target")
    with host.db.transaction() as uow:
        uow.repository("tasks", business_id=destination).insert({"id": target["id"], "title": "Unrelated destination target", "status": "open",
                                                               "created_by": host.instance.owner})
    written = stored_notes(host, holder)
    moved(host, context_global(host), select(source, destination, [holder["id"]]))
    shown = notes_of(host, context(host, destination), holder)[0]["refs"][0]
    assert shown["label"] == "Correct source target" and shown["business_id"] == source and shown["business_slug"] == "collide-source"
    assert stored_notes(host, holder) == written
    assert query(host, "SELECT DISTINCT reference_namespace FROM thread_messages WHERE item_id=?", holder["id"]) == [
        {"reference_namespace": "business:" + source}]
    # a note written after the move starts unpinned and keeps resolving in the destination, where the same-id Task really is
    revise(host, agent(host, destination), holder, "tasks_comment",
           {"text": "A later note", "note_refs": [{"type": "task", "id": target["id"]}]})
    later = notes_of(host, context(host, destination), holder)[-1]["refs"][0]
    assert later["label"] == "Unrelated destination target" and later["business_id"] == destination and later["business_slug"] == "collide-destination"


def test_references_follow_the_holder_and_the_target_through_successive_moves_and_notes_between_them(host):
    first = seed_project(host, name="Hop One", slug="hop-one")
    second = seed_project(host, name="Hop Two", slug="hop-two")
    third = seed_project(host, name="Hop Three", slug="hop-three")
    owner = context(host, first)
    holder, left, wanderer = make(host, owner, "Holder"), make(host, owner, "Left behind"), make(host, owner, "Wanderer")
    cite(host, owner, holder, left, "first note")
    cite(host, owner, holder, wanderer, "second note")
    moved(host, context_global(host), select(first, second, [holder["id"]]))
    peer = make(host, context(host, second), "Peer in the second project")
    cite(host, context(host, second), holder, peer, "note between moves")
    moved(host, context_global(host), select(first, third, [wanderer["id"]]))
    moved(host, context_global(host), select(second, third, [holder["id"]]))
    refs = {note["body"]: note["refs"][0] for note in notes_of(host, context(host, third), holder)}
    assert refs["first note"]["business_id"] == first and refs["first note"]["label"] == "Left behind"      # written in the first project
    assert refs["second note"]["label"] == "Wanderer" and refs["second note"]["business_id"] == third      # moved into the holder's own scope
    assert refs["note between moves"]["business_id"] == second and refs["note between moves"]["label"] == "Peer in the second project"
    assert [row["reference_namespace"] for row in query(host, "SELECT reference_namespace FROM thread_messages WHERE item_id=? ORDER BY created_at, id",
                                                         holder["id"])] == ["business:" + first, "business:" + first, "business:" + second]


def test_a_global_note_follows_a_global_task_that_moved_into_a_project(host):
    destination = seed_project(host, name="From Global", slug="from-global")
    owner = context_global(host)
    holder, target = make(host, owner, "Global holder"), make(host, owner, "Global target")
    cite(host, owner, holder, target)
    moved(host, context_global(host), select(None, destination, [target["id"]]))
    shown = notes_of(host, owner, holder)[0]["refs"][0]
    assert shown["label"] == "Global target" and shown["business_id"] == destination
    moved(host, context_global(host), select(None, destination, [holder["id"]]))
    # the holder now lives in the project; its note still names the Global scope it was written in
    again = notes_of(host, context(host, destination), holder)
    assert again[0]["refs"][0]["label"] == "Global target" and again[0]["refs"][0]["business_id"] == destination
    assert query(host, "SELECT DISTINCT reference_namespace FROM thread_messages WHERE item_id=?", holder["id"]) == [{"reference_namespace": "global"}]


def test_a_reader_without_access_to_the_new_place_sees_an_unavailable_reference_and_no_location(host):
    source = seed_project(host, name="Narrow Source", slug="narrow-source")
    destination = seed_project(host, name="Narrow Destination", slug="narrow-destination")
    owner = context(host, source)
    holder, target = make(host, owner, "Holder"), make(host, owner, "Target")
    cite(host, owner, holder, target)
    moved(host, context_global(host), select(source, destination, [target["id"]]))
    assert notes_of(host, owner, holder)[0]["refs"][0]["business_id"] == destination
    # the same reader, once it has lost membership where the Task now is, learns neither its title nor its place
    membership(host, destination, active=0)
    try:
        assert notes_of(host, owner, holder)[0]["refs"] == [{"unavailable": True}]
    finally:
        membership(host, destination, active=1)
    assert notes_of(host, owner, holder)[0]["refs"][0]["business_id"] == destination


def test_an_action_discussion_resolves_its_task_references_the_same_way(host):
    destination = seed_project(host, name="Action Destination", slug="action-destination")
    target = make(host, context_global(host), "Action target")
    worker = agent(host, None)
    reach = (destination, host.first)
    set_agent_overrides(host.instance, host.ordubashy, {"actions_comment": "allow"}, reach)       # no default names it
    try:
        recorded = ok(call(host, worker, "actions_record", {"text": "Shipped the release", "category": "global-operational"}))["action"]
        ok(call(host, worker, "actions_comment", {"action_id": recorded["action_id"], "text": "About the target",
                                                  "note_refs": [{"type": "task", "id": target["id"]}]}))
        before = ok(call(host, worker, "actions_thread_get", {"action_id": recorded["action_id"]}))["messages"][0]["refs"][0]
        assert before["label"] == "Action target" and "business_id" in before and before["business_id"] is None      # Global, by an explicit null
        stored = query(host, "SELECT refs_json, reference_namespace FROM thread_messages WHERE item_type='action'")
        moved(host, context_global(host), select(None, destination, [target["id"]]))
        after = ok(call(host, worker, "actions_thread_get", {"action_id": recorded["action_id"]}))["messages"][0]["refs"][0]
        assert after["label"] == "Action target" and after["business_id"] == destination and after["business_name"] == "Action Destination"
        assert query(host, "SELECT refs_json, reference_namespace FROM thread_messages WHERE item_type='action'") == stored
    finally:
        drop_override(host, reach)


def test_signoff_and_focus_note_readers_show_a_moved_task_read_only_and_strict_writes_do_not_follow_it(host):
    source = seed_project(host, name="Signoff Source", slug="signoff-source")
    destination = seed_project(host, name="Signoff Destination", slug="signoff-destination")
    global_target = make(host, context_global(host), "Global target")
    target = make(host, context(host, source), "Signoff target")
    notes = host.runtime.signoffs.notes
    refs = [{"type": "task", "id": target["id"]}]
    owner = context(host, source)

    def display(ctx, items):
        with host.db.read() as uow:
            return notes.references(ctx, items, uow, strict=False)

    assert display(owner, refs) == [{**refs[0], "business_id": source, "business_slug": "signoff-source", "business_name": "Signoff Source"}]
    moved(host, context_global(host), select(source, destination, [target["id"]]))
    shown = display(owner, refs)
    assert shown == [{"type": "task", "id": target["id"], "business_id": destination, "business_slug": "signoff-destination",
                      "business_name": "Signoff Destination"}]
    with host.db.read() as uow, pytest.raises(Exception) as strict:      # the strict (write) path refuses; it does not follow
        notes.references(owner, refs, uow, strict=True)
    assert getattr(strict.value, "code", None) in {"record_moved", "not_found"}
    # the Global scope (the Focus board's own) is a real place a Task leaves
    moved(host, context_global(host), select(None, destination, [global_target["id"]]))
    global_shown = display(context_global(host), [{"type": "task", "id": global_target["id"]}])
    assert global_shown[0]["business_id"] == destination and "unavailable" not in global_shown[0]
    # a reader who may not read the Task where it is now learns nothing about it
    narrow = seed_project(host, name="Signoff Narrow", slug="signoff-narrow")
    other = seed_project(host, name="Signoff Other", slug="signoff-other")
    elsewhere = make(host, context(host, narrow), "Elsewhere target")
    moved(host, context_global(host), select(narrow, other, [elsewhere["id"]]))
    membership(host, other, active=0)
    try:
        assert display(context(host, narrow), [{"type": "task", "id": elsewhere["id"]}]) == [
            {"type": "task", "id": elsewhere["id"], "unavailable": True}]
    finally:
        membership(host, other, active=1)


def test_the_owner_chat_of_a_moved_task_answers_record_moved_only_to_a_reader_of_its_new_place(host):
    source = seed_project(host, name="Chat Source", slug="chat-source")
    destination = seed_project(host, name="Chat Destination", slug="chat-destination")
    owner = context(host, source)
    target, stays = make(host, owner, "Chat target"), make(host, owner, "Chat stays")

    def channel(_, ctx, task, business):
        return call(host, ctx, "owner_conversation_get", {"source": {"type": "task", "id": task["id"], "business_id": business}})

    sent = ok(call(host, owner, "owner_conversation_send", {"source": {"type": "task", "id": target["id"], "business_id": source}, "text": "Before the move"}))
    assert sent["message"]["sequence"] == 1 and channel(host, owner, target, source).status == "ok"

    def directory():
        listed = ok(call(host, context_global(host), "owner_conversations_list", {"scope": "global"}))["items"]
        return [item["source"] for item in listed if item["source"]["id"] == target["id"]]

    assert directory() == [{"type": "task", "id": target["id"], "business_id": source}]
    moved(host, context_global(host), select(source, destination, [target["id"]]))
    assert directory() == [{"type": "task", "id": target["id"], "business_id": destination}]     # the Move carried the channel along
    old_source = {"type": "task", "id": target["id"], "business_id": source}
    with host.db.read() as uow:                   # an old-scope source is unavailable (skipped), not an error that aborts a listing
        assert host.runtime.private_conversations.channel_available(owner, old_source, uow) is False
    # a chat opened from the old place still names where its Task is now, so a stale click can follow it
    stale = channel(host, owner, target, source)
    assert stale.status == "error" and stale.error["code"] == "record_moved", stale
    details = plain(stale.error["details"])
    assert details["destination"] == {"id": destination, "name": "Chat Destination", "slug": "chat-destination"}
    assert details["task"] == {"type": "task", "id": target["id"], "business_id": destination}
    # the channel itself went with the Task, so the destination reads the same earlier message
    fresh = ok(channel(host, context(host, destination), target, destination))
    assert [message["body"] for message in fresh["messages"]] == ["Before the move"]
    assert fresh["conversation"]["source"]["business_id"] == destination
    # a Task that stayed, or a bare id that never existed, keeps the plain answers
    assert channel(host, owner, stays, source).status == "ok"
    ghost = channel(host, owner, {"id": new_id()}, source)
    assert ghost.status == "error" and ghost.error["code"] == "not_found" and not ghost.error.get("details")
    # a reader who may not read the destination learns nothing about where it went
    membership(host, destination, active=0)
    try:
        hidden = channel(host, owner, target, source)
        assert hidden.status == "error" and hidden.error["code"] == "not_found" and not hidden.error.get("details"), hidden
    finally:
        membership(host, destination, active=1)
    # the Task that is really in the old scope still wins over any relocation lookup for the same bare id
    with host.db.transaction() as uow:
        uow.repository("tasks", business_id=source).insert({"id": target["id"], "title": "Same id left behind", "status": "open",
                                                          "created_by": host.instance.owner})
    assert channel(host, owner, target, source).status == "ok"


def test_pending_global_inbox_events_stay_deliverable_after_a_global_task_moves(host):
    destination = seed_project(host, name="Global Inbox", slug="global-inbox")
    owner = context_global(host)
    root = make(host, owner, "Global root")
    revise(host, owner, root, "tasks_assign", {"assignee_ids": [host.ordubashy]})

    def pending():
        listed = ok(call(host, agent(host, None), "inbox_list", {"limit": 50}))
        return [item for item in listed["items"] if item["source_type"] == "tasks" and item["source_id"] == root["id"]]

    before = pending()
    assert before and {item["business_id"] for item in before} == {None}
    moved(host, context_global(host), select(None, destination, [root["id"]]))
    assert [item["id"] for item in pending()] == [item["id"] for item in before]            # kept as recorded, still delivered
    assert {item["business_id"] for item in pending()} == {None}


def test_a_readable_task_reference_always_names_its_authorized_place_even_in_the_readers_own_scope(host):
    source = seed_project(host, name="Together Source", slug="together-source")
    destination = seed_project(host, name="Together Destination", slug="together-destination")
    owner = context(host, source)
    holder, target = make(host, owner, "Holder"), make(host, owner, "Internal target")
    cite(host, owner, holder, target)
    plain = notes_of(host, owner, holder)[0]["refs"][0]
    assert plain["business_id"] == source and plain["business_slug"] == "together-source" and plain["business_name"] == "Together Source"
    # an unrelated Global Task with the same bare id exists; both records now move together into the destination
    with host.db.transaction() as uow:
        uow.repository("tasks", business_id=None).insert({"id": target["id"], "title": "Unrelated global target", "status": "open",
                                                         "created_by": host.instance.owner})
    written = stored_notes(host, holder)
    moved(host, context_global(host), select(source, destination, [holder["id"], target["id"]]))
    shown = notes_of(host, context(host, destination), holder)[0]["refs"][0]
    assert shown["label"] == "Internal target" and shown["business_id"] == destination
    assert shown["business_slug"] == "together-destination" and shown["business_name"] == "Together Destination"
    assert stored_notes(host, holder) == written
    # every Task reader says the same, and a Global reference is an explicit null rather than an omission
    assert ok(call(host, context(host, destination), "tasks_thread_get", {"task_id": holder["id"]}))["notes"][-1]["refs"][0]["business_id"] == destination
    anchor = make(host, context_global(host), "Global anchor")
    cite(host, context_global(host), anchor, make(host, context_global(host), "Global peer"))
    global_ref = notes_of(host, context_global(host), anchor)[0]["refs"][0]
    assert "business_id" in global_ref and global_ref["business_id"] is None and global_ref["business_name"] == "Global"


# -- CRM linked activity reads a note's references where the note was written ----------------------------------------------


def comment_on(host, ctx, task, text):
    return revise(host, agent(host, ctx.business_id), task, "tasks_comment", {"text": text})


def linked_bodies(host, ctx, holder):
    data = ok(call(host, ctx, "crm_activity_list", {"task_id": holder["id"], "include_linked": True, "limit": 100}))
    return [item.get("body") for item in data["items"] if item.get("origin") == "linked"]


def test_crm_linked_activity_does_not_retarget_a_same_id_task_in_the_destination(host):
    source = seed_project(host, name="Linked Source", slug="linked-source")
    destination = seed_project(host, name="Linked Destination", slug="linked-destination")
    owner = context(host, source)
    holder, target = make(host, owner, "Holder that moves"), make(host, owner, "Correct source target")
    cite(host, owner, holder, target)
    comment_on(host, owner, target, "Correct linked note")
    with host.db.transaction() as uow:
        uow.repository("tasks", business_id=destination).insert({"id": target["id"], "title": "Unrelated target", "status": "open",
                                                               "created_by": host.instance.owner})
    ok(call(host, agent(host, destination), "tasks_comment", {"task_id": target["id"], "text": "Wrong linked note"}, revision=1))
    assert "Correct linked note" in linked_bodies(host, owner, holder)
    moved(host, context_global(host), select(source, destination, [holder["id"]]))
    bodies = linked_bodies(host, context(host, destination), holder)
    assert "Correct linked note" in bodies and "Wrong linked note" not in bodies, bodies
    # a note written in the destination after the move cites by the destination's own meaning of the id
    cite(host, context(host, destination), holder, target, "A later note")
    assert "Wrong linked note" in linked_bodies(host, context(host, destination), holder)


def test_crm_linked_activity_follows_a_cited_task_that_moved_out_while_the_reader_may_read_it(host):
    source = seed_project(host, name="Follow Source", slug="follow-source")
    destination = seed_project(host, name="Follow Destination", slug="follow-destination")
    owner = context(host, source)
    holder, target = make(host, owner, "Holder that stays"), make(host, owner, "Target that moves")
    cite(host, owner, holder, target)
    comment_on(host, owner, target, "Linked note that follows")
    moved(host, context_global(host), select(source, destination, [target["id"]]))
    assert linked_bodies(host, owner, holder) == ["Linked note that follows"]
    # a reader who has lost the Task's new place gets nothing, and nothing reveals where it went
    membership(host, destination, active=0)
    try:
        assert linked_bodies(host, owner, holder) == []
    finally:
        membership(host, destination, active=1)
    assert linked_bodies(host, owner, holder) == ["Linked note that follows"]


def test_crm_linked_activity_omits_a_task_left_in_a_project_the_reader_can_no_longer_read(host):
    source = seed_project(host, name="Lost Source", slug="lost-source")
    destination = seed_project(host, name="Lost Destination", slug="lost-destination")
    owner = context(host, source)
    holder, target = make(host, owner, "Holder that moves"), make(host, owner, "Left in the source")
    cite(host, owner, holder, target)
    comment_on(host, owner, target, "Source-side note")
    moved(host, context_global(host), select(source, destination, [holder["id"]]))
    reader = context(host, destination)
    assert linked_bodies(host, reader, holder) == ["Source-side note"]
    membership(host, source, active=0)
    try:
        assert linked_bodies(host, reader, holder) == []
    finally:
        membership(host, source, active=1)


def test_crm_linked_activity_follows_a_holder_and_its_cited_tasks_through_successive_moves(host):
    first = seed_project(host, name="Chain One", slug="chain-one")
    second = seed_project(host, name="Chain Two", slug="chain-two")
    third = seed_project(host, name="Chain Three", slug="chain-three")
    owner = context(host, first)
    holder, stays, wanderer = make(host, owner, "Holder"), make(host, owner, "Stays in one"), make(host, owner, "Wanderer")
    cite(host, owner, holder, stays, "note in one")
    cite(host, owner, holder, wanderer, "second note in one")
    comment_on(host, owner, stays, "Stay event")
    comment_on(host, owner, wanderer, "Wanderer event")
    with host.db.transaction() as uow:
        uow.repository("tasks", business_id=third).insert({"id": stays["id"], "title": "Unrelated stay", "status": "open",
                                                          "created_by": host.instance.owner})
    ok(call(host, agent(host, third), "tasks_comment", {"task_id": stays["id"], "text": "Unrelated event in three"}, revision=1))
    moved(host, context_global(host), select(first, second, [holder["id"]]))
    peer = make(host, context(host, second), "Peer in two")
    comment_on(host, context(host, second), peer, "Peer event")
    cite(host, context(host, second), holder, peer, "note in two")
    moved(host, context_global(host), select(first, third, [wanderer["id"]]))
    moved(host, context_global(host), select(second, third, [holder["id"]]))
    bodies = linked_bodies(host, context(host, third), holder)
    assert {"Stay event", "Wanderer event", "Peer event"} <= set(bodies), bodies
    assert "Unrelated event in three" not in bodies, bodies


# -- active bindings are typed, scoped and complete --------------------------------------------------------------------------


def job(host, business, arguments, state="queued"):
    identity = new_id()
    with host.db.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO jobs(id,business_id,target_namespace,kind,principal_id,arguments_json,dedupe_key,state,created_at,updated_at) "
            "VALUES(?,?,?,'review.synthetic',?,?,?,?,?,?)",
            (identity, business, "global" if business is None else "business:" + business, host.instance.owner, json.dumps(arguments),
             identity, state, NOW, NOW))
    return identity


def routine(host, business, arguments, status="active"):
    identity = new_id()
    with host.db.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO routines(namespace,business_id,id,name,scope,target,schedule,timezone,status,job_kind,arguments_json,timeout_seconds,"
            "metadata_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,'0 0 * * *','UTC',?,'review.synthetic',?,900,'{}',1,?,?)",
            (business or "global", business, identity, "Synthetic " + identity[:8], "global" if business is None else "business",
             "root" if business is None else "business", status, json.dumps(arguments), NOW, NOW))
    return identity


def assignment(host, business, *, links=None, scope=None, state="queued"):
    identity = new_id()
    scope = scope or ({"kind": "global", "allowed_business_ids": []} if business is None else {"kind": "business", "business_id": business})
    with host.db.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO operational_assignments(id,parent_principal_id,business_id,scope_json,objective,plan_json,plan_revision,links_json,"
            "allowed_operations_json,resource_bounds_json,selected_model,reasoning_effort,state,max_attempts,created_at,updated_at) "
            "VALUES(?,?,?,?,'Synthetic objective','{}',1,?,'[]','{}','synthetic','low',?,3,?,?)",
            (identity, host.ordubashy, business, json.dumps(scope), json.dumps(links or {}), state, NOW, NOW))
    return identity


def approval(host, review, namespace, *, arguments=None, resources=()):
    """A pending approval whose prepared change names tasks the way the engine stores them."""
    identity = new_id()
    plan = {"operation_name": "tasks_update", "operation_version": 1, "principal_id": host.ordubashy, "target_namespace": namespace,
            "arguments": arguments or {}, "affected_scope": {"kind": "global"} if review is None else {"kind": "business", "business_id": review},
            "expected_targets": [{"resource": resource, "revision": 1, "exists": True} for resource in resources],
            "changes": [{"kind": "record", "resource": resource, "action": "update", "values": {}} for resource in resources]}
    with host.db.transaction() as uow:
        sequence = uow.connection.execute("SELECT COALESCE(MAX(updated_sequence),0)+1 FROM signoffs").fetchone()[0]
        uow.connection.execute(
            "INSERT INTO signoffs(id,review_business_id,review_scope_kind,proposer_principal_id,operation_name,operation_version,"
            "proposal_revision,payload_hash,change_set,preview_json,expected_targets_json,policy_binding_json,affected_scope_json,"
            "state,updated_sequence,created_at,updated_at) VALUES(?,?,?,?,'tasks_update',1,1,?,?,'{}',?,'{}','{}','pending',?,?,?)",
            (identity, review, "global" if review is None else "business", host.ordubashy, "0" * 64, json.dumps(plan).encode(),
             json.dumps(plan["expected_targets"]), sequence, NOW, NOW))
    return identity


def forget(host, table, identity):
    with host.db.transaction() as uow:
        uow.connection.execute(f"DELETE FROM {table} WHERE id=?", (identity,))


def blocked(host, selection):
    shown = preview(host, context_global(host), selection)
    return not shown["can_move"] and {item["code"] for item in shown["blockers"]} == {"unsupported_binding"}, shown


@pytest.fixture
def pair(host):
    source = seed_project(host, name="Binding Source " + new_id()[:6], slug="binding-source-" + new_id()[:8])
    destination = seed_project(host, name="Binding Destination " + new_id()[:6], slug="binding-destination-" + new_id()[:8])
    elsewhere = seed_project(host, name="Binding Elsewhere " + new_id()[:6], slug="binding-elsewhere-" + new_id()[:8])
    root = make(host, context(host, source), "Root")
    return source, destination, elsewhere, root, select(source, destination, [root["id"]])


def test_a_queued_job_blocks_only_when_it_names_the_selected_task_in_its_scope(host, pair):
    source, destination, elsewhere, root, selection = pair
    created = []
    try:
        # not bindings: another explicit scope, free text, a terminal job, a Global job that names the id in Global
        created += [job(host, elsewhere, {"task_id": root["id"], "business_id": elsewhere}),
                    job(host, source, {"task_id": root["id"], "business_id": elsewhere}),
                    job(host, source, {"note": "mentions " + root["id"] + " only in text", "tasks": [root["id"]]}),
                    job(host, source, {"task_id": root["id"]}, state="succeeded"),
                    job(host, None, {"task_id": root["id"]})]
        assert preview(host, context_global(host), selection)["can_move"]
        for arguments in ({"task_id": root["id"], "business_id": source},             # explicit scope
                          {"task_id": root["id"]},                                      # the job's own scope
                          {"items": [{"type": "task", "id": root["id"], "business_id": source}]},
                          {"deep": {"task_ids": [root["id"]]}}):
            identity = job(host, source, arguments)
            is_blocked, shown = blocked(host, selection)
            assert is_blocked and "background jobs" in shown["blockers"][0]["message"], arguments
            forget(host, "jobs", identity)
        assert preview(host, context_global(host), selection)["can_move"]
    finally:
        for identity in created:
            forget(host, "jobs", identity)


def test_a_typed_reference_inherits_the_explicit_scope_of_its_enclosing_object(host, pair):
    source, destination, elsewhere, root, selection = pair
    taskref = {"type": "task", "id": root["id"]}
    created = []
    try:
        # not bindings: the nearest explicit scope names another project, or Global
        created += [job(host, None, {"business_id": elsewhere, "items": [taskref]}),
                    job(host, None, {"business_id": source, "items": [{**taskref, "business_id": elsewhere}]}),     # the child overrides
                    job(host, source, {"scope": "global", "items": [taskref]}),
                    job(host, source, {"namespace": "business:" + elsewhere, "deep": {"task_ids": [root["id"]]}}),
                    job(host, None, {"business_id": source, "note": "mentions " + root["id"] + " as text only"})]
        assert preview(host, context_global(host), selection)["can_move"]
        for arguments in ({"business_id": source, "items": [taskref]},                                              # inherited business_id
                          {"namespace": "business:" + source, "deep": [{"task_ids": [root["id"]]}]},                 # inherited namespace
                          {"business_id": elsewhere, "items": [{**taskref, "business_id": source}]},                 # the child overrides to the source
                          {"business_id": source, "a": {"b": [{"resource_type": "tasks", "resource_id": root["id"]}]}}):
            identity = job(host, None, arguments)
            is_blocked, shown = blocked(host, selection)
            assert is_blocked and "background jobs" in shown["blockers"][0]["message"], arguments
            forget(host, "jobs", identity)
        assert preview(host, context_global(host), selection)["can_move"]
        # the same walk for a Global Task: an inherited `scope: global` binds it, an inherited project does not
        global_root = make(host, context_global(host), "Global root")
        wide = select(None, destination, [global_root["id"]])
        global_ref = {"type": "task", "id": global_root["id"]}
        created.append(job(host, source, {"business_id": elsewhere, "items": [global_ref]}))
        assert preview(host, context_global(host), wide)["can_move"]
        identity = job(host, source, {"scope": "global", "items": [global_ref]})
        is_blocked, shown = blocked(host, wide)
        assert is_blocked
        forget(host, "jobs", identity)
    finally:
        for identity in created:
            forget(host, "jobs", identity)


def test_a_routine_blocks_only_when_it_names_the_selected_task_in_its_scope(host, pair):
    source, destination, elsewhere, root, selection = pair
    made = [routine(host, elsewhere, {"task_id": root["id"]}), routine(host, source, {"task_id": root["id"], "business_id": elsewhere})]
    try:
        assert preview(host, context_global(host), selection)["can_move"]
        made.append(routine(host, source, {"task_id": root["id"]}, status="paused"))
        is_blocked, shown = blocked(host, selection)
        assert is_blocked and "routines" in shown["blockers"][0]["message"]
    finally:
        with host.db.transaction() as uow:
            for identity in made:
                uow.connection.execute("DELETE FROM routines WHERE id=?", (identity,))


def test_a_delegated_assignment_blocks_only_when_its_reach_includes_the_tasks_scope(host, pair):
    source, destination, elsewhere, root, selection = pair
    kept = [assignment(host, elsewhere, links={"tasks": [root["id"]]}),                                         # another project's assignment
            assignment(host, None, links={"tasks": [root["id"]]}, scope={"kind": "global", "allowed_business_ids": [elsewhere]}),
            assignment(host, source, links={"tasks": [root["id"]]}, state="completed"),
            assignment(host, source, links={"documents": [root["id"]]})]                                       # a document id is not a Task
    try:
        assert preview(host, context_global(host), selection)["can_move"]
        for business, links, scope in ((source, {"tasks": [root["id"]]}, None),
                                       (None, {"tasks": [root["id"]]}, {"kind": "global", "allowed_business_ids": [source]}),
                                       (source, {}, {"kind": "business", "business_id": source, "source_ids": {"tasks": [root["id"]]}}),
                                       (source, {}, {"kind": "business", "business_id": source, "record_ids": {"tasks": [root["id"]]}})):
            identity = assignment(host, business, links=links, scope=scope)
            is_blocked, shown = blocked(host, selection)
            assert is_blocked and "delegated assignments" in shown["blockers"][0]["message"], (business, links, scope)
            forget(host, "operational_assignments", identity)
        assert preview(host, context_global(host), selection)["can_move"]
    finally:
        for identity in kept:
            forget(host, "operational_assignments", identity)


def test_a_pending_approval_blocks_by_its_own_resource_scope(host, pair):
    source, destination, elsewhere, root, selection = pair
    mine = {"resource_type": "tasks", "resource_id": root["id"], "business_id": source, "namespace": "business:" + source}
    theirs = {**mine, "business_id": elsewhere, "namespace": "business:" + elsewhere}
    kept = [approval(host, elsewhere, "business:" + elsewhere, arguments={"task_id": root["id"]}, resources=[theirs]),
            approval(host, source, "business:" + source, arguments={"text": "mentions " + root["id"] + " in free text"})]
    try:
        assert preview(host, context_global(host), selection)["can_move"]
        for review, namespace, arguments, resources in ((source, "business:" + source, {}, [mine]),
                                                         (source, "business:" + source, {"task_id": root["id"]}, [])):
            identity = approval(host, review, namespace, arguments=arguments, resources=resources)
            is_blocked, shown = blocked(host, selection)
            assert is_blocked and "approval" in shown["blockers"][0]["message"], (arguments, resources)
            forget(host, "signoffs", identity)
        assert preview(host, context_global(host), selection)["can_move"]
    finally:
        for identity in kept:
            forget(host, "signoffs", identity)


def test_a_scan_that_cannot_be_completed_refuses_instead_of_granting_the_move(host, pair, monkeypatch):
    source, destination, elsewhere, root, selection = pair
    first, second = job(host, source, {}), job(host, source, {"task_id": root["id"], "business_id": source})
    try:
        monkeypatch.setattr(relocation, "MAXIMUM_ROWS", 1)
        refused = call(host, context_global(host), PREVIEW, selection)
        assert refused.status == "error" and refused.error["code"] == "result_too_large", refused
        before = state(host)
        result = move(host, context_global(host), selection, "0" * 64)
        assert result.status == "error" and result.error["code"] == "result_too_large" and state(host) == before
        monkeypatch.setattr(relocation, "MAXIMUM_ROWS", 5_000)
        is_blocked, _ = blocked(host, selection)
        assert is_blocked                                                                  # the same state, scanned fully, is blocked
    finally:
        forget(host, "jobs", first)
        forget(host, "jobs", second)


def test_a_binding_added_between_the_preview_and_the_apply_is_caught_and_an_unrelated_job_is_not(host, pair):
    source, destination, elsewhere, root, selection = pair
    shown = preview(host, context_global(host), selection)
    assert shown["can_move"]
    unrelated = job(host, source, {"task_id": new_id(), "business_id": source})
    bound = job(host, source, {"task_id": root["id"], "business_id": source})
    try:
        before = state(host)
        result = move(host, context_global(host), selection, shown["token"])
        assert result.status == "error" and result.error["code"] in {"revision_conflict", "move_blocked"}, result
        assert state(host) == before
        forget(host, "jobs", bound)
        # with the matching job gone again, only the unrelated job was added since the preview, which does not stale it
        again = preview(host, context_global(host), selection)
        assert again["can_move"]
        with_unrelated = job(host, source, {"task_id": new_id(), "business_id": source})
        try:
            done = move(host, context_global(host), selection, again["token"])
            assert done.status == "ok", done.error
        finally:
            forget(host, "jobs", with_unrelated)
    finally:
        forget(host, "jobs", unrelated)
        with host.db.transaction() as uow:
            uow.connection.execute("DELETE FROM jobs WHERE id=?", (bound,))
