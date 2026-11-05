from starlette.testclient import TestClient

from qorqut_operations.contracts import new_id
from bazarlyk.agents.context import MAX_CONTEXT_CHARACTERS
from bazarlyk.transport.auth import COOKIE_NAME
from test_service_transports import ORIGIN, ServiceFixture


def test_bootstrap_receipt_prompt_cache_and_external_context_drift(tmp_path):
    service = ServiceFixture(tmp_path)
    headers = {"Authorization": "Bearer " + service.tokens[service.agent.id]}
    body = {"registration_id": service.registrations[service.agent.id], "session_id": "owner-test-session", "event": "startup"}
    with TestClient(service.web) as client:
        initial = client.post("/api/agent/bootstrap", json=body, headers=headers)
        assert initial.status_code == 200, initial.text
        data = initial.json()["data"]
        assert data["ready"] is True
        assert len(data["mandatory_context"]["rendered"]) <= MAX_CONTEXT_CHARACTERS
        receipt = {**body, "effective_config_id": data["effective_config_id"], "bundle_hash": data["mandatory_context"]["bundle_hash"],
            "emitted_at": "2026-09-08T12:00:00+00:00", "briefing_hash": "sha256:" + "a" * 64}
        emitted = client.post("/api/agent/bootstrap/receipt", json=receipt, headers=headers)
        assert emitted.status_code == 200, emitted.text
        assert emitted.json()["data"]["observation"] == "hook_emitted"
        prompt = {**body, "event": "prompt", "last_effective_config_id": data["effective_config_id"]}
        cached = client.post("/api/agent/bootstrap", json=prompt, headers=headers).json()["data"]
        assert cached["refresh_required"] is False
        assert "mandatory_context" not in cached
        path = service.fixture.hq / "context/product.md"
        original = path.read_text()
        path.write_text(original + "\nOwner's new product fact.\n")
        refreshed = client.post("/api/agent/bootstrap", json=prompt, headers=headers)
        assert refreshed.status_code == 409, refreshed.text
        assert refreshed.json()["error"]["code"] == "document_conflict"
        path.write_text(original)
        service.fixture.update(service.agent, {"assignment_brief": "New explicit assignment."})
        refreshed = client.post("/api/agent/bootstrap", json=prompt, headers=headers)
        assert refreshed.status_code == 200, refreshed.text
        current = refreshed.json()["data"]
        assert current["refresh_required"] is True
        assert "New explicit assignment." in current["mandatory_context"]["rendered"]
        assert current["effective_config_id"] != data["effective_config_id"]
        stale = client.post("/api/agent/bootstrap/receipt", json=receipt, headers=headers)
        assert stale.status_code == 422
        assert stale.json()["error"]["code"] == "stale_delivery"
        cross = client.post("/api/agent/bootstrap", json={**body, "registration_id": service.registrations[service.manager.id]}, headers=headers)
        assert cross.status_code == 403


def test_dashboard_ignores_old_cookies_but_checks_current_owner_membership_and_strict_json(tmp_path):
    service = ServiceFixture(tmp_path)
    with TestClient(service.web) as client:
        service.login(client)
        for content in ('{"title":"first","title":"second"}', '{"title":"x","value":NaN}', '{"title":"x","nested":{"a":1,"a":2}}'):
            bad = client.post("/api/biz/widget/operations/tasks_create", content=content, headers={**ORIGIN, "Content-Type": "application/json"})
            assert bad.status_code == 400, bad.text
        client.cookies.clear()
        client.cookies.set(COOKIE_NAME, "expired-or-invalid-old-session")
        assert client.get("/api/session").status_code == 200
        client.cookies.clear()
        with service.fixture.db.transaction() as uow:
            membership = uow.repository("business_memberships", business_id=service.fixture.business_id).get(service.fixture.owner.id)
            uow.repository("business_memberships", business_id=service.fixture.business_id).update(service.fixture.owner.id,
                {"is_owner": False}, expected_revision=membership["revision"])
        assert client.get("/api/session").json()["data"]["is_installation_owner"] is True
        assert client.post("/api/biz/widget/operations/tasks_create", json={"title": "No owner"}, headers=ORIGIN).status_code == 403
        with service.fixture.db.transaction() as uow:
            install = uow.repository("installation").get("service")
            owner = new_id()
            uow.repository("principals").insert({"id": owner, "kind": "human", "display_name": "New owner"})
            uow.repository("installation").update("service", {"owner_principal_id": owner}, expected_revision=install["revision"])
        assert client.get("/api/session").json()["data"]["principal_id"] == owner
        with service.fixture.db.transaction() as uow:
            principal = uow.repository("principals").get(owner)
            uow.repository("principals").update(owner, {"active": False}, expected_revision=principal["revision"])
        assert client.get("/api/session").status_code == 401
