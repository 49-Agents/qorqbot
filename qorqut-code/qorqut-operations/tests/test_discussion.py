"""The canonical item discussion: one writer, required responsibility events apart from optional follows, sticky unfollow."""

from __future__ import annotations

import pytest

from operational_support import (AGENT, AGENT2, BUSINESS, COORDINATOR, HOME_AGENT, HUMAN, OTHER, OTHER_AGENT, OperationalHost, STRANGER)
from qorqut_operations.contracts import DomainError
from qorqut_operations.discussion import ThreadItem, auto_follow, follow_state, followers
from qorqut_operations.discussion.threads import attribution


@pytest.fixture
def host(tmp_path):
    return OperationalHost(tmp_path)


def required(host, agent):
    return [row["event_type"] for row in host.deliveries(agent, source_type="tasks") if row["required"]]


def optional(host, agent):
    return [row["event_type"] for row in host.deliveries(agent, source_type="tasks") if not row["required"]]


def follow(host, task, who, following, ctx):
    return host.data("tasks_follow", {"task_id": task["id"], "following": following}, ctx)


def test_responsible_assignees_get_required_events_and_follow_by_default(host):
    task = host.create_task("Ship it", assignee_ids=[AGENT])
    assert required(host, AGENT) == ["created"] and optional(host, AGENT) == []
    item = ThreadItem(BUSINESS, "task", task["id"])
    with host.database.read() as uow:
        assert set(followers(uow, item)) == {HUMAN, AGENT}
        assert follow_state(uow, item, AGENT) == {"following": True, "explicit": False, "reason": "assignee"}
        assert follow_state(uow, item, HUMAN)["reason"] == "creator"
    assert task["following"] is True and task["follow_reason"] == "creator"


def test_a_comment_reaches_the_assignee_only_as_an_optional_follow(host):
    task = host.create_task("Discuss", assignee_ids=[AGENT])
    host.data("tasks_comment", {"task_id": task["id"], "text": "Please look"}, expected_revision=task["revision"])
    assert optional(host, AGENT) == ["note_added"] and required(host, AGENT) == ["created"]
    # Unfollowing stops optional comment events and nothing else.
    assert follow(host, task, AGENT, False, host.agent())["following"] is False
    detail = host.read("tasks_get", {"task_id": task["id"]}, host.agent())["task"]
    host.data("tasks_comment", {"task_id": task["id"], "text": "Second note"}, expected_revision=detail["revision"])
    assert optional(host, AGENT) == ["note_added"]
    # A responsibility event (a state or detail change) is still required, whatever the follow state.
    detail = host.read("tasks_get", {"task_id": task["id"]})["task"]
    host.data("tasks_update", {"task_id": task["id"], "changes": {"status": "working"}}, expected_revision=detail["revision"])
    assert required(host, AGENT) == ["created", "updated"]


def test_unfollow_is_sticky_against_automatic_follows_until_followed_again(host):
    task = host.create_task("Sticky", assignee_ids=[AGENT])
    follow(host, task, AGENT, False, host.agent())
    # Assigning again, or commenting as that agent, never re-follows.
    detail = host.read("tasks_get", {"task_id": task["id"]})["task"]
    host.data("tasks_assign", {"task_id": task["id"], "assignee_ids": [AGENT, AGENT2]}, expected_revision=detail["revision"])
    detail = host.read("tasks_get", {"task_id": task["id"]})["task"]
    host.data("tasks_comment", {"task_id": task["id"], "text": "I commented"}, host.agent(), expected_revision=detail["revision"])
    item = ThreadItem(BUSINESS, "task", task["id"])
    with host.database.read() as uow:
        assert follow_state(uow, item, AGENT) == {"following": False, "explicit": True, "reason": "explicit"}
        assert set(followers(uow, item)) == {HUMAN, AGENT2}
    with host.database.transaction() as uow:
        assert auto_follow(uow, item, [AGENT], "assignee") == []
    follow(host, task, AGENT, True, host.agent())
    with host.database.read() as uow:
        assert follow_state(uow, item, AGENT)["following"] is True


def test_follow_is_limited_to_participants_of_the_owning_namespace(host):
    task = host.create_task("Private to the business", assignee_ids=[AGENT])
    # A global agent follows a record only through its owning namespace, never through another business.
    outsider = host.agent(OTHER_AGENT, business=OTHER)
    assert host.error("tasks_follow", {"task_id": task["id"], "following": True}, outsider) in {"not_found", "forbidden", "scope_forbidden"}
    # A retained business-home registration is no participant, even of its own business with an active membership.
    home = host.home_agent(BUSINESS)
    assert host.error("tasks_follow", {"task_id": task["id"], "following": True}, home) == "forbidden"
    item = ThreadItem(BUSINESS, "task", task["id"])
    with host.database.transaction() as uow:
        assert auto_follow(uow, item, [HOME_AGENT], "assignee") == []


