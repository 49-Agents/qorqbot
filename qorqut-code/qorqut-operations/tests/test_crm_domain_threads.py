"""The shared discussion of a Table (ordinary or CRM): comments, follows, structured membership developments, privacy and
the one-event-per-semantic-change inbox mapping."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from crm_support import CRM_COLUMNS, STAGES, CRMHost, ref_of
from operational_support import AGENT, AGENT2, BUSINESS, COORDINATOR, HUMAN, OTHER
from qorqut_operations.contracts import DomainError, new_id
from qorqut_operations.inbox import THREAD_SOURCES, InboxService


@pytest.fixture
def host(tmp_path):
    return CRMHost(tmp_path)


def tick(host):
    host.later(seconds=1)


def comment(host, table, text="A note", who=AGENT, **extra):
    tick(host)
    return host.data("tables_comment", {"table_id": table["id"], "text": text, **extra}, host.agent(who))


def follows(host, table):
    return {row["principal_id"]: (row["following"], row["reason"]) for row in host.query(
        "SELECT * FROM thread_follows WHERE item_type='table' AND item_id=?", table["id"])}


# -- comments and follows ---------------------------------------------------------------------------------------------------

def test_any_table_has_a_shared_discussion_with_attribution_and_default_follows(host):
    general = host.make("Plain", ctx=host.agent(AGENT))["table"]
    crm = host.crm_table("Pipeline", host.agent(AGENT2))
    for table, creator in ((general, AGENT), (crm, AGENT2)):
        assert follows(host, table) == {creator: (1, "creator")}
        spoken = comment(host, table, "First note", who=AGENT2 if creator == AGENT else AGENT)
        assert spoken["thread_id"] and spoken["message"]["kind"] == "comment" and spoken["message"]["body"] == "First note"
        assert spoken["table"]["revision"] == table["revision"] == 1           # a comment is not a data change
        page = host.thread(table, host.agent(creator))
        assert [m["body"] for m in page["messages"]] == ["First note"] and page["following"] is True and page["follow_reason"] == "creator"
        assert page["participants"] and page["thread_id"] == spoken["thread_id"]
    assert follows(host, general)[AGENT2] == (1, "commenter")
    # Following the Table follows nothing else: not the members' Tasks, not other Tables.
    task = host.create_task("Member")
    host.add(crm, task, ctx=host.agent(AGENT2))
    assert host.query("SELECT principal_id FROM thread_follows WHERE item_type='task' AND item_id=?", task["id"]) == [{"principal_id": HUMAN}]
    assert host.count("thread_follows", "item_type='table'") == 4


def test_an_explicit_unfollow_is_sticky_and_a_follow_is_personal(host):
    table = host.make("Plain", ctx=host.agent(AGENT))["table"]
    peer = host.agent(AGENT2)
    comment(host, table, who=AGENT2)
    assert follows(host, table)[AGENT2] == (1, "commenter")
    state = host.data("tables_follow", {"table_id": table["id"], "following": False}, peer)
    assert state["following"] is False and state["reason"] == "explicit" and state["table_id"] == table["id"]
    comment(host, table, "Again", who=AGENT2)                     # commenting again never undoes a choice
    assert follows(host, table)[AGENT2] == (0, "explicit") and follows(host, table)[AGENT] == (1, "creator")
    assert host.thread(table, peer)["following"] is False
    assert host.data("tables_follow", {"table_id": table["id"], "following": True}, peer)["following"] is True
    assert host.error("tables_follow", {"table_id": table["id"], "following": True}, host.agent(AGENT2, business=OTHER)) == "not_found"


def test_comments_follow_the_scope_the_owner_channel_and_the_archive_rules(host):
    table = host.crm_table()
    assert host.error("tables_comment", {"table_id": table["id"], "text": "x"}, host.owner()) == "channel_required"   # same rule as Task/Action threads
    assert host.error("tables_comment", {"table_id": table["id"], "text": "   "}, host.agent(AGENT)) == "validation_error"
    assert host.error("tables_comment", {"table_id": table["id"], "text": "x"}, host.agent(AGENT, business=OTHER)) == "not_found"
    assert host.error("tables_comment", {"table_id": table["id"], "text": "x"}, host.agent(AGENT), expected_revision=9) == "revision_conflict"
    assert host.data("tables_comment", {"table_id": table["id"], "text": "pinned"}, host.agent(AGENT), expected_revision=1)["message"]["body"] == "pinned"
    host.grants[AGENT2] = {"tables_comment": "deny"}
    assert host.error("tables_comment", {"table_id": table["id"], "text": "x"}, host.agent(AGENT2)) == "policy_denied"
    # An archived Table stays readable and can still document its archive; its data stays frozen until it is restored.
    archived = host.data("tables_archive", {"table_id": table["id"]}, expected_revision=1)["table"]
    note = comment(host, archived, "Archived because the deal closed")
    assert note["table"]["archived"] is True and [m["body"] for m in host.thread(archived)["messages"]][-1].startswith("Archived because")
    task = host.create_task("Late")
    assert host.error("crm_entries_add", {"table_id": table["id"], "record": ref_of(task)}, expected_revision=archived["revision"]) == "validation_error"


def test_note_references_and_paging_work_like_other_threads(host):
    table = host.crm_table()
    ticket = {"type": "dev_ticket", "id": "T-1"}
    spoken = comment(host, table, "See the ticket", note_refs=[ticket])
    assert spoken["message"]["refs"][0]["label"] == "Development ticket T-1" and spoken["message"]["refs"][0]["id"] == "T-1"
    assert host.error("tables_comment", {"table_id": table["id"], "text": "Twice", "note_refs": [ticket, ticket]}, host.agent(AGENT)) == "validation_error"
    assert host.error("tables_comment", {"table_id": table["id"], "text": "Bad", "note_refs": [{"type": "unknown", "id": "x"}]}, host.agent(AGENT)) == "validation_error"
    for number in range(4):
        comment(host, table, f"note {number}")
    first = host.thread(table, limit=2)
    assert len(first["messages"]) == 2 and first["next_cursor"]
    second = host.thread(table, limit=2, cursor=first["next_cursor"])
    assert not {m["id"] for m in first["messages"]} & {m["id"] for m in second["messages"]}


def test_global_table_threads_need_the_global_rule(host):
    owner = host.owner(business=None)
    table = host.data("tables_create", {"title": "G", "table_kind": "crm", "columns": CRM_COLUMNS, "crm_config": STAGES, "scope": "global"}, owner)["table"]
    assert host.error("tables_comment", {"table_id": table["id"], "scope": "global", "text": "x"}, host.eligible()) == "policy_denied"
    assert host.error("tables_thread_get", {"table_id": table["id"], "scope": "global"}, host.agent(AGENT)) == "forbidden"
    spoken = host.data("tables_comment", {"table_id": table["id"], "scope": "global", "text": "global note"}, host.coordinator())
    assert spoken["message"]["item_type"] == "table" and spoken["message"]["recorder_id"] == COORDINATOR
    page = host.read("tables_thread_get", {"table_id": table["id"], "scope": "global"}, host.coordinator())
    assert [m["body"] for m in page["messages"]] == ["global note"]
    assert host.error("tables_thread_get", {"table_id": table["id"]}, host.owner()) == "not_found"       # a business view never reads Global


# -- structured membership developments ---------------------------------------------------------------------------------------------

def test_membership_changes_are_structured_generic_and_never_copy_task_data(host):
    table = host.crm_table()
    task = host.create_task("Confidential Acme", task_type="customer", customer_profile={"email": "secret@acme.test"})
    tick(host)
    added = host.add(table, task, cells={"value": 9, "notes": "private note"}, stage_id="lead")
    entry = added["entry"]["id"]
    tick(host)
    updated = host.data("crm_entries_update", {"table_id": table["id"], "entry_id": entry, "changes": {"cells": {"value": 10}, "stage_id": "won"}},
                        expected_revision=added["table"]["revision"])
    tick(host)
    removed = host.data("crm_entries_remove", {"table_id": table["id"], "entry_id": entry}, expected_revision=updated["table"]["revision"])
    tick(host)
    restored = host.add(removed["table"], task)
    host.data("crm_entries_update", {"table_id": table["id"], "entry_id": entry, "changes": {"cells": {"value": 10}}}, expected_revision=restored["table"]["revision"])
    messages = host.thread(table)["messages"]
    assert [m["change"]["operation"] for m in messages] == ["added", "updated", "removed", "restored"]    # the no-op update wrote nothing
    first, second = messages[0]["change"], messages[1]["change"]
    assert first["kind"] == "crm_membership" and first["entry_id"] == entry and first["record"] == ref_of(task) | {"type": "task"}
    assert first["changed_fields"] == ["notes", "value"] and first["stage"] == {"from": None, "to": "lead"}
    assert second["changed_fields"] == ["value"] and second["stage"] == {"from": "lead", "to": "won"}
    assert all(m["kind"] == "system" and m["body"] in {"An entry was added.", "An entry was updated.", "An entry was removed.", "An entry was restored."}
               for m in messages)
    stored = " ".join(f"{row['body']} {row['change_json']}" for row in host.query("SELECT body, change_json FROM thread_messages WHERE item_type='table'"))
    for leaked in ("Confidential", "secret@acme.test", "private note"):
        assert leaked not in stored, leaked
    assert messages[0]["event_id"] is None and all(m["author_id"] is None for m in messages)


def test_a_reader_without_task_access_sees_a_generic_development_only(host):
    table = host.crm_table()
    owner = host.owner(business=None)
    hidden = host.data("tasks_create", {"title": "Global secret", "scope": "global"}, owner)["task"]
    shown = host.create_task("Readable Corp")
    host.add(table, hidden)
    tick(host)
    host.add(host.get(table), shown)
    agent = host.agent(AGENT)       # no Global capability
    messages = host.thread(table, agent)["messages"]
    assert messages[0]["change"] == {"kind": "crm_membership", "entry_id": messages[0]["change"]["entry_id"], "operation": "added", "unavailable": True}
    assert messages[1]["change"]["record"]["id"] == shown["id"] and messages[1]["change"]["record_label"] == "Readable Corp"
    assert hidden["id"] not in repr(messages) and "Global secret" not in repr(messages)
    full = host.thread(table)["messages"]
    assert full[0]["change"]["record"]["id"] == hidden["id"] and full[0]["change"]["record_label"] == "Global secret"


def test_creating_a_record_with_its_entry_writes_one_development_and_the_tasks_own_history(host):
    table = host.crm_table()
    made = host.add(table, create={"title": "Fresh"})
    assert [m["change"]["operation"] for m in host.thread(table)["messages"]] == ["added"]
    task_id = made["entry"]["record"]["id"]
    assert host.count("thread_messages", "item_type='table'") == 1 and host.count("thread_messages", "item_type='task' AND item_id=?", task_id) == 0
    assert host.query("SELECT principal_id, reason FROM thread_follows WHERE item_type='task' AND item_id=?", task_id) == [
        {"principal_id": HUMAN, "reason": "creator"}]


# -- inbox: one announcement per semantic change -----------------------------------------------------------------------------------------

def test_a_comment_is_announced_once_to_its_followers_and_never_to_the_speaker(host):
    table = host.make("Plain", ctx=host.agent(AGENT))["table"]
    comment(host, table, "warm up", who=AGENT2)
    host.forget_changes()
    before = len(host.events())
    spoken = comment(host, table, "Anything new?", who=AGENT)
    host.finalize()
    announced = host.events()[before:]
    assert [(e["source_type"], e["event_type"]) for e in announced] == [("data_tables", "note_added")] and announced[0]["source_id"] == table["id"]
    assert [row["agent_id"] for row in host.deliveries(source_type="data_tables")][-1:] == [AGENT2]
    assert host.query("SELECT event_id FROM thread_messages WHERE id=?", spoken["message"]["id"])[0]["event_id"] == announced[0]["id"]


def test_data_changes_are_announced_by_the_capture_mapping_exactly_once(host):
    crm = host.crm_table("Pipeline", host.agent(AGENT))
    general = host.make("Plain", ctx=host.agent(AGENT))["table"]
    for table in (crm, general):
        comment(host, table, "subscribe me", who=AGENT2)
    task = host.create_task("Acme")
    host.forget_changes()
    start = len(host.events())
    host.add(crm, task, ctx=host.agent(AGENT))
    host.finalize()
    crm_events = host.events()[start:]
    host.forget_changes()
    start = len(host.events())
    host.write(general, [{"cells": {"name": "row"}}], revision=1, ctx=host.agent(AGENT))
    host.finalize()
    general_events = host.events()[start:]
    # A membership change is announced exactly as a general row batch is: one event on the Table, no second comment-style event.
    assert len(crm_events) == len(general_events) == 1
    assert crm_events[0]["source_type"] == general_events[0]["source_type"] == "data_tables"
    assert crm_events[0]["event_type"] == general_events[0]["event_type"] and "rows_changed" in crm_events[0]["event_type"]
    assert "note_added" not in crm_events[0]["event_type"]
    recipients = [row["agent_id"] for row in host.deliveries(source_type="data_tables") if row["event_id"] == crm_events[0]["id"]]
    assert recipients == [AGENT2]          # the follower; never the actor who made the change
    # An unfollowed principal hears nothing more.
    host.data("tables_follow", {"table_id": crm["id"], "following": False}, host.agent(AGENT2))
    host.forget_changes()
    start = len(host.events())
    host.data("crm_entries_add", {"table_id": crm["id"], "create": {"title": "Second"}}, host.agent(AGENT), expected_revision=host.get(crm)["revision"])
    host.finalize()
    assert [row for row in host.deliveries(AGENT2, source_type="data_tables") if row["event_id"] > crm_events[0]["id"] and row["source_id"] == crm["id"]] == []
    assert len(host.events()) == start         # nobody is left to receive it, so no event is written


def test_create_and_add_announces_the_task_through_its_own_engine_and_the_table_once(host):
    crm = host.crm_table("Pipeline", host.agent(AGENT))
    comment(host, crm, "follow", who=AGENT2)
    host.forget_changes()
    start = len(host.events())
    host.data("crm_entries_add", {"table_id": crm["id"], "create": {"title": "Both"}}, host.agent(AGENT), expected_revision=1)
    host.finalize()
    sources = [e["source_type"] for e in host.events()[start:] if e["source_type"] in {"data_tables", "tasks"}]
    assert sources.count("data_tables") == 1


# -- the inbox follow surface ------------------------------------------------------------------------------------------------------------------

def test_tables_are_followed_through_the_discussion_follow_state(host):
    assert THREAD_SOURCES["data_tables"] == "table"
    table = host.make("Plain", ctx=host.agent(AGENT))["table"]
    related = SimpleNamespace(get=lambda ctx, uow, source, identity, allow_busy=False: host.tables.table(ctx, identity, uow))
    inbox_host = SimpleNamespace(
        managed_subject=lambda ctx, target, uow: ctx, business_member=lambda ctx, business, uow: True,
        private_visible=lambda *a, **k: None, integration_visible=lambda *a, **k: None, event_visible=lambda *a, **k: None,
        decorate_before_extensions=lambda *a, **k: None, decorate_after_extensions=lambda *a, **k: None,
        active_event_claim=lambda *a, **k: None, augment_subscriptions=lambda service, ctx, result, uow: result)
    service = InboxService(host.app, items=host.items, related=related, host=inbox_host,
                           sources=SimpleNamespace(get=lambda key: None, item_types={}, subscribable=()),
                           source_types=("tasks", "data_tables"), item_types={"tasks": "task", "data_tables": "table"},
                           message_factory=lambda service: SimpleNamespace(), clock=host.clock)
    peer = host.agent(AGENT2)
    args = {"source_type": "data_tables", "source_id": table["id"]}
    with host.database.transaction() as uow:
        ctx = service.validate_mutation("inbox_subscribe", peer, args, uow)
        assert service.follow(ctx, args, uow) == {"following": True, **args}
        # An older plain subscription to the Table would otherwise keep delivering after an unfollow.
        uow.connection.execute("INSERT INTO inbox_subscriptions(id,agent_id,business_id,source_type,source_id,include_related,created_at) VALUES(?,?,?,?,?,0,'now')",
                               (new_id(), AGENT2, BUSINESS, "data_tables", table["id"]))
        listed = [item for item in service.subscriptions(ctx, uow) if item["source_type"] == "data_tables"]
        assert len(listed) == 1 and listed[0]["managed_by"] == "thread_follow" and listed[0]["source_id"] == table["id"]
        assert service.unfollow(ctx, args, uow)["following"] is False
        assert uow.connection.execute("SELECT count(*) FROM inbox_subscriptions WHERE source_type='data_tables'").fetchone()[0] == 0
        assert [item for item in service.subscriptions(ctx, uow) if item["source_type"] == "data_tables"] == []
    assert follows(host, table)[AGENT2] == (0, "explicit")
    with host.database.transaction() as uow:
        with pytest.raises(DomainError):
            service.validate_mutation("inbox_subscribe", peer, {"source_type": "data_tables", "source_id": new_id()}, uow)


# -- one preference across the UI/API and inbox surfaces ----------------------------------------------------------------------------

def seed_subscription(host, agent, business, table_id):
    """A pre-upgrade explicit subscription to a Table (the shape an older installation holds)."""
    with host.database.transaction() as uow:
        uow.connection.execute("INSERT INTO inbox_subscriptions(id,agent_id,business_id,source_type,source_id,include_related,created_at) "
                               "VALUES(?,?,?,'data_tables',?,0,'now')", (new_id(), agent, business, table_id))


def subscribed(host, table_id):
    return {(row["agent_id"], row["business_id"]) for row in host.query(
        "SELECT agent_id, business_id FROM inbox_subscriptions WHERE source_type='data_tables' AND source_id=?", table_id)}


def test_a_direct_table_unfollow_also_removes_the_pre_upgrade_subscription(host):
    table = host.crm_table("Pipeline", host.agent(AGENT))
    for agent, business in ((AGENT2, BUSINESS), (AGENT, BUSINESS), (AGENT2, OTHER)):      # the last is the same id in a foreign scope
        seed_subscription(host, agent, business, table["id"])
    peer = host.agent(AGENT2)
    assert host.data("tables_follow", {"table_id": table["id"], "following": False}, peer)["following"] is False
    # only this agent's subscription to this scope's Table went; other agents, other scopes and the sticky follow are unaffected
    assert subscribed(host, table["id"]) == {(AGENT, BUSINESS), (AGENT2, OTHER)} and follows(host, table)[AGENT2] == (0, "explicit")
    # future data changes and comments no longer reach the unfollowed agent, but still reach the one who kept its subscription
    host.forget_changes()
    host.add(table, host.create_task("Acme"))
    tick(host)
    comment(host, table, "A later note", who=AGENT)
    host.finalize()
    reached = {row["agent_id"] for row in host.deliveries(source_type="data_tables")}
    assert AGENT2 not in reached and AGENT in reached
    # an explicit re-follow is the new preference
    assert host.data("tables_follow", {"table_id": table["id"], "following": True}, peer)["following"] is True
    host.forget_changes()
    host.add(host.get(table), host.create_task("Beta"))
    host.finalize()
    assert AGENT2 in {row["agent_id"] for row in host.deliveries(source_type="data_tables")}


def test_the_inbox_unfollow_route_and_the_direct_route_agree(host):
    table = host.make("Plain", ctx=host.agent(AGENT))["table"]
    seed_subscription(host, AGENT2, BUSINESS, table["id"])
    seed_subscription(host, AGENT, BUSINESS, table["id"])
    related = SimpleNamespace(get=lambda ctx, uow, source, identity, allow_busy=False: host.tables.table(ctx, identity, uow))
    inbox_host = SimpleNamespace(
        managed_subject=lambda ctx, target, uow: ctx, business_member=lambda ctx, business, uow: True,
        private_visible=lambda *a, **k: None, integration_visible=lambda *a, **k: None, event_visible=lambda *a, **k: None,
        decorate_before_extensions=lambda *a, **k: None, decorate_after_extensions=lambda *a, **k: None,
        active_event_claim=lambda *a, **k: None, augment_subscriptions=lambda service, ctx, result, uow: result)
    service = InboxService(host.app, items=host.items, related=related, host=inbox_host,
                           sources=SimpleNamespace(get=lambda key: None, item_types={}, subscribable=()),
                           source_types=("tasks", "data_tables"), item_types={"tasks": "task", "data_tables": "table"},
                           message_factory=lambda service: SimpleNamespace(), clock=host.clock)
    args = {"source_type": "data_tables", "source_id": table["id"]}
    with host.database.transaction() as uow:
        ctx = service.validate_mutation("inbox_unsubscribe", host.agent(AGENT2), args, uow)
        service.unfollow(ctx, args, uow)
    assert subscribed(host, table["id"]) == {(AGENT, BUSINESS)}
    host.data("tables_follow", {"table_id": table["id"], "following": False}, host.agent(AGENT))
    assert subscribed(host, table["id"]) == set()


# -- typed references are re-resolved when a comment is applied --------------------------------------------------------------------------

def revoked(*args, **kwargs):
    raise DomainError("forbidden", "Synthetic referenced source revoked")


def test_a_staged_comment_revalidates_its_references_when_it_is_applied(host, monkeypatch):
    table = host.crm_table()
    host.grants[AGENT] = {"tables_comment": "signoff"}
    agent, ticket = host.agent(AGENT), {"type": "dev_ticket", "id": "T-9"}
    fine = host.stage("tables_comment", {"table_id": table["id"], "text": "First", "note_refs": [ticket]}, agent)
    lost = host.stage("tables_comment", {"table_id": table["id"], "text": "Second", "note_refs": [ticket]}, agent)
    assert host.count("thread_messages", "item_type='table'") == 0
    # references still readable: applied as prepared
    assert host.apply_approved(fine)["state"] == "applied"
    comments = lambda: host.count("thread_messages", "item_type='table' AND kind='comment'")
    assert comments() == 1 and host.get(table)["revision"] == table["revision"]
    # the referenced source's authority is revoked without any change to the Table row
    monkeypatch.setattr(host.tasks.ports.references, "resolve", revoked)
    approved = host.approve(lost)
    if approved.status == "accepted":
        host.service.run_one(lost)
    assert comments() == 1 and host.row(lost)["state"] != "applied" and host.get(table)["revision"] == table["revision"]
    # historical visibility is a separate check: the earlier comment now shows its reference as unavailable
    assert host.thread(table)["messages"][0]["refs"] == [{"unavailable": True}]


def test_saved_comment_and_follow_receipts_are_returned_only_while_their_authority_stands(host, monkeypatch):
    table = host.crm_table()
    agent, ticket, key, follow_key = host.agent(AGENT), {"type": "dev_ticket", "id": "T-9"}, new_id(), new_id()
    arguments = {"table_id": table["id"], "text": "Linked", "note_refs": [ticket]}
    first = host.invoke("tables_comment", arguments, agent, idempotency_key=key)
    followed = host.invoke("tables_follow", {"table_id": table["id"], "following": True}, agent, idempotency_key=follow_key)
    assert first.status == followed.status == "ok"
    same = host.invoke("tables_comment", arguments, agent, idempotency_key=key)
    assert same.status == "ok" and same.data == first.data and comment_count(host) == 1
    host.grants[AGENT] = {"tables_rows_write": "deny"}
    assert host.invoke("tables_comment", arguments, agent, idempotency_key=key).error["code"] == "policy_denied"
    host.grants[AGENT] = {"tables_get": "deny"}
    assert host.invoke("tables_follow", {"table_id": table["id"], "following": True}, agent, idempotency_key=follow_key).error["code"] == "policy_denied"
    host.grants[AGENT] = {}
    assert host.invoke("tables_follow", {"table_id": table["id"], "following": True}, agent, idempotency_key=follow_key).status == "ok"
    monkeypatch.setattr(host.tasks.ports.references, "resolve", revoked)
    refused = host.invoke("tables_comment", arguments, agent, idempotency_key=key)
    assert refused.status == "error" and refused.error["code"] == "forbidden" and comment_count(host) == 1


def comment_count(host):
    return host.count("thread_messages", "item_type='table' AND kind='comment'")
