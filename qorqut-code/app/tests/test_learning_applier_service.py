from dataclasses import replace
from pathlib import Path

from starlette.testclient import TestClient

from bazarlyk.agents.context import MAX_CONTEXT_CHARACTERS
from bazarlyk.bootstrap.learning_applier import prepare_learning_applier
from qorqut_operations.contracts import Principal, new_id
from test_service_transports import ServiceFixture


def test_standalone_applier_preparation_mcp_scope_tools_and_workflow(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    fixture, runtime = service.fixture, service.runtime
    second_business = new_id()
    second_hq = fixture.root / "second/hq"
    second_hq.mkdir(parents=True)
    with runtime.database.transaction() as uow:
        uow.repository("businesses").insert({"id": second_business, "slug": "second", "name": "Second",
            "workspace": str(second_hq.parent), "hq_path": str(second_hq)})
        uow.repository("business_memberships", business_id=second_business).insert({"principal_id": fixture.owner.id, "is_owner": True})
    fixture.ctx = replace(fixture.ctx, allowed_business_ids=frozenset({fixture.business_id, second_business}))
    args = dict(role_ref=fixture.refs["learning-applier"], allowed_business_ids=[fixture.business_id, second_business],
        endpoint="http://127.0.0.1:9988/mcp", claude_version="2.1.263")
    prepared = prepare_learning_applier(runtime, fixture.ctx, **args)
    assert prepared["ready"], prepared
    workspace = Path(prepared["workspace"])
    assert workspace == fixture.root / "agents/learning-applier"
    agent = Principal(prepared["agent_id"], "agent")
    again = prepare_learning_applier(runtime, fixture.ctx, **args)
    assert again["agent_id"] == prepared["agent_id"]
    assert (workspace / ".claude/skills/bazarlyk-role-learning-application/references/workflow.md").is_file()
    assert (workspace / ".claude/skills/bazarlyk-role-learning-application/references/review.md").is_file()
    config = runtime.resolver.resolve_agent_config(agent).to_dict()
    assert config["business_id"] is None and config["role"]["id"] == "learning-applier" and not config["plugins"]
    startup = config["mandatory_context"]["rendered"]
    assert "Read agents_list and tasks_list" not in startup and "Coordinate through tasks" not in startup
    assert "learnings_work_list" in startup and "context_search" in startup
    assert "learnings_review_list" in startup
    assert {x["name"] for x in config["skills"]} == {"role", "bazarlyk-role-learning-application", "bazarlyk-role-owner-input"}
    with runtime.database.transaction() as uow:
        for business in (fixture.business_id, second_business):
            uow.repository("learning_candidates", business_id=business).insert({"id": "delta",
                "candidate_json": {"text": "Keep owner correction", "scope": "global", "bucket": "delta", "tag": "rule"}})
    for business in (fixture.business_id, second_business):
        accepted = runtime.application.invoke(fixture.ctx.select_business(business), "learnings_review",
            {"learning_id": "delta", "state": "accepted", "feedback": "Keep this note"},
            {"expected_revision": 1, "idempotency_key": new_id()})
        assert accepted.status == "ok", accepted.to_dict()
    service.tokens[agent.id] = (workspace / ".bazarlyk/credential").read_text().strip()
    with TestClient(service.web) as client:
        headers = service.initialize(client, agent)
        listed = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
        names = {tool["name"] for tool in listed}
        assert {"learnings_work_list", "learnings_work_done", "context_list", "context_search", "context_get"} <= names
        assert {"learnings_review_list", "learnings_review_get", "learnings_decide"} <= names
        completion_schema = next(tool["inputSchema"] for tool in listed if tool["name"] == "learnings_work_done")
        assert "agent_note" in completion_schema["required"]
        denied = {"tasks_create", "tasks_assign", "signoffs_platform_submit", "ledger_add", "agents_create", "routines_create", "learnings_review", "learnings_edit", "learnings_keep_both"}
        assert not names & denied
        for name in denied:
            result = service.tool(client, headers, name, {"business_id": fixture.business_id, "idempotency_key": new_id()})
            assert result["isError"], (name, result)
        agenda = service.tool(client, headers, "learnings_work_list", {})["decoded"]
        assert agenda["status"] == "ok", agenda
        assert {(row["business_id"], row["id"]) for row in agenda["data"]["items"]} == {
            (fixture.business_id, "delta"), (second_business, "delta")}
        first_page = service.tool(client, headers, "learnings_work_list", {"limit": 1})["decoded"]["data"]
        second_page = service.tool(client, headers, "learnings_work_list",
            {"limit": 1, "cursor": first_page["next_cursor"]})["decoded"]["data"]
        assert first_page["items"][0]["business_id"] != second_page["items"][0]["business_id"]
        assert first_page["counts"]["accepted"] == 2 and second_page["next_cursor"] is None
        def tool(name, arguments):
            result = service.tool(client, headers, name, {"business_id": fixture.business_id, **arguments})["decoded"]
            assert result["status"] == "ok", result
            return result["data"]
        assert not tool("learnings_review_list", {})["items"]
        assert len(service.tool(client, headers, "learnings_review_list", {"states": ["accepted"]})["decoded"]["data"]["items"]) == 2
        evidence = tool("learnings_review_get", {"learning_id": "delta"})
        assert evidence["item"]["decision"]["note"] == "Keep this note"
        assert not evidence["original_transcripts_included"] and evidence["complete"]
        forbidden = service.tool(client, headers, "learnings_review_get", {"business_id": new_id(), "learning_id": "delta"})["decoded"]
        assert forbidden["error"]["code"] == "forbidden"
        assert tool("learnings_work_get", {"learning_id": "delta"})["item"]["decision"]["note"] == "Keep this note"
        item = tool("learnings_work_start", {"learning_id": "delta", "expected_revision": 2, "idempotency_key": new_id()})["item"]
        assert item["state"] == "in-progress"
        item = tool("learnings_work_done", {"learning_id": "delta", "expected_revision": 3, "idempotency_key": new_id(),
            "outcome": "already-covered", "reason": "Existing rule verified in source.",
            "agent_note": "Verified the existing rule covers the correction; no source changes needed."})["item"]
        assert item["state"] == "implemented" and item["agent_note"].startswith("Verified the existing rule")
        assert not tool("learnings_work_list", {})["items"]
        remaining = service.tool(client, headers, "learnings_work_list", {})["decoded"]["data"]["items"]
        assert len(remaining) == 1 and remaining[0]["business_id"] == second_business
        shared = service.tool(client, headers, "context_list", {"scope": "shared"})["decoded"]
        assert shared["status"] == "ok" and shared["data"]["items"], shared
        assert all("body" not in row for row in shared["data"]["items"])
        own_notes = service.tool(client, headers, "notes_create", {
            "title": "Session", "body": "Private implementation notes", "idempotency_key": new_id()})["decoded"]
        assert own_notes["status"] == "ok", own_notes
        assert own_notes["data"]["document"]["business_id"] is None
        assert "related_business_id" not in own_notes["data"]["document"]["metadata"]
        selected_notes = service.tool(client, headers, "notes_create", {
            "business_id": fixture.business_id, "title": "Mis-scoped session",
            "body": "This must not land in Global", "idempotency_key": new_id()})["decoded"]
        assert selected_notes["status"] == "ok", selected_notes
        selected_document = selected_notes["data"]["document"]
        assert selected_document["business_id"] is None
        assert selected_document["metadata"]["related_business_id"] == fixture.business_id
        assert "business_slug" not in selected_document["metadata"]
        assert selected_document["related_business"] == {"business_id": fixture.business_id,
            "business_slug": "widget", "business_name": "Widget"}
        listed = service.tool(client, headers, "notes_list", {
            "business_id": fixture.business_id})["decoded"]
        assert listed["status"] == "ok", listed
        assert {row["id"] for row in listed["data"]["items"]} == {selected_document["id"]}
        assert not tool("context_search", {"query": "Private implementation notes", "scope": "all"})["items"]
        with runtime.database.read() as uow:
            owner_context = runtime.policy.context_for(fixture.owner.id, business_id=None,
                review_business_id=None, uow=uow)
        owner_list = runtime.application.invoke(owner_context, "notes_list", {})
        assert owner_list.status == "ok", owner_list.to_dict()
        assert not {row["id"] for row in owner_list.data["items"]} & {own_notes["data"]["document"]["id"], selected_document["id"]}
        owner_read = runtime.application.invoke(owner_context, "notes_get", {"document_id": selected_document["id"]})
        assert owner_read.status == "ok", owner_read.to_dict()
        other = service.tool(client, headers, "context_list", {"business_id": new_id()})["decoded"]
        assert other["status"] == "error" and other["error"]["code"] == "forbidden"
        rows = client.get("/api/biz/widget/agents").json()
        assert all(row["agent_id"] != agent.id for row in rows)
        denied_task = service.tool(client, headers, "tasks_create", {"business_id": fixture.business_id,
            "title": "This should be denied", "idempotency_key": new_id()})["decoded"]
        assert denied_task["error"]["code"] == "policy_denied"
    assert config["mandatory_context"]["character_count"] <= MAX_CONTEXT_CHARACTERS
    assert "Establish the evidence" not in startup  # Detailed review loads on demand.
