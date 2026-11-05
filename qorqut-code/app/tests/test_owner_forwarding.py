"""Ordubashy's private notice of an accepted owner message to a worker: the writer, its storage and its privacy.

Real: the stock host assembly and database, the registered Ordubashy, the owner's ``management_coordinator_set``, the
generated schema (including the widened ``agent_messages.body`` limit), the inbox projection and ``agents_messages_list``.
Synthetic: the tmp instance and its principals. The writer is called directly with a named subject principal; whether a
recipient is an active worker (``agents.worker_access.is_worker``) is not exercised here, see
``test_owner_message_forwarding_workers.py``. No model, native session, provider or network is used.
"""

from __future__ import annotations

import uuid

import pytest

from qorqut_operations.contracts import DomainError, InvocationOptions, Principal, new_id

from bazarlyk.inbox.owner_forwarding import (NAME_LIMIT, NOTICE_SUMMARY, WRAPPER_LIMIT, forward_owner_message, forwarded_id,
                                             notice_body, write_notice)
from host_foundation_support import (build_instance, create_registered_global, create_worker, owner_global_context, register_sources,
                                     select_coordinator)

ACCEPTED = "2026-10-15T10:00:00.000000Z"
TOKEN = "private-owner-line-" + "q" * 20


class Host:
    def __init__(self, instance, ordubashy, subject):
        self.instance, self.database, self.runtime = instance, instance.database, instance.runtime
        self.owner, self.ordubashy, self.subject = instance.owner, ordubashy, subject

    def write(self, body=TOKEN, *, kind="chat", source_id="input-1", sender=None):
        with self.database.transaction() as uow:
            return write_notice(uow.connection, kind=kind, source_id=source_id, sender_id=sender or self.owner,
                                manager_id=self.ordubashy, worker_id=self.subject, body=body, accepted_at=ACCEPTED)

    def rows(self, sql, *parameters):
        with self.database.read() as uow:
            return [dict(row) for row in uow.connection.execute(sql, parameters)]

    def counts(self):
        """Message rows, their events and the deliveries of those events (other inbox sources are not this feature's)."""
        return {"agent_messages": self.rows("SELECT count(*) AS n FROM agent_messages")[0]["n"],
                "inbox_events": self.rows("SELECT count(*) AS n FROM inbox_events WHERE source_type='agent_messages'")[0]["n"],
                "inbox_deliveries": self.rows("SELECT count(*) AS n FROM inbox_deliveries d JOIN inbox_events e ON e.id=d.event_id "
                                              "WHERE e.source_type='agent_messages'")[0]["n"]}

    def agent(self, identity):
        return self.runtime.resolver.invocation_context(Principal(identity, "agent"))

    def pending(self, identity):
        with self.database.read() as uow:
            return self.runtime.inbox.pending(self.runtime.application.current_context(self.agent(identity), uow), uow)["items"]

    def tables_holding(self, token):
        """Every table with a stored value containing ``token``: where a private text actually lives."""
        found = set()
        with self.database.read() as uow:
            for table in [r[0] for r in uow.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
                if any(token in str(value) for row in uow.connection.execute(f'SELECT * FROM "{table}"') for value in tuple(row)):
                    found.add(table)
        return found


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path)
    register_sources(instance, ids["first"], "first")
    ordubashy = select_coordinator(instance, create_registered_global(instance, "ordubashy"))
    subject = create_worker(instance, "colleague")  # a named principal; eligibility is not under test here
    instance.runtime.freeze_registry()
    try:
        yield Host(instance, ordubashy, subject)
    finally:
        try:
            instance.runtime.release_instance()
        finally:
            instance.database.close()


