"""Dev, the business's software and design peer: discovery, real scoped calls, bootstrap and models."""

import json

import pytest
from starlette.testclient import TestClient

from bazarlyk.agents import roles
from bazarlyk.bootstrap.codex_config import role_model_default
from qorqut_operations.contracts import InvocationOptions, new_id
from test_service_transports import ServiceFixture

CAMPAIGN = {"stories_campaign_responsible_set", "campaign_attribution_set"}


@pytest.fixture
def s(tmp_path):
    service = ServiceFixture(tmp_path, full=True, dev=True)
    yield service
    service.runtime.stop()


def listed(s, client, principal):
    headers = s.initialize(client, principal)
    result = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    return {row["name"] for row in result.json()["result"]["tools"]}


def call(s, principal, operation, args, revision=None):
    ctx = s.runtime.resolver.invocation_context(principal, business_id=s.fixture.business_id)
    result = s.runtime.application.invoke(ctx, operation, args, InvocationOptions(
        business_id=s.fixture.business_id, expected_revision=revision, idempotency_key=new_id()))
    return result


def ok(result):
    assert result.status == "ok", result.to_dict()
    return result.data


def test_dev_discovers_business_operations_without_campaign_or_mail_plugins(s):
    with TestClient(s.web) as client:
        s.login(client)
        dev, onbashy = listed(s, client, s.dev), listed(s, client, s.agent)
    # Core defaults apply to Dev as to any business agent.
    assert {"inbox_list", "inbox_acknowledge", "agent_context_get", "working_memory_update", "agent_work_update",
            "business_briefing", "agents_message_send", "agents_message_recipients", "tasks_create", "tasks_update",
            "stories_create", "signoffs_list", "notes_create", "context_search", "context_instruct",
            "tables_rows_write", "dev_tickets_create"} <= dev
    assert dev <= onbashy and not CAMPAIGN & dev and CAMPAIGN <= onbashy
    profile = s.runtime.resolver.resolve_agent_config(s.dev).to_dict()
    assert profile["plugins"] == [] and profile["runtime"] == "claude-code"


def test_dev_policy_denies_campaign_ownership_even_when_called_directly(s):
    story = ok(call(s, s.dev, "stories_create", {"title": "Rebuild signup", "purpose": "Owner-selected project."}))["story"]
    result = call(s, s.dev, "stories_campaign_responsible_set", {"story_id": story["id"], "agent_id": None}, story["revision"])
    assert result.status == "error" and result.error["code"] != "validation_error", result.to_dict()
    allowed = call(s, s.agent, "stories_campaign_responsible_set", {"story_id": story["id"], "agent_id": None}, story["revision"])
    assert allowed.status != "error" or allowed.error["code"] != result.error["code"], allowed.to_dict()


def test_dev_works_through_scoped_messages_tasks_memory_and_notes(s):
    task = ok(call(s, s.agent, "tasks_create", {"title": "Fix the signup form", "assignee_ids": [s.dev.id]}))["task"]
    ok(call(s, s.dev, "tasks_comment", {"task_id": task["id"], "text": "Validation fixed; tests pass."}, task["revision"]))
    ok(call(s, s.dev, "agents_message_send", {"to_agent_id": s.agent.id, "message": "Signup form fix is live in staging.",
                                              "summary": "Signup fix live"}))
    # Ordubashy finds each business's Dev directly, as its briefing directory does.
    recipients = ok(call(s, s.manager, "agents_message_recipients", {"role_id": roles.DEV_ROLE}))["items"]
    assert [item["agent_id"] for item in recipients] == [s.dev.id]
    ok(call(s, s.manager, "agents_message_send", {"to_agent_id": s.dev.id, "message": "Status on the signup fix?",
                                                  "summary": "Signup status"}))
    note = ok(call(s, s.dev, "notes_create", {"title": "Use Postgres for this service", "body": "Settled.",
                                              "category": "engineering-decisions", "target_business_id": s.fixture.business_id}))
    assert note["document"]["metadata"]["author_id"] == s.dev.id


def test_dev_memory_survives_into_new_sessions_of_both_clients(s):
    from bazarlyk.bootstrap.preparation import prepare_agent
    prepared = prepare_agent(s.runtime.resolver, s.dev, endpoint="http://127.0.0.1:9988",
                             versions={"claude-code": "2.1.267 (Claude Code)", "codex": "codex-cli 0.153.4"})
    assert prepared["registration_id"] == s.registrations[s.dev.id]
    assert all(prepared["runtimes"][name]["ready"] for name in ("claude-code", "codex"))
    body = "Settled: use Postgres. Owner-selected project: signup form. Next: verify validation."
    args = {"body": body, "sources": [], "create_if_missing": True}
    legacy = ok(call(s, s.dev, "plans_get", {}))["document"]
    if legacy:
        args["legacy_plan_content_hash"] = legacy["content_hash"]
    ok(call(s, s.dev, "working_memory_update", args))
    assert ok(call(s, s.dev, "working_memory_get", {}))["document"]["body"] == body
    assert ok(call(s, s.dev, "agent_context_get", {}))["memory"]["body"] == body
    for runtime in ("claude-code", "codex"):
        result = s.runtime.resolver.bootstrap(s.dev, registration_id=s.registrations[s.dev.id],
                                              session_id=new_id(), event="startup", runtime=runtime)
        assert result.get("ready") is True, result
        assert body in result["work_briefing"]["rendered"], runtime


