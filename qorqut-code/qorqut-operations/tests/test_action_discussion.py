"""An Action's discussion: read with the Action's own authorization, write only as a participant of its owning namespace.

The thread is the one canonical item discussion. A comment adds one message, follows and one optional event; it never
adds an Action revision or a Context input, and a read link is never a mutation grant."""

from __future__ import annotations

import pytest

from operational_support import AGENT, AGENT2, BUSINESS, COORDINATOR, HUMAN, OTHER, OTHER_AGENT, OperationalHost, STRANGER
from qorqut_operations.contracts import DomainError
from qorqut_operations.discussion import ThreadItem, follow_state, followers


@pytest.fixture
def host(tmp_path):
    return OperationalHost(tmp_path)


def comment(host, action, text, ctx=None, **extra):
    return host.data("actions_comment", {"action_id": action["action_id"], "text": text, **extra}, ctx or host.agent(AGENT2))


def thread(host, action, ctx=None, **arguments):
    return host.read("actions_thread_get", {"action_id": action["action_id"], **arguments}, ctx or host.agent())


def counts(host):
    return (host.count("action_revisions"), host.count("admitted_revisions"), host.count("action_logs"))


def shared_global(host):
    """A Global-stored business-operational Action that BUSINESS and OTHER can read through their links."""
    return host.data("actions_record", {"text": "Shared result", "category": "business-operational", "business_ids": [BUSINESS, OTHER]},
                     host.coordinator())["action"]


def optional_notes(host, agent):
    return [row for row in host.deliveries(agent, source_type="actions") if row["event_type"] == "note_added"]


def test_a_comment_is_one_message_one_optional_event_and_no_revision_or_queue_input(host):
    action = host.record_action("Shipped the release")
    before = counts(host)
    row = host.query("SELECT * FROM action_logs")[0]
    posted = comment(host, action, "Nice work", summary="Praise")
    assert counts(host) == before and host.query("SELECT * FROM action_logs")[0] == row           # the Action is untouched
    assert posted["action_id"] == action["action_id"] and posted["message"]["body"] == "Nice work"
    assert posted["message"]["author_id"] == AGENT2 and posted["message"]["recorder_id"] == AGENT2 and posted["message"]["summary"] == "Praise"
    assert posted["thread_id"] == ThreadItem(BUSINESS, "action", action["legacy"]["id"]).thread_id
    # Exactly one optional event, scoped to the owning namespace, for the recorder who follows by default; the commenter is the actor.
    events = [row for row in host.query("SELECT * FROM inbox_events WHERE event_type='note_added'")]
    assert len(events) == 1 and events[0]["business_id"] == BUSINESS and events[0]["source_type"] == "actions"
    assert events[0]["source_id"] == action["action_id"] and events[0]["source_revision"] == 1 and events[0]["actor_id"] == AGENT2
    assert [(row["agent_id"], row["required"]) for row in optional_notes(host, AGENT)] == [(AGENT, 0)] and optional_notes(host, AGENT2) == []
    assert host.query("SELECT event_id FROM thread_messages")[0]["event_id"] == events[0]["id"]
    # The commenter now follows its own discussion.
    with host.database.read() as uow:
        item = ThreadItem(BUSINESS, "action", action["legacy"]["id"])
        assert set(followers(uow, item)) == {AGENT, AGENT2} and follow_state(uow, item, AGENT2)["reason"] == "commenter"


def test_the_thread_read_is_bounded_ordered_and_follows_nobody(host):
    action = host.record_action("Discussed")
    for index in range(3):
        host.later(seconds=1)
        comment(host, action, f"Note {index}", host.agent(AGENT2 if index % 2 else AGENT))
    page = thread(host, action, limit=2)
    assert [message["body"] for message in page["messages"]] == ["Note 1", "Note 2"] and page["next_cursor"]
    older = thread(host, action, limit=2, cursor=page["next_cursor"])
    assert [message["body"] for message in older["messages"]] == ["Note 0"] and older["next_cursor"] is None
    assert page["action_id"] == action["action_id"] and {person["created_by"] for person in page["participants"]} >= {AGENT, AGENT2}
    # A cursor belongs to the thread and the reader that got it.
    other = host.record_action("Another")
    assert host.error("actions_thread_get", {"action_id": other["action_id"], "cursor": page["next_cursor"]}, host.agent()) == "validation_error"
    # A reader who has not commented is not made a follower by reading.
    with host.database.read() as uow:
        assert STRANGER not in followers(uow, ThreadItem(BUSINESS, "action", action["legacy"]["id"]))
    # The first comment is the recorder's own (nobody else follows yet); the next two reach the other party as optional events.
    assert host.count("inbox_events", "event_type='note_added'") == 2


