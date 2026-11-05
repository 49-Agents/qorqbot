"""Ordubashy's notice of an owner message to a worker (inbox message or Chat), and the record thread that is Ordubashy's alone.

The owner's private conversation on a record is with Ordubashy only: a worker has no private owner channel, so nothing in it is
forwarded (the retired per-worker conversations and their ``private_conversation`` notices are removed by the offline cleanup
in ``test_private_peer_cleanup.py``). The notices that remain are those of the owner's inbox message and Chat input to a worker.

COMPOSED. A worker is an active registered global holding the ``global-worker`` role, decided by Foundation's
``bazarlyk.agents.worker_access.is_worker``, and the synthetic worker comes from its ``create_worker`` fixture helper. Both
are part of this host, so the module runs whole; the guards below only protect an older checkout.

Real: the stock host assembly, the owner's ``agents_create`` of workers, the owner's ``agents_message_send``,
``tasks_create``, the ``owner_conversation_*`` operations, and the actual Chat path
(``NativeChat.send`` into ``InputArbiter.submit`` and the backend delivery pass). Synthetic: the tmp instance and its principals,
and the fake Claude/Codex panes, transcripts and tmux of ``native_chat_support``. No model, real session, provider or network
is used.
"""

from __future__ import annotations

import pytest

from qorqut_operations.contracts import DomainError, InvocationOptions, Principal, new_id, plain

pytest.importorskip("bazarlyk.agents.worker_access", reason="Foundation's worker eligibility is not composed on this branch")
support = pytest.importorskip("host_foundation_support")
if not hasattr(support, "create_worker"):
    pytest.skip("Foundation's synthetic worker fixture is not composed on this branch", allow_module_level=True)

from bazarlyk.inbox.owner_forwarding import forwarded_id
from bazarlyk.native_chat.contracts import InputRequest, TYPE_DIRECT
from host_foundation_support import (build_instance, create_registered_global, create_worker, owner_global_context, register_sources,
                                     select_coordinator, set_agent_overrides)
from native_chat_support import Native, count, rows
from test_native_chat_producers import FakeTerminals, profile_row

PRIVATE_GRANTS = {"owner_conversation_get": "allow", "owner_conversation_send": "allow"}
TEXT = "private-owner-line-" + "q" * 20
MARK = "\nOwner message:\n"


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first",))
    register_sources(instance, ids["first"], "first")
    coordinator = select_coordinator(instance, create_registered_global(instance, "ordubashy"))
    colleague = create_worker(instance, "colleague")
    learner = create_registered_global(instance, "learning-applier")
    workers = [create_worker(instance, "worker-a"), create_worker(instance, "worker-b")]
    for identity in (coordinator, colleague, *workers):
        set_agent_overrides(instance, identity, PRIVATE_GRANTS, (ids["first"],))
    instance.runtime.freeze_registry()
    try:
        yield _Host(instance, coordinator, colleague, learner, *workers)
    finally:
        try:
            instance.runtime.release_instance()
        finally:
            instance.database.close()


class _Host:
    def __init__(self, instance, coordinator, colleague, learner, first, second):
        self.instance, self.runtime, self.database = instance, instance.runtime, instance.database
        self.coordinator, self.colleague, self.learner, self.first, self.second = coordinator, colleague, learner, first, second
        self.owner = owner_global_context(instance)

    def call(self, ctx, name, arguments, key=None):
        return self.runtime.application.invoke(ctx, name, arguments, InvocationOptions(idempotency_key=key or new_id()))

    def agent(self, identity):
        return self.runtime.resolver.invocation_context(Principal(identity, "agent"))

    def query(self, sql, *parameters):
        with self.database.read() as uow:
            return [dict(row) for row in uow.connection.execute(sql, parameters)]

    def notices(self):
        """The forwarded notices Ordubashy holds (not the owner's ordinary messages to him)."""
        return self.query("SELECT * FROM agent_messages WHERE recipient_id=? AND sender_id=? AND body LIKE 'Forwarded notice:%' "
                          "ORDER BY created_at, id", self.coordinator, self.instance.owner)

    def deliveries(self, identity, event_type):
        return self.query("SELECT count(*) AS n FROM inbox_deliveries d JOIN inbox_events e ON e.id=d.event_id "
                          "WHERE d.agent_id=? AND e.event_type=?", identity, event_type)[0]["n"]


