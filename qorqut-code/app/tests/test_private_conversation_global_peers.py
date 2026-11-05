"""Owner private conversations on records: the one channel with Ordubashy, through effective scope.

Contract: the owner's private conversation on a Task, Signoff, Email thread or Reference is with the registered Ordubashy alone.
Ordubashy takes part as an active supported global (no business home, no membership row, no saved list), read again in the same
unit of work; a person keeps the active-membership rule. There is no worker or other-agent channel: no request selects one (an
``agent_id`` other than Ordubashy, or any ``peer_role`` but ``ordubashy``, is refused), no caller has a default channel of its own,
nothing is forwarded, and nothing falls back from one channel into another.

Conversations that earlier releases stored for individual workers are retired history. They are never served: not by get, send,
mark-read or the peer list; not by the Focus counters, the reply feed, the inbox or the business-creation evidence readers. So a
retired row can neither leak nor leave an unread marker the owner has no way to clear. The offline cleanup that removes them (and
the replay of their saved requests) is proven in ``test_private_peer_cleanup.py``.

COMPOSED. A worker is an active registered global holding the ``global-worker`` role, decided by Foundation's
``bazarlyk.agents.worker_access.is_worker``, and the synthetic worker comes from its ``create_worker`` fixture helper. Both are
part of this host, so the module runs whole; the guards below only protect an older checkout.

Real: the stock host assembly and runtime, the owner's catalog publication and ``agents_create``/``agents_deactivate``, local
business seeding, ``tasks_create`` and the ``owner_conversation_*`` operations. Synthetic: the tmp instance, its principals, the
explicitly HISTORICAL business-home row, and the retired per-worker rows planted by ``private_peer_support``. No model, native
session, provider, network or operational data is used.
"""

from __future__ import annotations

import json

import pytest

from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal, new_id, plain

pytest.importorskip("bazarlyk.agents.worker_access", reason="Foundation's worker eligibility is not composed on this branch")
support = pytest.importorskip("host_foundation_support")
if not hasattr(support, "create_worker"):
    pytest.skip("Foundation's synthetic worker fixture is not composed on this branch", allow_module_level=True)

from bazarlyk.inbox.private_conversations import private_index, private_state
from bazarlyk.reply_notifications import replies
from host_foundation_support import (build_instance, create_registered_global, create_worker, historical_business_agent,
                                     owner_global_context, register_sources, select_coordinator, set_agent_overrides)
from private_peer_support import Legacy, MARKER

PRIVATE_GRANTS = {"owner_conversation_get": "allow", "owner_conversation_send": "allow"}
EMPTY = {"private_unread": False, "private_message_count": 0}


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first",))
    register_sources(instance, ids["first"], "first")
    coordinator = select_coordinator(instance, create_registered_global(instance, "ordubashy"))
    worker, other = create_worker(instance, "worker-a"), create_worker(instance, "worker-b")
    colleague = create_worker(instance, "colleague")
    for identity in (coordinator, worker, other, colleague):
        set_agent_overrides(instance, identity, PRIVATE_GRANTS, (ids["first"],))
    # Registered after every global exists, the way creation does: nothing gives a global agent a membership of it.
    fresh = new_id()
    with instance.database.transaction() as uow:
        uow.repository("businesses").insert({"id": fresh, "slug": "fresh", "name": "Fresh", "workspace": str(instance.paths.business_root("fresh")),
                                             "hq_path": str(instance.paths.business_hq("fresh"))})
        uow.repository("business_memberships", business_id=fresh).insert({"principal_id": instance.owner, "is_owner": True})
    instance.runtime.freeze_registry()
    try:
        yield _Host(instance, ids["first"], fresh, coordinator, worker, other, colleague)
    finally:
        try:
            instance.runtime.release_instance()
        finally:
            instance.database.close()


