"""Credential metadata and founder handoffs through the real application."""
from dataclasses import replace
import json

import pytest
from starlette.testclient import TestClient

from qorqut_operations.contracts import InvocationOptions, new_id
from bazarlyk.policy import resolve_operation_policy
from test_global_agents import global_agent
from test_service_transports import ServiceFixture


@pytest.fixture
def service(tmp_path):
    return ServiceFixture(tmp_path, full=True)


def invoke(service, principal, name="environment_list", args=None, **options):
    ctx = service.runtime.resolver.invocation_context(principal)
    return service.runtime.application.invoke(ctx, name, args or {}, InvocationOptions(**options))


def test_discovery_is_metadata_only_and_matches_runtime_precedence(service):
    (service.runtime.root / ".env").write_text("TOKEN=global-secret\nGLOBAL_ONLY=inherited-secret\nBIZ_ACTOR=ignored\n")
    (service.fixture.hq / ".env").write_text("TOKEN=\nLOCAL_ONLY=local-secret\n")
    result = invoke(service, service.agent, args={"names": ["TOKEN", "GLOBAL_ONLY", "LOCAL_ONLY", "MISSING"]})
    assert result.status == "ok", result.to_dict()
    assert result.to_dict()["data"]["items"] == [
        {"name": "GLOBAL_ONLY", "configured": True, "origin": "global"},
        {"name": "LOCAL_ONLY", "configured": True, "origin": "business"},
        {"name": "MISSING", "configured": False, "origin": None},
        {"name": "TOKEN", "configured": False, "origin": "business"},
    ]
    assert "secret" not in json.dumps(result.to_dict())
    listing = invoke(service, service.agent).to_dict()["data"]["items"]
    assert {row["name"] for row in listing} == {"TOKEN", "GLOBAL_ONLY", "LOCAL_ONLY"}
    (service.fixture.hq / ".env").write_text("")
    assert invoke(service, service.agent, args={"names": ["TOKEN"]}).data["items"][0]["origin"] == "global"


def test_global_scope_pagination_and_forged_business_selection(service):
    (service.runtime.root / ".env").write_text("A=one\nB=two\n")
    (service.fixture.hq / ".env").write_text("C=business-secret\n")
    applier = global_agent(service)
    guidance = service.runtime.resolver.resolve_agent_config(applier).to_dict()["mandatory_context"]["rendered"]
    assert "human founder/owner" in guidance and "persistent workspace path" in guidance
    assert "If task tools, an authorized scope" in guidance
    first = invoke(service, applier, args={"limit": 1}).data
    second = invoke(service, applier, args={"limit": 1, "cursor": first["next_cursor"]}).data
    assert [first["items"][0]["name"], second["items"][0]["name"]] == ["A", "B"]
    assert second["next_cursor"] is None
    assert invoke(service, service.agent, args={"cursor": first["next_cursor"]}).status == "error"
    assert invoke(service, service.agent, args={"business_id": new_id()}).status == "error"
    ctx = service.runtime.resolver.invocation_context(service.agent)
    forged = replace(ctx, business_id=new_id())
    assert service.runtime.application.invoke(forged, "environment_list", {}).status == "error"
    with service.runtime.database.transaction() as uow:
        member = uow.repository("business_memberships", business_id=service.fixture.business_id).get(service.agent.id)
        uow.repository("business_memberships", business_id=service.fixture.business_id).update(service.agent.id,
            {"active": False}, expected_revision=member["revision"])
    assert service.runtime.application.invoke(ctx, "environment_list", {}).status == "error"


def test_discovery_default_and_explicit_denial(service):
    registry = service.runtime.registry
    assert resolve_operation_policy("environment_list", {}, {}, registry).mode == "allow"
    assert resolve_operation_policy("environment_list", {"environment_list": "deny"}, {}, registry).mode == "deny"
    config = service.runtime.resolver.resolve_agent_config(service.agent).to_dict()
    assert next(tool for tool in config["tools"] if tool["canonical_operation"] == "environment_list")["available"]
    assert "environment_list for Settings variable names" in config["mandatory_context"]["rendered"]
    assert invoke(service, service.agent).status == "ok"
    service.fixture.update(service.agent, {"tool_policy": {"overrides": {"environment_list": "deny"}}})
    assert invoke(service, service.agent).status == "error"


