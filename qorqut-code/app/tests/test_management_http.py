"""Transport-level coverage for the owner management surface.

These tests deliberately create the coordinator through the normal agent
operation and send messages/input through MCP.  Only the installation row is
configured by the test; canonical messages, routes, and inbox deliveries are
created by the application itself.
"""

from pathlib import Path

from starlette.testclient import TestClient

from qorqut_operations.contracts import new_id
from test_service_transports import ORIGIN, ServiceFixture
from test_signoff_inputs import FORM


def root_operation(client, name, arguments):
    response = client.post(f"/api/root/operations/{name}", headers=ORIGIN, json=arguments)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get("status") == "ok", body
    return body["data"]


def root_request(client, name, arguments):
    response = client.post(f"/api/root/operations/{name}", headers=ORIGIN, json=arguments)
    # A registered operation may return HTTP 404 for a missing record.  The
    # endpoint itself must still return the structured operation error.
    assert response.headers.get("content-type", "").startswith("application/json"), response.text
    return response


def configure_coordinator(service, client):
    coordinator = service.fixture.create("coordinator", "ordubashy", runtime="codex")
    data = root_operation(client, "management_coordinator_set", {
        "coordinator_id": coordinator.id,
        "idempotency_key": new_id(),
    })
    assert data["coordinator_id"] == coordinator.id
    return coordinator


def db_row(service, table, identity):
    with service.fixture.db.read() as uow:
        return uow.connection.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone()


def routing_for_source(service, source_type, source_id):
    with service.fixture.db.read() as uow:
        return uow.connection.execute(
            "SELECT * FROM management_routing WHERE source_type=? AND source_id=?",
            (source_type, source_id)).fetchone()