class _Host:
    def __init__(self, instance, first, fresh, coordinator, worker, other, colleague):
        self.instance, self.runtime, self.database = instance, instance.runtime, instance.database
        self.first, self.fresh, self.coordinator, self.worker, self.other, self.colleague = first, fresh, coordinator, worker, other, colleague

    def call(self, ctx, name, arguments, key=None):
        return self.runtime.application.invoke(ctx, name, arguments, InvocationOptions(idempotency_key=key or new_id()))

    def agent(self, identity, business):
        return self.runtime.resolver.invocation_context(Principal(identity, "agent"), business_id=business)

    def owner(self, business):
        return self.instance.human(business) if business else owner_global_context(self.instance)

    def query(self, sql, *parameters):
        with self.database.read() as uow:
            return [tuple(row) for row in uow.connection.execute(sql, parameters)]

    def retired(self, business):
        """The planting helper over this very instance: retired per-worker rows are written by direct SQL, as earlier releases left them."""
        return Legacy(self.instance, business, self.instance.owner, self.coordinator, {"a": self.worker, "b": self.other})

    def counters(self, ctx, source):
        """The Focus counters of one record for ``ctx``: the single-card state and the batch index both Focus and attention read."""
        binding = {"type": source["type"], "id": source["id"], "business_id": source["business_id"]}
        with self.database.read() as uow:
            context = self.runtime.application.current_context(ctx, uow)
            return private_state(context, binding, uow), private_index(context, [binding], uow)

    def pending(self, identity, business=None):
        with self.database.read() as uow:
            context = self.runtime.application.current_context(self.agent(identity, business), uow)
            return self.runtime.inbox.pending(context, uow)["items"]

    def feed(self):
        """The owner's reply feed, read with the owner's own context over every active business as the dashboard route builds it."""
        with self.database.read() as uow:
            owner = self.runtime.policy.context_for(self.instance.owner, business_id=None, review_business_id=None, uow=uow)
        return replies(self.runtime, owner)

    def thread_feed(self):
        return [item for item in self.feed()["items"] if item["kind"] == "thread"]


def ok(result):
    assert result.status == "ok", result.error
    return plain(result.data)


def refused(result, code="forbidden"):
    assert result.status == "error" and result.error["code"] == code, (result.status, result.error)


def task(host, business, title="Private work"):
    created = ok(host.call(host.owner(business), "tasks_create", {"title": title, "scope": "business" if business else "global"}))
    return {"type": "task", "id": created["task"]["id"], "business_id": business}


def scoped(source, **arguments):
    return {"source": source, **({} if source["business_id"] else {"scope": "global"}), **arguments}


def texts(host, ctx, source, **arguments):
    return [message["body"] for message in ok(host.call(ctx, "owner_conversation_get", scoped(source, **arguments)))["messages"]]


def deliveries(host, agent):
    return host.query("SELECT count(*) FROM inbox_deliveries d JOIN inbox_events e ON e.id=d.event_id "
                      "WHERE d.agent_id=? AND e.event_type='private_message'", agent)[0][0]


def converse(host, source):
    """The owner writes to Ordubashy's channel and Ordubashy answers in it."""
    owner = host.owner(source["business_id"])
    ok(host.call(owner, "owner_conversation_send", scoped(source, text="Owner to Ordubashy")))
    ordubashy = host.agent(host.coordinator, source["business_id"])
    key = new_id()
    return key, ok(host.call(ordubashy, "owner_conversation_send", scoped(source, text="Ordubashy answers"), key))


def test_ordubashy_converses_privately_on_a_newly_founded_business_and_a_global_task_without_any_agent_membership(host):
    assert host.query("SELECT m.principal_id FROM business_memberships m JOIN principals p ON p.id=m.principal_id "
                      "WHERE m.business_id=? AND p.kind='agent'", host.fresh) == []
    for source in (task(host, host.fresh), task(host, None)):
        before = {agent: deliveries(host, agent) for agent in (host.coordinator, host.worker, host.other)}
        key, sent = converse(host, source)
        # The owner's message is delivered to Ordubashy only, and no forwarded notice is written for anyone.
        assert {agent: deliveries(host, agent) - count for agent, count in before.items()} == {host.coordinator: 1, host.worker: 0, host.other: 0}
        assert host.query("SELECT count(*) FROM agent_messages") == [(0,)]
        business = source["business_id"]
        ordubashy = host.agent(host.coordinator, business)
        assert texts(host, ordubashy, source) == ["Owner to Ordubashy", "Ordubashy answers"]
        assert texts(host, host.owner(business), source) == ["Owner to Ordubashy", "Ordubashy answers"]
        assert texts(host, host.owner(business), source, agent_id=host.coordinator, peer_role="ordubashy") == [
            "Owner to Ordubashy", "Ordubashy answers"]
        # A replay returns the saved receipt after the same reauthorization and appends nothing.
        again = ok(host.call(ordubashy, "owner_conversation_send", scoped(source, text="Ordubashy answers"), key))
        assert again["message"]["id"] == sent["message"]["id"]
        assert texts(host, ordubashy, source) == ["Owner to Ordubashy", "Ordubashy answers"]
        listed = ok(host.call(host.owner(business), "owner_conversation_peers", scoped(source)))["peers"]
        assert [(peer["agent_id"], peer["channel"]) for peer in listed] == [(host.coordinator, "private")]
    assert host.query("SELECT count(*) FROM business_memberships m JOIN principals p ON p.id=m.principal_id WHERE p.kind='agent'") == [(0,)]