def test_the_notice_is_one_private_row_with_the_exact_complete_body_and_a_fixed_summary(host):
    lead = "  First line\n\nsecond line with trailing space  \n"
    body = lead + "é" * (20000 - len(lead))
    assert len(body) == 20000
    identity = host.write(body, source_id="input-9")
    [message] = host.rows("SELECT * FROM agent_messages")
    assert (message["id"], message["thread_id"], message["business_id"], message["reply_to"]) == (identity, identity, None, None)
    assert (message["sender_id"], message["current_sender_id"]) == (host.owner, host.owner)
    assert (message["recipient_id"], message["current_recipient_id"]) == (host.ordubashy, host.ordubashy)
    # The owner's text is the exact tail: never cut, stripped or re-encoded.
    assert message["body"].endswith("\nOwner message:\n" + body)
    header = message["body"][:-len(body)]
    assert len(header) <= WRAPPER_LIMIT and len(message["body"]) <= 24000
    assert host.subject in header and "input-9" in header and ACCEPTED in header
    assert "not a second assignment" in header and "Forwarded notice" in header
    [event] = host.rows("SELECT * FROM inbox_events WHERE source_type='agent_messages'")
    assert (event["source_type"], event["source_id"], event["event_type"], event["actor_id"], event["business_id"]) == (
        "agent_messages", identity, "direct_message", host.owner, None)
    assert event["summary"] == NOTICE_SUMMARY and "First line" not in event["summary"]
    assert message["event_id"] == event["id"]
    assert [(r["agent_id"], r["event_id"], r["acknowledged_at"]) for r in host.rows(
        "SELECT * FROM inbox_deliveries WHERE event_id=?", event["id"])] == [(host.ordubashy, event["id"], None)]


def test_the_identity_derives_from_source_kind_original_id_and_manager_so_a_retry_adds_nothing(host):
    assert forwarded_id("chat", "input-1", "manager-1") == "1659168a-5103-5cdc-aa61-3d25e2e39329"
    assert len({forwarded_id("chat", "a", "m"), forwarded_id("agent_message", "a", "m"),
                forwarded_id("chat", "b", "m"), forwarded_id("chat", "a", "n")}) == 4
    first = host.write(source_id="input-1")
    again = host.write(source_id="input-1")
    assert first == again == forwarded_id("chat", "input-1", host.ordubashy)
    assert host.counts() == {"agent_messages": 1, "inbox_events": 1, "inbox_deliveries": 1}
    other = host.write(source_id="input-2")
    assert other != first and host.counts() == {"agent_messages": 2, "inbox_events": 2, "inbox_deliveries": 2}


def test_the_stored_limit_holds_the_largest_accepted_owner_message_and_a_refusal_leaves_nothing(host):
    name = host.rows("SELECT display_name FROM principals WHERE id=?", host.subject)[0]["display_name"]
    wrapper = len(notice_body("agent_message", "m" * 36, host.subject, name, ACCEPTED, ""))
    host.write("x" * (24000 - wrapper), kind="agent_message", source_id="m" * 36)       # the exact boundary fits
    assert host.rows("SELECT length(body) AS n FROM agent_messages")[0]["n"] == 24000
    before = host.counts()
    with pytest.raises(DomainError) as refused:
        host.write("x" * (24001 - wrapper), kind="agent_message", source_id="n" * 36)
    assert refused.value.code == "validation_error"
    # The event inserted before the refused row went with its transaction.
    assert host.counts() == before


