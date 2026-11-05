"""Owner-session MCP route: explicit catalog, business binding, owner identity."""

import json
from pathlib import Path

import pytest

from starlette.testclient import TestClient

from bazarlyk import owner_sessions
from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.domains.documents.operations import BASE_COMPAT_WRITES
from test_owner_private_conversations import create_ordubashy
from test_service_transports import MCP_HEADERS, ORIGIN, ServiceFixture


def enable(fixture, business_id=None, enabled=True):
    business_id = business_id or fixture.business_id
    with fixture.db.transaction() as uow:
        row = uow.repository("businesses").get(business_id)
        config = {**row["config_json"], "owner_sessions": {"enabled": enabled}}
        uow.connection.execute("UPDATE businesses SET config_json=? WHERE id=?",
                               (json.dumps(config), business_id))


def issue(fixture, business_id=None, label="laptop"):
    return owner_sessions.issue_credential(fixture.db, business_id=business_id or fixture.business_id, label=label)


def connect(client, token, path="/mcp/owner"):
    headers = {**MCP_HEADERS, "Authorization": "Bearer " + token}
    result = client.post(path, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "owner", "version": "1"}}})
    assert result.status_code == 200, result.text
    headers["Mcp-Session-Id"] = result.headers["mcp-session-id"]
    headers["MCP-Protocol-Version"] = result.json()["result"]["protocolVersion"]
    assert client.post(path, headers=headers,
                       json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    return headers


def call(client, headers, name, arguments, path="/mcp/owner", status=200):
    result = client.post(path, headers=headers, json={"jsonrpc": "2.0", "id": new_id(), "method": "tools/call",
        "params": {"name": name, "arguments": arguments}})
    assert result.status_code == status, result.text
    if status != 200:
        return result.json()
    return json.loads(result.json()["result"]["content"][0]["text"])


def ok(decoded):
    assert decoded["status"] == "ok", decoded
    return decoded["data"]


def code(decoded):
    assert decoded["status"] == "error", decoded
    return decoded["error"]["code"]


def tool_names(client, headers, path="/mcp/owner"):
    result = client.post(path, headers=headers,
                         json={"jsonrpc": "2.0", "id": new_id(), "method": "tools/list", "params": {}})
    assert result.status_code == 200, result.text
    return {tool["name"] for tool in result.json()["result"]["tools"]}


@pytest.fixture
def full(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    enable(service.fixture)
    service.token = issue(service.fixture)
    return service


@pytest.fixture
def light(tmp_path):
    service = ServiceFixture(tmp_path)
    enable(service.fixture)
    service.token = issue(service.fixture)
    return service


def test_owner_route_lists_exactly_the_catalog(full):
    with TestClient(full.web) as client:
        headers = connect(client, full.token)
        names = tool_names(client, headers)
        assert names == set(owner_sessions.CATALOG)
        assert {"context_edit", "context_instruct", "context_upload"} <= names
        assert not names & BASE_COMPAT_WRITES
        for legacy in BASE_COMPAT_WRITES:
            assert code(call(client, headers, legacy, {"idempotency_key": new_id()})) == "forbidden"
        for excluded in ("agent_config_get", "owner_conversation_mark_read",
                         "inbox_list", "signoffs_review", "requests_create"):
            assert excluded not in names
        for included in ("tasks_create", "tasks_assign", "agents_message_send",
                         "agents_messages_list", "agents_message_recipients"):
            assert included in names
        # The registered-agent route keeps its own inventory untouched.
        agent_headers = full.initialize(client, full.agent)
        agent_names = {tool["name"] for tool in client.post("/mcp", headers=agent_headers,
            json={"jsonrpc": "2.0", "id": new_id(), "method": "tools/list", "params": {}}).json()["result"]["tools"]}
        assert "tasks_create" in agent_names and "agent_config_get" in agent_names
        assert not agent_names & BASE_COMPAT_WRITES
        for legacy in BASE_COMPAT_WRITES:
            descriptor = full.runtime.registry.get(legacy)
            assert "mcp" not in descriptor.exposure
            assert {"dashboard", "worker"} <= descriptor.exposure


def test_credentials_do_not_cross_routes(light):
    with TestClient(light.web) as client:
        agent_token = light.tokens[light.agent.id]
        bad = client.post("/mcp/owner", headers={**MCP_HEADERS, "Authorization": "Bearer " + agent_token},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}})
        assert bad.status_code == 401, bad.text
        bad = client.post("/mcp", headers={**MCP_HEADERS, "Authorization": "Bearer " + light.token},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}})
        assert bad.status_code == 401, bad.text
        # Existing registered-agent MCP still works with its own credential.
        headers = light.initialize(client, light.agent)
        assert light.tool(client, headers, "tasks_list", {})["decoded"]["status"] == "ok"