def test_no_caller_can_select_a_worker_and_no_agent_has_a_channel_of_its_own(host):
    source = task(host, host.fresh)
    converse(host, source)
    ordubashy, worker, other = (host.agent(identity, host.fresh) for identity in (host.coordinator, host.worker, host.other))
    owner = host.owner(host.fresh)
    # Nobody - Ordubashy, a worker or the owner - can name a worker's channel; the request is refused and writes nothing.
    for caller in (ordubashy, owner):
        for arguments in ({"agent_id": host.worker}, {"peer_role": "ordubashy", "agent_id": host.worker}):
            refused(host.call(caller, "owner_conversation_get", scoped(source, **arguments)))
            refused(host.call(caller, "owner_conversation_send", scoped(source, text="Into the worker's channel", **arguments)))
    for arguments in ({"agent_id": host.worker}, {"peer_role": "ordubashy", "agent_id": host.worker}):
        refused(host.call(owner, "owner_conversation_peers", scoped(source, **arguments)))
    # ``global-worker`` is not a peer role, so it never means a first or default worker, whoever asks and whatever else is named.
    for name, extra in (("owner_conversation_get", {}), ("owner_conversation_send", {"text": "Not delivered"})):
        for arguments in ({"peer_role": "global-worker"}, {"peer_role": "global-worker", "agent_id": host.worker},
                          {"peer_role": "global-worker", "agent_id": host.coordinator}):
            refused(host.call(owner, name, scoped(source, **arguments, **extra)), "validation_error")
            refused(host.call(worker, name, scoped(source, **arguments, **extra)), "validation_error")
    refused(host.call(owner, "owner_conversation_peers", scoped(source, peer_role="global-worker")), "validation_error")
    # A worker has no default channel: with or without a selector, it reaches nothing of the owner's conversation.
    for caller, name in ((worker, "worker"), (other, "other")):
        for arguments in ({}, {"agent_id": host.worker}, {"agent_id": host.coordinator}, {"peer_role": "ordubashy"}):
            refused(host.call(caller, "owner_conversation_get", scoped(source, **arguments)))
            refused(host.call(caller, "owner_conversation_send", scoped(source, text=f"Into a channel ({name})", **arguments)))
    # Another global reaches every active business and holds the same grants, yet has no private owner channel either.
    outsider = host.agent(host.colleague, host.fresh)
    assert host.fresh in outsider.allowed_business_ids
    for arguments in ({}, {"agent_id": host.colleague}):
        refused(host.call(outsider, "owner_conversation_get", scoped(source, **arguments)))
        refused(host.call(outsider, "owner_conversation_send", scoped(source, text="Outsider", **arguments)))
    refused(host.call(owner, "owner_conversation_get", scoped(source, agent_id=host.colleague)))
    refused(host.call(owner, "owner_conversation_mark_read", scoped(source, sequence=1, agent_id=host.worker)))
    assert texts(host, ordubashy, source) == ["Owner to Ordubashy", "Ordubashy answers"]
    assert host.query("SELECT count(*) FROM owner_conversation_messages") == [(2,)]
    assert host.query("SELECT count(*) FROM agent_messages") == [(0,)]