def test_only_the_display_label_is_cut_when_it_is_excessive(host):
    body = "kept whole " * 1500
    text = notice_body("agent_message", str(uuid.uuid4()), host.subject, "Name" * 5000, ACCEPTED, body)
    assert text.endswith("\nOwner message:\n" + body) and len(text) - len(body) <= WRAPPER_LIMIT
    assert "Name" * (NAME_LIMIT // 4) in text and "Name" * (NAME_LIMIT // 4 + 1) not in text


def test_a_rolled_back_acceptance_leaves_no_notice(host):
    class Refused(Exception):
        pass

    with pytest.raises(Refused):
        with host.database.transaction() as uow:
            write_notice(uow.connection, kind="chat", source_id="input-1", sender_id=host.owner, manager_id=host.ordubashy,
                         worker_id=host.subject, body=TOKEN, accepted_at=ACCEPTED)
            raise Refused()
    assert host.counts() == {"agent_messages": 0, "inbox_events": 0, "inbox_deliveries": 0}
    host.write()
    assert host.counts() == {"agent_messages": 1, "inbox_events": 1, "inbox_deliveries": 1}


def test_only_ordubashy_reads_the_notice_and_its_text_lives_in_no_other_store(host):
    host.write(source_id="input-1")
    [item] = host.pending(host.ordubashy)
    assert item["source_type"] == "agent_messages" and item["summary"] == NOTICE_SUMMARY and TOKEN not in item["summary"]
    assert item["message"]["body"].endswith("\nOwner message:\n" + TOKEN)
    assert item["message"]["sender_name"] == "Owner"
    assert host.pending(host.subject) == []
    listed = host.runtime.application.invoke(host.agent(host.ordubashy), "agents_messages_list", {"scope": "global"},
                                             InvocationOptions(idempotency_key=new_id()))
    assert listed.status == "ok" and [m["body"] for m in listed.data["items"]] == [item["message"]["body"]]
    named = host.runtime.application.invoke(host.agent(host.subject), "agents_messages_list", {"scope": "global"},
                                            InvocationOptions(idempotency_key=new_id()))
    assert named.status == "ok" and list(named.data["items"]) == []
    # Nothing else stores it: no event summary, Task thread, status, Context or exported table holds the text.
    assert host.tables_holding(TOKEN) == {"agent_messages"}


def test_the_notice_is_never_part_of_operational_history(host):
    from bazarlyk.operations_history.catalog import catalog
    from qorqut_operations.inbox.history import ITEM_SOURCES
    from qorqut_operations.history_exports import PRIVATE_EXCLUDED

    exports = catalog()
    assert "agent_messages" not in exports.exports and "agent_messages" not in ITEM_SOURCES
    assert exports.classifications["agent_messages"][1].kind == PRIVATE_EXCLUDED


def test_agent_authored_messages_and_a_missing_coordinator_never_reach_the_worker_check(host):
    # The owner gate runs before the worker check, so these refuse without consulting ``is_worker``.
    for sender in (host.ordubashy, host.subject, new_id()):
        with host.database.transaction() as uow:
            assert forward_owner_message(uow, kind="agent_message", source_id="m-1", sender_id=sender, worker_id=host.subject,
                                         body=TOKEN, accepted_at=ACCEPTED) is None
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE installation SET management_coordinator_id=NULL WHERE id='service'")
    with host.database.transaction() as uow:
        assert forward_owner_message(uow, kind="chat", source_id="m-2", sender_id=host.owner, worker_id=host.subject,
                                     body=TOKEN, accepted_at=ACCEPTED) is None
    assert host.counts() == {"agent_messages": 0, "inbox_events": 0, "inbox_deliveries": 0}


def test_a_worker_role_selector_never_selects_ordubashy_s_conversation(host):
    # ``global-worker`` is not a peer role of the one Ordubashy-only channel: it is refused as invalid input, and the service's own
    # participant check refuses it too. This needs no worker.
    owner = owner_global_context(host.instance)
    created = host.runtime.application.invoke(owner, "tasks_create", {"title": "Private work", "scope": "global"},
                                              InvocationOptions(idempotency_key=new_id()))
    assert created.status == "ok", created.error
    source = {"type": "task", "id": created.data["task"]["id"], "business_id": None}

    def call(name, **arguments):
        return host.runtime.application.invoke(owner, name, {"source": source, "scope": "global", **arguments},
                                               InvocationOptions(idempotency_key=new_id()))

    assert call("owner_conversation_get", peer_role="ordubashy", agent_id=host.ordubashy).status == "ok"    # the matching pair opens
    for name, extra in (("owner_conversation_get", {}), ("owner_conversation_send", {"text": TOKEN})):
        mismatched = call(name, peer_role="global-worker", agent_id=host.ordubashy, **extra)
        assert mismatched.status == "error" and mismatched.error["code"] == "validation_error", (name, mismatched.status, mismatched.error)
    with host.database.read() as uow, pytest.raises(DomainError) as direct:
        host.runtime.application.private_conversations.participants(uow, None, {"peer_role": "global-worker", "agent_id": host.ordubashy})
    assert direct.value.code == "forbidden"
    # No channel opened: nothing stored, nothing delivered, no notice.
    assert host.rows("SELECT count(*) AS n FROM owner_conversation_messages")[0]["n"] == 0
    assert host.counts() == {"agent_messages": 0, "inbox_events": 0, "inbox_deliveries": 0}
