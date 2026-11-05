"""The direct messages and broadcasts after their move into Operations, driven through the stock host composition.

Real: the host assembly and schema, the Operations ``Messages``/``Broadcasts``/``BroadcastDispatch`` with the host adapters bound,
the delegation store and the terminal controller. Synthetic: a tmp instance, its globals and delegated assignment, a fake terminal
port and fake terminal-control objects. No model, native session, provider or network is used.

Each scenario states the behavior it pins: the order of the adapter calls, the scoped history and its pagination, the refusal of a
revoked binding or a protected recipient both when a message is prepared and when it is applied, the same-transaction owner
forwarding, the unchanged terminal outcomes, and the broadcast engine's once-only, re-authorizing and uncertain-before-effect
delivery."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import DomainError, InvocationOptions, Principal, new_id, plain
from qorqut_operations.inbox import BroadcastDispatch

from bazarlyk.agents.operational_delegation import OperationalDelegation
from bazarlyk.inbox import messages as host_messages
from bazarlyk.inbox.broadcasts import BroadcastDispatcher
from bazarlyk.inbox.messages import MessageHost, Messages
from host_foundation_support import build_instance, create_registered_global, create_worker, register_sources

NOW = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
LATER = datetime(2031, 1, 1, tzinfo=timezone.utc)    # after every due time the scenarios create


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first", "second"))
    first, second = ids["first"], ids["second"]
    register_sources(instance, first, "first")
    register_sources(instance, second, "second")
    coordinator = create_registered_global(instance, "ordubashy")
    applier = create_registered_global(instance, "learning-applier")
    instance.runtime.freeze_registry()
    return SimpleNamespace(instance=instance, runtime=instance.runtime, database=instance.database, first=first, second=second,
                           coordinator=coordinator, applier=applier, colleague=create_worker(instance, "colleague"),
                           astra=create_worker(instance, "astra"), other=create_worker(instance, "other"))


def agent(host, identity, business=None):
    return host.runtime.resolver.invocation_context(Principal(identity, "agent"), business_id=business)


def call(host, ctx, operation, arguments, *, revision=None):
    return host.runtime.application.invoke(ctx, operation, arguments,
                                           InvocationOptions(idempotency_key=new_id(), expected_revision=revision))


def ok(result):
    assert result.status == "ok", result.error
    return plain(result.data)


def code(result):
    assert result.status == "error", result.data
    return result.error["code"]


def rows(host, sql, *parameters):
    with host.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(sql, parameters)]


def counts(host):
    return {name: rows(host, sql)[0]["n"] for name, sql in {
        "messages": "SELECT count(*) AS n FROM agent_messages",
        "events": "SELECT count(*) AS n FROM inbox_events WHERE source_type='agent_messages'",
        "deliveries": "SELECT count(*) AS n FROM inbox_deliveries d JOIN inbox_events e ON e.id=d.event_id "
                      "WHERE e.source_type='agent_messages'"}.items()}


def prepare(host, ctx, arguments):
    with host.database.read() as uow:
        return host.runtime.inbox.messages.prepare(ctx, arguments, InvocationOptions(idempotency_key=new_id()), uow)


def apply(host, ctx, plan):
    with host.database.transaction() as uow:
        return host.runtime.inbox.messages.apply(ctx, plan, uow)


def send(host, ctx, to, text, **fields):
    return ok(call(host, ctx, "agents_message_send", {"to_agent_id": to, "message": text, **fields}))["message"]


# -- the host adapter is the only way in -----------------------------------------------------------------------------------

def test_the_inbox_service_message_flow_is_operations_with_the_host_adapter_bound(host):
    from qorqut_operations.inbox import Messages as Generic
    messages = host.runtime.inbox.messages
    assert isinstance(messages, Messages) and isinstance(messages, Generic)
    assert isinstance(messages.host, MessageHost) and messages.host.inbox is host.runtime.inbox
    assert type(host.runtime.broadcasts).__mro__[1].__module__ == "qorqut_operations.inbox.broadcasts"
    assert host_messages.Messages.view is Generic.view  # one implementation, an instance method


def test_the_composed_host_uses_the_operations_inbox_declarations_at_their_existing_places():
    """The host owner tuple, table specs, history classifications and standing policy come from the module, in the old places."""
    import dataclasses
    from qorqut_operations import inbox
    from qorqut_operations.history_exports import PRIVATE_EXCLUDED, TECHNICAL_EXCLUDED
    from bazarlyk.composition import policy, schema_ddl, tables
    from bazarlyk.inbox import defaults
    from bazarlyk.operations_history.catalog import catalog
    statements = schema_ddl.OWNER_SCHEMA["inbox"]
    assert len(statements) == 40
    assert (statements[0:1], statements[2:14], statements[14:18], statements[24:27], statements[27:36], statements[37:40]) == (
        inbox.CHANGE_SCHEMA, inbox.CAPTURE_SCHEMA, inbox.BROADCAST_SCHEMA, inbox.PRINCIPAL_CAPTURE_SCHEMA, inbox.MESSAGE_SCHEMA,
        inbox.RELATIONSHIP_CAPTURE_SCHEMA)
    assert statements[1].startswith("CREATE TABLE native_session_owners (") and statements[18].startswith("CREATE TABLE owner_conversations (")
    assert statements[23].startswith("CREATE TABLE owner_conversation_reads (") and statements[36].startswith("CREATE INDEX owner_conversation_peer ")
    for name in inbox.TABLE_NAMES:
        assert dataclasses.asdict(inbox.TABLE_SPECS[name]) == dataclasses.asdict(tables.TABLE_SPECS[name])
        assert name in schema_ddl.OWNER_TABLES["inbox"] and name not in tables.IMMUTABLE_FIELDS
    names = list(tables.TABLE_SPECS)
    first = names.index("agent_messages")
    assert names[first:first + 5] == ["agent_messages", "broadcasts", "broadcast_deliveries", "inbox_changes", "native_session_owners"]
    classifications = {name: catalog().classifications[name][1] for name in inbox.TABLE_NAMES}
    assert {c.table: c for c in inbox.CLASSIFICATIONS} == classifications
    assert classifications["inbox_changes"].kind == TECHNICAL_EXCLUDED
    assert all(classifications[name].kind == PRIVATE_EXCLUDED for name in ("agent_messages", "broadcasts", "broadcast_deliveries"))
    assert policy.INBOX_OPERATIONS is inbox.INBOX_OPERATIONS and not hasattr(defaults, "INBOX_OPERATIONS")


# -- scoped messages, replies, history and pagination ------------------------------------------------------------------------

def test_a_message_and_its_reply_share_a_thread_and_history_is_scoped_and_paginated(host):
    ordubashy, colleague = agent(host, host.coordinator, host.first), agent(host, host.colleague, host.first)
    first = send(host, ordubashy, host.colleague, "Please review the supplier note", summary="Supplier note")
    assert first["thread_id"] == first["id"] and first["reply_to"] is None
    assert first["sender_name"] and first["recipient_kind"] == "agent" and "terminal_delivery" not in first
    assert first["counterpart_id"] == host.colleague and first["acknowledged_at"] is None and first["outcome"] is None
    assert not any(field.startswith("delegation_") and field != "delegation_attribution" for field in first)
    reply = ok(call(host, colleague, "agents_message_send", {"reply_to": first["id"], "message": "Reviewed"}))["message"]
    assert (reply["thread_id"], reply["recipient_id"], reply["counterpart_id"]) == (first["id"], host.coordinator, host.coordinator)
    for number in range(3):
        send(host, ordubashy, host.colleague, f"Follow-up {number}")
    pages, cursor = [], None
    while True:
        page = ok(call(host, colleague, "agents_messages_list", {"limit": 2, **({"after": cursor} if cursor else {})}))
        pages.append([item["body"] for item in page["items"]])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == [["Please review the supplier note", "Reviewed"], ["Follow-up 0", "Follow-up 1"], ["Follow-up 2"]]
    thread = ok(call(host, colleague, "agents_messages_list", {"thread_id": first["id"]}))["items"]
    assert [item["body"] for item in thread] == ["Please review the supplier note", "Reviewed"]
    counterpart = ok(call(host, ordubashy, "agents_messages_list", {"counterpart_id": host.colleague}))["items"]
    assert len(counterpart) == 5
    assert ok(call(host, ordubashy, "agents_messages_list", {"counterpart_id": host.other}))["items"] == []
    # Another scope and another participant see none of it.
    assert ok(call(host, agent(host, host.coordinator, host.second), "agents_messages_list", {}))["items"] == []
    assert ok(call(host, agent(host, host.other, host.first), "agents_messages_list", {}))["items"] == []
    recipients = ok(call(host, ordubashy, "agents_message_recipients", {}))
    ids = {item["agent_id"] for item in recipients["items"]}
    assert host.colleague in ids and host.applier not in ids and host.coordinator not in ids


def test_a_collapsed_thread_is_read_only_history(host):
    ordubashy, colleague = agent(host, host.coordinator, host.first), agent(host, host.colleague, host.first)
    first = send(host, ordubashy, host.colleague, "Original")
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_messages SET current_recipient_id=current_sender_id WHERE id=?", (first["id"],))
    assert code(call(host, ordubashy, "agents_message_send", {"reply_to": first["id"], "message": "Again"})) == "invalid_transition"
    listed = ok(call(host, ordubashy, "agents_messages_list", {}))["items"]
    assert [item["counterpart_id"] for item in listed] == [None]
    assert code(call(host, colleague, "agents_message_send", {"reply_to": first["id"], "message": "No"})) == "not_found"


def test_an_owner_directed_recipient_is_refused_at_prepare_and_at_apply(host):
    ordubashy = agent(host, host.coordinator, host.first)
    before = counts(host)
    with pytest.raises(DomainError) as refused:
        prepare(host, ordubashy, {"to_agent_id": host.applier, "message": "Hello"})
    assert refused.value.code == "not_found" and "owner-directed" in refused.value.message
    plan = prepare(host, ordubashy, {"to_agent_id": host.colleague, "message": "Hello"})
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.role.id','learning-applier') "
                               "WHERE agent_id=?", (host.colleague,))
    with pytest.raises(DomainError) as refused:
        apply(host, ordubashy, plan)
    assert refused.value.code == "not_found" and "owner-directed" in refused.value.message
    assert counts(host) == before


# -- a delegated worker ----------------------------------------------------------------------------------------------------

@pytest.fixture
def delegated(host):
    store = OperationalDelegation(clock=lambda: NOW.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                                  dispatch_reason=None)
    stamp = store.clock()
    with host.database.transaction() as uow:
        assignment = store.create(uow, parent_principal_id=host.astra, business_id=host.first,
            scope={"kind": "business", "business_id": host.first, "record_ids": {}, "source_ids": {}},
            objective="Coordinate one approved operational update", plan={"steps": ["message", "verify"]},
            allowed_operations=["agents_message_send", "agents_messages_list"], internal_recipients=[host.colleague])
        uow.repository("machine_registrations").insert({"id": new_id(), "principal_id": host.astra,
            "workspace": str(host.instance.workspace), "service_url": "http://127.0.0.1:9000", "profile_revision": 1,
            "created_at": stamp, "updated_at": stamp})
        running = store.claim(uow, assignment["id"], actor_principal_id=host.astra)
        issued = store.issue_worker_binding(uow, assignment["id"], parent_principal_id=host.astra,
            generation=running["execution_generation"], lease_id=running["lease_id"], expected_revision=running["revision"],
            workspace=str(host.instance.workspace.parent / "delegated-worker"), service_url="http://127.0.0.1:9000", expires_at=running["lease_until"],
            runtime_thread_id="thread-split")
    worker = replace(agent(host, host.astra, host.first), session_registration_id=issued["registration_id"],
                     delegation_binding_id=issued["binding"]["id"], delegation_assignment_id=assignment["id"],
                     delegation_generation=running["execution_generation"])
    return SimpleNamespace(assignment=assignment, running=running, issued=issued, worker=worker)


def test_a_delegated_worker_message_keeps_its_assignment_attribution_recipients_and_scoped_history(host, delegated):
    messages, worker = host.runtime.inbox.messages, delegated.worker
    plan = prepare(host, worker, {"to_agent_id": host.colleague, "message": "Please check the approved supplier update."})
    sent = apply(host, worker, plan)["message"]
    assert sent["sender_id"] == host.astra
    assert sent["delegation_attribution"] == {"assignment_id": delegated.assignment["id"],
        "generation": delegated.running["execution_generation"], "binding_id": delegated.issued["binding"]["id"],
        "plan_revision": delegated.assignment["plan_revision"], "worker_authored": True}
    assert not {"delegation_assignment_id", "delegation_generation", "delegation_binding_id", "delegation_plan_revision"} & set(sent)
    with pytest.raises(DomainError, match="recipient is outside"):
        prepare(host, worker, {"to_agent_id": host.coordinator, "message": "Out of scope"})
    # The peer's reply, a plain message of its own, inherits the assignment's attribution without a worker binding.
    reply = apply(host, agent(host, host.colleague, host.first), prepare(host, agent(host, host.colleague, host.first),
        {"reply_to": sent["id"], "message": "Checked"}))["message"]
    assert reply["thread_id"] == sent["thread_id"] and reply["delegation_attribution"]["worker_authored"] is False
    assert reply["delegation_attribution"]["assignment_id"] == delegated.assignment["id"]
    # An unrelated message of the same parent is outside the worker's history; a linked or assignment thread is inside.
    plain_ctx = agent(host, host.astra, host.first)
    unrelated = send(host, plain_ctx, host.colleague, "Parent only")
    with host.database.read() as uow:
        scoped = messages.history(worker, {}, None, uow)
        limited = messages.history(worker, {"limit": 1}, None, uow)
        by_thread = messages.history(worker, {"thread_id": sent["thread_id"]}, None, uow)
        outside = messages.history(worker, {"thread_id": unrelated["thread_id"]}, None, uow)
    assert [item["id"] for item in scoped["items"]] == [sent["id"], reply["id"]] and scoped["next_cursor"] is None
    assert [item["id"] for item in limited["items"]] == [sent["id"]] and limited["next_cursor"] is not None
    assert [item["id"] for item in by_thread["items"]] == [sent["id"], reply["id"]]
    assert outside["items"] == []
    with host.database.read() as uow:
        assert [item["id"] for item in messages.history(plain_ctx, {}, None, uow)["items"]] == [sent["id"], reply["id"], unrelated["id"]]


def test_a_reply_outside_the_assignment_threads_is_refused(host, delegated):
    outsider = send(host, agent(host, host.colleague, host.first), host.astra, "Unrelated thread")
    with pytest.raises(DomainError, match="only within a thread bound"):
        prepare(host, delegated.worker, {"reply_to": outsider["id"], "message": "Reply"})


def test_a_revoked_delegated_binding_is_refused_at_prepare_and_at_apply(host, delegated):
    worker, before = delegated.worker, counts(host)
    plan = prepare(host, worker, {"to_agent_id": host.colleague, "message": "Before revocation"})
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE operational_delegation_bindings SET revoked_at=? WHERE id=?",
                               (NOW.isoformat(), delegated.issued["binding"]["id"]))
    for attempt in (lambda: prepare(host, worker, {"to_agent_id": host.colleague, "message": "After"}), lambda: apply(host, worker, plan)):
        with pytest.raises(DomainError) as refused:
            attempt()
        assert refused.value.code == "forbidden" and refused.value.message == "Worker message requires its current assignment binding"
    assert counts(host) == before


# -- owner forwarding is part of the same unit of work ----------------------------------------------------------------------

def test_a_failure_while_forwarding_to_the_owner_conversation_rolls_back_the_whole_message(host, monkeypatch):
    ordubashy, calls = agent(host, host.coordinator, host.first), []
    plan = prepare(host, ordubashy, {"to_agent_id": host.colleague, "message": "Forwarded text"})
    before = counts(host)

    def failing(uow, **fields):
        calls.append(fields)
        assert uow.connection.execute("SELECT count(*) FROM agent_messages").fetchone()[0] == before["messages"] + 1
        raise RuntimeError("forwarding failed")

    monkeypatch.setattr(host_messages, "forward_owner_message", failing)
    with pytest.raises(RuntimeError, match="forwarding failed"):
        apply(host, ordubashy, plan)
    assert counts(host) == before
    (fields,) = calls
    assert fields["kind"] == "agent_message" and fields["sender_id"] == host.coordinator and fields["worker_id"] == host.colleague
    assert fields["body"] == "Forwarded text" and fields["source_id"] and fields["accepted_at"]
    # A broadcast is not an accepted direct message and is never forwarded.
    calls.clear()
    assert ok(call(host, agent(host, host.coordinator, host.first), "broadcasts_create", {"send_now": True, "definition": {
        "name": "Note", "delivery": "inbox", "message": "Broadcast", "recipients": [host.colleague],
        "timing": {"kind": "once", "at": "2030-01-01T09:00:00Z"}}}))
    dispatcher = BroadcastDispatcher(host.runtime, host.runtime.broadcasts)
    while dispatcher.tick() is not None:
        pass
    assert calls == [] and counts(host)["messages"] == before["messages"] + 1


# -- terminal delivery ------------------------------------------------------------------------------------------------------

def test_terminal_delivery_keeps_its_refusal_and_unavailable_outcomes(host, monkeypatch):
    ordubashy, application = agent(host, host.coordinator, host.first), host.runtime.application
    arguments = {"to_agent_id": host.colleague, "message": "Type this", "delivery": "terminal"}
    before = counts(host)
    with pytest.raises(DomainError) as refused:
        prepare(host, ordubashy, {**arguments, "message": "bad \x07 bell"})
    assert (refused.value.code, refused.value.message) == ("validation_error", "Terminal messages cannot contain control characters")
    monkeypatch.setattr(application, "terminal_control", None, raising=False)
    with pytest.raises(DomainError) as refused:
        prepare(host, ordubashy, arguments)
    assert (refused.value.code, refused.value.message) == ("operation_unavailable",
                                                           "Terminal message delivery is unavailable; use inbox delivery")
    seen = []

    class Controller:
        backend = SimpleNamespace(validate=lambda *args: seen.append(("validate", args)))

        def message_target(self, ctx, identity, uow):
            seen.append(("target", identity))
            raise DomainError("forbidden", "Direct terminal messages targeted retired project Dev agents; use inbox delivery")

    monkeypatch.setattr(application, "terminal_control", Controller())
    with pytest.raises(DomainError) as refused:
        prepare(host, ordubashy, arguments)
    assert (refused.value.code, refused.value.message) == ("forbidden",
        "Direct terminal messages targeted retired project Dev agents; use inbox delivery")
    assert seen == [("target", host.colleague)] and counts(host) == before
    # The ordinary inbox delivery has no terminal state at all.
    assert "terminal_delivery" not in send(host, ordubashy, host.colleague, "Inbox only", delivery="inbox")


def test_the_terminal_adapter_validates_queues_and_projects_through_the_controller(host, monkeypatch):
    ordubashy, application, seen = agent(host, host.coordinator, host.first), host.runtime.application, []

    class Controller:
        backend = SimpleNamespace(validate=lambda *args: seen.append(("validate", args)))

        def message_target(self, ctx, identity, uow):
            seen.append(("target", identity))
            return {"revision": 3}

        def binding(self, target):
            seen.append(("binding", target))
            return {"terminal": "pane-1"}

        def queue_message(self, ctx, row, uow):
            seen.append(("queue", row["id"], row["recipient_id"]))

    monkeypatch.setattr(application, "terminal_control", Controller())
    messages = host.runtime.inbox.messages
    plan = prepare(host, ordubashy, {"to_agent_id": host.colleague, "message": "Type this", "delivery": "terminal"})
    assert seen == [("target", host.colleague), ("binding", {"revision": 3}), ("validate", ("ping", "pane-1"))]
    seen.clear()
    sent = apply(host, ordubashy, plan)["message"]
    assert seen[:3] == [("target", host.colleague), ("binding", {"revision": 3}), ("validate", ("ping", "pane-1"))]
    assert seen[3] == ("queue", sent["id"], host.colleague) and "terminal_delivery" not in sent
    with host.database.transaction() as uow:
        uow.connection.execute("INSERT INTO terminal_actions(id,batch_id,proposer_id,target_id,action,message,binding_json,state,"
            "detail,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (sent["id"], new_id(), host.coordinator, host.colleague,
            "ping", "Type this", json.dumps({"message_id": sent["id"]}), "queued", "waiting", NOW.isoformat(), NOW.isoformat()))
    with host.database.read() as uow:
        row = uow.connection.execute("SELECT * FROM agent_messages WHERE id=?", (sent["id"],)).fetchone()
        assert messages.view(row, uow, host.coordinator)["terminal_delivery"] == {
            "action_id": sent["id"], "state": "queued", "detail": "waiting"}
        assert messages.host.terminal_projection(row, uow) == {"action_id": sent["id"], "state": "queued", "detail": "waiting"}
    other = send(host, ordubashy, host.colleague, "Later inbox")
    with host.database.read() as uow:
        row = uow.connection.execute("SELECT * FROM agent_messages WHERE id=?", (other["id"],)).fetchone()
        assert messages.host.terminal_projection(row, uow) is None


# -- the broadcast engine ---------------------------------------------------------------------------------------------------

def broadcast(host, ctx, people, *, delivery="inbox", message="Weekly note", scope=None):
    arguments = {"definition": {"name": "Note", "delivery": delivery, "message": message, "recipients": list(people),
                                "timing": {"kind": "once", "at": "2030-01-01T09:00:00Z"}}, "send_now": True}
    if scope:
        arguments["scope"] = scope
    return ok(call(host, ctx, "broadcasts_create", arguments))["records"][0]


def owner_ctx(host, business=None):
    return host.instance.human(business or host.first)


def deliveries(host, identity):
    return {row["recipient_id"]: row for row in rows(host,
        "SELECT * FROM broadcast_deliveries WHERE broadcast_id=? ORDER BY occurrence,recipient_id", identity)}


def drain(dispatcher):
    for _ in range(20):
        if dispatcher.tick() is None:
            return
    raise AssertionError("the dispatcher kept finding due work")


class Terminals:
    def __init__(self, behaviour=None):
        self.calls, self.behaviour = [], behaviour

    def input_existing(self, business_id, agent_id, data, key=None):
        self.calls.append((business_id, agent_id, data, key))
        return self.behaviour(self, agent_id) if self.behaviour else {"state": "delivered"}


class Crash(BaseException):
    """A process that dies mid-write: not an Exception, so nothing in the dispatcher can record an outcome for it."""


def test_the_dispatcher_delegates_to_the_operations_engine_and_keeps_its_lifecycle(host):
    dispatcher = BroadcastDispatcher(host.runtime, host.runtime.broadcasts, None, interval=0.01)
    assert isinstance(dispatcher.engine, BroadcastDispatch) and dispatcher.engine.broadcasts is host.runtime.broadcasts
    assert dispatcher.engine.application is host.runtime.application and dispatcher.engine.database is host.runtime.database
    assert dispatcher.thread is None and not dispatcher.stop_event.is_set()
    dispatcher.start()
    thread = dispatcher.thread
    assert thread.name == "bazarlyk-broadcast-dispatch" and thread.daemon
    dispatcher.start()
    assert dispatcher.thread is thread
    dispatcher.stop()
    assert dispatcher.thread is None and not thread.is_alive() and dispatcher.stop_event.is_set()
    assert BroadcastDispatcher._next({"kind": "once"}, NOW) is None
    dispatcher.stop()


def test_an_occurrence_is_claimed_and_delivered_once_and_each_delivery_is_authorized_again(host):
    created = broadcast(host, owner_ctx(host), [host.colleague, host.other])
    dispatcher = BroadcastDispatcher(host.runtime, host.runtime.broadcasts)
    drain(dispatcher)
    drain(BroadcastDispatcher(host.runtime, host.runtime.broadcasts))      # a second dispatcher claims nothing more
    done = deliveries(host, created["id"])
    assert {key: (row["occurrence"], row["state"], row["business_id"]) for key, row in done.items()} == {
        host.colleague: (1, "delivered", host.first), host.other: (1, "delivered", host.first)}
    assert counts(host) == {"messages": 2, "events": 2, "deliveries": 2}
    message = rows(host, "SELECT * FROM agent_messages WHERE id=?", done[host.colleague]["message_id"])[0]
    assert (message["sender_id"], message["recipient_id"], message["reply_to"], message["thread_id"], message["business_id"]) == (
        host.instance.owner, host.colleague, None, message["id"], host.first)
    assert message["current_sender_id"] == host.instance.owner and message["current_recipient_id"] == host.colleague
    # A recipient that has left scope by the time of delivery is denied, and the denial is final.
    later = broadcast(host, owner_ctx(host), [host.colleague])
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET status='retired' WHERE agent_id=?", (host.colleague,))
    drain(dispatcher)
    (only,) = deliveries(host, later["id"]).values()
    assert only["state"] == "denied" and only["message_id"] is None and counts(host)["messages"] == 2
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET status='active' WHERE agent_id=?", (host.colleague,))
    drain(dispatcher)
    assert list(deliveries(host, later["id"]).values()) == [only] and counts(host)["messages"] == 2


def test_the_engine_calls_the_host_in_order_and_input_checks_protection_at_dispatch_time(host):
    created = broadcast(host, owner_ctx(host), [host.colleague], delivery="input")
    events, application = [], host.runtime.application

    class Admission:
        def writer(self):
            events.append("writer")
            return _Context(events)

    class _Context:
        def __init__(self, events):
            self.events = events

        def __enter__(self):
            self.events.append("admitted")

        def __exit__(self, *exc):
            self.events.append("released")

    class Application:
        writer_admission = Admission()

        def __getattr__(self, name):
            return getattr(application, name)

    def protected(uow, principal_id, principal_kind, agent_ids):
        events.append(("protected", principal_id, principal_kind, agent_ids))

    def input_existing(business_id, recipient, message, *, key):
        state = rows(host, "SELECT state FROM broadcast_deliveries WHERE broadcast_id=?", created["id"])[0]["state"]
        events.append(("input", business_id, recipient, message, key, state))
        return {"state": "delivered", "detail": "typed"}

    engine = BroadcastDispatch(Application(), host.database, host.runtime.broadcasts, require_authority=lambda: events.append("authority"),
        require_protected_control=protected, input_existing=input_existing, stopping=lambda: False, clock=lambda: LATER)
    assert engine.tick() == created["id"]
    assert events == ["writer", "admitted", "authority", ("protected", host.instance.owner, "human", [host.colleague]),
        ("input", None, host.colleague, "Weekly note", f"broadcast:{created['id']}:1:{host.colleague}", "uncertain"), "released"]
    (done,) = deliveries(host, created["id"]).values()
    assert (done["state"], done["detail"], done["finished_at"]) == ("delivered", "typed", "2031-01-01T00:00:00Z")
    # An inbox delivery needs no protected-control check and no terminal call.
    events.clear()
    inbox = broadcast(host, owner_ctx(host), [host.colleague])
    assert engine.tick() == inbox["id"] and not any(isinstance(event, tuple) for event in events)


def test_a_stop_request_leaves_the_remaining_deliveries_pending_and_a_protected_recipient_is_denied(host):
    created = broadcast(host, owner_ctx(host), [host.colleague, host.other])
    engine = BroadcastDispatch(host.runtime.application, host.database, host.runtime.broadcasts, require_authority=lambda: None,
        require_protected_control=lambda *a: None, input_existing=lambda *a, **k: {}, stopping=lambda: True)
    assert engine.tick() == created["id"]
    assert {row["state"] for row in deliveries(host, created["id"]).values()} == {"pending"}
    assert counts(host)["messages"] == 0
    # A protected input recipient is denied when the delivery is made, before any terminal write.
    guarded = broadcast(host, owner_ctx(host), [host.other], delivery="input")
    terminals = Terminals()

    def deny(uow, principal_id, principal_kind, agent_ids):
        raise DomainError("forbidden", "Only the installation owner may perform this Dev control operation")

    engine = BroadcastDispatch(host.runtime.application, host.database, host.runtime.broadcasts, require_authority=lambda: None,
        require_protected_control=deny, input_existing=terminals.input_existing, stopping=lambda: False)
    while engine.tick() is not None:
        pass
    (only,) = deliveries(host, guarded["id"]).values()
    assert (only["state"], only["detail"]) == ("denied", "Only the installation owner may perform this Dev control operation")
    assert terminals.calls == []


def test_an_input_delivery_is_marked_uncertain_before_the_write_and_is_not_replayed_after_a_restart(host):
    created = broadcast(host, owner_ctx(host), [host.colleague], delivery="input")
    witnessed = []

    def dies(terminals, agent_id):
        witnessed.append(deliveries(host, created["id"])[agent_id]["state"])    # a separate connection sees only committed state
        raise Crash()

    terminals = Terminals(dies)
    dispatcher = BroadcastDispatcher(host.runtime, host.runtime.broadcasts, terminals)
    with pytest.raises(Crash):
        dispatcher.tick()
    assert witnessed == ["uncertain"] and len(terminals.calls) == 1
    assert terminals.calls[0] == (None, host.colleague, "Weekly note", f"broadcast:{created['id']}:1:{host.colleague}")
    # Restart: a new dispatcher over the same store finds nothing pending, writes nothing and leaves the row uncertain.
    restarted = Terminals()
    again = BroadcastDispatcher(host.runtime, host.runtime.broadcasts, restarted)
    drain(again)
    assert restarted.calls == [] and len(terminals.calls) == 1
    (only,) = deliveries(host, created["id"]).values()
    assert only["state"] == "uncertain" and only["finished_at"] and only["detail"] == "Terminal write outcome is not confirmed"
    assert counts(host)["messages"] == 0
    # Each outcome the terminal reports is recorded once, in a second transaction that completes only an uncertain row.
    outcomes = {"unavailable": {"state": "unavailable", "detail": "No live terminal"}, "failed": {"state": "failed", "detail": "x"},
                "odd": {"state": "mystery"}}
    for name, outcome in outcomes.items():
        made = broadcast(host, owner_ctx(host), [host.other], delivery="input")
        drain(BroadcastDispatcher(host.runtime, host.runtime.broadcasts, Terminals(lambda t, a, o=outcome: o)))
        (row,) = deliveries(host, made["id"]).values()
        assert row["state"] == ("uncertain" if name == "odd" else name), name
    # With no terminal backend at all, the delivery is recorded unavailable.
    missing = broadcast(host, owner_ctx(host), [host.other], delivery="input")
    drain(BroadcastDispatcher(host.runtime, host.runtime.broadcasts, None))
    (row,) = deliveries(host, missing["id"]).values()
    assert (row["state"], row["detail"]) == ("unavailable", "Terminal backend is unavailable")
