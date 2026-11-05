import asyncio
import json

import pytest
from starlette.testclient import TestClient

from qorqut_operations.contracts import DomainError, OperationResult, new_id
from bazarlyk.application.registry import OperationDescriptor
from bazarlyk.transport import batches
from test_service_transports import ORIGIN, ServiceFixture
from test_signoff_inputs import FORM


def entry(label, operation="tasks_create", **arguments):
    if operation == "tasks_create":
        arguments = {"title": label, "idempotency_key": new_id(), **arguments}
    return {"id": label, "operation": operation, "arguments": arguments}


def call(service, client, headers, operations, **options):
    return service.tool(client, headers, "operations_batch", {"operations": operations, **options})["decoded"]


def test_ten_story_tasks_and_mixed_work_in_single_requests(tmp_path):
    s = ServiceFixture(tmp_path, full=True)
    with TestClient(s.web) as client:
        headers = s.initialize(client, s.agent)
        story = s.tool(client, headers, "stories_create", {"title": "Batch story", "purpose": "Test ordered writes",
            "idempotency_key": new_id()})["decoded"]["data"]["story"]
        operations = [entry("task_" + str(i), story_ids=[story["id"]]) for i in range(10)]
        first = call(s, client, headers, operations)
        assert first["status"] == "ok" and first["data"]["all_ok"], first
        assert first["data"]["attempted"] == 10
        rows = first["data"]["results"]
        assert [row["id"] for row in rows] == [op["id"] for op in operations]
        ids = [row["result"]["data"]["task"]["id"] for row in rows]
        assert len(set(ids)) == 10
        assert all(row["result"]["response_detail"] == "compact" for row in rows)
        repeated = call(s, client, headers, operations)
        assert [row["result"]["data"]["task"]["id"] for row in repeated["data"]["results"]] == ids
        full = s.tool(client, headers, "tasks_create", {**operations[0]["arguments"], "response_detail": "full"})["decoded"]
        assert full["data"]["task"]["id"] == ids[0] and "notes" in full["data"]["task"]
        linked = s.tool(client, headers, "stories_get", {"story_id": story["id"]})["decoded"]
        assert all(task_id in json.dumps(linked["data"]) for task_id in ids)
        with s.fixture.db.read() as uow:
            assert uow.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 10

        mixed = [entry("one"), entry("two"),
            entry("question", "signoffs_input_create", request_key="batch-question", form=FORM, idempotency_key=new_id()),
            entry("message", "agents_message_send", to_agent_id=s.manager.id,
                message="Please review the owner question; no answer yet.", idempotency_key=new_id())]
        result = call(s, client, headers, mixed)
        assert result["data"]["all_ok"], result
        assert result["data"]["attempted"] == 4
        assert result["data"]["results"][2]["result"]["data"]["signoff"]["state"] == "pending"
        replay = call(s, client, headers, mixed)
        assert [row["result"]["data"] for row in replay["data"]["results"]] == [row["result"]["data"] for row in result["data"]["results"]]


def test_partial_failure_stop_continue_and_envelope_preflight(tmp_path):
    s = ServiceFixture(tmp_path)
    with TestClient(s.web) as client:
        headers = s.initialize(client, s.agent)
        good = entry("kept")
        invalid_envelopes = [[], [entry(str(i)) for i in range(21)], [good, good],
            [good, entry("nested", "operations_batch", operations=[entry("child")])],
            [good, {"id": "bad", "operation": "tasks_create", "arguments": []}]]
        for operations in invalid_envelopes:
            assert call(s, client, headers, operations)["error"]["code"] == "validation_error"
        assert call(s, client, headers, [good], idempotency_key=new_id())["error"]["code"] == "validation_error"
        with s.fixture.db.read() as uow:
            assert uow.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        bad = entry("bad", title=3)
        last = entry("later")
        result = call(s, client, headers, [good, bad, last])
        assert result["status"] == "error" and result["error"]["code"] == "batch_incomplete"
        assert result["data"]["attempted"] == 2
        rows = result["data"]["results"]
        assert rows[0]["result"]["status"] == "ok"
        assert rows[1]["result"]["error"]["code"] == "validation_error"
        assert rows[2]["state"] == "not_executed"
        continued = call(s, client, headers, [good, bad, last], on_non_ok="continue")
        assert continued["status"] == "error" and continued["data"]["attempted"] == 3
        assert continued["data"]["results"][2]["result"]["status"] == "ok"
        with s.fixture.db.read() as uow:
            assert uow.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2


