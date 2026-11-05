"""Exercise summaries through real authentication, policy, domains and MCP wire."""
import pytest
import copy
from starlette.testclient import TestClient

from qorqut_operations.contracts import new_id
from bazarlyk.transport.mcp_presentation import description, present
from mcp_efficiency_support import EVIDENCE, isolated_service, seed_reads


@pytest.fixture
def service(tmp_path):
    with isolated_service(tmp_path) as value:
        yield value


def test_summary_service_smoke(service):
    with TestClient(service.web) as client:
        headers = service.initialize(client, service.manager)
        for operation in ("agents_activity_list", "agent_config_get"):
            result = service.tool(client, headers, operation, {"response_detail": "summary"})
            assert not result["isError"], result
            assert result["decoded"]["status"] == "ok", result


def test_real_focus_activity_and_context_preserve_evidence(service):
    records = seed_reads(service)
    with TestClient(service.web) as client:
        headers = service.initialize(client, service.ordu)
        def call(name, args):
            result = service.tool(client, headers, name, args)["decoded"]
            assert result["status"] == "ok", result
            return result
        page = call("focus_get", {"scope": "global", "limit": 2, "response_detail": "summary"})["data"]
        assert len(page["items"]) == 2 and page["next_offset"] == 2
        next_page = call("focus_get", {"scope": "global", "offset": page["next_offset"], "limit": 2,
                                      "response_detail": "summary"})["data"]
        assert next_page["next_offset"] is None
        assert len({row["id"] for row in page["items"] + next_page["items"]}) == 4
        reference = next(row for row in page["items"] + next_page["items"] if row["card_kind"] == "reference")
        assert reference["reference"]["target"]["url"] == "https://example.test/preview"
        assert reference["context"] == records["sources"][1]
        evidence = call("focus_get", {"scope": "global", "item_id": reference["id"], "conversation": True})
        assert EVIDENCE in str(evidence["data"]["item"]["thread"])
        scope = {"business_id": service.fixture.business_id}
        activity = call("agents_activity_list", {**scope, "response_detail": "summary"})["data"]
        agent = next(row for row in activity["agents"] if row["agent_id"] == service.agent.id)
        assert len(agent["sessions"]) == 2
        assert agent["work_items_replacement_safe"] is False
        for session in agent["sessions"]:
            assert session["work_revision"] == 2
            assert session["items"][0]["blocked"] is True
            assert session["checkpoint"]["issues"][0]["code"] == "work_review_missing"
        minimal = call("agent_config_get", {**scope, "response_detail": "summary"})["data"]["effective_config"]
        assert "mandatory_context" not in minimal
        assert "mandatory instructions" in minimal["not_included"]
        full = call("agent_config_get", {**scope, "response_detail": "full"})["data"]["effective_config"]
        context = call("agent_config_get", {**scope, "response_detail": "context"})["data"]["effective_config"]
        assert context["mandatory_context"]["rendered"] == full["mandatory_context"]["rendered"]

        # A lost source grant must redact before projection, not resurrect old
        # source identity, scope, title or authored reasons in summary mode.
        service.fixture.update(service.ordu, {"tool_policy": {"overrides": {"tasks_get": "deny"}}})
        redacted = call("focus_get", {"scope": "global", "response_detail": "summary"})["data"]["items"]
        assert redacted and all(row["unavailable"] for row in redacted)
        assert all(set(row) == {"id", "revision", "archived", "unavailable"} for row in redacted)


