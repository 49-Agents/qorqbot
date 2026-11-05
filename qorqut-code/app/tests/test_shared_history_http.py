"""Owner-only history reads over the dashboard HTTP routes; not exposed to agents over MCP."""

from starlette.testclient import TestClient

from qorqut_operations.contracts import new_id
from test_service_transports import ORIGIN, ServiceFixture


def test_history_reads_route_through_dashboard_scopes_only(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    service.runtime.background.start = lambda: None
    with TestClient(service.web) as client:
        service.login(client)
        created = client.post("/api/biz/widget/operations/tasks_create", headers=ORIGIN,
                              json={"title": "Ship", "idempotency_key": new_id()}).json()
        assert created["status"] == "ok", created
        task = created["data"]["task"]
        done = client.post("/api/biz/widget/operations/tasks_complete", headers=ORIGIN,
                           json={"task_id": task["id"], "status": "done", "expected_revision": task["revision"],
                                 "idempotency_key": new_id()}).json()
        assert done["status"] == "ok", done
        listed = client.post("/api/biz/widget/operations/business_history_list", headers=ORIGIN, json={"limit": 5})
        assert listed.status_code == 200, listed.text
        kinds = [event["kind"] for event in listed.json()["data"]["events"]]
        assert "task.completed" in kinds
        missing = client.post("/api/root/operations/document_history_get", headers=ORIGIN,
                              json={"source_kind": "document", "source_id": new_id()})
        assert missing.json()["error"]["code"] == "not_found"
        # business_history_list is business-scoped: there is no Global route for it.
        assert client.post("/api/root/operations/business_history_list", headers=ORIGIN, json={}).status_code >= 400
        headers = service.initialize(client, service.agent)
        for name in ("document_history_get", "business_history_list"):
            denied = service.tool(client, headers, name, {"source_kind": "document", "source_id": new_id()}
                                  if name == "document_history_get" else {})
            assert denied["isError"], denied