def test_owner_business_binding_cannot_be_bypassed_with_global_scope(full):
    with TestClient(full.web) as client:
        headers = connect(client, full.token)
        for operation, arguments in (
            ("tasks_list", {}),
            ("signoffs_list", {}),
            ("tasks_update", {"task_id": new_id(), "changes": {"title": "Global edit"},
                              "idempotency_key": new_id()}),
            ("owner_conversation_get", {"source": {"type": "task", "id": new_id()}}),
        ):
            assert code(call(client, headers, operation, {**arguments, "scope": "global"})) == "forbidden"
        batch = call(client, headers, "operations_batch", {"operations": [
            {"id": "business", "operation": "tasks_list", "arguments": {}},
            {"id": "global", "operation": "tasks_list", "arguments": {"scope": "global"}},
        ]})["data"]
        assert batch["results"][0]["result"]["status"] == "ok"
        assert batch["results"][1]["result"]["error"]["code"] == "forbidden"


def test_catalog_binding_and_batch_enforcement(full):
    fixture = full.fixture
    other = new_id()
    (fixture.root / "second/hq").mkdir(parents=True)
    with fixture.db.transaction() as uow:
        uow.repository("businesses").insert({"id": other, "slug": "second", "name": "Second",
            "workspace": str(fixture.root / "second"), "hq_path": str(fixture.root / "second/hq"), "stage": "validating"})
        uow.repository("business_memberships", business_id=other).insert(
            {"principal_id": fixture.owner.id, "is_owner": True})
    with TestClient(full.web) as client:
        headers = connect(client, full.token)
        created = call(client, headers, "tasks_create",
                       {"title": "New", "idempotency_key": new_id()})
        assert created["status"] == "ok"
        assert code(call(client, headers, "owner_conversation_mark_read",
                         {"source": {"type": "task", "id": new_id()}, "sequence": 1})) == "forbidden"
        assert code(call(client, headers, "agent_config_get", {})) == "forbidden"
        assert len(ok(call(client, headers, "tasks_list", {})).get("tasks", [])) == 1
        assert len(ok(call(client, headers, "tasks_list", {"business_id": fixture.business_id})).get("tasks", [])) == 1
        assert code(call(client, headers, "tasks_list", {"business_id": other})) == "forbidden"
        assert code(call(client, headers, "tasks_list", {"review_business_id": other})) == "forbidden"
        batch = call(client, headers, "operations_batch", {"operations": [
            {"id": "read", "operation": "tasks_list", "arguments": {}},
            {"id": "write", "operation": "tasks_create",
             "arguments": {"title": "Queued", "idempotency_key": new_id()}}]})
        rows = batch["data"]["results"]
        assert rows[0]["result"]["status"] == "ok"
        assert rows[1]["result"]["status"] == "ok"
        with fixture.db.read() as uow:
            assert uow.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2