def test_batch_scope_owner_catalog_and_revocation(service):
    from test_owner_session_mcp import enable, issue, connect, call as owner_call
    with TestClient(service.web) as client:
        headers = service.initialize(client, service.agent)
        operations = [
            {"id": "activity", "operation": "agents_activity_list", "arguments": {"response_detail": "summary"}},
            {"id": "config", "operation": "agent_config_get", "arguments": {"response_detail": "summary"}},
            {"id": "foreign", "operation": "agents_activity_list", "arguments": {"response_detail": "summary", "business_id": new_id()}},
            {"id": "global", "operation": "focus_get", "arguments": {"scope": "global", "response_detail": "summary"}},
        ]
        batch = service.tool(client, headers, "operations_batch", {"operations": operations, "on_non_ok": "continue"})["decoded"]
        results = [row["result"] for row in batch["data"]["results"]]
        assert [row["status"] for row in results] == ["ok", "ok", "error", "error"]
        assert all(row["response_detail"] == "summary" for row in results[:2])
        assert all(row["error"]["code"] == "forbidden" for row in results[2:])
        for operation in operations[2:]:
            single = service.tool(client, headers, operation["operation"], operation["arguments"])["decoded"]
            assert single["error"]["code"] == "forbidden"
        enable(service.fixture)
        owner_headers = connect(client, issue(service.fixture))
        for name in ("agent_config_get", "focus_get", "agents_activity_list"):
            denied = owner_call(client, owner_headers, name, {"response_detail": "summary"})
            assert denied["error"]["code"] == "forbidden"
        with service.fixture.db.transaction() as uow:
            credential = uow.repository("credentials").list(filters={"principal_id": service.agent.id})[0]
            uow.repository("credentials").update(credential["id"], {"revoked_at": "2026-09-22T12:00:00Z"},
                expected_revision=credential["revision"])
        response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "agent_config_get", "arguments": {"response_detail": "summary"}}})
        assert response.status_code == 401


def test_unavailable_and_mixed_checkpoint_summaries_are_lossless():
    unavailable = {"id": "card", "revision": 4, "archived": False, "unavailable": True}
    result = present("focus_get", {"status": "ok", "data": {"items": [unavailable]}}, "summary")
    assert result["data"]["items"] == [unavailable]
    identified = {"finding_id": "f", "code": "known", "message": "Keep this observation"}
    anonymous = {"code": "unknown", "message": "Keep this one too", "correction_attempted": False}
    value = {"status": "ok", "data": {"agents": [{"agent_id": "a", "sessions": [
        {"session_id": "s1", "checkpoint": {"issues": [identified], "outstanding_count": 1}},
        {"session_id": "s2", "checkpoint": {"issues": [anonymous], "outstanding_count": 1}},
    ]}]}}
    before = copy.deepcopy(value)
    sessions = present("agents_activity_list", value, "summary")["data"]["agents"][0]["sessions"]
    assert [session["checkpoint"]["issues"] for session in sessions] == [[identified], [anonymous]]
    assert value == before
    # Even matching identities cannot justify discarding different observations.
    value["data"]["agents"][0]["sessions"][1]["checkpoint"]["issues"][0] = {**identified, "message": "New observation"}
    result = present("agents_activity_list", value, "summary")["data"]["agents"][0]
    assert [s["checkpoint"]["issues"][0]["message"] for s in result["sessions"]] == ["Keep this observation", "New observation"]


def test_activity_global_scope_is_not_elided():
    value = {"status": "ok", "data": {"agents": [{"agent_id": "a", "business_id": None, "sessions": []}]}}
    result = present("agents_activity_list", value, "summary")["data"]["agents"][0]
    assert "business_id" in result and result["business_id"] is None


@pytest.mark.parametrize("preparation,problem", [
    ({"ready": False, "status": "unprepared", "projection_drift": [], "missing_artifacts": []}, True),
    ({"ready": True, "status": "ready", "projection_drift": [], "missing_artifacts": []}, False),
    (None, False),
])
def test_readiness_is_explicit_and_not_instruction_delivery(preparation, problem):
    value = {"status": "ok", "data": {"effective_config": {"preparation": preparation}}}
    result = present("agent_config_get", value, "summary")["data"]["effective_config"]
    assert result["readiness"]["problem"] is problem
    assert "ready" in result["readiness"]
    assert "mandatory instructions" in result["not_included"]
    guidance = description("agent_config_get")
    assert "Before entering another project" in guidance and "without sections" in guidance