def test_reads_use_the_action_read_authorization_and_its_business_links(host):
    own = host.record_action("Business result")
    shared = shared_global(host)
    comment(host, shared, "Coordinator note", host.coordinator())
    comment(host, own, "Business note")
    # The business that merely has a read link reads the shared Global discussion; an unlinked business does not.
    seen = thread(host, shared, host.agent())
    assert [message["body"] for message in seen["messages"]] == ["Coordinator note"]
    assert thread(host, shared, host.agent(OTHER_AGENT, OTHER))["messages"][0]["body"] == "Coordinator note"
    assert host.error("actions_thread_get", {"action_id": own["action_id"]}, host.agent(OTHER_AGENT, OTHER)) == "not_found"
    assert host.error("actions_thread_get", {"action_id": "business/%s/missing" % BUSINESS}, host.agent()) == "not_found"
    # The owner reads everything; a withdrawn Action keeps its readable discussion.
    assert thread(host, own, host.owner())["messages"][0]["body"] == "Business note"
    host.data("actions_withdraw", {"action_id": own["action_id"]}, host.agent(), expected_revision=1)
    assert thread(host, own)["messages"][0]["body"] == "Business note"
    # Thread reads obey their own policy, apart from the Action read.
    host.agents.overrides["actions_thread_get"] = "deny"
    assert host.error("actions_thread_get", {"action_id": shared["action_id"]}, host.agent()) in {"forbidden", "policy_denied"}
    assert host.read("actions_get", {"action_id": shared["action_id"]}, host.agent())["action"]["text"] == "Shared result"


def test_a_read_link_is_never_a_mutation_grant(host):
    shared = shared_global(host)
    for ctx in (host.agent(), host.agent(OTHER_AGENT, OTHER), host.stranger()):
        assert host.error("actions_comment", {"action_id": shared["action_id"], "text": "Linked only"}, ctx) in {"forbidden", "policy_denied"}
    assert host.count("thread_messages") == 0 and host.count("inbox_events", "event_type='note_added'") == 0
    # The owning namespace (Global: the coordinator) writes; a business Action is written by its own business.
    assert comment(host, shared, "Mine", host.coordinator())["message"]["recorder_id"] == COORDINATOR
    own = host.record_action("Business result")
    assert host.error("actions_comment", {"action_id": own["action_id"], "text": "Elsewhere"}, host.agent(OTHER_AGENT, OTHER)) in {"not_found", "forbidden"}
    assert host.error("actions_comment", {"action_id": own["action_id"], "text": "Unknown"}, host.stranger()) in {"forbidden", "policy_denied", "not_found"}
    # The coordinator is a registered participant of each business it may act in, so it writes there like any member.
    assert comment(host, own, "Coordinator in the business", host.coordinator())["message"]["recorder_id"] == COORDINATOR


def test_comments_need_their_own_policy_and_an_explicit_deny_stops_them(host):
    action = host.record_action("Policy")
    host.agents.overrides["actions_comment"] = "deny"
    assert host.error("actions_comment", {"action_id": action["action_id"], "text": "Denied"}, host.agent(AGENT2)) in {"forbidden", "policy_denied"}
    assert host.count("thread_messages") == 0
    host.agents.overrides["actions_comment"] = "signoff"
    signoff_id = host.stage("actions_comment", {"action_id": action["action_id"], "text": "After approval"}, host.agent(AGENT2))
    assert host.count("thread_messages") == 0
    assert host.apply_approved(signoff_id)["state"] == "applied"
    assert [message["body"] for message in thread(host, action)["messages"]] == ["After approval"]
    assert counts(host) == (1, 1, 1)


def test_the_owner_uses_the_private_channel_not_the_shared_action_thread(host):
    action = host.record_action("Owner sees it")
    assert host.error("actions_comment", {"action_id": action["action_id"], "text": "From the owner"}, host.owner(BUSINESS)) == "channel_required"
    assert host.count("thread_messages") == 0


def test_an_unfollow_is_sticky_and_a_followed_discussion_is_told_again(host):
    action = host.record_action("Sticky")
    host.data("actions_follow", {"action_id": action["action_id"], "following": False}, host.agent(AGENT))
    comment(host, action, "One")
    comment(host, action, "Two", host.agent(AGENT))              # commenting never re-follows an explicit unfollow
    with host.database.read() as uow:
        assert follow_state(uow, ThreadItem(BUSINESS, "action", action["legacy"]["id"]), AGENT) == {"following": False, "explicit": True, "reason": "explicit"}
    assert optional_notes(host, AGENT) == []
    host.data("actions_follow", {"action_id": action["action_id"], "following": True}, host.agent(AGENT))
    comment(host, action, "Three")
    assert len(optional_notes(host, AGENT)) == 1