def test_removed_assignees_and_dependency_followers_get_required_events(host):
    blocker = host.create_task("Blocker", assignee_ids=[AGENT2])
    waiting = host.create_task("Waiting", assignee_ids=[AGENT], blocked_by=[blocker["id"]])
    before = len(host.deliveries(AGENT, source_type="tasks"))
    detail = host.read("tasks_get", {"task_id": blocker["id"]})["task"]
    host.data("tasks_update", {"task_id": blocker["id"], "changes": {"status": "done"}}, expected_revision=detail["revision"])
    # AGENT is not assigned to the blocker but its work depends on it: a required event.
    delivered = host.deliveries(AGENT, source_type="tasks")[before:]
    assert [(row["source_id"], row["required"]) for row in delivered] == [(blocker["id"], 1)]
    # Reassigning away from AGENT tells AGENT too, exactly as in v1.
    detail = host.read("tasks_get", {"task_id": waiting["id"]})["task"]
    host.data("tasks_assign", {"task_id": waiting["id"], "assignee_ids": [AGENT2]}, expected_revision=detail["revision"])
    assert any("assignment_changed" in row["event_type"] for row in host.deliveries(AGENT, source_type="tasks") if row["source_id"] == waiting["id"])
    assert [row for row in host.deliveries(AGENT, source_type="tasks") if row["source_id"] == waiting["id"]][-1]["required"] == 1


def test_the_actor_is_never_notified_and_ineligible_agents_are_dropped(host):
    task = host.create_task("Own work", ctx=host.agent(), assignee_ids=[AGENT, AGENT2])
    assert host.deliveries(AGENT) == [] and required(host, AGENT2) == ["created"]
    host.home_agent(BUSINESS)
    host.routing.required, host.routing.optional = [HOME_AGENT], [HOME_AGENT]
    detail = host.read("tasks_get", {"task_id": task["id"]})["task"]
    host.data("tasks_comment", {"task_id": task["id"], "text": "Ping"}, expected_revision=detail["revision"])
    assert host.deliveries(HOME_AGENT) == []             # a business-home registration is never addressable, members included
    assert host.deliveries(HUMAN) == [] and host.deliveries(STRANGER) == []   # people receive no agent inbox deliveries


def test_host_routes_add_required_and_optional_recipients_once(host):
    host.routing.required, host.routing.optional = [COORDINATOR], [COORDINATOR, AGENT2]
    task = host.create_task("Routed", assignee_ids=[AGENT])
    rows = host.deliveries(source_type="tasks")
    assert {(row["agent_id"], row["required"]) for row in rows} == {(COORDINATOR, 1), (AGENT, 1), (AGENT2, 0)}
    assert len({(row["event_id"], row["agent_id"]) for row in rows}) == len(rows)
    assert host.routing.calls[0][:2] == (BUSINESS, task["id"])


def test_the_one_writer_attributes_comments_and_relays_without_granting_anything(host):
    task = host.create_task("Relay", assignee_ids=[AGENT])
    coordinator = host.coordinator(BUSINESS)
    relayed = {"kind": "explicit_owner_statement", "owner_id": HUMAN, "statement_text": "Ship it on Friday"}
    updated = host.data("tasks_comment", {"task_id": task["id"], "text": "Owner said ship it", "relay": relayed}, coordinator,
                        expected_revision=task["revision"])["task"]
    note = updated["notes"][-1]
    assert note["author_id"] == HUMAN and note["recorder_id"] == COORDINATOR and note["attribution"]["kind"] == "explicit_owner_statement"
    # Only the registered coordinator may relay, and an owner relay must name the installation owner.
    assert host.error("tasks_comment", {"task_id": task["id"], "text": "x", "relay": relayed}, host.agent(), expected_revision=updated["revision"]) == "forbidden"
    wrong = {**relayed, "owner_id": STRANGER}
    assert host.error("tasks_comment", {"task_id": task["id"], "text": "x", "relay": wrong}, coordinator, expected_revision=updated["revision"]) == "validation_error"
    # A relay needs its note, and every message is immutable once written.
    assert host.error("tasks_update", {"task_id": task["id"], "changes": {"title": "T2"}, "relay": relayed}, coordinator, expected_revision=updated["revision"]) == "validation_error"
    with pytest.raises(DomainError):
        with host.database.transaction() as uow:
            uow.connection.execute("UPDATE thread_messages SET body='edited' WHERE id=?", (note["id"],))
    # The same transaction wrote the message, the follow and the event it belongs to.
    stored = host.query("SELECT * FROM thread_messages WHERE id=?", note["id"])[0]
    assert stored["event_id"] is not None and host.count("inbox_events", "id=?", stored["event_id"]) == 1