def test_inner_permissions_scope_revision_keys_and_owner_boundary(tmp_path):
    s = ServiceFixture(tmp_path)
    s.fixture.update(s.agent, {"tool_policy": {"overrides": {"tasks_assign": "deny"}}})
    s.runtime.registry.register(OperationDescriptor("test_worker_read", {"type": "object"},
        effect_kind="read", supports_signoff=False, exposure=frozenset({"worker"}), read=lambda *_: {"must_not_run": True}))
    with TestClient(s.web) as client:
        headers = s.initialize(client, s.agent)
        created = s.tool(client, headers, "tasks_create", entry("existing")["arguments"])["decoded"]["data"]["task"]
        operations = [entry("no_key", "tasks_create", title="Missing key"),
            entry("foreign", business_id=new_id()),
            entry("owner", "signoffs_review"), entry("unknown", "not_a_real_operation"),
            entry("alias", "fixture_reddit_action", idempotency_key=new_id()),
            entry("denied", "tasks_assign", task_id=created["id"], assignee_ids=[s.manager.id], expected_revision=1, idempotency_key=new_id()),
            entry("stale", "tasks_update", task_id=created["id"], changes={"title": "Stale"}, expected_revision=999, idempotency_key=new_id()),
            entry("worker", "test_worker_read")]
        # The helper normally supplies create keys; remove it for this case.
        operations[0]["arguments"].pop("idempotency_key")
        result = call(s, client, headers, operations, on_non_ok="continue")
        assert result["data"]["attempted"] == len(operations)
        rows = result["data"]["results"]
        assert all(row["result"]["status"] == "error" for row in rows), rows
        assert rows[1]["result"]["error"]["code"] == "forbidden"
        assert rows[2]["result"]["error"]["code"] == "forbidden"
        assert rows[5]["result"]["error"]["code"] == "policy_denied"
        assert rows[6]["result"]["error"]["code"] == "revision_conflict"
        assert rows[7]["result"]["error"]["code"] == "forbidden"
        with s.fixture.db.read() as uow:
            assert uow.connection.execute("SELECT title FROM tasks").fetchone()[0] == "existing"


def test_approval_stops_by_default_and_is_never_reported_as_applied(tmp_path):
    s = ServiceFixture(tmp_path)
    s.fixture.update(s.agent, {"tool_policy": {"overrides": {"tasks_create": "signoff"}}})
    with TestClient(s.web) as client:
        headers = s.initialize(client, s.agent)
        operations = [entry("proposal"), entry("read", "tasks_list")]
        result = call(s, client, headers, operations)
        assert result["data"]["attempted"] == 1 and not result["data"]["all_ok"]
        proposal = result["data"]["results"][0]["result"]
        assert proposal["status"] == "signoff_required" and proposal["signoff"]["id"]
        continued = call(s, client, headers, operations, on_non_ok="continue")
        assert continued["data"]["attempted"] == 2 and not continued["data"]["all_ok"]
        assert continued["data"]["results"][0]["result"]["signoff"] == proposal["signoff"]
        assert continued["warnings"]
        with s.fixture.db.read() as uow:
            assert uow.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_batch_discovery_denial_and_direct_invocation_closed(tmp_path):
    s = ServiceFixture(tmp_path)
    ctx = s.runtime.resolver.invocation_context(s.agent)
    direct = s.runtime.application.invoke(ctx, "operations_batch", {"operations": [entry("direct")]})
    assert direct.status == "error" and direct.error["code"] == "operation_unavailable"
    with TestClient(s.web) as client:
        headers = s.initialize(client, s.agent)
        listed = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"}).json()["result"]["tools"]
        schema = next(tool for tool in listed if tool["name"] == "operations_batch")
        assert schema["inputSchema"]["required"] == ["operations"]
        assert not schema["annotations"]["idempotentHint"]
        assert not schema["annotations"]["readOnlyHint"]
        with pytest.raises(DomainError):
            s.fixture.update(s.agent, {"tool_policy": {"overrides": {"operations_batch": "signoff"}}})
        s.fixture.update(s.agent, {"tool_policy": {"overrides": {"operations_batch": "deny"}}})
        assert call(s, client, headers, [entry("denied")])["error"]["code"] == "policy_denied"
        listed = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 10, "method": "tools/list"}).json()["result"]["tools"]
        assert "operations_batch" not in {tool["name"] for tool in listed}
        refused = client.post("/api/biz/widget/operations/operations_batch", headers=ORIGIN,
            json={"operations": [entry("dashboard")]})
        assert refused.status_code >= 400