def test_owner_catalog_descriptions_and_business_settings_are_bound(full):
    fixture = full.fixture
    (fixture.root / ".env").write_text("OWNER_GLOBAL_ONLY=global-secret\n")
    (fixture.hq / ".env").write_text("KEEP=business\n")
    private = fixture.hq / ".env.private.json"
    private.write_text(json.dumps({
        "TOKEN": {"value": "old", "allowed_roles": ["onbashy"]},
        "OTHER": {"value": "keep", "allowed_roles": ["founder"]}}))

    with TestClient(full.web) as client:
        headers = connect(client, full.token)
        names = tool_names(client, headers)
        assert "environment_set" in owner_sessions.WRITE_OPERATIONS
        assert "environment_set" in owner_sessions.CATALOG and "environment_set" in names

        described = ok(call(client, headers, "operations_describe", {
            "names": ["environment_set", "shared_instructions_update"]}))
        tools = {item["name"]: item for item in described["tools"]}
        assert tools["environment_set"]["available"] is True
        assert tools["shared_instructions_update"] == {
            "name": "shared_instructions_update", "available": False, "reason": "forbidden"}
        assert code(call(client, headers, "operations_describe", {
            "names": ["environment_set"], "plugins": [full.fixture.refs["email-management"]]})) == "forbidden"

        key = new_id()
        args = {"key": "CREDENTIAL", "value": "business-secret", "idempotency_key": key}
        assert ok(call(client, headers, "environment_set", args))["key"] == "CREDENTIAL"
        assert ok(call(client, headers, "environment_set", {"key": "CREDENTIAL", "value": "business-secret",
                                                              "idempotency_key": key}))["key"] == "CREDENTIAL"
        assert ok(call(client, headers, "environment_get", {"key": "CREDENTIAL"})) == {
            "key": "CREDENTIAL", "found": True, "value": "business-secret", "scope": "business"}
        assert (fixture.root / ".env").read_text() == "OWNER_GLOBAL_ONLY=global-secret\n"
        assert "KEEP=business\n" in (fixture.hq / ".env").read_text()

        assert ok(call(client, headers, "environment_set", {
            "key": "TOKEN", "value": "new", "idempotency_key": new_id()}))["key"] == "TOKEN"
        saved = json.loads(private.read_text())
        assert saved["TOKEN"] == {"value": "new", "allowed_roles": ["onbashy"]}
        assert saved["OTHER"] == {"value": "keep", "allowed_roles": ["founder"]}

        assert code(call(client, headers, "environment_set", {
            "key": "CREDENTIAL", "value": "foreign", "business_id": new_id(),
            "idempotency_key": new_id()})) == "forbidden"
        assert code(call(client, headers, "environment_set", {
            "key": "CREDENTIAL", "value": "global", "scope": "global",
            "idempotency_key": new_id()})) == "forbidden"

        with fixture.db.read() as uow:
            business = uow.repository("businesses").get(fixture.business_id)
        assert code(call(client, headers, "business_config_patch", {
            "changes": {"owner_sessions": {"enabled": False}},
            "expected_revision": business["revision"], "idempotency_key": new_id()})) == "forbidden"


def test_owner_to_ordubashy_task_delivery_and_reply_round_trip(full):
    fixture = full.fixture
    ordubashy = create_ordubashy(fixture, [fixture.business_id])
    ready = fixture.prepare(ordubashy)
    full.tokens[ordubashy.id] = (Path(ready["workspace"]) / ".bazarlyk/credential").read_text().strip()
    with TestClient(full.web) as client:
        headers = connect(client, full.token)
        task = ok(call(client, headers, "tasks_create", {
            "title": "Owner handoff", "assignee_ids": [ordubashy.id], "idempotency_key": new_id()}))["task"]
        assert task["assignee_ids"] == [ordubashy.id]
        sent = ok(call(client, headers, "agents_message_send", {
            "to_agent_id": ordubashy.id, "message": "Please review the owner handoff.",
            "idempotency_key": new_id()}))["message"]
        assert sent["recipient_id"] == ordubashy.id

        agent_headers = full.initialize(client, ordubashy)
        history = full.tool(client, agent_headers, "agents_messages_list", {
            "business_id": fixture.business_id, "response_detail": "full"})["decoded"]
        assert history["status"] == "ok", history
        assert any(item.get("body") == "Please review the owner handoff."
                   for item in history["data"].get("items", [])), history
        reply = full.tool(client, agent_headers, "agents_message_send", {
            "business_id": fixture.business_id, "reply_to": sent["id"], "message": "Reviewed and ready.",
            "idempotency_key": new_id()})["decoded"]
        assert reply["status"] == "ok", reply
        history = ok(call(client, headers, "agents_messages_list", {}))
        assert any(item.get("body") == "Reviewed and ready." for item in history["items"])


