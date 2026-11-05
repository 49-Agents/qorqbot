"""Acceptance case: the corrected operational-record contracts on the composed host.

Four contracts of the P3 correction round, each shown where only the composed host can show it:

* the new Action mutations (``actions_correct``, ``actions_withdraw``, ``actions_comment``) and ``tags_create`` carry no
  inherited grant, so an ungranted registered agent is denied until an explicit override names them, and a revoked
  override stops a prepared (Signoff-staged) application;
* the queued Action envelope (frozen ``{id, name, business_id}`` tag snapshots) is accepted by the exact Context
  ``begin_action`` API, for a create, a correction and a withdrawal, and an earlier queued revision never changes;
* an Action's discussion is read with the Action's own read authorization and written only with its own explicit policy,
  as the one canonical discussion, without an Action revision or a Context input;
* a snooze wake is decided from all responsible principals (every assignee, else the creator) through the host's typed
  Task snooze-routing port, including an owner-created unassigned Task.

Everything is synthetic: a temporary instance, registered test agents, a disposable Context and no model."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import InvocationOptions, Principal, new_id, plain

from host_foundation_support import (agent_context, build_instance, create_registered_global, create_worker, historical_business_agent,
                                     owner_global_context, register_sources, set_agent_overrides, snapshot)
from instance_support import CODE


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    instance, ids = build_instance(tmp_path_factory.mktemp("contracts"), businesses=("first", "second"))
    first, second = ids["first"], ids["second"]
    register_sources(instance, first, "first")
    register_sources(instance, second, "second")
    # Businesses have no resident agents. a worker is the independent recording agent; the coordinator (the owner-work
    # and relay identity) is the registered Ordubashy. ``foreign`` is a historical business-home identity of the other business.
    agent = create_worker(instance, "colleague")
    coordinator = create_registered_global(instance, "ordubashy")
    historical = historical_business_agent(instance, second)
    instance.runtime.freeze_registry()
    return SimpleNamespace(instance=instance, first=first, second=second, agent=agent, coordinator=coordinator,
                           owner=instance.human(first), owner_global=owner_global_context(instance), actor=agent_context(agent, first),
                           foreign=agent_context(historical, second),
                           coordinating=instance.runtime.resolver.invocation_context(Principal(coordinator, "agent"), business_id=first),
                           root=tmp_path_factory.getbasetemp())


def call(host, ctx, name, arguments=None, *, revision=None, scope="business", key="new"):
    arguments = {"scope": scope, **(arguments or {})} if scope else dict(arguments or {})
    return host.instance.runtime.application.invoke(ctx, name, arguments, InvocationOptions(
        expected_revision=revision, idempotency_key=new_id() if key == "new" else key))


def ok(result):
    assert result.status == "ok", result.error
    return plain(result.data)


def code(result):
    assert result.status == "error", result.data
    return result.error["code"]


def rows(host, sql, *parameters):
    with host.instance.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(sql, parameters)]


def counts(host, *tables):
    with host.instance.database.read() as uow:
        return snapshot(uow.connection, tables)


def set_overrides(host, overrides, agent=None):
    return set_agent_overrides(host.instance, agent or host.agent, overrides, (host.first, host.second))


def record(host, text, **arguments):
    # A global agent records in Global storage and states the kind of work (the retained v1 Global-agent Action rule): here
    # business work, linked to the business it selected.
    return ok(call(host, host.actor, "actions_record", {"text": text, "category": "business-operational",
                                                         "business_ids": [host.first], **arguments}, scope=None))


def test_new_mutations_have_no_inherited_grant_and_an_explicit_override_enables_and_revokes_them(host):
    set_overrides(host, {})
    recorded = record(host, "Published the launch post", tags=["Launch"])
    key = recorded["action"]["action_id"]
    # The record grant, the Task grants and the read grants decide nothing for the new mutations: they are denied.
    assert code(call(host, host.actor, "actions_correct", {"action_id": key, "text": "Edited"}, scope=None, revision=1)) == "policy_denied"
    assert code(call(host, host.actor, "actions_withdraw", {"action_id": key}, scope=None, revision=1)) == "policy_denied"
    assert code(call(host, host.actor, "actions_comment", {"action_id": key, "text": "Hello"}, scope=None)) == "policy_denied"
    assert code(call(host, host.actor, "tags_create", {"label": "Prepared"}, scope=None)) == "policy_denied"
    # Reads and the personal follow keep their read-grant defaults; an inline label inside an authorized write still works.
    assert ok(call(host, host.actor, "actions_thread_get", {"action_id": key}, scope=None))["messages"] == []
    assert ok(call(host, host.actor, "actions_follow", {"action_id": key, "following": True}, scope=None))["following"] is True
    assert record(host, "Inline label", tags=["Brand New Label"])["action"]["tags"] == ["Brand New Label"]
    assert rows(host, "SELECT COUNT(*) AS n FROM tags WHERE label_key='prepared'") == [{"n": 0}]
    assert counts(host, "action_revisions")["action_revisions"] == 2

    # An explicit grant enables the correction (and only it); the revision is queued once.
    set_overrides(host, {"actions_correct": "allow"})
    corrected = ok(call(host, host.actor, "actions_correct", {"action_id": key, "text": "Published the launch post on Reddit"},
                        scope=None, revision=1))
    assert corrected["revision"] == 2
    assert code(call(host, host.actor, "actions_withdraw", {"action_id": key}, scope=None, revision=2)) == "policy_denied"
    # Revoke the grant: the correction is denied again, and a correction staged for approval before the revocation
    # does not apply once the grant is gone.
    set_overrides(host, {"actions_correct": "signoff"})
    staged = call(host, host.actor, "actions_correct", {"action_id": key, "text": "Staged correction"}, scope=None, revision=2)
    assert staged.status == "signoff_required", staged.error
    before = counts(host, "action_revisions", "context_inputs")
    set_overrides(host, {})
    assert code(call(host, host.actor, "actions_correct", {"action_id": key, "text": "Revoked"}, scope=None, revision=2)) == "policy_denied"
    # A global agent's Action is Global-stored, so its staged correction is reviewed in the Global scope.
    pending = plain(host.instance.invoke(host.owner_global, "signoffs_list", {"scope": "global"}).data)["items"]
    signoff = plain(host.instance.invoke(host.owner_global, "signoffs_get", {"scope": "global", "signoff_id": pending[0]["id"]}).data)["signoff"]
    decision = host.instance.runtime.signoffs.review(
        host.owner_global, signoff["id"], decision="approve", proposal_revision=signoff["proposal_revision"],
        payload_hash=signoff["payload_hash"], expected_status_revision=signoff["status_revision"], idempotency_key=new_id())
    assert host.instance.runtime.signoffs.run_pending() == 0
    # The approval itself finds the authority gone: the proposal is conflicted, never applied.
    assert decision.status == "error" and decision.error["code"] == "stale_signoff"
    assert rows(host, "SELECT state FROM signoffs WHERE id=?", signoff["id"]) == [{"state": "conflicted"}]
    assert rows(host, "SELECT COUNT(*) AS n FROM apply_receipts WHERE signoff_id=?", signoff["id"]) == [{"n": 0}]
    assert counts(host, "action_revisions", "context_inputs") == before
    assert rows(host, "SELECT action_text FROM action_logs WHERE action_key=?", key) == [{"action_text": "Published the launch post on Reddit"}]


# -- the Context bridge --------------------------------------------------------------------------------------------------

def context_engine():
    path = str(CODE / "qorqut-context")
    if path not in sys.path:
        sys.path.insert(0, path)
    from qorqut_context import Context, ContextError
    return Context, ContextError


def publish_input(context, payload, previous=None):
    """Hand one queued Action input to the exact Context ``begin_action`` API and publish it (no model, no pointers)."""
    event = context.begin_action(
        payload["operation"], payload["action_ref"], text=payload.get("text"), revision_ref=payload["revision_ref"],
        related_objects=payload["related_objects"], tags=payload["tags"], submitted_by=payload["submitted_by"],
        expected_previous_commit=previous)
    context.stage_action_associations(event.event_id, "2026-10-12", "Recorded.", [])
    context.stage_handoff_note(event.event_id, "")
    return event, context.publish(event.event_id)


def test_the_queued_tag_envelope_is_accepted_by_the_exact_context_action_api(host):
    Context, ContextError = context_engine()
    set_overrides(host, {"actions_correct": "allow", "actions_withdraw": "allow"})
    recorded = record(host, "Bridge result", tags=["Bridge A", "bridge b"])
    key = recorded["action"]["action_id"]
    queued = lambda: [json.loads(row["payload_json"]) for row in rows(
        host, "SELECT payload_json FROM context_inputs WHERE item_id=? ORDER BY sequence", key)]
    [created] = queued()
    catalog = {row["label"]: row["id"] for row in rows(host, "SELECT id, label FROM tags")}
    assert created["tags"] == [{"id": catalog["Bridge A"], "name": "Bridge A", "business_id": None},
                               {"id": catalog["bridge b"], "name": "bridge b", "business_id": None}]
    assert created["scope"] == {"kind": "global"}       # the owning (Global storage) scope is kept apart from the tags

    corrected = ok(call(host, host.actor, "actions_correct", {"action_id": key, "text": "Bridge result, corrected", "tags": ["Bridge C"]},
                        scope=None, revision=1))
    ok(call(host, host.actor, "actions_withdraw", {"action_id": key}, scope=None, revision=corrected["action"]["revision"]))
    first, second, third = queued()
    assert first == created                                                         # the earlier queued revision never changes
    bridge_c = {row["label"]: row["id"] for row in rows(host, "SELECT id, label FROM tags")}["Bridge C"]
    assert second["tags"] == [{"id": bridge_c, "name": "Bridge C", "business_id": None}] and third["tags"] == second["tags"]
    assert [payload["operation"] for payload in (first, second, third)] == ["add", "edit", "remove"]

    context = Context.initialize(host.root / "bridge-context" / "data", host.root / "bridge-context" / "runtime")
    with pytest.raises(ContextError, match="tag snapshot requires id, name, and business_id"):
        context.begin_action("add", key, text="Old shape", tags=["Bridge A"], submitted_by=host.agent)       # the retired label list
    one, commit_one = publish_input(context, first)
    two, commit_two = publish_input(context, second, commit_one)
    three, commit_three = publish_input(context, third, commit_two)
    snapshot = context.snapshot()
    assert [snapshot.event(event.event_id)["action"]["tags"] for event in (one, two, three)] == [first["tags"], second["tags"], third["tags"]]
    assert snapshot.event(one.event_id)["action"]["tags"] == created["tags"]        # Context keeps the earlier revision's snapshot too
    assert snapshot.action(key)["withdrawn"] is True and len({commit_one, commit_two, commit_three}) == 3


# -- the Action discussion -----------------------------------------------------------------------------------------------

def test_an_action_discussion_is_read_with_the_action_and_written_with_its_own_policy(host):
    instance = host.instance
    set_overrides(host, {})
    set_overrides(host, {}, host.coordinator)
    key = record(host, "Discussed result")["action"]["action_id"]
    watched = ("action_logs", "action_revisions", "context_inputs", "owner_conversation_messages")
    before = counts(host, *watched)
    # No inherited grant: neither the record grant nor the read grant lets an agent comment.
    assert code(call(host, host.actor, "actions_comment", {"action_id": key, "text": "Ungranted"}, scope=None)) == "policy_denied"
    set_overrides(host, {"actions_comment": "allow"})
    set_overrides(host, {"actions_comment": "allow"}, host.coordinator)
    ok(call(host, host.actor, "actions_comment", {"action_id": key, "text": "Self note"}, scope=None))
    assert rows(host, "SELECT COUNT(*) AS n FROM inbox_events WHERE event_type='note_added'") == [{"n": 0}]    # nobody else follows yet
    # The registered coordinator relays an explicit owner statement: the owner is the speaker, the coordinator the recorder.
    relayed = ok(call(host, host.coordinating, "actions_comment", {
        "action_id": key, "text": "The owner agrees", "relay": {"kind": "explicit_owner_statement", "owner_id": instance.owner,
                                                               "statement_text": "I agree"}}, scope=None))["message"]
    assert (relayed["author_id"], relayed["recorder_id"]) == (instance.owner, host.coordinator)
    follows = {row["principal_id"]: row["reason"] for row in rows(
        host, "SELECT principal_id, reason FROM thread_follows WHERE item_type='action' AND item_id=?", key.rsplit("/", 1)[1])}
    assert follows == {host.agent: "creator", host.coordinator: "commenter", instance.owner: "represented"}
    notes = rows(host, "SELECT e.business_id, e.source_id, d.agent_id, d.required FROM inbox_events e JOIN inbox_deliveries d "
                       "ON d.event_id=e.id WHERE e.event_type='note_added'")
    assert notes == [{"business_id": None, "source_id": key, "agent_id": host.agent, "required": 0}]       # a Global Action
    # The discussion grew by messages only: no Action revision, no Context input, no private conversation row.
    assert counts(host, *watched) == before
    assert rows(host, "SELECT COUNT(*) AS n FROM thread_messages WHERE item_type='action'") == [{"n": 2}]
    # Reads follow the Action's own authorization (reader, owner, coordinator); a business-home identity is refused.
    read = ok(call(host, host.actor, "actions_thread_get", {"action_id": key}, scope=None))
    assert [message["body"] for message in read["messages"]] == ["Self note", "The owner agrees"]
    assert len(ok(call(host, host.owner, "actions_thread_get", {"action_id": key}, scope=None))["messages"]) == 2
    assert code(call(host, host.foreign, "actions_thread_get", {"action_id": key}, scope=None)) == "inactive_agent"
    assert code(call(host, host.foreign, "actions_comment", {"action_id": key, "text": "Intruder"}, scope=None)) == "inactive_agent"
    # An explicit unfollow is sticky: a later comment reaches nobody else and does not re-follow.
    ok(call(host, host.actor, "actions_follow", {"action_id": key, "following": False}, scope=None))
    ok(call(host, host.coordinating, "actions_comment", {"action_id": key, "text": "Again"}, scope=None))
    assert rows(host, "SELECT COUNT(*) AS n FROM inbox_events WHERE event_type='note_added'") == [{"n": 1}]
    assert rows(host, "SELECT following FROM thread_follows WHERE principal_id=? AND item_id=?", host.agent, key.rsplit("/", 1)[1]) == [{"following": 0}]
    # A comment by the installation owner is refused by the retained team-channel rule.
    assert code(call(host, host.owner, "actions_comment", {"action_id": key, "text": "From the owner"}, scope=None)) == "channel_required"
    set_overrides(host, {})
    set_overrides(host, {}, host.coordinator)


# -- the snooze wake -----------------------------------------------------------------------------------------------------

def wake(host, days=10):
    now = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="microseconds").replace("+00:00", "Z")
    with host.instance.database.transaction() as uow:
        return host.instance.runtime.domains.tasks.reconcile_snoozes(uow, now=now)


def snoozed(host, ctx, title, **arguments):
    made = ok(call(host, ctx, "tasks_create", {"title": title, **arguments}))["task"]
    until = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    return ok(call(host, ctx, "tasks_update", {"task_id": made["id"], "changes": {"snoozed_until": until}}, revision=made["revision"]))["task"]


def wakes(host, task_id):
    return rows(host, "SELECT d.agent_id, d.required FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id "
                      "WHERE e.event_type='snooze_expired' AND e.source_id=? ORDER BY d.agent_id", task_id)


def test_a_snooze_wake_uses_all_assignees_and_the_hosts_owner_work_routing(host):
    # The agent created this Task for the owner alone: the creator is not notified; the host's owner-work route reaches the
    # registered coordinator instead (a required inbox row for it, none invented for the human).
    for_owner = snoozed(host, host.actor, "Owner decides", assignee_ids=[host.instance.owner])
    assert for_owner["assignee_ids"] == [host.instance.owner]
    woken = wake(host)
    assert [entry["task_id"] for entry in woken] == [for_owner["id"]]
    assert wakes(host, for_owner["id"]) == [{"agent_id": host.coordinator, "required": 1}]
    assert wake(host, 20) == []                                                       # one durable wake per generation
    # The owner created this Task and assigned nobody (and no Focus card names it): the responsible human is the owner as
    # creator, so the same owner-work route reaches the registered coordinator with exactly one required wake and no inbox
    # row exists for the human; repeating the reconciliation emits nothing.
    mine_alone = snoozed(host, host.owner, "Owner keeps this", assignee_ids=[])
    assert mine_alone["assignee_ids"] == [] and mine_alone["created_by"] == host.instance.owner
    assert [entry["task_id"] for entry in wake(host, 25)] == [mine_alone["id"]]
    assert wakes(host, mine_alone["id"]) == [{"agent_id": host.coordinator, "required": 1}]
    assert rows(host, "SELECT COUNT(*) AS n FROM inbox_deliveries WHERE agent_id=?", host.instance.owner) == [{"n": 0}]
    assert wake(host, 26) == [] and wake(host, 90) == [] and wakes(host, mine_alone["id"]) == [{"agent_id": host.coordinator, "required": 1}]
    # A Task with no assignee at all falls back to its creator, which receives the one required wake.
    nobody = snoozed(host, host.actor, "Nobody yet", assignee_ids=[])
    assert nobody["assignee_ids"] == [] and [entry["task_id"] for entry in wake(host, 30)] == [nobody["id"]]
    assert wakes(host, nobody["id"]) == [{"agent_id": host.agent, "required": 1}]
    # An assigned agent keeps its inbox row; a closed Task is never woken and nothing is started.
    mine = snoozed(host, host.owner, "Agent work", assignee_ids=[host.agent])
    done = snoozed(host, host.owner, "Finished", assignee_ids=[host.agent])
    ok(call(host, host.owner, "tasks_complete", {"task_id": done["id"]}, revision=done["revision"]))
    assert [entry["task_id"] for entry in wake(host, 40)] == [mine["id"]]
    assert wakes(host, mine["id"]) == [{"agent_id": host.agent, "required": 1}] and wakes(host, done["id"]) == []
    assert ok(call(host, host.owner, "tasks_get", {"task_id": mine["id"]}))["task"]["status"] == "open"
