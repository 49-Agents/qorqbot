"""Private Settings reads use ordinary grants, with no cross-scope fallback."""
from dataclasses import replace
import json
from pathlib import Path

from starlette.testclient import TestClient

from qorqut_operations.contracts import new_id
from bazarlyk.policy import resolve_operation_policy
from test_environment_discovery import service, invoke


def reader(service, *, global_scope=False):
    """The business agent, or a Global reader, explicitly granted environment_get."""
    extra = {"tool_policy": {"overrides": {"environment_get": "allow"}}}
    if not global_scope:
        service.fixture.update(service.agent, extra)
        return service.agent
    extra["scope"] = {"kind": "ecosystem", "allowed_business_ids": [service.fixture.business_id],
                      "global_capabilities": ["read_global_context"]}
    return service.fixture.create("private-reader", "ordubashy", **extra)


def test_denied_by_default_and_explicit_grant_reads_arbitrary_context(service):
    (service.fixture.hq / ".env").write_text('PRIVATE_CONTEXT="Private business context, not an API token"\n')
    assert resolve_operation_policy("environment_get", {}, {}, service.runtime.registry).mode == "deny"
    denied = invoke(service, service.agent, "environment_get", {"key": "PRIVATE_CONTEXT"})
    assert denied.status == "error" and "Private business context" not in json.dumps(denied.to_dict())
    agent = reader(service)
    result = invoke(service, agent, "environment_get", {"key": "PRIVATE_CONTEXT"})
    assert result.status == "ok", result.to_dict()
    assert result.data == {"key": "PRIVATE_CONTEXT", "found": True,
                           "value": "Private business context, not an API token", "scope": "business"}
    owner_read = service.runtime.application.invoke(service.fixture.ctx, "environment_get", {"key": "PRIVATE_CONTEXT"})
    assert owner_read.status == "ok"
    ctx = service.runtime.resolver.invocation_context(agent)
    with service.runtime.database.transaction() as uow:
        repo = uow.repository("business_memberships", business_id=service.fixture.business_id)
        member = repo.get(agent.id)
        repo.update(agent.id, {"active": False}, expected_revision=member["revision"])
    assert service.runtime.application.invoke(ctx, "environment_get", {"key": "PRIVATE_CONTEXT"}).status == "error"


def test_business_reads_never_inherit_global_and_cannot_forge_scope(service):
    (service.runtime.root / ".env").write_text("SHARED=global-private\nGLOBAL_ONLY=hidden\n")
    (service.fixture.hq / ".env").write_text("SHARED=local-private\nEMPTY=\n")
    agent = reader(service)
    assert invoke(service, agent, "environment_get", {"key": "SHARED"}).data["value"] == "local-private"
    assert invoke(service, agent, "environment_get", {"key": "GLOBAL_ONLY"}).data["found"] is False
    assert invoke(service, agent, "environment_get", {"key": "EMPTY"}).data["value"] == ""
    assert invoke(service, agent, "environment_get", {"key": "SHARED", "scope": "global"}).status == "error"
    ctx = service.runtime.resolver.invocation_context(agent)
    # A missing selection is normalized to the business agent's home scope.
    normalized = service.runtime.application.invoke(replace(ctx, business_id=None), "environment_get", {"key": "SHARED"})
    assert normalized.status == "ok" and normalized.data["value"] == "local-private"
    assert service.runtime.application.invoke(replace(ctx, business_id=new_id()), "environment_get", {"key": "SHARED"}).status == "error"


def test_global_grant_and_revocation_rechecked(service):
    (service.runtime.root / ".env").write_text("PRIVATE=global-private\n")
    agent = reader(service, global_scope=True)
    ctx = service.runtime.resolver.invocation_context(agent)
    assert invoke(service, agent, "environment_get", {"key": "PRIVATE"}).data["value"] == "global-private"
    service.fixture.update(agent, {"tool_policy": {"overrides": {"environment_get": "deny"}}})
    result = service.runtime.application.invoke(ctx, "environment_get", {"key": "PRIVATE"})
    assert result.status == "error" and "global-private" not in json.dumps(result.to_dict())


def test_empty_missing_reserved_and_size_limit(service):
    (service.fixture.hq / ".env").write_text("BIZ_ACTOR=internal\nBIG=" + "s" * 17000 + "\n")
    agent = reader(service)
    absent = invoke(service, agent, "environment_get", {"key": "MISSING"})
    assert absent.data["value"] is None and absent.data["found"] is False
    for key in ("BIZ_ACTOR", "BIG", "../.env"):
        result = invoke(service, agent, "environment_get", {"key": key})
        assert result.status == "error"
        assert "internal" not in json.dumps(result.to_dict())
        assert "s" * 100 not in json.dumps(result.to_dict())


def test_mcp_discovery_and_value_read(service):
    (service.fixture.hq / ".env").write_text("PRIVATE=context-synthetic\n")
    agent = reader(service)
    ready = service.fixture.prepare(agent)
    service.tokens[agent.id] = (Path(ready["workspace"]) / ".bazarlyk/credential").read_text().strip()
    with TestClient(service.web) as client:
        headers = service.initialize(client, agent)
        listed = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
        assert "environment_get" in {t["name"] for t in listed}
        result = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "environment_get", "arguments": {"key": "PRIVATE"}}}).json()
        assert "context-synthetic" in json.dumps(result)