def ok(result):
    assert result.status == "ok", result.error
    return plain(result.data)


def refused(result, code="forbidden"):
    # An agent without the explicit private-channel grants is stopped by policy before the service's own participant check.
    codes = {code, "policy_denied"} if code == "forbidden" else {code}
    assert result.status == "error" and result.error["code"] in codes, (result.status, result.error)


def task(host):
    created = ok(host.call(host.owner, "tasks_create", {"title": "Private work", "scope": "global"}))
    return {"type": "task", "id": created["task"]["id"], "business_id": None}


def scoped(source, **arguments):
    return {"source": source, "scope": "global", **arguments}


def texts(host, ctx, source, **arguments):
    return [message["body"] for message in ok(host.call(ctx, "owner_conversation_get", scoped(source, **arguments)))["messages"]]


# ----- the record thread is Ordubashy's alone: nothing in it is forwarded ----------------------------------------------

def test_a_record_thread_message_is_delivered_to_ordubashy_alone_and_never_forwarded_as_a_notice(host):
    source, key = task(host), new_id()
    sent = ok(host.call(host.owner, "owner_conversation_send", scoped(source, text=TEXT), key))
    ok(host.call(host.agent(host.coordinator), "owner_conversation_send", scoped(source, text="Ordubashy answers")))
    assert texts(host, host.owner, source) == [TEXT, "Ordubashy answers"]
    assert host.deliveries(host.coordinator, "private_message") == 1
    for worker in (host.first, host.second, host.colleague, host.learner):
        assert host.deliveries(worker, "private_message") == 0
    # No notice of any kind is written: the thread is already Ordubashy's, so there is nothing to keep him informed of.
    assert host.notices() == [] and host.deliveries(host.coordinator, "direct_message") == 0
    assert host.query("SELECT count(*) AS n FROM agent_messages")[0]["n"] == 0
    # The same key replays the saved receipt and writes nothing.
    assert ok(host.call(host.owner, "owner_conversation_send", scoped(source, text=TEXT), key))["message"]["id"] == sent["message"]["id"]
    assert host.query("SELECT count(*) AS n FROM owner_conversation_messages")[0]["n"] == 2
    assert [(peer["agent_id"], peer["channel"]) for peer in ok(host.call(host.owner, "owner_conversation_peers", scoped(source)))["peers"]] \
        == [(host.coordinator, "private")]


def test_the_owner_cannot_select_a_worker_or_any_other_agent_and_nothing_is_written_or_forwarded(host):
    source = task(host)
    # ``global-worker`` is not a peer role, and no UUID but the registered Ordubashy's is accepted.
    for name, extra in (("owner_conversation_get", {}), ("owner_conversation_send", {"text": TEXT})):
        refused(host.call(host.owner, name, scoped(source, peer_role="global-worker", **extra)), "validation_error")
        for other in (host.first, host.second, host.colleague, host.learner):
            refused(host.call(host.owner, name, scoped(source, agent_id=other, **extra)))
            refused(host.call(host.owner, name, scoped(source, peer_role="ordubashy", agent_id=other, **extra)))
        refused(host.call(host.owner, name, scoped(source, peer_role="global-worker", agent_id=host.first, **extra)), "validation_error")
    assert host.notices() == []
    assert host.query("SELECT count(*) AS n FROM owner_conversations")[0]["n"] == 0
    assert host.query("SELECT count(*) AS n FROM owner_conversation_messages")[0]["n"] == 0
    # Stating the real Ordubashy is only a restatement of the one channel.
    ok(host.call(host.owner, "owner_conversation_send", scoped(source, text=TEXT, peer_role="ordubashy", agent_id=host.coordinator)))
    assert texts(host, host.owner, source) == [TEXT] and host.notices() == []