def test_inactive_and_business_home_callers_and_archived_or_inaccessible_sources_fail(host):
    source, elsewhere = task(host, host.fresh), task(host, host.first, "Other business work")
    converse(host, source)
    ordubashy = host.agent(host.coordinator, host.fresh)
    # A Task of another business, or one that does not exist, is not a source of this business.
    for missing in ({**source, "id": elsewhere["id"]}, {**source, "id": new_id()}):
        refused(host.call(ordubashy, "owner_conversation_get", scoped(missing)), "not_found")
    # HISTORICAL / INVALID: a business-home identity with its stored active status and old membership never operates, and the
    # owner cannot select it as a peer.
    historical = historical_business_agent(host.instance, host.fresh)
    with pytest.raises(DomainError) as error:
        host.agent(historical, host.fresh)
    assert error.value.code == "inactive_agent"
    stale = InvocationContext(Principal(historical, "agent"), business_id=host.fresh, source="mcp")
    assert host.call(stale, "owner_conversation_get", scoped(source)).status == "error"
    refused(host.call(host.owner(host.fresh), "owner_conversation_get", scoped(source, agent_id=historical)))
    listed = ok(host.call(host.owner(host.fresh), "owner_conversation_peers", scoped(source)))["peers"]
    assert {peer["agent_id"] for peer in listed} == {host.coordinator}
    # An inactive principal is read again in the unit of work: the context built before it changed is refused.
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (host.coordinator,))
    assert host.call(ordubashy, "owner_conversation_get", scoped(source)).status == "error"
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=1 WHERE id=?", (host.coordinator,))
    assert texts(host, ordubashy, source) == ["Owner to Ordubashy", "Ordubashy answers"]
    # Retiring a worker changes nothing about the one channel.
    revision = host.query("SELECT revision FROM agent_profiles WHERE agent_id=?", host.worker)[0][0]
    retired = host.runtime.application.invoke(owner_global_context(host.instance), "agents_deactivate", {"agent_id": host.worker},
                                              InvocationOptions(expected_revision=revision, idempotency_key=new_id()))
    assert retired.status == "ok", retired.error
    assert texts(host, host.owner(host.fresh), source) == ["Owner to Ordubashy", "Ordubashy answers"]
    assert texts(host, ordubashy, source) == ["Owner to Ordubashy", "Ordubashy answers"]
    # Archiving the business closes it to Ordubashy and to the owner.
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at=updated_at WHERE id=?", (host.fresh,))
    assert host.call(ordubashy, "owner_conversation_get", scoped(source)).status == "error"
    assert host.call(host.owner(host.fresh), "owner_conversation_get", scoped(source)).status == "error"


def test_a_persons_private_conversation_keeps_the_active_membership_rule(host):
    source = task(host, host.fresh)
    converse(host, source)
    ordubashy = host.agent(host.coordinator, host.fresh)
    with host.database.read() as uow:
        owner = host.runtime.policy.context_for(host.instance.owner, business_id=None, review_business_id=None, uow=uow)
    assert host.fresh in owner.allowed_business_ids
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE business_memberships SET active=0 WHERE business_id=? AND principal_id=?",
                               (host.fresh, host.instance.owner))
    assert host.call(host.owner(host.fresh), "owner_conversation_get", scoped(source)).status == "error"
    # Even a context resolved before the change meets the membership rule, read again in the unit of work.
    with host.database.read() as uow, pytest.raises(DomainError) as error:
        host.runtime.application.private_conversations.select(owner, host.fresh, uow)
    assert error.value.code == "forbidden" and "membership" in error.value.message
    # The global peer's participation never depended on that row.
    assert texts(host, ordubashy, source) == ["Owner to Ordubashy", "Ordubashy answers"]
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE business_memberships SET active=1 WHERE business_id=? AND principal_id=?",
                               (host.fresh, host.instance.owner))
    assert texts(host, host.owner(host.fresh), source) == ["Owner to Ordubashy", "Ordubashy answers"]
    # Another person with an active membership is still no party to the owner's private channel.
    other = new_id()
    with host.database.transaction() as uow:
        uow.repository("principals").insert({"id": other, "kind": "human", "display_name": "Colleague"})
        uow.repository("business_memberships", business_id=host.fresh).insert({"principal_id": other, "is_owner": False})
    colleague = InvocationContext(Principal(other, "human"), business_id=host.fresh, allowed_business_ids=frozenset({host.fresh}),
                                  source="dashboard")
    assert host.call(colleague, "owner_conversation_get", scoped(source)).status == "error"


# ----- retired per-worker conversations are history in every projection ----------------------------------------------------

