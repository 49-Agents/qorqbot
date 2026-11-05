"""Agents can file globally without gaining cross-business queue access."""
from dataclasses import replace

from starlette.testclient import TestClient

from qorqut_operations.contracts import new_id
from test_global_agents import global_agent
from test_service_transports import ORIGIN, ServiceFixture


def test_agent_reports_are_shared_attributed_and_retry_safe(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    app = service.runtime.application
    business = service.runtime.resolver.invocation_context(service.agent)
    global_ctx = service.runtime.resolver.invocation_context(global_agent(service, "global-operator"))
    owner = replace(service.fixture.ctx, business_id=None)
    with TestClient(service.web) as client:
        headers = service.initialize(client, service.agent)
        listed = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}).json()
        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert "dev_tickets_create" in names
        assert not {"dev_tickets_list", "dev_tickets_status_set"} & names
        response = client.post("/api/root/operations/dev_tickets_list", headers=ORIGIN, json={"details": True})
        assert response.status_code == 200 and response.json()["status"] == "ok", response.text
    ids = []
    for ctx in (business, global_ctx):
        key = new_id()
        args = {"title": "Cannot save Settings", "details": "Got an error; please investigate.", "kind": "tooling"}
        result = app.invoke(ctx, "dev_tickets_create", args, {"idempotency_key": key})
        assert result.status == "ok", result.to_dict()
        assert app.invoke(ctx, "dev_tickets_create", args, {"idempotency_key": key}).data == result.data
        ids.append(result.data["ticket_id"])
        assert app.invoke(ctx, "dev_tickets_list", {}).status == "error"
        assert app.invoke(ctx, "dev_tickets_status_set", {"ticket_id": ids[0], "status": "closed"},
                          {"idempotency_key": new_id(), "expected_revision": 1}).status == "error"
    result = app.invoke(owner, "dev_tickets_list", {"details": True})
    assert result.status == "ok", result.to_dict()
    rows = result.data["items"]
    assert {r["id"] for r in rows} == set(ids)
    assert {(r["reporter_id"], r["business_id"]) for r in rows} == {
        (business.principal_id, business.business_id), (global_ctx.principal_id, None)}
    assert all(r["details"] == "Got an error; please investigate." for r in rows)
    compact = app.invoke(owner, "dev_tickets_list", {"limit": 1}).data
    assert "details" not in compact["items"][0] and compact["next_offset"] == 1
    assert len(app.invoke(owner, "dev_tickets_list", {"offset": 1}).data["items"]) == 1
    changed = app.invoke(owner, "dev_tickets_status_set", {"ticket_id": ids[0], "status": "closed"},
                         {"idempotency_key": new_id(), "expected_revision": 1})
    assert changed.status == "ok", changed.to_dict()
    assert len(app.invoke(owner, "dev_tickets_list", {}).data["items"]) == 1
    stale = app.invoke(owner, "dev_tickets_status_set", {"ticket_id": ids[0], "status": "open"},
                       {"idempotency_key": new_id(), "expected_revision": 1})
    assert stale.status == "error" and stale.error["code"] == "revision_conflict"
    assert len(app.invoke(owner, "dev_tickets_list", {"status": "all"}).data["items"]) == 2


def test_create_defaults_validation_and_explicit_denial(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    app = service.runtime.application
    ctx = service.runtime.resolver.invocation_context(service.agent)
    made = app.invoke(ctx, "dev_tickets_create", {"title": "Need a new tool"}, {"idempotency_key": new_id()})
    assert made.status == "ok", made.to_dict()
    for args in ({"title": "  "}, {"title": "x", "reporter_id": new_id()}, {"title": "x", "details": "x" * 12001}):
        result = app.invoke(ctx, "dev_tickets_create", args, {"idempotency_key": new_id()})
        assert result.status == "error" and result.error["code"] == "validation_error"
    service.fixture.update(service.agent, {"tool_policy": {"overrides": {"dev_tickets_create": "deny"}}})
    assert app.invoke(ctx, "dev_tickets_create", {"title": "Blocked"}, {"idempotency_key": new_id()}).status == "error"
