from dataclasses import replace
import json
from pathlib import Path
import time

import pytest
from starlette.testclient import TestClient

from qorqut_operations.contracts import new_id
from bazarlyk.domains.tasks import TaskOperations
from bazarlyk.service.runtime import Runtime, build_runtime
from bazarlyk.service.web import create_app
from bazarlyk.transport.auth import Authentication, human_global_capabilities
from test_agent_configuration import Fixture, REPO, PATHS

OWNER_TOKEN = "owner-bootstrap-secret-" + "x" * 40
ORIGIN = {"Origin": "http://testserver"}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class ServiceFixture:
    def __init__(self, root, *, full=False, model_client_factory=None, dev=False, learning_engine=None):
        self.fixture = Fixture(root)
        fixture = self.fixture
        TaskOperations().register(fixture.registry)
        # The business's Onbashy and its Global coordinator.
        self.agent = fixture.create()
        self.manager = fixture.create("manager", "ordubashy")
        # Its Dev peer, when a test needs both business roles.
        self.dev = fixture.create(role="dev") if dev else None
        application, signoffs = fixture.application()
        application.policy.human_global_capabilities = human_global_capabilities
        with fixture.db.transaction() as uow:
            uow.repository("installation").insert({"id": "service", "installation_id": new_id(), "owner_principal_id": fixture.owner.id})
        if full:
            from bazarlyk.background.integration import attach_background
            from qorqut_operations.background.programs import ProgramRegistry
            from bazarlyk.service.local import LocalFiles
            programs = ProgramRegistry()
            complete = build_runtime(fixture.root, REPO, paths=PATHS, database=fixture.db, job_programs=programs)
            attach_background(complete, programs, transcript_source=LocalFiles(complete).transcripts,
                model_client_factory=model_client_factory, learning_engine=learning_engine)
            with fixture.db.transaction() as uow:
                complete.background.authority.register_workers(uow, [fixture.business_id])
        else:
            complete = None
        if complete:
            fixture.resolver, fixture.registry, fixture.ops = complete.resolver, complete.registry, complete.agents
        self.tokens, self.registrations = {}, {}
        for principal in (self.agent, self.manager, *([self.dev] if dev else [])):
            ready = fixture.prepare(principal)
            self.tokens[principal.id] = (Path(ready["workspace"]) / ".bazarlyk/credential").read_text().strip()
            self.registrations[principal.id] = ready["registration_id"]
        self.runtime = complete or Runtime(fixture.db, fixture.root, REPO, fixture.registry, fixture.resolver,
            application.policy, application, fixture.ops, fixture.catalog, fixture.publisher, None,
            application.file_executor.file_applier, signoffs, paths=PATHS)
        self.authentication = Authentication(fixture.db, fixture.root, bootstrap_token=OWNER_TOKEN, signing_key=b"k" * 48)
        from bazarlyk.transport.compatibility import DashboardCompatibility
        self.web = create_app(self.runtime, origins=["http://testserver"], authentication=self.authentication,
            compatibility=DashboardCompatibility(self.runtime, background=self.runtime.background) if full else None)

    def login(self, client):
        result = client.get("/api/session")
        assert result.status_code == 200, result.text
        assert "set-cookie" not in result.headers
        return result.json()["data"]

    def initialize(self, client, principal):
        headers = {**MCP_HEADERS, "Authorization": "Bearer " + self.tokens[principal.id]}
        result = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        assert result.status_code == 200, result.text
        headers["Mcp-Session-Id"] = result.headers["mcp-session-id"]
        headers["MCP-Protocol-Version"] = result.json()["result"]["protocolVersion"]
        result = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert result.status_code == 202, result.text
        return headers

    @staticmethod
    def tool(client, headers, name, arguments):
        result = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": new_id(), "method": "tools/call",
            "params": {"name": name, "arguments": arguments}})
        assert result.status_code == 200, result.text
        wire = result.json()["result"]
        assert "structuredContent" not in wire
        assert len(wire["content"]) == 1 and wire["content"][0]["type"] == "text"
        # Test convenience only: decoded is not a field on the MCP wire.
        return {**wire, "decoded": json.loads(wire["content"][0]["text"])}

    def close(self):
        self.runtime.database.close()