def test_retired_worker_conversations_are_history_in_every_projection_and_leave_no_stuck_unread(host):
    source = task(host, host.fresh)
    owner, ordubashy, worker = host.owner(host.fresh), host.agent(host.coordinator, host.fresh), host.agent(host.worker, host.fresh)
    legacy = host.retired(host.fresh)
    conversation = legacy.conversation("retired", peer=host.worker, business=host.fresh, task=source["id"])
    legacy.message("owner_old", conversation, role="owner", author=host.instance.owner, text="old owner words",
                   deliver_to=host.worker, source=source)
    legacy.message("worker_old", conversation, role="coordinator", author=host.worker, text="old worker reply")
    legacy.message("worker_old2", conversation, role="coordinator", author=host.worker, text="another old reply")
    legacy.legacy_notice("notice_old", "owner_old", host.worker)
    # Served by nothing: not get (with or without a selector), the peer list, the mark-read, the Focus counters, the reply feed,
    # the inbox. Project creation no longer accepts conversation evidence.
    assert texts(host, owner, source) == [] and texts(host, ordubashy, source) == []
    refused(host.call(owner, "owner_conversation_get", scoped(source, agent_id=host.worker)))
    refused(host.call(worker, "owner_conversation_get", scoped(source)))
    listed = ok(host.call(owner, "owner_conversation_peers", scoped(source)))["peers"]
    assert [(peer["agent_id"], peer["unread"]) for peer in listed] == [(host.coordinator, False)]
    for ctx in (owner, ordubashy):
        state, index = host.counters(ctx, source)
        assert state == EMPTY and list(index.values()) in ([], [EMPTY])
    state, index = host.counters(worker, source)
    assert state == EMPTY and index == {}
    assert host.thread_feed() == [] and MARKER not in json.dumps(host.feed())
    assert host.pending(host.worker) == []
    # No way to clear it, no need to: the stored unread replies of the retired peer are not a marker anywhere.
    refused(host.call(owner, "owner_conversation_mark_read",
                      scoped(source, sequence=legacy.ids["worker_old2:sequence"], agent_id=host.worker)))
    assert host.query("SELECT count(*) FROM owner_conversation_reads") == [(0,)]
    # The real channel on the very same Task works beside the retired row and counts only its own messages.
    sent = ok(host.call(owner, "owner_conversation_send", scoped(source, text="Owner to Ordubashy")))
    reply = ok(host.call(ordubashy, "owner_conversation_send", scoped(source, text="Ordubashy answers")))
    assert texts(host, owner, source) == ["Owner to Ordubashy", "Ordubashy answers"]
    state, _ = host.counters(owner, source)
    assert state == {"private_unread": True, "private_message_count": 2}
    assert [item["text"] for item in host.thread_feed()] == ["Ordubashy answers"]
    ok(host.call(owner, "owner_conversation_mark_read", scoped(source, sequence=reply["message"]["sequence"])))
    state, _ = host.counters(owner, source)
    assert state == {"private_unread": False, "private_message_count": 2}
    assert host.thread_feed() == []
    # The retired rows were never written to, read or acknowledged by any of that.
    assert host.query("SELECT count(*) FROM owner_conversation_reads WHERE conversation_id=?", conversation) == [(0,)]
    assert host.query("SELECT count(*) FROM owner_conversation_messages WHERE conversation_id=?", conversation) == [(3,)]


def test_a_retired_peers_event_is_invisible_to_the_inbox_and_the_nudge_selection_while_ordubashys_is_not(host):
    source = task(host, host.fresh)
    legacy = host.retired(host.fresh)
    conversation = legacy.conversation("retired", peer=host.worker, business=host.fresh, task=source["id"])
    legacy.message("owner_old", conversation, role="owner", author=host.instance.owner, text="old owner words",
                   deliver_to=host.worker, source=source)
    event = legacy.ids["owner_old:event"]
    assert host.query("SELECT count(*) FROM inbox_deliveries WHERE agent_id=? AND event_id=?", host.worker, event) == [(1,)]
    with host.database.read() as uow:
        context = host.runtime.application.current_context(host.agent(host.worker, None), uow)
        row = uow.connection.execute("SELECT * FROM inbox_events WHERE id=?", (event,)).fetchone()
        # Both the inbox listing and the backend nudge selection ask the inbox for visibility event by event.
        assert host.runtime.inbox.event_visible(context, row, uow) is False
    assert host.pending(host.worker) == []
    # Ordubashy's own event on the same Task is visible to him, so the retired one is not merely hidden by a general rule.
    ok(host.call(host.owner(host.fresh), "owner_conversation_send", scoped(source, text="Owner to Ordubashy")))
    assert [item["source_type"] for item in host.pending(host.coordinator)] == ["owner_conversations"]
