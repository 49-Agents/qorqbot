import copy

import pytest
from starlette.testclient import TestClient

from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.transport.mcp_reads import ReadRevalidation, split
from mcp_efficiency_support import EVIDENCE, isolated_service, seed_reads


@pytest.fixture
def service(tmp_path):
    with isolated_service(tmp_path) as value:
        yield value


def call(service, client, headers, name, arguments):
    result = service.tool(client, headers, name, arguments)["decoded"]
    assert result["status"] == "ok", result
    return result


def test_opt_in_rechecks_current_task_and_exact_view(service, monkeypatch):
    with TestClient(service.web) as client:
        headers = service.initialize(client, service.manager)
        tools = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 7,
            "method": "tools/list"}).json()["result"]["tools"]
        selectable = {row["name"] for row in tools if "if_read_token" in row["inputSchema"].get("properties", {})}
        assert {"tasks_get", "tasks_list"} <= selectable
        assert selectable == {row["name"] for row in tools} & {"focus_get", "tasks_get", "tasks_list"}
        task = call(service, client, headers, "tasks_create", {"title": "Review current evidence", "idempotency_key": new_id()})["data"]["task"]
        args = {"task_id": task["id"]}
        normal = call(service, client, headers, "tasks_get", args)
        assert "read_state" not in normal
        first = call(service, client, headers, "tasks_get", {**args, "if_read_token": ""})
        assert first["data"] == normal["data"] and first["read_state"]["unchanged"] is False
        token = first["read_state"]["token"]
        invocations = []
        original = service.runtime.application.invoke
        def observe(ctx, name, *args, **kwargs):
            invocations.append(name)
            return original(ctx, name, *args, **kwargs)
        monkeypatch.setattr(service.runtime.application, "invoke", observe)
        repeated = call(service, client, headers, "tasks_get", {**args, "if_read_token": token})
        assert "tasks_get" in invocations
        assert repeated["data"] == {"not_modified": True} and repeated["read_state"]["unchanged"] is True
        # A summary/compact token cannot suppress a different evidence view.
        full = call(service, client, headers, "tasks_get", {**args, "if_read_token": token, "response_detail": "full"})
        assert not full["read_state"]["unchanged"] and full["data"]["task"]["id"] == task["id"]
        changed = call(service, client, headers, "tasks_comment", {**args, "text": EVIDENCE,
            "expected_revision": task["revision"], "idempotency_key": new_id()})
        read = call(service, client, headers, "tasks_get", {**args, "if_read_token": token})
        assert not read["read_state"]["unchanged"] and EVIDENCE in str(read["data"])
        assert read["data"]["task"]["revision"] == changed["data"]["task"]["revision"]
        # No token means unconditional evidence again, even in the same session.
        assert EVIDENCE in str(call(service, client, headers, "tasks_get", args)["data"])
        invalid = service.tool(client, headers, "agent_config_get", {"response_detail": "context", "if_read_token": ""})["decoded"]
        assert invalid["status"] == "error" and invalid["error"]["code"] == "validation_error"


def test_tokens_bind_sessions_queries_principals_and_current_grants(service):
    with TestClient(service.web) as client:
        headers = service.initialize(client, service.manager)
        read = lambda h, args: call(service, client, h, "tasks_list", args)
        first = read(headers, {"if_read_token": ""})
        token = first["read_state"]["token"]
        assert read(headers, {"if_read_token": token})["read_state"]["unchanged"]
        for other in (service.initialize(client, service.manager), service.initialize(client, service.agent)):
            assert not read(other, {"if_read_token": token})["read_state"]["unchanged"]
        assert not read(headers, {"if_read_token": token, "limit": 1})["read_state"]["unchanged"]
        assert not read(headers, {"if_read_token": "r1." + "A" * 43})["read_state"]["unchanged"]
        # Even an unrelated policy revision invalidates the existing comparison.
        service.fixture.update(service.manager, {"assignment_brief": "Updated scope of work; recheck current instructions."})
        assert not read(headers, {"if_read_token": token})["read_state"]["unchanged"]
        service.fixture.update(service.manager, {"tool_policy": {"overrides": {"tasks_list": "deny"}}})
        denied = service.tool(client, headers, "tasks_list", {"if_read_token": token})["decoded"]
        assert denied["status"] == "error" and "read_state" not in denied
        with service.fixture.db.transaction() as uow:
            credential = uow.repository("credentials").list(filters={"principal_id": service.manager.id})[0]
            uow.repository("credentials").update(credential["id"], {"revoked_at": "2026-09-22T12:00:00Z"}, expected_revision=credential["revision"])
        response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "tasks_list", "arguments": {"if_read_token": token}}})
        assert response.status_code == 401