def test_global_agent_batch_uses_each_entrys_scope(tmp_path):
    s = ServiceFixture(tmp_path)
    with TestClient(s.web) as client:
        headers = s.initialize(client, s.manager)
        result = call(s, client, headers, [entry("global", "agent_config_get"),
            entry("business", business_id=s.fixture.business_id, assignee_ids=[s.agent.id])])
        assert result["data"]["all_ok"], result
        task = result["data"]["results"][1]["result"]["data"]["task"]
        with s.fixture.db.read() as uow:
            assert uow.connection.execute("SELECT business_id FROM tasks WHERE id=?", (task["id"],)).fetchone()[0] == s.fixture.business_id


@pytest.mark.parametrize("revoke", ["credential", "wrapper", "inner"])
def test_permissions_and_credentials_refresh_between_entries(tmp_path, monkeypatch, revoke):
    s = ServiceFixture(tmp_path)
    original = s.runtime.application.invoke
    calls = []
    def invoke(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(args[1])
        if len(calls) == 1:
            if revoke == "credential":
                with s.fixture.db.transaction() as uow:
                    credential = uow.repository("credentials").list(filters={"principal_id": s.agent.id})[0]
                    uow.repository("credentials").update(credential["id"], {"revoked_at": "2026-09-11T00:00:00Z"}, expected_revision=credential["revision"])
            else:
                name = "operations_batch" if revoke == "wrapper" else "tasks_create"
                s.fixture.update(s.agent, {"tool_policy": {"overrides": {name: "deny"}}})
        return result
    monkeypatch.setattr(s.runtime.application, "invoke", invoke)
    with TestClient(s.web) as client:
        headers = s.initialize(client, s.agent)
        result = call(s, client, headers, [entry("first"), entry("second")], on_non_ok="continue")
        rows = result["data"]["results"]
        assert rows[0]["result"]["status"] == "ok"
        if revoke == "inner":
            assert rows[1]["result"]["error"]["code"] == "policy_denied"
        else:
            assert rows[1]["state"] == "not_executed" and rows[1]["reason"] == "authorization_changed"
            assert len(calls) == 1
        with s.fixture.db.read() as uow:
            assert uow.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


@pytest.mark.parametrize("failure", ["budget", "slow_authorization", "authorization", "dispatch", "accepted"])
def test_runner_preserves_receipts_and_reports_skips(failure):
    ticks = [0]
    invoked = []
    async def authorize():
        if invoked and failure == "slow_authorization":
            ticks[0] += batches.START_BUDGET_SECONDS
        if invoked and failure == "authorization":
            raise RuntimeError("test unavailable auth")
        return object()
    async def invoke(name, arguments, actor):
        invoked.append(name)
        if len(invoked) == 2 and failure == "dispatch":
            raise RuntimeError("test failed dispatcher")
        if failure == "budget":
            ticks[0] += batches.START_BUDGET_SECONDS
        if failure == "accepted":
            return OperationResult(request_id=new_id(), status="accepted", job={"id": "job"}).to_dict()
        return OperationResult.ok({"receipt": len(invoked)}).to_dict()
    result = asyncio.run(batches.execute({"operations": [entry("a"), entry("b"), entry("c")]},
        invoke=invoke, authorize=authorize, clock=lambda: ticks[0]))
    assert result["data"]["results"][0]["state"] == "returned"
    assert result["data"]["results"][-1]["state"] == "not_executed"
    assert result["data"]["attempted"] == (2 if failure == "dispatch" else 1)
    assert not result["data"]["all_ok"]