def test_shared_discussion_boundary_holds_for_notes_and_note_fields(full):
    with TestClient(full.web) as client:
        agent_headers = full.initialize(client, full.agent)
        story = full.tool(client, agent_headers, "stories_create",
                          {"title": "Pilot", "purpose": "Learn pricing", "idempotency_key": new_id()})["decoded"]["data"]["story"]
        task = full.tool(client, agent_headers, "tasks_create",
                         {"title": "Boundary", "idempotency_key": new_id()})["decoded"]["data"]["task"]
        headers = connect(client, full.token)
        assert "stories_note_add" not in tool_names(client, headers)
        assert code(call(client, headers, "stories_note_add",
                         {"story_id": story["id"], "body": "Leak", "idempotency_key": new_id()})) == "forbidden"
        assert code(call(client, headers, "stories_update",
                         {"story_id": story["id"], "changes": {"title": "Pilot 2"}, "note": "Leak",
                          "idempotency_key": new_id()})) == "channel_required"
        assert ok(call(client, headers, "stories_update",
                       {"story_id": story["id"], "changes": {"title": "Pilot 2"},
                        "expected_revision": story["revision"], "idempotency_key": new_id()}))
        # Existing task-note guard: an owner note on completion is refused too.
        assert code(call(client, headers, "tasks_complete",
                         {"task_id": task["id"], "note": "Leak", "expected_revision": task["revision"],
                          "idempotency_key": new_id()})) == "channel_required"
        with full.fixture.db.read() as uow:
            assert uow.connection.execute("SELECT COUNT(*) FROM story_events WHERE kind='note'").fetchone()[0] == 0
            assert uow.connection.execute("SELECT COUNT(*) FROM task_notes").fetchone()[0] == 0
            assert "Leak" not in json.dumps([dict(row) for row in uow.connection.execute(
                "SELECT title, body FROM story_events").fetchall()])


def test_owner_attribution_matches_dashboard_and_reads_stay_silent(full):
    fixture = full.fixture
    full.app = full.runtime.application
    ordu = create_ordubashy(fixture, [fixture.business_id])
    with fixture.db.transaction() as uow:
        repository = uow.repository("installation")
        row = repository.get("service")
        repository.update("service", {"management_coordinator_id": ordu.id}, expected_revision=row["revision"])
    with TestClient(full.web) as client:
        agent_headers = full.initialize(client, full.agent)
        task = full.tool(client, agent_headers, "tasks_create",
                         {"title": "Session task", "idempotency_key": new_id()})["decoded"]["data"]["task"]
        source = {"type": "task", "id": task["id"]}
        headers = connect(client, full.token)
        sent = ok(call(client, headers, "owner_conversation_send",
                       {"source": source, "text": "Private CLI note", "idempotency_key": new_id()}))
        assert sent["message"]["author_role"] == "owner"
        dashboard = client.post("/api/biz/widget/operations/owner_conversation_send", headers=ORIGIN,
            json={"source": source, "text": "Private dashboard note", "idempotency_key": new_id()})
        assert dashboard.status_code in {200, 201}, dashboard.text
        with fixture.db.read() as uow:
            rows = uow.connection.execute(
                "SELECT created_by, author_role, body FROM owner_conversation_messages ORDER BY sequence").fetchall()
            assert [(row["created_by"], row["author_role"]) for row in rows] == [
                (fixture.owner.id, "owner"), (fixture.owner.id, "owner")]
            deliveries = uow.connection.execute(
                "SELECT d.agent_id FROM inbox_deliveries d JOIN inbox_events e ON e.id=d.event_id "
                "WHERE e.source_type='owner_conversations'").fetchall()
            assert {row["agent_id"] for row in deliveries} == {ordu.id} and len(deliveries) == 2
        read = ok(call(client, headers, "owner_conversation_get", {"source": source}))
        assert {message["created_by"] for message in read["messages"]} == {fixture.owner.id}
        assert all(message["author_kind"] == "human" and message["is_owner"] for message in read["messages"])
        assert read["read_state"]["last_read_sequence"] == 0
        with fixture.db.read() as uow:
            assert uow.connection.execute("SELECT COUNT(*) FROM owner_conversation_reads").fetchone()[0] == 0


def test_writes_use_existing_revision_and_idempotency_behavior(full):
    with TestClient(full.web) as client:
        agent_headers = full.initialize(client, full.agent)
        task = full.tool(client, agent_headers, "tasks_create",
                         {"title": "Draft", "idempotency_key": new_id()})["decoded"]["data"]["task"]
        headers = connect(client, full.token)
        key = new_id()
        arguments = {"task_id": task["id"], "changes": {"title": "Draft, retitled"},
                     "expected_revision": task["revision"], "idempotency_key": key}
        first = ok(call(client, headers, "tasks_update", arguments))
        again = ok(call(client, headers, "tasks_update", arguments))
        assert first["task"]["revision"] == again["task"]["revision"]
        assert code(call(client, headers, "tasks_update",
                         {**arguments, "idempotency_key": new_id()})) == "revision_conflict"


