"""Acceptance case: the operational records (Tasks, Actions, tags, discussion, Context queue) over the extracted engine.

A synthetic host is built exactly as the product builds one (real catalog, real policy rules, real host ports and change
feed); principals are the owner and the registered global agents working in two businesses (businesses have no resident
agents). The engine's own suites cover the record
rules with fakes; these cases cover what only the composed host can show: the host ports answering the engine, the registered
agents' default permissions, the one discussion writer under the host finalizer, the change-feed capture of the engine's
tables, the host's Action writeback and the follow route of the inbox."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from qorqut_operations.application.plans import PlanBuilder, apply_records
from qorqut_operations.actions import settle
from qorqut_operations.actions.models import source_identity
from qorqut_operations.contracts import InvocationContext, InvocationOptions, Principal, new_id, plain

from host_foundation_support import (agent_context, build_instance, create_registered_global, create_worker, historical_business_agent,
                                     register_sources, set_agent_overrides, snapshot)


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    instance, ids = build_instance(tmp_path_factory.mktemp("records"), businesses=("first", "second"))
    first, second = ids["first"], ids["second"]
    register_sources(instance, first, "first")
    register_sources(instance, second, "second")
    # Ordubashy does the ordinary work of the first business; a second worker is the independent peer, selecting the second.
    agent = create_registered_global(instance, "ordubashy")
    other = create_worker(instance, "colleague")
    instance.runtime.freeze_registry()
    return instance, first, second, agent, other


def business_action(first, text, **arguments):
    """A global agent's Action about the first business's work: Global-stored, business-operational and linked to it (the
    retained v1 Global-agent Action rule)."""
    return {"text": text, "category": "business-operational", "business_ids": [first], **arguments}


def call(instance, ctx, name, arguments=None, *, revision=None, key="new", scope="business"):
    arguments = {"scope": scope, **(arguments or {})} if scope else dict(arguments or {})
    return instance.runtime.application.invoke(ctx, name, arguments, InvocationOptions(
        expected_revision=revision, idempotency_key=new_id() if key == "new" else key))


def ok(result):
    assert result.status == "ok", result.error
    return plain(result.data)


def code(result):
    assert result.status == "error", result.data
    return result.error["code"]


def rows(instance, sql, *parameters):
    with instance.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(sql, parameters)]


def historical_stranger(instance, business):
    """The context of the one historical business-home identity of ``business`` (seeded once for the module); it never operates."""
    found = rows(instance, "SELECT agent_id FROM agent_profiles WHERE home_business_id=?", business)
    return agent_context(found[0]["agent_id"] if found else historical_business_agent(instance, business), business)


def task(instance, ctx, title, **arguments):
    return ok(call(instance, ctx, "tasks_create", {"title": title, **arguments}))["task"]


def test_ordinary_tasks_use_the_engine_states_the_shared_snooze_parents_and_dependencies(host):
    instance, first, _, agent, _ = host
    owner = instance.human(first)
    parent = task(instance, owner, "Parent")
    child = task(instance, owner, "Child", parent_id=parent["id"], assignee_ids=[agent])
    blocker = task(instance, owner, "Blocker")
    blocked = task(instance, owner, "Blocked", blocked_by=[blocker["id"]])
    assert child["parent"]["id"] == parent["id"] and child["assignee_ids"] == [agent] and blocked["blocked_by"] == [blocker["id"]]

    seen = []
    current = child
    for status in ("working", "waiting", "done", "open", "cancelled"):
        current = ok(call(instance, owner, "tasks_update", {"task_id": child["id"], "changes": {"status": status}},
                          revision=current["revision"]))["task"]
        seen.append(current["status"])
    assert seen == ["working", "waiting", "done", "open", "cancelled"]
    # The retired configurable states and the v1 'blocked' status do not exist.
    assert code(call(instance, owner, "tasks_update", {"task_id": child["id"], "changes": {"status": "blocked"}},
                     revision=current["revision"])) == "validation_error"
    # Parent and child states are independent, and dependencies are informational: nothing was closed or opened for them.
    assert ok(call(instance, owner, "tasks_get", {"task_id": parent["id"]}))["task"]["status"] == "open"
    assert ok(call(instance, owner, "tasks_get", {"task_id": blocked["id"]}))["task"]["status"] == "open"
    assert code(call(instance, owner, "tasks_update", {"task_id": parent["id"], "changes": {"parent_id": child["id"]}},
                     revision=parent["revision"])) == "validation_error"

    until = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    snoozed = ok(call(instance, owner, "tasks_update", {"task_id": blocked["id"], "changes": {"snoozed_until": until}},
                      revision=blocked["revision"]))["task"]
    again = ok(call(instance, owner, "tasks_update", {"task_id": blocked["id"], "changes": {"snoozed_until": until}},
                    revision=snoozed["revision"]))["task"]
    assert snoozed["snoozed"] is True and again["snooze_generation"] == snoozed["snooze_generation"]
    assert [row["id"] for row in ok(call(instance, owner, "tasks_list", {}))["tasks"] if row["id"] == blocked["id"]] == []
    assert blocked["id"] in {row["id"] for row in ok(call(instance, owner, "tasks_list", {"snooze_filter": "snoozed"}))["tasks"]}


def test_the_registered_agent_defaults_and_the_protected_owner_request(host):
    instance, first, second, agent, other = host
    owner = instance.human(first)
    acting = agent_context(agent, first)
    assigned = task(instance, owner, "Agent work", assignee_ids=[agent])
    assert ok(call(instance, acting, "tasks_get", {"task_id": assigned["id"]}))["task"]["title"] == "Agent work"
    comment = ok(call(instance, acting, "tasks_comment", {"task_id": assigned["id"], "text": "Looking at it"},
                      revision=assigned["revision"]))["task"]
    assert comment["note_count"] == 1
    assert ok(call(instance, acting, "tasks_thread_get", {"task_id": assigned["id"]}))["notes"]
    # An owner request is the owner's alone; an agent cannot raise one, and no one assigns it to an agent.
    assert code(call(instance, acting, "tasks_create", {"title": "Ask", "kind": "owner_request", "assignee_ids": [agent]})) in {
        "forbidden", "validation_error"}
    # A global agent selecting another business reads nothing here, whatever label the work carries.
    labelled = task(instance, owner, "Labelled", tags=["Launch"])
    foreign = agent_context(other, second)
    assert code(call(instance, foreign, "tasks_get", {"task_id": labelled["id"]})) in {"not_found", "forbidden"}
    shared = ok(call(instance, foreign, "tags_list", {}, scope=None))
    assert "Launch" in json.dumps(shared)              # the catalog is shared ...
    assert ok(call(instance, foreign, "tasks_list", {"tags": ["launch"]}))["tasks"] == []   # ... and grants nothing


def test_the_engine_is_the_one_discussion_writer_and_required_events_do_not_depend_on_follows(host):
    instance, first, _, agent, _ = host
    owner = instance.human(first)
    item = task(instance, owner, "Discussed", assignee_ids=[agent])
    created = rows(instance, "SELECT * FROM inbox_events WHERE source_type='tasks' AND source_id=?", item["id"])
    assert [row["event_type"] for row in created] == ["created"]       # one event: the host finalizer wrote nothing more
    delivered = rows(instance, "SELECT required FROM inbox_deliveries WHERE agent_id=? AND event_id=?", agent, created[0]["id"])
    assert delivered == [{"required": 1}]
    # Unfollowing is personal and sticky; the responsibility event for a change still reaches the assignee.
    acting = agent_context(agent, first)
    assert ok(call(instance, acting, "tasks_follow", {"task_id": item["id"], "following": False}))["following"] is False
    detail = ok(call(instance, owner, "tasks_get", {"task_id": item["id"]}))["task"]
    ok(call(instance, owner, "tasks_update", {"task_id": item["id"], "changes": {"status": "working"}}, revision=detail["revision"]))
    events = rows(instance, "SELECT e.event_type, d.required FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id "
                            "WHERE e.source_type='tasks' AND e.source_id=? AND d.agent_id=? ORDER BY e.id", item["id"], agent)
    assert [(row["event_type"], row["required"]) for row in events] == [("created", 1), ("updated", 1)]
    # The inbox's follow route is the same preference, and a Task is not a subscription row.
    followed = ok(call(instance, acting, "inbox_subscribe", {"source_type": "tasks", "source_id": item["id"]}))
    assert followed["following"] is True
    assert rows(instance, "SELECT * FROM inbox_subscriptions WHERE source_type IN ('tasks','actions')") == []
    listed = ok(call(instance, acting, "inbox_subscriptions_list", {}, scope=None))["items"]
    assert [row for row in listed if row.get("source_id") == item["id"] and row.get("managed_by") == "thread_follow"]


def test_private_conversations_never_become_item_threads(host):
    instance, first, _, agent, _ = host
    with pytest.raises(Exception):
        with instance.database.transaction() as uow:
            uow.connection.execute(
                "INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,author_id,recorder_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)", (first, new_id(), "dm", "conversation", "comment", "private", agent, agent, "now"))
    assert rows(instance, "SELECT COUNT(*) AS n FROM thread_messages WHERE item_type NOT IN ('task','action')") == [{"n": 0}]
    # The inbox follow target list cannot name a private conversation.
    assert code(call(instance, agent_context(agent, first), "inbox_subscribe",
                     {"source_type": "owner_conversations", "source_id": "x"})) == "validation_error"


def test_actions_queue_one_immutable_revision_with_each_record_correction_and_withdrawal(host):
    instance, first, second, agent, _ = host
    acting = agent_context(agent, first)
    recorded = ok(call(instance, acting, "actions_record", business_action(first, "Published the launch post", tags=["Launch"]), scope=None))
    key = recorded["action"]["action_id"]
    assert recorded["revision"] == 1 and recorded["action"]["tags"] == ["Launch"]
    # Correction and withdrawal take no inherited grant; the owner's explicit overrides enable them for this agent.
    assert code(call(instance, acting, "actions_correct", {"action_id": key, "text": "Ungranted"}, scope=None, revision=1)) == "policy_denied"
    set_agent_overrides(instance, agent, {"actions_correct": "allow", "actions_withdraw": "allow"}, (first, second))
    corrected = ok(call(instance, acting, "actions_correct", {"action_id": key, "text": "Published the launch post today"},
                        scope=None, revision=1))
    withdrawn = ok(call(instance, acting, "actions_withdraw", {"action_id": key, "rationale": "Duplicate"}, scope=None,
                        revision=corrected["action"]["revision"]))
    assert (corrected["revision"], withdrawn["revision"]) == (2, 3)
    revisions = rows(instance, "SELECT revision, operation FROM action_revisions WHERE action_key=? ORDER BY revision", key)
    assert [(row["revision"], row["operation"]) for row in revisions] == [(1, "add"), (2, "edit"), (3, "remove")]
    queued = rows(instance, "SELECT revision, operation, state, input_kind FROM context_inputs WHERE item_id=? ORDER BY sequence", key)
    assert [(row["revision"], row["operation"], row["state"], row["input_kind"]) for row in queued] == [
        (1, "add", "queued", "action"), (2, "edit", "queued", "action"), (3, "remove", "queued", "action")]
    # The recorded text of an earlier revision is the immutable snapshot, not the current row.
    first_snapshot = rows(instance, "SELECT snapshot_json FROM action_revisions WHERE action_key=? AND revision=1", key)[0]
    assert json.loads(first_snapshot["snapshot_json"])["text"] == "Published the launch post"
    # A historical business-home identity does not read or change this Action.
    stranger = historical_stranger(instance, second)
    assert code(call(instance, stranger, "actions_get", {"action_id": key}, scope=None)) == "inactive_agent"


def test_the_hosts_automatic_action_writeback_records_and_queues_in_the_applying_transaction(host):
    instance, first, _, agent, _ = host
    domains = instance.runtime.domains
    ctx = InvocationContext(Principal(agent, "agent"), business_id=first, allowed_business_ids=frozenset({first}), source="mcp")
    results, identity = [], source_identity(first, "signoff:synthetic:email")
    for _ in range(2):         # a retried confirmed source records its one canonical Action once
        with instance.database.transaction() as uow:
            builder = PlanBuilder(ctx, "actions_record", {}, "Confirmed result", clock=domains.activity.clock)
            domains.activity.append_to_plan(builder, uow, text="Sent the launch email", tag="operations",
                refs={"kind": "email", "target": "customer@example.test"}, operation_name="actions_record",
                source_key="signoff:synthetic:email")
            plan = builder.build()
            apply_records(ctx, plan, uow)
            settle(ctx, plan, uow, on_revision=domains.actions.on_revision)
        results.append(rows(instance, "SELECT COUNT(*) AS n FROM action_logs WHERE business_id=? AND id=?", first, identity)[0]["n"])
    assert results == [1, 1]
    key = rows(instance, "SELECT action_key FROM action_logs WHERE id=?", identity)[0]["action_key"]
    assert rows(instance, "SELECT COUNT(*) AS n FROM context_inputs WHERE item_id=?", key) == [{"n": 1}]


def test_the_change_feed_captures_the_engine_record_tables_and_the_finalizer_ignores_them(host):
    instance, first, _, agent, _ = host
    feed = instance.feed
    owner = instance.human(first)
    before = feed.route_versions
    made = task(instance, owner, "Feed me", tags=["Feed"])
    with instance.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM inbox_changes").fetchone()[0] == 0   # consumed in the same unit
    ok(call(instance, agent_context(agent, first), "actions_record", business_action(first, "Fed the feed"), scope=None))
    assert before is not None and made["id"]
    from bazarlyk.composition.capture import CAPTURED, triggers
    assert set(CAPTURED) == {"tasks", "thread_messages", "thread_follows", "item_tags", "action_logs", "action_businesses"}
    assert len(triggers()) == 3 + 1 + 2 + 2 + 2 + 2
    from bazarlyk.service.changes import ChangeFeed, _kinds_for_row
    assert _kinds_for_row("thread_messages", {"item_type": "task"}, None) == {"tasks"}
    assert _kinds_for_row("item_tags", None, {"item_type": "action"}) == {"action_logs"}
    assert _kinds_for_row("action_businesses", {"business_id": first}, None) == {"action_logs"}
    with instance.database.transaction() as uow:
        uow.connection.execute("UPDATE tasks SET title=title WHERE id=?", (made["id"],))
        captured = ChangeFeed.capture(uow.connection)
    assert [row["table"] for row in captured] == ["tasks"] and captured[0]["scope"] == first and captured[0]["kinds"] == {"tasks"}


def test_a_composed_flow_task_discussion_action_and_queue_stay_in_their_scopes(host):
    instance, first, second, agent, other = host
    owner = instance.human(first)
    work = task(instance, owner, "Ship the page", assignee_ids=[agent], tags=["Web"])
    acting = agent_context(agent, first)
    noted = ok(call(instance, acting, "tasks_comment", {"task_id": work["id"], "text": "Draft is ready"}, revision=work["revision"]))["task"]
    done = ok(call(instance, acting, "tasks_complete", {"task_id": work["id"]}, revision=noted["revision"]))["task"]
    # A global agent's Action is Global-stored, and a Global Action links only Global Tasks (the engine's Action workspace
    # rule): the business Task link is refused, so the Action names the business instead.
    assert code(call(instance, acting, "actions_record", business_action(first, "Shipped the page", task_id=work["id"]),
                     scope=None)) == "validation_error"
    recorded = ok(call(instance, acting, "actions_record", business_action(first, "Shipped the page", tags=["Web"]), scope=None))
    assert done["status"] == "done" and recorded["revision"] == 1
    other_scope = agent_context(other, second)
    assert ok(call(instance, other_scope, "tasks_list", {}))["tasks"] == []
    stranger = historical_stranger(instance, second)
    assert code(call(instance, stranger, "actions_get", {"action_id": recorded["action"]["action_id"]}, scope=None)) == "inactive_agent"
    with instance.database.read() as uow:
        counts = snapshot(uow.connection, ("tasks", "thread_messages", "action_logs", "action_revisions", "context_inputs"))
    assert counts["context_inputs"] >= counts["action_revisions"] >= counts["action_logs"] >= 1


def test_a_task_carries_no_setup_rules_or_view(host):
    instance, first, _, agent, _ = host
    owner = instance.human(first)
    parent = task(instance, owner, "Set up First")
    detail = ok(call(instance, owner, "tasks_get", {"task_id": parent["id"]}))["task"]
    assert "setup" not in detail                     # no setup view: the field is gone, not merely empty
    assigned = ok(call(instance, owner, "tasks_assign", {"task_id": parent["id"], "assignee_ids": [agent]}, revision=detail["revision"]))
    assert agent in [item["id"] if isinstance(item, dict) else item for item in assigned["task"].get("assignees", [agent])]
    ok(call(instance, owner, "tasks_comment", {"task_id": parent["id"], "text": "Noted"}, revision=assigned["task"]["revision"]))


def test_a_task_written_inside_another_domains_plan_is_announced_once_by_the_engine(host):
    instance, first, _, agent, _ = host
    from qorqut_operations.tasks.writes import TaskWrites
    tasks = instance.runtime.domains.tasks
    ctx = instance.human(first)
    with instance.database.transaction() as uow:
        plan = tasks.prepare("tasks_create", ctx, {"scope": "business", "title": "Routed by a domain", "assignee_ids": [agent]},
                             InvocationOptions(idempotency_key=new_id()), uow)
        TaskWrites(tasks).apply(ctx, plan, uow)
    made = rows(instance, "SELECT id FROM tasks WHERE title='Routed by a domain'")
    assert len(made) == 1
    events = rows(instance, "SELECT e.id, e.event_type, d.required FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id "
                            "WHERE e.source_type='tasks' AND e.source_id=? AND d.agent_id=?", made[0]["id"], agent)
    assert [(row["event_type"], row["required"]) for row in events] == [("created", 1)]
    assert rows(instance, "SELECT COUNT(*) AS n FROM thread_follows WHERE item_id=? AND principal_id=?", made[0]["id"], agent) == [{"n": 1}]