def test_focus_source_changes_redaction_and_pagination_invalidate(service):
    records = seed_reads(service)
    with TestClient(service.web) as client:
        headers = service.initialize(client, service.ordu)
        refused = service.tool(client, headers, "focus_get", {"scope": "global", "item_id": records["cards"][0]["id"],
            "response_detail": "full", "if_read_token": ""})["decoded"]
        assert refused["status"] == "error" and refused["error"]["code"] == "validation_error"
        def read(args):
            return call(service, client, headers, "focus_get", {"scope": "global", **args})
        query = {"limit": 2, "response_detail": "summary"}
        first = read({**query, "if_read_token": ""})
        token = first["read_state"]["token"]
        assert read({**query, "if_read_token": token})["read_state"]["unchanged"]
        page2 = read({**query, "if_read_token": token, "offset": first["data"]["next_offset"]})
        assert not page2["read_state"]["unchanged"] and len(page2["data"]["items"]) == 2
        agent = service.initialize(client, service.agent)
        task = call(service, client, agent, "tasks_get", {"task_id": records["tasks"][0]["id"]})["data"]["task"]
        call(service, client, agent, "tasks_comment", {"task_id": task["id"], "expected_revision": task["revision"],
            "text": "Fresh source evidence, unchanged card revision.", "idempotency_key": new_id()})
        changed = read({**query, "if_read_token": token})
        assert not changed["read_state"]["unchanged"]
        service.fixture.update(service.ordu, {"tool_policy": {"overrides": {"tasks_get": "deny"}}})
        redacted = read({**query, "if_read_token": changed["read_state"]["token"]})
        assert not redacted["read_state"]["unchanged"]
        assert all(row["unavailable"] for row in redacted["data"]["items"])


def test_batch_does_not_reuse_before_an_intervening_write(service):
    with TestClient(service.web) as client:
        headers = service.initialize(client, service.manager)
        first = call(service, client, headers, "tasks_list", {"if_read_token": ""})
        token = first["read_state"]["token"]
        result = call(service, client, headers, "operations_batch", {"operations": [
            {"id": "before", "operation": "tasks_list", "arguments": {"if_read_token": token}},
            {"id": "write", "operation": "tasks_create", "arguments": {"title": "Must appear", "idempotency_key": new_id()}},
            {"id": "after", "operation": "tasks_list", "arguments": {"if_read_token": token}},
        ]})
        before, _, after = [row["result"] for row in result["data"]["results"]]
        assert before["data"] == {"not_modified": True}
        assert not after["read_state"]["unchanged"] and after["data"]["tasks"][0]["title"] == "Must appear"


def test_owner_route_keeps_business_binding_and_http_has_no_selector(service):
    from test_owner_session_mcp import enable, issue, connect, call as owner_call
    from test_service_transports import ORIGIN
    enable(service.fixture)
    with TestClient(service.web) as client:
        headers = connect(client, issue(service.fixture))
        first = owner_call(client, headers, "tasks_list", {"if_read_token": ""})
        token = first["read_state"]["token"]
        assert owner_call(client, headers, "tasks_list", {"if_read_token": token})["read_state"]["unchanged"]
        for args in ({"scope": "global"}, {"business_id": new_id()}):
            denied = owner_call(client, headers, "tasks_list", {**args, "if_read_token": token})
            assert denied["status"] == "error" and "read_state" not in denied
        normal = client.post("/api/biz/widget/operations/tasks_list", json={}, headers=ORIGIN).json()
        assert normal["status"] == "ok" and "read_state" not in normal
        forged = client.post("/api/biz/widget/operations/tasks_list", json={"if_read_token": token}, headers=ORIGIN).json()
        assert forged["status"] == "error"


@pytest.mark.parametrize("name,detail,token", [
    ("agent_config_get", "context", ""), ("agents_activity_list", "summary", ""),
    ("signoffs_get", "full", ""), ("inbox_list", "compact", ""),
    ("tasks_create", "compact", ""), ("tasks_get", "text", ""),
    ("tasks_get", "compact", None), ("tasks_get", "compact", True),
    ("tasks_get", "compact", "invalid"), ("tasks_get", "compact", "r1." + "A" * 44),
])
def test_revalidation_selector_rejects_unsafe_or_malformed_requests(name, detail, token):
    with pytest.raises(DomainError):
        split(name, {"if_read_token": token}, detail)


@pytest.mark.parametrize("selector", [{"item_id": "card"}, {"conversation": True}])
def test_focus_evidence_never_accepts_conditional_delivery(selector):
    with pytest.raises(DomainError, match="board lists only"):
        split("focus_get", {**selector, "if_read_token": ""}, "full")


def test_stateless_comparison_preserves_notices_and_resets_after_restart():
    projection = ReadRevalidation(key=b"test-only-first-key")
    canonical = {"status": "ok", "request_id": "one", "data": {"task": {"id": "t", "fields": {"false": False, "null": None}}}}
    before = copy.deepcopy(canonical)
    def view(value, token="", binding="same"):
        return projection.present(requested=token, binding=binding, canonical=value, presented=value)
    first = view(canonical)
    token = first["read_state"]["token"]
    assert first["data"] == canonical["data"] and canonical == before
    assert view({**canonical, "request_id": "two"}, token)["read_state"]["unchanged"]
    for notice in ({"warnings": ["Read this warning"]}, {"context_refresh_required": True},
                   {"status": "error", "error": {"code": "forbidden"}}, {"status": "accepted"}):
        changed = {**canonical, **notice}
        assert view(changed, token) == changed
    assert not view(canonical, token, "new scope")["read_state"]["unchanged"]
    restarted = ReadRevalidation(key=b"test-only-second-key")
    assert not restarted.present(requested=token, binding="same", canonical=canonical, presented=canonical)["read_state"]["unchanged"]