def test_owner_management_operations_and_real_message_lifecycle(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        service.login(client)
        coordinator = configure_coordinator(service, client)
        agent_headers = service.initialize(client, service.agent)

        sent = service.tool(client, agent_headers, "management_message_send", {
            "scope": "business", "message": "Need a current go-to-market decision.",
            "idempotency_key": new_id(),
        })
        assert sent["isError"] is False, sent
        message_id = sent["decoded"]["data"]["message_id"]
        routing_id = sent["decoded"]["data"]["routing"]["id"]
        message = db_row(service, "agent_messages", message_id)
        assert message["sender_id"] == service.agent.id
        assert message["recipient_id"] == coordinator.id
        with service.fixture.db.read() as uow:
            delivery = uow.connection.execute(
                "SELECT d.* FROM inbox_deliveries d JOIN inbox_events e ON e.id=d.event_id "
                "WHERE e.source_type='agent_messages' AND e.source_id=?", (message_id,)).fetchone()
        assert delivery and delivery["agent_id"] == coordinator.id

        listed = root_operation(client, "management_list", {"scope": "global", "all_items": True})
        item = next(row for row in listed["items"] if row["id"] == routing_id)
        assert item["source"]["id"] == message_id
        assert root_operation(client, "management_get", {"scope": "global", "routing_id": routing_id})["routing"]["body"] == message["body"]

        resolved = root_operation(client, "management_resolve", {
            "scope": "global", "routing_id": routing_id, "disposition": "resolved",
            "expected_revision": item["revision"], "idempotency_key": new_id(),
        })
        assert resolved["routing"]["disposition"] == "resolved"
        assert not root_operation(client, "management_list", {"scope": "global", "all_items": True})["items"]

        # The same owner operation surface also reaches the incident readers.
        assert isinstance(root_operation(client, "system_incidents_list", {"status": "all"}), dict)
        missing = root_request(client, "system_incidents_get", {"incident_id": new_id()})
        assert missing.status_code == 404
        assert missing.json()["error"]["message"] == "Incident was not found"


def test_management_allowed_input_is_delivered_only_to_coordinator(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        service.login(client)
        coordinator = configure_coordinator(service, client)
        agent_headers = service.initialize(client, service.agent)
        form = {**FORM, "response_policy": "management_allowed"}
        created = service.tool(client, agent_headers, "signoffs_input_create", {
            "scope": "business", "request_key": "management-question", "form": form,
            "idempotency_key": new_id(),
        })
        assert created["isError"] is False, created
        signoff_id = created["decoded"]["data"]["signoff"]["id"]
        route = routing_for_source(service, "signoffs", signoff_id)
        assert route and route["attention_destination"] == "management"
        with service.fixture.db.read() as uow:
            delivery = uow.connection.execute(
                "SELECT d.* FROM inbox_deliveries d JOIN inbox_events e ON e.id=d.event_id "
                "WHERE e.source_type='signoffs' AND e.source_id=?", (signoff_id,)).fetchone()
        assert delivery and delivery["agent_id"] == coordinator.id
        attention = client.get("/api/me/attention")
        assert attention.status_code == 200, attention.text
        attention_data = attention.json()["data"]
        assert signoff_id not in {item.get("id") for item in attention_data["urgent"]}
        business_count = next(row for row in attention_data["businesses"] if row["id"] == service.fixture.business_id)
        assert business_count["signoffs"] == 0
        hidden = client.get("/api/biz/widget/signoffs", params={"category": "input_request", "states": "pending"})
        assert hidden.status_code == 200, hidden.text
        assert signoff_id not in {row["id"] for row in hidden.json()["data"]["items"]}
        inspected = client.post("/api/biz/widget/operations/signoffs_list", headers=ORIGIN, json={
            "scope": "business", "category": "input_request", "states": ["pending"],
            "include_managed": True})
        assert inspected.status_code == 200, inspected.text
        assert signoff_id in {row["id"] for row in inspected.json()["data"]["items"]}


def test_real_mcp_delegated_answer_is_attributed_and_replayed(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        service.login(client)
        coordinator = configure_coordinator(service, client)
        ready = service.fixture.prepare(coordinator)
        service.tokens[coordinator.id] = (Path(ready["workspace"]) / ".bazarlyk/credential").read_text().strip()
        business_headers = service.initialize(client, service.agent)
        created = service.tool(client, business_headers, "signoffs_input_create", {
            "scope": "business", "request_key": "delegated-http-answer",
            "form": {**FORM, "response_policy": "management_allowed"},
            "idempotency_key": new_id(),
        })
        assert created["isError"] is False, created
        signoff = created["decoded"]["data"]["signoff"]
        assert routing_for_source(service, "signoffs", signoff["id"])
        coordinator_headers = service.initialize(client, coordinator)
        answer = {
            "scope": "business", "signoff_id": signoff["id"],
            "business_id": service.fixture.business_id,
            "proposal_revision": signoff["proposal_revision"],
            "payload_hash": signoff["payload_hash"],
            "expected_status_revision": signoff["status_revision"],
            "notes": "Use the first supplier", "selected_choices": ["a"],
            "idempotency_key": "delegated-http-" + signoff["id"],
        }
        with service.fixture.db.read() as uow:
            before_receipts = uow.connection.execute("SELECT COUNT(*) FROM apply_receipts").fetchone()[0]
        result = service.tool(client, coordinator_headers, "signoffs_input_answer_delegated", answer)
        assert result["isError"] is False, result
        answered = result["decoded"]["data"]["signoff"]
        assert answered["state"] == "answered"
        assert answered["decision"]["by"] == coordinator.id
        with service.fixture.db.read() as uow:
            after_receipts = uow.connection.execute("SELECT COUNT(*) FROM apply_receipts").fetchone()[0]
        assert after_receipts == before_receipts
        replay = service.tool(client, coordinator_headers, "signoffs_input_answer_delegated", answer)
        assert replay["isError"] is False, replay
        replayed = replay["decoded"]["data"]["signoff"]
        assert replayed["decision"] == answered["decision"]
        assert replayed["status_revision"] == answered["status_revision"]

        owner_only = service.tool(client, business_headers, "signoffs_input_create", {
            "scope": "business", "request_key": "delegated-http-owner-only",
            "form": {**FORM, "response_policy": "owner_only"},
            "idempotency_key": new_id(),
        })
        protected = owner_only["decoded"]["data"]["signoff"]
        denied = service.tool(client, coordinator_headers, "signoffs_input_answer_delegated", {
            **answer, "signoff_id": protected["id"],
            "proposal_revision": protected["proposal_revision"],
            "payload_hash": protected["payload_hash"],
            "expected_status_revision": protected["status_revision"],
            "idempotency_key": "delegated-http-protected-" + protected["id"],
        })
        assert denied["isError"] is True
        assert denied["decoded"]["error"]["code"] in {"forbidden", "policy_denied"}


def test_owner_only_input_has_no_management_route_or_message_resolution(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        service.login(client)
        configure_coordinator(service, client)
        agent_headers = service.initialize(client, service.agent)
        created = service.tool(client, agent_headers, "signoffs_input_create", {
            "scope": "business", "request_key": "human-question", "form": {
                **FORM, "kind": "question", "response_policy": "management_allowed",
            }, "idempotency_key": new_id(),
        })
        assert created["isError"] is False, created
        signoff_id = created["decoded"]["data"]["signoff"]["id"]
        route = routing_for_source(service, "signoffs", signoff_id)
        assert route is not None
        # A real routed input still belongs to the Signoffs lifecycle.  The
        # owner must not be able to resolve it through message routing.
        response = client.post("/api/root/operations/management_resolve", headers=ORIGIN, json={
            "scope": "global", "routing_id": route["id"], "disposition": "dismissed",
            "expected_revision": route["revision"], "idempotency_key": new_id(),
        })
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"

        owner_only = service.tool(client, agent_headers, "signoffs_input_create", {
            "scope": "business", "request_key": "human-question-only", "form": {
                **FORM, "kind": "question", "response_policy": "owner_only",
            }, "idempotency_key": new_id(),
        })
        assert owner_only["isError"] is False
        assert routing_for_source(service, "signoffs", owner_only["decoded"]["data"]["signoff"]["id"]) is None