@pytest.fixture
def service(tmp_path):
    fixture = ServiceFixture(tmp_path)
    try:
        yield fixture
    finally:
        # Drain the connection pool explicitly: tmp_path is removed once this
        # fixture tears down, and a pooled connection must not outlive it.
        fixture.close()


def test_business_terminal_route_receives_decoded_configuration(service):
    from starlette.responses import JSONResponse

    config = {"browser": {"enabled": True}, "engineering": {"github_organizations": ["fixture-org"]}}
    with service.fixture.db.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET config_json=? WHERE id=?",
            (json.dumps(config), service.fixture.business_id))

    class TerminalBoundary:
        async def route(self, request, context, business, tail):
            assert business["id"] == context.business_id == service.fixture.business_id
            assert business["config_json"] == config
            return JSONResponse({"ok": True})

    web = create_app(service.runtime, origins=["http://testserver"],
        authentication=service.authentication, terminals=TerminalBoundary())
    with TestClient(web) as client:
        result = client.post("/api/biz/widget/terminal/agent/start", headers=ORIGIN, json={})
        assert result.status_code == 200, result.text
        assert result.json() == {"ok": True}


def test_owner_dashboard_without_auth_keeps_host_origin_and_authority_boundary(service):
    with TestClient(service.web) as client:
        assert client.get("/health/live").json() == {"live": True}
        assert client.get("/api/session").status_code == 200
        assert not client.cookies
        session = service.login(client)
        assert session == {"principal_id": service.fixture.owner.id, "principal_kind": "human", "display_name": "Owner", "is_owner": True, "is_installation_owner": True}
        body = {"agent_id": service.agent.id, "overrides": {}, "expected_revision": 1, "idempotency_key": new_id()}
        path = "/api/biz/widget/operations/permissions_set"
        assert client.post(path, json=body).status_code == 403
        assert client.post(path, json=body, headers={"Origin": "http://other.invalid"}).status_code == 403
        assert client.get("/api/session", headers={"Host": "other.invalid"}).status_code == 403
        good = client.post(path, json=body, headers=ORIGIN)
        assert good.status_code == 200, good.text


