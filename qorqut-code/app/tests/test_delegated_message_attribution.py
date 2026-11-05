"""Focused assignment-bound message attribution and recipient checks."""

from types import SimpleNamespace

import pytest

from datetime import datetime, timezone

from qorqut_operations.contracts import ArtifactRef, DomainError, InvocationContext, Principal, new_id, utc_now
from bazarlyk.inbox.messages import Messages
from test_operational_delegation_store import setup_store
from bazarlyk.agents.operational_delegation import OperationalDelegation
from bazarlyk.application.engine import Application


def test_worker_messages_are_recipient_bound_and_replies_keep_assignment_attribution(tmp_path):
    database, store, parent_id, business_id, _ = setup_store(tmp_path)
    store.clock = utc_now
    recipient_id, other_id = new_id(), new_id()
    with database.transaction() as uow:
        for identity, name in ((recipient_id, "Onbashy"), (other_id, "Other")):
            uow.repository("principals").insert({"id": identity, "kind": "agent", "display_name": name,
                "active": 1, "created_at": store.clock(), "updated_at": store.clock()})
            uow.repository("agent_profiles", business_id=None).insert({"agent_id": identity,
                "principal_id": identity, "namespace": "ecosystem", "name": name,
                "workspace": str(tmp_path / name.lower()),
                "profile_json": {"role": {"id": "onbashy"}, "scope": {"kind": "ecosystem",
                    "allowed_business_ids": [business_id], "global_capabilities": []}},
                "status": "active", "created_at": store.clock(), "updated_at": store.clock()})
        assignment = store.create(uow, parent_principal_id=parent_id, business_id=business_id,
            scope={"kind": "business", "business_id": business_id,
                   "record_ids": {}, "source_ids": {}},
            objective="Coordinate one approved operational update", plan={"steps": ["message", "verify"]},
            allowed_operations=["agents_message_send", "agents_messages_list"],
            internal_recipients=[recipient_id])
        uow.repository("machine_registrations").insert({"id": new_id(), "principal_id": parent_id,
            "workspace": str(tmp_path / "parent"), "service_url": "http://127.0.0.1:9000",
            "profile_revision": 1, "created_at": store.clock(), "updated_at": store.clock()})
        running = store.claim(uow, assignment["id"], actor_principal_id=parent_id)
        issued = store.issue_worker_binding(uow, assignment["id"], parent_principal_id=parent_id,
            generation=running["execution_generation"], lease_id=running["lease_id"],
            expected_revision=running["revision"], workspace=str(tmp_path / "worker"),
            service_url="http://127.0.0.1:9000", expires_at=running["lease_until"],
            runtime_thread_id="thread-message-attribution")

    class TestApp:
        @staticmethod
        def current_context(context, uow):
            uow.actor_id = context.principal_id
            return context

        class policy:
            @staticmethod
            def context_for(identity, *, business_id, review_business_id, uow):
                principal = uow.repository("principals").get(identity, required=True)
                return InvocationContext(Principal(identity, principal["kind"], principal["display_name"]),
                    business_id=business_id, allowed_business_ids=frozenset({business_id}))

    inbox = SimpleNamespace(app=TestApp(), clock=lambda: datetime.now(timezone.utc))
    messages = Messages(inbox)
    worker = InvocationContext(Principal(parent_id, "agent", "Astra"), business_id=business_id,
        allowed_business_ids=frozenset({business_id}), profile_revision=1, source="mcp",
        global_capabilities=frozenset({"manage_delegated_assignments"}),
        role_artifact_ref=ArtifactRef("ordubashy", "1.34.0", "sha256:" + "a" * 64),
        session_registration_id=issued["registration_id"], delegation_binding_id=issued["binding"]["id"],
        delegation_assignment_id=assignment["id"], delegation_generation=running["execution_generation"])
    peer = InvocationContext(Principal(recipient_id, "agent", "Onbashy"), business_id=business_id,
        allowed_business_ids=frozenset({business_id}))

    with database.transaction() as uow:
        args = {"to_agent_id": recipient_id, "message": "Please check the approved supplier update."}
        worker_row = messages.apply(worker, SimpleNamespace(arguments=args), uow)["message"]
        assert worker_row["sender_id"] == parent_id
        assert worker_row["delegation_attribution"] == {
            "assignment_id": assignment["id"], "generation": running["execution_generation"],
            "binding_id": issued["binding"]["id"], "plan_revision": assignment["plan_revision"],
            "worker_authored": True,
        }
        with pytest.raises(DomainError, match="recipient is outside"):
            messages.validate(worker, {"to_agent_id": other_id, "message": "Out of scope"}, uow)

    with database.transaction() as uow:
        reply = messages.apply(peer, SimpleNamespace(arguments={"reply_to": worker_row["id"],
            "message": "Checked; source record is current."}), uow)["message"]
        assert reply["thread_id"] == worker_row["thread_id"]
        assert reply["delegation_attribution"]["assignment_id"] == assignment["id"]
        assert reply["delegation_attribution"]["worker_authored"] is False
        history = messages.history(worker, {"thread_id": worker_row["thread_id"]}, None, uow)
        assert [row["id"] for row in history["items"]] == [worker_row["id"], reply["id"]]
        assert Application.authorize_delegation(worker, "agents_messages_list", (), uow,
            namespace="business:" + business_id, arguments={"thread_id": worker_row["thread_id"]})["id"] == assignment["id"]
        with pytest.raises(DomainError, match="in-boundary record selector"):
            Application.authorize_delegation(worker, "agents_messages_list", (), uow,
                namespace="business:" + business_id, arguments={"thread_id": new_id()})