def test_no_other_agent_reads_or_writes_the_ordubashy_channel_and_a_worker_has_no_default_channel(host):
    source = task(host)
    ok(host.call(host.owner, "owner_conversation_send", scoped(source, text=TEXT)))
    for other in (host.first, host.second, host.colleague, host.learner):
        for arguments in ({}, {"agent_id": other}, {"agent_id": host.coordinator}, {"peer_role": "ordubashy"}):
            refused(host.call(host.agent(other), "owner_conversation_get", scoped(source, **arguments)))
            refused(host.call(host.agent(other), "owner_conversation_send", scoped(source, text="Into the channel", **arguments)))
    # Ordubashy himself cannot name a worker's channel either.
    refused(host.call(host.agent(host.coordinator), "owner_conversation_get", scoped(source, agent_id=host.first)))
    refused(host.call(host.agent(host.coordinator), "owner_conversation_send", scoped(source, text="To a worker", agent_id=host.first)))
    assert texts(host, host.agent(host.coordinator), source) == [TEXT]
    assert host.query("SELECT count(*) AS n FROM owner_conversation_messages")[0]["n"] == 1


# ----- the owner's inbox message to a worker -------------------------------------------------------------------------

def test_an_owner_inbox_message_to_a_worker_is_noticed_but_not_one_to_another_global_agent_or_from_a_worker(host):
    for recipient in (host.colleague, host.learner, host.coordinator):
        ok(host.call(host.owner, "agents_message_send", {"scope": "global", "to_agent_id": recipient, "message": TEXT}))
    assert host.notices() == []
    sent = ok(host.call(host.owner, "agents_message_send", {"scope": "global", "to_agent_id": host.first, "message": TEXT}))
    [notice] = host.notices()
    assert notice["id"] == forwarded_id("agent_message", sent["message"]["id"], host.coordinator)
    assert notice["body"].endswith(MARK + TEXT) and host.first in notice["body"]
    # A worker's own message to a peer is agent-authored and never noticed.
    ok(host.call(host.agent(host.first), "agents_message_send", {"scope": "global", "to_agent_id": host.second, "message": TEXT}))
    assert len(host.notices()) == 1


# ----- the owner's Chat message to a worker, through the real admission path ---------------------------------------------

CHAT_KEY = "chat-key-000001"
# 17000 characters (the Chat limit), multi-line and non-ASCII, neither starting nor ending in whitespace.
LONG = ("Owner line é — one\n\nsecond paragraph\n" * 1000)[:16999] + "!"


def chat_worker(tmp_path, provider="claude"):
    """The tmp host's one chat agent is a worker ``chat-worker``; the synthetic Ordubashy is created and selected first. Returns
    the Native harness and Ordubashy's identity."""
    made = {}

    def factory(instance):
        made["ordubashy"] = select_coordinator(instance, create_registered_global(instance, "ordubashy"))
        return create_worker(instance, "chat-worker")

    native = Native(tmp_path, provider, agent_factory=factory)
    if provider == "codex":
        native.transport, native.thread_owner = "codex_queue", "owner-1"
    native.publish()
    if provider == "codex":
        native.write(native.codex_session_meta())
    return native, made["ordubashy"]