def test_mcp_authenticates_every_request_and_binds_transport_session(service):
    with TestClient(service.web) as client:
        assert client.post("/mcp", json={}).status_code == 401
        first = service.initialize(client, service.agent)
        second = service.initialize(client, service.manager)
        cross = {**second, "Mcp-Session-Id": first["Mcp-Session-Id"]}
        assert client.post("/mcp", headers=cross, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).status_code == 404
        assert client.post("/mcp?token=" + service.tokens[service.agent.id], headers=first, json={}).status_code == 400
        result = client.post("/mcp", headers=first, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {row["name"] for row in result.json()["result"]["tools"]}
        assert "tasks_create" in names
        assert "agents_create" not in names
        assert "signoffs_review" not in names
        assert "reddit_post" not in names
        forbidden = service.tool(client, first, "signoffs_review", {})
        assert forbidden["isError"] is True
        assert forbidden["decoded"]["error"]["code"] in {"forbidden", "operation_unavailable"}
        with service.fixture.db.transaction() as uow:
            credential = uow.repository("credentials").list(filters={"principal_id": service.agent.id})[0]
            uow.repository("credentials").update(credential["id"], {"revoked_at": "2026-09-08T00:00:00Z"}, expected_revision=credential["revision"])
        assert client.post("/mcp", headers=first, json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"}).status_code == 401


def test_http_and_mcp_share_tasks_identity_policy_and_unknown_field_rejection(service):
    with TestClient(service.web) as client:
        service.login(client)
        headers = service.initialize(client, service.agent)
        key = new_id()
        created = service.tool(client, headers, "tasks_create", {"title": "Real shared task", "idempotency_key": key})
        assert created["isError"] is False, created
        assert created["decoded"]["status"] == "ok"
        task = created["decoded"]["data"]["task"]
        assert task["assignee_ids"] == [service.agent.id]
        read = client.post("/api/biz/widget/operations/tasks_get", json={"task_id": task["id"]}, headers=ORIGIN)
        assert read.json()["data"]["task"]["created_by"] == service.agent.id
        forged = service.tool(client, headers, "tasks_create", {"title": "Spoof", "actor": service.fixture.owner.id, "idempotency_key": new_id()})
        assert forged["decoded"]["error"]["code"] == "validation_error"
        scoped = service.tool(client, headers, "tasks_create", {"title": "Foreign", "business_id": new_id(), "idempotency_key": new_id()})
        assert scoped["decoded"]["error"]["code"] == "forbidden"
        hook = {"registration_id": service.registrations[service.agent.id], "session_id": "one", "event": "startup", "actor": "owner"}
        denied = client.post("/api/agent/bootstrap", json=hook, headers={"Authorization": "Bearer " + service.tokens[service.agent.id]})
        assert denied.status_code == 422


def test_owner_approval_applies_agent_proposal_without_agent_connection(service):
    with TestClient(service.web) as client:
        service.login(client)
        # Exercise approval transport independently of the role's default grant.
        gated = client.post("/api/biz/widget/operations/permissions_set", headers=ORIGIN, json={
            "agent_id": service.agent.id, "overrides": {"tasks_create": "signoff"},
            "expected_revision": 1, "idempotency_key": new_id()})
        assert gated.status_code == 200, gated.text
        headers = service.initialize(client, service.agent)
        proposed = service.tool(client, headers, "tasks_create", {"title": "Reviewed task", "idempotency_key": new_id()})
        assert proposed["isError"] is False, proposed
        result = proposed["decoded"]
        assert result["status"] == "signoff_required", result
        def titles():
            with service.fixture.db.read() as uow:
                return {row[0] for row in uow.connection.execute("SELECT title FROM tasks")}
        assert "Reviewed task" not in titles()
        client.delete("/mcp", headers=headers)
        reference = result["signoff"]
        filtered = client.get("/api/biz/widget/signoffs", params={
            "category": "internal_change", "execution_states": "not_started,conflicted"})
        assert filtered.status_code == 200, filtered.text
        assert reference["id"] in {item["id"] for item in filtered.json()["data"]["items"]}
        decision = {key: reference[key] for key in ("proposal_revision", "payload_hash")}
        decision.update(expected_status_revision=reference["status_revision"], idempotency_key=new_id())
        path = "/api/biz/widget/signoffs/" + reference["id"]
        approved = client.post(path + "/approve", json=decision, headers=ORIGIN)
        assert approved.status_code == 202, approved.text
        for _ in range(100):
            detail = client.get(path).json()["data"]["signoff"]
            if detail["state"] != "approved":
                break
            time.sleep(0.02)
        assert detail["state"] == "applied", detail
        assert "Reviewed task" in titles()
        assert detail["proposer_display_name"] == service.agent.display_name


def test_complete_runtime_preserves_dashboard_tasks_documents_and_assets(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        service.login(client)
        businesses = client.get("/api/biz")
        assert businesses.status_code == 200, businesses.text
        assert businesses.json()[0]["agents"] == ["business-agent"]
        roster = client.get("/api/biz/widget/agents")
        assert roster.status_code == 200, roster.text
        assert {row["agent_id"] for row in roster.json()} == {service.agent.id}
        task = client.post("/api/biz/widget/task", json={"title": "An owner task", "owner": "agent:business-agent"}, headers=ORIGIN)
        assert task.status_code == 201, task.text
        assert task.json()["task"]["assignee_ids"] == [service.agent.id]
        tasks = client.get("/api/biz/widget/tasks")
        assert tasks.status_code == 200, tasks.text
        assert tasks.json()[0]["owner"] == "agent:business-agent"
        for domain in ("notes",):
            created = client.post("/api/biz/widget/" + domain, json={"title": "Owner text", "content": "Exact authored bytes."}, headers=ORIGIN)
            assert created.status_code == 422, created.text
            assert created.json()["error"]["code"] == "scope_required"
        upload = client.post("/api/biz/widget/assets/upload", data={"group": "drafted", "notes": "A visible asset"}, files={"file": ("pixel.png", b"PNG bytes", "image/png")}, headers=ORIGIN)
        assert upload.status_code == 200, upload.text
        path = upload.json()["path"]
        assert client.get("/api/biz/widget/asset/" + path).content == b"PNG bytes"
        groups = client.get("/api/biz/widget/assets")
        assert groups.json()["drafted"][0]["notes"] == "A visible asset"
        tree = client.get("/api/biz/widget/context")
        assert tree.status_code == 200, tree.text
        assert len(tree.json()["pillars"]) == 3
        assert client.get("/api/biz/widget/context/file", params={"p": "../../.env"}).status_code == 422
        assert client.post("/api/biz/widget/chat", json={"agent": "business-agent", "message": "do work"}, headers=ORIGIN).status_code == 410


def test_complete_runtime_platform_gate_and_owner_review(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    fixture = service.fixture
    with fixture.db.transaction() as uow:
        business = uow.repository("businesses").get(fixture.business_id)
        uow.repository("businesses").update(fixture.business_id, {"config_json": {"accounts": {"reddit": [{"id": "reddit-founder", "handle": "founder", "manual": True}]}}}, expected_revision=business["revision"])
    reddit = service.agent
    fixture.enable_reddit(reddit)
    fixture.update(reddit, {"tool_policy": {"overrides": {"signoffs_platform_submit": "allow"}}})
    ready = fixture.prepare(reddit)
    service.tokens[reddit.id] = (Path(ready["workspace"]) / ".bazarlyk/credential").read_text().strip()
    with TestClient(service.web) as client:
        service.login(client)
        headers = service.initialize(client, reddit)
        listing = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
        assert any(row["name"] == "signoffs_platform_submit" for row in listing)
        assert not any(row["name"] in {"reddit_post", "requests_create", "requests_review"} for row in listing)
        payload = {"subreddits": ["ClaudeAI"], "title": "A tested workflow", "body": "Current evidence.", "flair": "Built with Claude"}
        args = {"plugin_id": "reddit-organic", "tool": "reddit.post", "payload": payload, "why": "Share a useful result", "idempotency_key": new_id()}
        submitted = service.tool(client, headers, "signoffs_platform_submit", args)["decoded"]
        assert submitted["status"] == "signoff_required", submitted
        identity = submitted["signoff"]["id"]
        read = client.get("/api/biz/widget/signoffs/" + identity)
        assert read.status_code == 200, read.text
        row = read.json()["data"]["signoff"]
        assert row["proposer_agent_id"] == reddit.id
        assert row["platform"]["submission"]["payload"] == payload
        assert client.get("/api/biz/widget/requests/" + identity).status_code == 404
        review = client.post("/api/biz/widget/signoffs/" + identity + "/approve", headers=ORIGIN, json={
            "proposal_revision": row["proposal_revision"], "payload_hash": row["payload_hash"],
            "expected_status_revision": row["status_revision"], "idempotency_key": new_id()})
        assert review.status_code == 202, review.text
        plain_headers = service.initialize(client, service.manager)
        denied = service.tool(client, plain_headers, "signoffs_platform_submit", {**args,"idempotency_key":new_id()})
        assert denied["decoded"]["status"] == "error"


def test_owner_secret_route_is_scoped_atomic_and_not_identity(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        service.login(client)
        saved = client.post("/api/biz/widget/env", json={"pairs": [{"key": "DEEPSEEK_API_KEY", "value": "business-only"}]}, headers=ORIGIN)
        assert saved.status_code == 200, saved.text
        assert (service.fixture.hq / ".env").stat().st_mode & 0o777 == 0o600
        invalid = client.post("/api/biz/widget/env", json={"pairs": [{"key": "BIZ_ACTOR", "value": "human:owner"}]}, headers=ORIGIN)
        assert invalid.status_code == 422
        assert client.get("/api/biz/widget/env").json()["pairs"] == [{"key": "DEEPSEEK_API_KEY", "value": "business-only"}]
        with service.fixture.db.transaction() as uow:
            membership = uow.repository("business_memberships", business_id=service.fixture.business_id).get(service.fixture.owner.id)
            uow.repository("business_memberships", business_id=service.fixture.business_id).update(service.fixture.owner.id, {"is_owner": False}, expected_revision=membership["revision"])
        assert client.get("/api/biz/widget/env").status_code == 403