def test_state_changes_fail_cleanly_within_an_open_session(light):
    fixture = light.fixture
    with TestClient(light.web) as client:
        headers = connect(client, light.token)
        assert ok(call(client, headers, "tasks_list", {})).get("tasks", []) == []
        enable(fixture, enabled=False)
        assert call(client, headers, "tasks_list", {}, status=403)["error"]["code"] == "forbidden"
        enable(fixture)
        assert ok(call(client, headers, "tasks_list", {})).get("tasks", []) == []
        with fixture.db.transaction() as uow:
            uow.connection.execute("UPDATE credentials SET revoked_at='2026-01-01T00:00:00+00:00' "
                                   "WHERE principal_id=?", (fixture.owner.id,))
        assert call(client, headers, "tasks_list", {}, status=401)["error"]["code"] == "unauthenticated"
        light.token = issue(fixture, label="second")
        headers = connect(client, light.token)
        with fixture.db.transaction() as uow:
            uow.connection.execute("UPDATE credentials SET expires_at='2020-01-01T00:00:00+00:00' "
                                   "WHERE principal_id=? AND revoked_at IS NULL", (fixture.owner.id,))
        assert call(client, headers, "tasks_list", {}, status=401)["error"]["code"] == "unauthenticated"
        light.token = issue(fixture, label="third")
        headers = connect(client, light.token)
        with fixture.db.transaction() as uow:
            uow.connection.execute("UPDATE businesses SET archived_at='2026-01-01T00:00:00+00:00' WHERE id=?",
                                   (fixture.business_id,))
        assert call(client, headers, "tasks_list", {}, status=403)["error"]["code"] == "forbidden"
        with fixture.db.transaction() as uow:
            uow.connection.execute("UPDATE businesses SET archived_at=NULL WHERE id=?", (fixture.business_id,))
            uow.connection.execute("UPDATE business_memberships SET active=0 WHERE principal_id=? AND business_id=?",
                                   (fixture.owner.id, fixture.business_id))
        assert call(client, headers, "tasks_list", {}, status=403)["error"]["code"] == "forbidden"


def test_issuance_requires_enabled_business_and_rotates_per_label(tmp_path):
    service = ServiceFixture(tmp_path)
    fixture = service.fixture
    with pytest.raises(DomainError) as denied:
        issue(fixture)
    assert denied.value.code == "forbidden"
    enable(fixture)
    laptop = issue(fixture, label="laptop")
    desk = issue(fixture, label="desk")
    with TestClient(service.web) as client:
        connect(client, laptop)
        connect(client, desk)
        replacement = issue(fixture, label="laptop")
        bad = client.post("/mcp/owner", headers={**MCP_HEADERS, "Authorization": "Bearer " + laptop},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}})
        assert bad.status_code == 401
        connect(client, replacement)
        connect(client, desk)


def test_config_section_is_validated_and_owner_managed(tmp_path):
    for section in ({"enabled": "yes"}, {"enabled": True, "extra": 1}, {"enabled": True, "revision": 0}, []):
        with pytest.raises(DomainError):
            owner_sessions.validate_section(section)
    owner_sessions.validate_section({"enabled": False})
    owner_sessions.validate_section({"enabled": True, "revision": 3})
    # Agents cannot introduce or change the section; an untouched section persists.
    with pytest.raises(DomainError) as denied:
        owner_sessions.prepare_owner_sessions({"owner_sessions": {"enabled": True}}, {}, principal_kind="agent")
    assert denied.value.code == "forbidden"
    kept = {}
    owner_sessions.prepare_owner_sessions(kept, {"owner_sessions": {"enabled": True}}, principal_kind="agent")
    assert kept == {"owner_sessions": {"enabled": True}}
    unchanged = {"owner_sessions": {"enabled": True}}
    owner_sessions.prepare_owner_sessions(unchanged, {"owner_sessions": {"enabled": True}}, principal_kind="agent")

    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        path = "/api/biz/widget/operations/business_config_patch"
        bad = client.post(path, headers=ORIGIN, json={"changes": {"owner_sessions": {"enabled": "yes"}},
                                                      "expected_revision": 1, "idempotency_key": new_id()})
        assert bad.status_code == 422, bad.text
        good = client.post(path, headers=ORIGIN, json={"changes": {"owner_sessions": {"enabled": True}},
                                                       "expected_revision": 1, "idempotency_key": new_id()})
        assert good.status_code == 200, good.text
    with service.fixture.db.read() as uow:
        row = uow.repository("businesses").get(service.fixture.business_id)
        assert owner_sessions.enabled(row["config_json"])