def test_owner_can_read_note_referenced_by_agent_handoff(service):
    body = "Script: scratchpads/collector.py\nRun: python collector.py --date YYYY-MM-DD\nCredential binding: TELEMETRY_TOKEN"
    result = invoke(service, service.agent, "notes_create", {
        "title": "Collector handoff", "body": body},
        idempotency_key=new_id())
    assert result.status == "ok", result.to_dict()
    document = result.data["document"]
    assignees = invoke(service, service.agent, "assignees_list").data["assignees"]
    owners = [row for row in assignees if row["is_owner"]]
    assert len(owners) == 1 and owners[0]["id"] == service.fixture.owner.id and owners[0]["kind"] == "human"
    note = f"Prepared instructions: notes_get(document_id={document['id']}). Script: scratchpads/collector.py in the business agent's workspace."
    task = invoke(service, service.agent, "tasks_create", {
        "title": "Provide collector execution capability", "assignee_ids": [owners[0]["id"]], "note": note}, idempotency_key=new_id())
    assert task.status == "ok", task.to_dict()
    assert task.data["task"]["notes"][0]["body"] == note
    read = service.runtime.application.invoke(service.fixture.ctx, "notes_get", {"document_id": document["id"]})
    assert read.status == "ok" and read.data["document"]["body"] == body


def test_metadata_is_discoverable_through_mcp_and_errors_never_reveal_values(service):
    (service.runtime.root / ".env").write_text("TOKEN=private-value\n")
    with TestClient(service.web) as client:
        headers = service.initialize(client, service.manager)
        listed = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
        assert "environment_list" in {tool["name"] for tool in listed}
        result = service.tool(client, headers, "environment_list", {})
        assert not result["isError"] and "private-value" not in json.dumps(result)
        forbidden = service.tool(client, headers, "environment_list", {"business_id": new_id()})
        assert forbidden["isError"]
        (service.runtime.root / ".env").write_bytes(b"TOKEN=private-value\xff")
        failure = service.tool(client, headers, "environment_list", {})
        assert failure["isError"] and "private-value" not in json.dumps(failure)
        assert failure["decoded"]["error"]["code"] == "configuration_unavailable"


def test_bootstrap_environment_obeys_scope_and_policy(service):
    from bazarlyk.service.environment import bootstrap_environment
    (service.runtime.root / '.env').write_text('CLOUDFLARE_API_TOKEN=private-global\n')
    (service.fixture.hq / '.env').write_text('BUSINESS_TOKEN=private-business\n')
    ctx = service.runtime.agent_context(global_agent(service))
    result = bootstrap_environment(service.runtime, ctx)
    assert result == {'status': 'ok', 'items': [
        {'name': 'CLOUDFLARE_API_TOKEN', 'configured': True, 'origin': 'global'}], 'has_more': False}
    (service.runtime.root / '.env').write_text(''.join(f'KEY_{i:02d}=hidden\n' for i in range(31)))
    paged = bootstrap_environment(service.runtime, ctx)
    assert len(paged['items']) == 30 and paged['has_more'] is True
    service.fixture.update(service.agent, {'tool_policy': {'overrides': {'environment_list': 'deny'}}})
    result = bootstrap_environment(service.runtime, service.runtime.agent_context(service.agent))
    assert result == {'status': 'unavailable', 'items': [], 'has_more': False}


def test_http_bootstrap_delivers_settings_without_process_injection(service):
    (service.runtime.root / '.env').write_text('CLOUDFLARE_API_TOKEN=private-token\n')
    with TestClient(service.web) as client:
        response = client.post('/api/agent/bootstrap',
            headers={'Authorization': 'Bearer ' + service.tokens[service.agent.id]},
            json={'registration_id': service.registrations[service.agent.id],
                  'session_id': new_id(), 'event': 'startup'})
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['environment']['status'] == 'ok'
    assert {'name': 'CLOUDFLARE_API_TOKEN', 'configured': True, 'origin': 'global'} in data['environment']['items']
    assert 'private-token' not in response.text


def test_environment_section_is_bounded_and_never_renders_values():
    from bazarlyk.bootstrap.environment import bootstrap_environment_context
    metadata = {'status': 'ok', 'items': [
        {'name': 'CLOUDFLARE_API_TOKEN', 'configured': True, 'origin': 'global', 'value': 'private-token'},
        {'name': 'EMPTY_TOKEN', 'configured': False, 'origin': 'business'}], 'has_more': True}
    text = bootstrap_environment_context(metadata, {'HOME': 'private-home'})
    assert text.startswith('## Environment and credentials (names only)')
    assert 'CLOUDFLARE_API_TOKEN (global, configured)' in text
    assert 'EMPTY_TOKEN (business, empty)' in text
    assert 'Startup-process variables: HOME' in text
    assert 'next_cursor' in text and 'private-' not in text
    for budget in (0, 100, 300, 900, 2400):
        assert len(bootstrap_environment_context(metadata, {}, max_chars=budget)) <= budget
    assert 'does not mean no credentials' in bootstrap_environment_context(None, {})