def chat_notices(native, ordubashy):
    return rows(native.runtime, "SELECT * FROM agent_messages WHERE recipient_id=? AND sender_id=? AND body LIKE 'Forwarded notice:%' "
                "ORDER BY created_at, id", (ordubashy, native.owner))


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_an_accepted_owner_chat_to_a_worker_is_noticed_once_at_admission_and_the_notice_outlives_the_scrubbed_input(tmp_path, provider):
    native, ordubashy = chat_worker(tmp_path, provider)
    assert len(LONG) == 17000
    pin = native.pin()
    sent = native.chat.send(native.owner, native.agent, LONG, CHAT_KEY, pin)
    assert sent["created"] is True and sent["input"]["state"] == "waiting"
    assert native.pane.typed() == [] and native.queued == []                   # delivery waits; nothing is typed yet
    # Admission alone wrote the one complete notice, in its own transaction, before any delivery.
    [notice] = chat_notices(native, ordubashy)
    accepted = rows(native.runtime, "SELECT created_at FROM native_inputs WHERE id=?", (sent["input"]["id"],))[0]["created_at"]
    assert notice["id"] == forwarded_id("chat", sent["input"]["id"], ordubashy)
    assert notice["body"].endswith("\nOwner message:\n" + LONG) and len(notice["body"]) <= len(LONG) + 1000
    assert native.agent in notice["body"] and sent["input"]["id"] in notice["body"] and accepted in notice["body"]
    assert (notice["thread_id"], notice["current_recipient_id"]) == (notice["id"], ordubashy)
    assert rows(native.runtime, "SELECT count(*) AS n FROM inbox_deliveries d JOIN inbox_events e ON e.id=d.event_id "
                "WHERE d.agent_id=? AND e.source_id=? AND e.summary='Owner message to a worker'", (ordubashy, notice["id"]))[0]["n"] == 1
    # The exact retry returns the original input and adds no second notice.
    again = native.chat.send(native.owner, native.agent, LONG, CHAT_KEY, pin)
    assert again["created"] is False and again["input"]["id"] == sent["input"]["id"]
    assert [row["id"] for row in chat_notices(native, ordubashy)] == [notice["id"]]
    # Delivery and transcript reconciliation scrub the native input's body; Ordubashy's accepted private copy remains, unchanged.
    native.pass_()
    if provider == "claude":
        assert native.pane.typed() == [LONG]
    else:
        assert native.queued[0][0][-1] == LONG
        native.client_user_turn(LONG)
    native.pass_()
    state = native.chat.state(native.owner, native.agent)
    assert [i["state"] for i in state["inputs"]] == ["submitted"] and state["inputs"][0]["body"] is None
    assert rows(native.runtime, "SELECT body FROM native_inputs") == [{"body": None}]
    assert chat_notices(native, ordubashy) == [notice]
    assert len(chat_notices(native, ordubashy)) == 1


def test_chat_input_that_is_refused_offline_stale_or_invalid_leaves_no_notice(tmp_path):
    native, ordubashy = chat_worker(tmp_path)
    old = native.pin()
    native.chat.send(native.owner, native.agent, "Accepted words", CHAT_KEY, old)
    [accepted] = chat_notices(native, ordubashy)
    stored = count(native.runtime, "native_inputs")
    # A conflicting reuse of the key and an invalid text are refused before admission.
    for text, key in (("Something else", CHAT_KEY), ("", "chat-key-000002"), ("x" * 20000, "chat-key-000003")):
        with pytest.raises(DomainError):
            native.chat.send(native.owner, native.agent, text, key, old)
    # The pinned session ended and a new one is live: a new request for the old pin admits nothing.
    native.end()
    native.pass_(3)
    native.new_session()
    for pin in (old, "no-such-binding-id"):
        with pytest.raises(DomainError) as stale:
            native.chat.send(native.owner, native.agent, "Fresh words", "chat-key-000004", pin)
        assert stale.value.code == "terminal_changed"
    # The session is offline: rejected, stored nothing.
    live = native.pin()
    native.end()
    with pytest.raises(DomainError) as offline:
        native.chat.send(native.owner, native.agent, "Anyone there?", "chat-key-000005", live)
    assert offline.value.code == "terminal_offline"
    assert count(native.runtime, "native_inputs") == stored and chat_notices(native, ordubashy) == [accepted]


def test_a_broadcast_a_command_ping_and_raw_direct_input_to_a_worker_are_never_noticed(tmp_path):
    native, ordubashy = chat_worker(tmp_path)
    terminals = FakeTerminals(native)
    assert terminals.input_existing(None, native.agent, "Weekly note", key="broadcast:b1:1:r1")["state"] == "delivered"
    row, backend = profile_row(native), native.terminal_backend
    note = backend.execute(row, "ping", "A colleague asks", backend.snapshot(row), input_key="terminal-action:a1")
    assert note.startswith("Direct message submitted")
    # A direct-message input is owner-origin and may wait like Chat; only the Chat type is noted.
    held = native.arbiter.submit(InputRequest(native.agent, TYPE_DIRECT, "raw-direct:1", "Raw owner words", wait=True,
                                              principal_id=native.owner))
    assert held.created is True
    native.arbiter.submit(InputRequest(native.agent, TYPE_DIRECT, "raw-direct:2", "More raw words", principal_id=native.owner))
    assert {r["input_type"] for r in rows(native.runtime, "SELECT input_type FROM native_inputs")} == {"broadcast", "direct_message"}
    assert chat_notices(native, ordubashy) == []
    assert rows(native.runtime, "SELECT count(*) AS n FROM agent_messages")[0]["n"] == 0