def test_dev_asks_the_owner_through_an_input_signoff(s):
    from test_signoff_inputs import FORM
    question = ok(call(s, s.dev, "signoffs_input_create", {"request_key": "dev-database-choice", "form": FORM}))["signoff"]
    assert question["category"] == "input_request"
    read = ok(call(s, s.dev, "signoffs_get", {"signoff_id": question["id"]}))
    assert read["signoff"]["id"] == question["id"] and read["signoff"]["category"] == "input_request"


def test_bootstrap_carries_the_review_role_and_acknowledged_time_on_every_delivery(s):
    session = new_id()
    ctx = s.runtime.resolver.invocation_context(s.dev)
    assert s.runtime.activity.register_session(ctx, s.registrations[s.dev.id], session, "claude-code")
    headers = {"Authorization": "Bearer " + s.tokens[s.dev.id]}
    body = {"registration_id": s.registrations[s.dev.id], "session_id": session, "runtime": "claude-code", "event": "startup"}
    with TestClient(s.web) as client:
        first = client.post("/api/agent/bootstrap", headers=headers, json=body)
        assert first.status_code == 200, first.text
        data = first.json()["data"]
        assert data["review"] == {"role_id": roles.DEV_ROLE, "work_reviewed_at": None}
        # An empty-patch review acknowledgement advances the time the hook watches.
        work = ok(s.runtime.application.invoke(ctx, "agent_work_update", {"session_id": session, "patch": [],
            "expected_work_revision": 1}, InvocationOptions(idempotency_key=new_id())))
        cached = client.post("/api/agent/bootstrap", headers=headers, json={**body, "event": "prompt",
            "last_effective_config_id": data["effective_config_id"]})
        review = cached.json()["data"]["review"]
        assert review["role_id"] == roles.DEV_ROLE and review["work_reviewed_at"] == work["work_reviewed_at"]
        onbashy = client.post("/api/agent/bootstrap", headers={"Authorization": "Bearer " + s.tokens[s.agent.id]},
            json={**body, "registration_id": s.registrations[s.agent.id], "session_id": new_id()})
        assert onbashy.json()["data"]["review"]["role_id"] == roles.BUSINESS_AGENT_ROLE


def test_dev_uses_the_clients_configured_codex_model():
    assert role_model_default("business", roles.DEV_ROLE) is None
    assert role_model_default("business", roles.BUSINESS_AGENT_ROLE) is not None


def test_dev_preparation_grants_the_business_code_root_and_orients_it(s):
    ready = s.fixture.prepare(s.dev)
    workspace = ready["workspace"]
    local = json.loads(open(workspace + "/.claude/settings.local.json").read())["permissions"]["allow"]
    assert "Bash" in local
    config = s.runtime.resolver.resolve_agent_config(s.dev).to_dict()
    assert config["workspace"] == str(roles.dev_workspace(s.fixture.hq))


def test_prepared_dev_context_is_role_neutral_and_owner_project_led(s):
    session = new_id()
    ctx = s.runtime.resolver.invocation_context(s.dev)
    assert s.runtime.activity.register_session(ctx, s.registrations[s.dev.id], session, "claude-code")
    with TestClient(s.web) as client:
        response = client.post("/api/agent/bootstrap", headers={"Authorization": "Bearer " + s.tokens[s.dev.id]},
                               json={"registration_id": s.registrations[s.dev.id], "session_id": session,
                                     "runtime": "claude-code", "event": "startup"})
    assert response.status_code == 200, response.text
    context = response.json()["data"]["mandatory_context"]
    context = context["rendered"] if isinstance(context, dict) else context
    scope = " ".join(context[context.index("## Scope Operating Brief"):context.index("## Role Brief")].split())
    # The shared business scope never tells Dev it is Onbashy or to hand its own implementation away.
    assert "You are Onbashy" not in context and "your role brief states your own responsibilities" in scope
    routing = scope[scope.index("Onbashy only:"):]
    assert "route implementation to Dev" in routing and "route implementation to Dev" not in scope.replace(routing, "")
    assert "Dev implements within the project and authority the owner selects" in scope
    # The Dev coding brief follows, and the shared work loop starts with Dev's exception, not a queue takeover.
    loop = " ".join(context[context.index("## Role Work Loop"):].split())
    assert loop.index("does not run the queue") < loop.index("Every other role") < loop.index("begin the next authorized actionable work")