def test_a_registered_coordinators_owner_relay_follows_both_parties_and_is_never_a_grant(host):
    shared = shared_global(host)
    relayed = host.data("actions_comment", {"action_id": shared["action_id"], "text": "The owner agrees",
                                            "relay": {"kind": "explicit_owner_statement", "owner_id": HUMAN,
                                                      "statement_text": "I agree"}}, host.coordinator())
    message = relayed["message"]
    assert message["author_id"] == HUMAN and message["recorder_id"] == COORDINATOR
    assert message["attribution"]["kind"] == "explicit_owner_statement" and message["attribution"]["owner_id"] == HUMAN
    with host.database.read() as uow:
        item = ThreadItem(None, "action", shared["legacy"]["id"])
        assert follow_state(uow, item, COORDINATOR)["reason"] in {"commenter", "creator"} and follow_state(uow, item, HUMAN)["reason"] == "represented"
    # Only the registered coordinator relays, and only the registered owner's statement.
    own = host.record_action("Business result")
    assert host.error("actions_comment", {"action_id": own["action_id"], "text": "Fake", "relay": {
        "kind": "explicit_owner_statement", "owner_id": HUMAN, "statement_text": "I agree"}}, host.agent(AGENT2)) == "forbidden"
    assert host.error("actions_comment", {"action_id": shared["action_id"], "text": "Wrong owner", "relay": {
        "kind": "explicit_owner_statement", "owner_id": STRANGER, "statement_text": "I agree"}}, host.coordinator()) == "validation_error"
    # A relay carries no extra authority: the coordinator's own comment policy still decides.
    host.agents.overrides["actions_comment"] = "deny"
    assert host.error("actions_comment", {"action_id": shared["action_id"], "text": "Owner agrees again", "relay": {
        "kind": "explicit_owner_statement", "owner_id": HUMAN, "statement_text": "I agree"}}, host.coordinator()) in {"forbidden", "policy_denied"}


def test_note_references_use_the_task_reference_ports_and_validation(host):
    action = host.record_action("With references")
    ticket = {"type": "dev_ticket", "id": "T-1"}
    posted = comment(host, action, "See the ticket", note_refs=[ticket])
    assert posted["message"]["refs"] == [{**ticket, "label": "Development ticket T-1", "state": "open"}]
    # The same validation as a Task note: distinct references and a note when references or a summary are supplied.
    assert host.error("actions_comment", {"action_id": action["action_id"], "text": "Twice", "note_refs": [ticket, ticket]}, host.agent(AGENT2)) == "validation_error"
    assert host.error("actions_comment", {"action_id": action["action_id"], "text": "   "}, host.agent(AGENT2)) == "validation_error"
    assert host.error("actions_comment", {"action_id": action["action_id"], "text": "Bad", "note_refs": [{"type": "unknown", "id": "x"}]},
                      host.agent(AGENT2)) == "validation_error"
    assert thread(host, action)["messages"][0]["refs"][0]["label"] == "Development ticket T-1"
    assert host.incidents.links == []                       # an Action comment links no Task to the ticket
    assert host.count("thread_messages") == 1


def test_a_discussion_belongs_to_its_item_and_a_private_conversation_never_enters_it(host):
    action = host.record_action("Separate")
    task = host.create_task("A task")
    comment(host, action, "On the action")
    host.data("tasks_comment", {"task_id": task["id"], "text": "On the task"}, expected_revision=task["revision"])
    assert [message["body"] for message in thread(host, action)["messages"]] == ["On the action"]
    # A Task id is not an Action thread, and a private conversation (not an item kind) cannot be one.
    assert host.error("actions_thread_get", {"action_id": task["id"]}, host.agent()) == "not_found"
    with pytest.raises(DomainError):
        ThreadItem(BUSINESS, "dm", "conversation-1")
    assert host.count("thread_messages", "item_type='action'") == 1 and host.count("thread_messages", "item_type='task'") == 1


def test_the_new_operations_are_registered_with_exact_metadata(host):
    thread_get, post = (host.registry.get(name) for name in ("actions_thread_get", "actions_comment"))
    assert thread_get.effect_kind == "read" and post.effect_kind != "read"
    assert set(thread_get.input_schema["properties"]) == {"action_id", "limit", "cursor"} and list(thread_get.input_schema["required"]) == ["action_id"]
    assert set(post.input_schema["properties"]) == {"action_id", "text", "summary", "note_refs", "relay", "rationale"}
    assert list(post.input_schema["required"]) == ["action_id", "text"]
    assert post.input_schema["properties"]["note_refs"]["items"]["properties"]["type"]["enum"] == tuple(host.references.types)
