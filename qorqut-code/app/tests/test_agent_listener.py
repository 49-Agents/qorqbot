"""The sandbox listener must never fall through to installation-owner routes."""
import pytest
from starlette.testclient import TestClient

from qorqut_operations.contracts import new_id
from bazarlyk.service.web import create_app
from test_service_transports import MCP_HEADERS, OWNER_TOKEN, ServiceFixture


@pytest.fixture
def isolated(tmp_path):
    service = ServiceFixture(tmp_path)
    app = create_app(service.runtime, origins=["http://testserver"],
        authentication=service.authentication, agent_only=True,
        dashboard_root=tmp_path / "no-dashboard-installed")
    with TestClient(app) as client:
        yield service, client


@pytest.mark.parametrize("path", ["/", "/app.css", "/js/app.js", "/health/ready",
    "/api/session", "/api/root/agents", "/api/biz/widget/operations/tasks_create",
    "/api/agent/browser", "/api/agent/../session", "/api/%73ession",
    "/api/agent/bootstrap/../../session"])
def test_owner_and_host_routes_are_absent_even_with_agent_credentials(isolated, path):
    service, client = isolated
    for headers in ({}, {"Authorization": "Bearer " + service.tokens[service.agent.id]}):
        for method in ("GET", "POST"):
            assert client.request(method, path, headers=headers).status_code == 404


def test_bootstrap_requires_own_identity_and_preserves_document_checks(isolated):
    service, client = isolated
    body = {"registration_id": service.registrations[service.agent.id],
        "session_id": "sandbox-session", "event": "startup"}
    headers = {"Authorization": "Bearer " + service.tokens[service.agent.id]}
    assert client.post("/api/agent/bootstrap", json=body).status_code == 401
    assert client.post("/api/agent/bootstrap", json=body, headers=headers).json()["data"]["ready"]
    assert client.post("/api/agent/bootstrap", json={**body,
        "registration_id": service.registrations[service.manager.id]}, headers=headers).status_code == 403
    (service.fixture.hq / "context/product.md").write_text("Unregistered edit")
    result = client.post("/api/agent/bootstrap", json=body, headers=headers)
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "document_conflict"


def test_owner_bearer_cannot_authenticate_agent_listener_or_mcp(isolated):
    service, client = isolated
    # This is the configured owner bootstrap secret, while the listener accepts
    # only scoped agent credentials. The owner principal itself is registered.
    assert service.authentication.verify_bootstrap_token(OWNER_TOKEN)
    assert service.authentication.installation_owner()[0].id == service.fixture.owner.id
    body = {"registration_id": service.registrations[service.agent.id],
        "session_id": "sandbox-session", "event": "startup"}
    owner = {"Authorization": "Bearer " + OWNER_TOKEN}
    assert client.post("/api/agent/bootstrap", json=body, headers=owner).status_code == 401
    assert client.post("/mcp", json={}, headers={**MCP_HEADERS, **owner}).status_code == 401


@pytest.mark.parametrize("extra", [
    {"Host": "attacker.invalid"},
    {"Origin": "http://attacker.invalid"},
    {"Host": "testserver:9988", "Origin": "http://testserver"},
])
def test_agent_listener_rejects_untrusted_host_and_origin(isolated, extra):
    service, client = isolated
    body = {"registration_id": service.registrations[service.agent.id],
        "session_id": "sandbox-session", "event": "startup"}
    headers = {"Authorization": "Bearer " + service.tokens[service.agent.id], **extra}
    result = client.post("/api/agent/bootstrap", json=body, headers=headers)
    assert result.status_code == 403


def test_mcp_retains_individual_task_attribution_and_session_binding(isolated):
    service, client = isolated
    first = service.initialize(client, service.agent)
    second = service.initialize(client, service.manager)
    for principal, headers in ((service.agent, first), (service.manager, second)):
        result = service.tool(client, headers, "tasks_create",
            {"title": "Own sandbox task", "idempotency_key": new_id()})
        assert result["decoded"]["data"]["task"]["created_by"] == principal.id
    foreign_id = new_id()
    with service.fixture.db.transaction() as uow:
        uow.repository("businesses").insert({"id": foreign_id, "slug": "foreign",
            "workspace": str(service.fixture.root / "foreign"),
            "hq_path": str(service.fixture.root / "foreign/hq"),
            "name": "Foreign business", "stage": "validating"})
    refused = service.tool(client, first, "tasks_list", {"business_id": foreign_id})
    assert refused["isError"]
    assert refused["decoded"]["status"] == "error"
    stolen = {**first, "Authorization": second["Authorization"]}
    result = client.post("/mcp", headers=stolen,
        json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
    assert result.status_code in {401, 403, 404}
