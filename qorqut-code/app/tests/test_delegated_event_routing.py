"""Focused parent-inbox ownership and generation-fenced helper routing tests."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest

from bazarlyk.agents.operational_delegation import OperationalDelegation, OperationalDelegationOperations
from qorqut_operations.contracts import ArtifactRef, DomainError, InvocationContext, Principal, new_id
from bazarlyk.inbox.service import InboxService
from bazarlyk.transport.auth import Authentication
from test_operational_delegation_store import setup_store


def add_message_event(uow, parent_id, business_id, *, thread_id, assignment_id=None):
    peer_id, message_id = new_id(), new_id()
    now = "2026-09-21T12:00:00.000000Z"
    uow.repository("principals").insert({"id": peer_id, "kind": "agent", "display_name": "Peer",
        "active": 1, "created_at": now, "updated_at": now})
    if not uow.connection.execute("SELECT 1 FROM agent_messages WHERE id=?", (thread_id,)).fetchone():
        root_event_id = uow.connection.execute(
            "INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (business_id, "agent_messages", thread_id, parent_id,
                "direct_message", "Approved coordination thread", now)).lastrowid
        uow.connection.execute(
            "INSERT INTO agent_messages(id,event_id,business_id,sender_id,recipient_id,reply_to,thread_id,body,created_at,"
            "current_sender_id,current_recipient_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (thread_id, root_event_id, business_id,
                parent_id, peer_id, None, thread_id, "Please reply within this approved thread.", now, parent_id, peer_id))
    event_id = uow.connection.execute(
        "INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) "
        "VALUES(?,?,?,?,?,?,?)", (business_id, "agent_messages", message_id, peer_id,
            "direct_message", "Reply in the assigned thread", now)).lastrowid
    uow.connection.execute(
        "INSERT INTO agent_messages(id,event_id,business_id,sender_id,recipient_id,reply_to,thread_id,body,created_at,"
        "delegation_assignment_id,delegation_generation,delegation_plan_revision,current_sender_id,current_recipient_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (message_id, event_id, business_id, peer_id, parent_id, thread_id, thread_id,
         "The approved coordination reply", now, assignment_id, 1 if assignment_id else None,
         1 if assignment_id else None, peer_id, parent_id))
    uow.connection.execute("INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)", (parent_id, event_id))
    return event_id, message_id


def assignment(store, uow, parent_id, business_id, thread_id):
    return store.create(uow, parent_principal_id=parent_id, business_id=business_id,
        scope={"kind": "business", "business_id": business_id, "record_ids": {}, "source_ids": {}},
        objective="Continue one explicitly linked internal conversation",
        plan={"steps": ["read reply", "continue approved plan"]},
        allowed_operations=["agents_messages_list", "agents_message_send"],
        links={"message_threads": [thread_id]}, internal_recipients=[new_id()])


def test_claim_settlement_owns_one_parent_delivery_until_outcome_is_saved(tmp_path):
    database, store, parent_id, business_id, _ = setup_store(tmp_path)
    store.clock = lambda: "2026-09-21T12:00:00.000000Z"
    thread_id = new_id()
    with database.transaction() as uow:
        saved = assignment(store, uow, parent_id, business_id, thread_id)
        event_id, _ = add_message_event(uow, parent_id, business_id, thread_id=thread_id)
        running = store.claim(uow, saved["id"], actor_principal_id=parent_id)
        claimed = store.claim_events(uow, saved["id"], actor_principal_id=parent_id,
            generation=running["execution_generation"], lease_id=running["lease_id"], readable=lambda _row: True)
        assert [row["id"] for row in claimed] == [event_id]
        assert [row["id"] for row in store.claim_events(uow, saved["id"], actor_principal_id=parent_id,
            generation=running["execution_generation"], lease_id=running["lease_id"], readable=lambda _row: True)] == [event_id]
        assert uow.connection.execute("SELECT acknowledged_at FROM inbox_deliveries WHERE event_id=?",
            (event_id,)).fetchone()[0] is None
        with pytest.raises(DomainError, match="Settle every claimed"):
            store.checkpoint(uow, saved["id"], actor_principal_id=parent_id,
                generation=running["execution_generation"], lease_id=running["lease_id"],
                expected_revision=running["revision"], checkpoint={"reply_read": True},
                state="completed", result={"summary": "Done"})

        settled = store.settle_event(uow, saved["id"], event_id, actor_principal_id=parent_id,
            generation=running["execution_generation"], lease_id=running["lease_id"],
            outcome="Reply reviewed; approved next step completed.")
        delivery = uow.connection.execute("SELECT acknowledged_at,outcome FROM inbox_deliveries WHERE event_id=?",
            (event_id,)).fetchone()
        assert settled["state"] == "handled"
        assert delivery["acknowledged_at"] == settled["handled_at"]
        assert delivery["outcome"] == settled["outcome"]


def test_parent_cannot_ack_live_claim_but_can_ack_after_assignment_stop(tmp_path):
    database, store, parent_id, business_id, _ = setup_store(tmp_path)
    store.clock = lambda: "2026-09-21T12:00:00.000000Z"
    thread_id, consumer_id = new_id(), new_id()
    with database.transaction() as uow:
        saved = assignment(store, uow, parent_id, business_id, thread_id)
        event_id, _ = add_message_event(uow, parent_id, business_id, thread_id=thread_id)
        running = store.claim(uow, saved["id"], actor_principal_id=parent_id)
        store.claim_events(uow, saved["id"], actor_principal_id=parent_id,
            generation=running["execution_generation"], lease_id=running["lease_id"], readable=lambda _row: True)
        uow.connection.execute("INSERT INTO inbox_watchers(agent_id,consumer_id,last_check_at,lease_until) VALUES(?,?,?,?)",
            (parent_id, consumer_id, store.clock(), "2026-09-21T12:15:00.000000Z"))

    class App:
        @staticmethod
        def current_context(context, _uow):
            return context

    context = InvocationContext(Principal(parent_id, "agent", "Astra"), business_id=business_id,
        allowed_business_ids=frozenset({business_id}))
    inbox = InboxService(App(), None, clock=lambda: datetime(2026, 9, 21, 12, tzinfo=timezone.utc))
    with database.transaction() as uow:
        with pytest.raises(DomainError, match="live delegated assignment owns"):
            inbox.validate_mutation("inbox_acknowledge", context,
                {"consumer_id": consumer_id, "event_ids": [event_id]}, uow)
        stopped = store.control(uow, saved["id"], parent_principal_id=parent_id,
            expected_revision=running["revision"], action="cancel")
        assert stopped["state"] == "cancelled"
        with pytest.raises(DomainError, match="no longer current"):
            store.settle_event(uow, saved["id"], event_id, actor_principal_id=parent_id,
                generation=running["execution_generation"], lease_id=running["lease_id"],
                outcome="A stale helper cannot settle this event.")
        assert inbox.validate_mutation("inbox_acknowledge", context,
            {"consumer_id": consumer_id, "event_ids": [event_id]}, uow) == context


def test_business_match_is_not_routing_and_explicit_message_attribution_wins(tmp_path):
    database, store, parent_id, business_id, _ = setup_store(tmp_path)
    store.clock = lambda: "2026-09-21T12:00:00.000000Z"
    thread_id = new_id()
    with database.transaction() as uow:
        first = assignment(store, uow, parent_id, business_id, thread_id)
        second = assignment(store, uow, parent_id, business_id, thread_id)
        unlinked_event_id, _ = add_message_event(uow, parent_id, business_id, thread_id=new_id())
        unlinked_event = uow.connection.execute("SELECT * FROM inbox_events WHERE id=?", (unlinked_event_id,)).fetchone()
        assert OperationalDelegation.routed_assignments(uow, parent_id, unlinked_event) == []

        ambiguous_event_id, _ = add_message_event(uow, parent_id, business_id, thread_id=thread_id)
        ambiguous_event = uow.connection.execute("SELECT * FROM inbox_events WHERE id=?", (ambiguous_event_id,)).fetchone()
        assert len(OperationalDelegation.routed_assignments(uow, parent_id, ambiguous_event)) == 2

        # Trusted worker attribution is more specific than the now-ambiguous
        # thread link. Caller-authored text cannot manufacture this DB field.
        attributed_id, _ = add_message_event(uow, parent_id, business_id,
            thread_id=thread_id, assignment_id=second["id"])
        attributed = uow.connection.execute("SELECT * FROM inbox_events WHERE id=?", (attributed_id,)).fetchone()
        matches = OperationalDelegation.routed_assignments(uow, parent_id, attributed)
        assert [row["id"] for row in matches] == [second["id"]]
        assert first["id"] != second["id"]


def test_canonical_source_routing_requires_an_explicit_link(tmp_path):
    database, store, parent_id, business_id, _ = setup_store(tmp_path)
    task_id, unrelated_id = new_id(), new_id()
    with database.transaction() as uow:
        linked = store.create(uow, parent_principal_id=parent_id, business_id=business_id,
            scope={"kind": "business", "business_id": business_id,
                   "record_ids": {}, "source_ids": {}},
            objective="Continue one task-bound assignment", plan={"steps": ["review task update"]},
            allowed_operations=["tasks_get"], links={"tasks": [task_id]})
        linked_event_id = uow.connection.execute(
            "INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (business_id, "tasks", task_id, parent_id,
                "updated", "Linked task changed", store.clock())).lastrowid
        unrelated_event_id = uow.connection.execute(
            "INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (business_id, "tasks", unrelated_id, parent_id,
                "updated", "Another task changed", store.clock())).lastrowid
        linked_event = uow.connection.execute("SELECT * FROM inbox_events WHERE id=?", (linked_event_id,)).fetchone()
        unrelated_event = uow.connection.execute("SELECT * FROM inbox_events WHERE id=?", (unrelated_event_id,)).fetchone()
        assert [row["id"] for row in OperationalDelegation.routed_assignments(uow, parent_id, linked_event)] == [linked["id"]]
        assert OperationalDelegation.routed_assignments(uow, parent_id, unrelated_event) == []


def test_local_dispatch_claim_issues_a_generation_scoped_worker_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    database, store, parent_id, business_id, _ = setup_store(tmp_path)
    now = datetime.now(timezone.utc)
    store.clock = lambda: now.isoformat(timespec="microseconds").replace("+00:00", "Z")
    registration_id, consumer_id = new_id(), new_id()
    workspace = tmp_path / "parent"
    workspace.mkdir()
    with database.transaction() as uow:
        uow.repository("machine_registrations").insert({"id": registration_id,
            "principal_id": parent_id, "workspace": str(workspace), "service_url": "http://127.0.0.1:9000",
            "profile_revision": 1, "compatibility_json": {"durable_delegation_enabled": True},
            "created_at": store.clock(), "updated_at": store.clock()})
        saved = assignment(store, uow, parent_id, business_id, new_id())
        uow.connection.execute("INSERT INTO inbox_watchers(agent_id,consumer_id,last_check_at,lease_until) VALUES(?,?,?,?)",
            (parent_id, consumer_id, store.clock(),
             (now + timedelta(minutes=15)).isoformat(timespec="microseconds").replace("+00:00", "Z")))

    app = SimpleNamespace(database=database, current_context=lambda context, _uow: context)
    inbox = InboxService(app, None, clock=lambda: now)
    operations = OperationalDelegationOperations(store)
    operations.application, operations.inbox = app, inbox
    parent_context = InvocationContext(Principal(parent_id, "agent", "Astra"), profile_revision=1,
        global_capabilities=frozenset({"manage_delegated_assignments"}),
        role_artifact_ref=ArtifactRef("ordubashy", "1.34.0", "sha256:" + "a" * 64),
        session_registration_id=registration_id)

    result = operations.claim_next_for_dispatch(parent_context, registration_id, consumer_id)
    assert result["assignment"]["id"] == saved["id"]
    assert result["assignment"]["state"] == "running"
    assert result["assignment"]["dispatch_reason"] is None
    assert result["generation"] == 1
    assert result["worker_workspace"] == str(tmp_path / "state" / "bazarlyk" / "delegated-workers" / parent_id / saved["id"])
    assert result["worker_registration_id"] != registration_id
    assert result["worker_token"]
    worker = Authentication(database, tmp_path).agent("Bearer " + result["worker_token"])
    assert worker.delegation_assignment_id == saved["id"]
    assert worker.delegation_generation == result["generation"]
    assert worker.registration_id == result["worker_registration_id"]

    with pytest.raises(DomainError, match="Another verified session owns this inbox"):
        with database.transaction() as uow:
            inbox.session_owner(parent_context, new_id(), uow)