def report(agent):
    return {"kind": "agent_report", "agent_id": agent, "report_text": "The draft is ready"}


def registration(host, sql, *parameters):
    with host.database.transaction() as uow:
        uow.connection.execute(sql, parameters)


def test_an_agent_report_names_another_current_global_and_stays_readable_after_it_stops_qualifying(host):
    task = host.create_task("Report", assignee_ids=[AGENT])
    coordinator = host.coordinator(BUSINESS)
    updated = host.data("tasks_comment", {"task_id": task["id"], "text": "Second reported", "relay": report(AGENT2)}, coordinator,
                        expected_revision=task["revision"])["task"]
    note = updated["notes"][-1]
    assert (note["author_id"], note["recorder_id"]) == (AGENT2, COORDINATOR)
    assert note["attribution"] == {"kind": "agent_report", "agent_id": AGENT2, "report_text": "The draft is ready"}
    # The speaker is another current global: not the recorder itself, not a business-home registration (whose row and
    # principal are active), and not a global whose role is retired.
    host.home_agent()
    registration(host, "UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.role.id','onbashy') WHERE agent_id=?", AGENT2)
    for speaker in (COORDINATOR, HOME_AGENT, AGENT2):
        assert host.error("tasks_comment", {"task_id": task["id"], "text": "x", "relay": report(speaker)}, coordinator,
                          expected_revision=updated["revision"]) == "validation_error", speaker
    # New relays only: the stored report keeps its speaker and attribution and is read as written.
    retained = host.read("tasks_get", {"task_id": task["id"]})["task"]
    assert retained["revision"] == updated["revision"] and retained["notes"][-1] == note


@pytest.mark.parametrize("change", [
    ("UPDATE agent_profiles SET home_business_id=?, namespace=? WHERE agent_id=?", BUSINESS, BUSINESS),   # ecosystem scope kept
    ("UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.status','inactive') WHERE agent_id=?",),
    ("UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.role.id','onbashy') WHERE agent_id=?",),
    ("UPDATE agent_profiles SET status='retired' WHERE agent_id=?",),
    ("UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.role','ordubashy') WHERE agent_id=?",),
], ids=["business_home", "profile_inactive", "retired_role", "row_retired", "malformed_role"])
def test_only_a_current_global_ordubashy_is_the_registered_coordinator(host, change):
    def relayed(ctx):
        with host.database.read() as uow:
            try:
                return attribution(ctx, uow, report(AGENT2))[0]
            except DomainError as error:
                return error.code

    assert relayed(host.coordinator(BUSINESS)) == AGENT2
    assert relayed(host.agent()) == "forbidden"                  # a current global without the Ordubashy role
    sql, *parameters = change
    registration(host, sql, *parameters, COORDINATOR)
    assert relayed(host.coordinator(BUSINESS)) == "forbidden"

def test_changes_are_described_by_system_entries_in_the_same_thread(host):
    task = host.create_task("Describe", assignee_ids=[AGENT])
    updated = host.data("tasks_update", {"task_id": task["id"], "changes": {"status": "working", "title": "Described"}, "note": "Started"},
                        host.agent(), expected_revision=task["revision"])["task"]
    thread = host.read("tasks_thread_get", {"task_id": task["id"]})
    kinds = [(entry["entry_kind"], entry["body"]) for entry in thread["notes"]]
    assert ("system", "State changed from open to working. Task details changed.") in kinds and ("note", "Started") in kinds
    assert thread["thread_id"] == ThreadItem(BUSINESS, "task", task["id"]).thread_id
    notes_only = host.read("tasks_notes_list", {"task_id": task["id"]})["notes"]
    assert [entry["body"] for entry in notes_only] == ["Started"] and updated["note_count"] == 1
    events = host.query("SELECT event_type FROM inbox_events WHERE source_id=? ORDER BY id", task["id"])
    assert [event["event_type"] for event in events] == ["created"] or events[-1]["event_type"] == "updated,note_added"


def test_only_task_and_action_items_have_threads_and_a_private_conversation_is_never_one(host):
    with pytest.raises(DomainError):
        ThreadItem(BUSINESS, "dm", "conversation-1")
    with pytest.raises(Exception):
        with host.database.transaction() as uow:
            uow.connection.execute(
                "INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,author_id,recorder_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)", (BUSINESS, "m1", "dm", "conversation-1", "comment", "private", HUMAN, HUMAN, "now"))
    assert host.count("thread_messages") == 0
