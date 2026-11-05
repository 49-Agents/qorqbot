"""Workspace-type registry: declared types, role gating and presentation state."""

import pytest

from qorqut_operations.contracts import InvocationContext, new_id, plain
from bazarlyk.workspaces import WORKSPACE_TYPES, WorkspaceRegistry, WorkspaceService
from test_signoffs import Scenario


def add_global_marketing(s):
    """Global Marketing over both businesses, with an active membership in each."""
    marketing = s.add_agent("global-marketing")
    with s.db.transaction() as uow:
        uow.repository("business_memberships", business_id=s.other_business).insert({"principal_id": marketing.id})
    return marketing


@pytest.fixture
def workspaces(tmp_path):
    s = Scenario(tmp_path)
    with s.db.transaction() as uow:
        uow.repository("installation").insert({"id": "service", "installation_id": new_id(),
            "owner_principal_id": s.owner.id})
    s.workspaces = WorkspaceService(s.app).register(s.registry)
    s.ordubashy = s.add_agent("ordubashy")
    return s


def ok(result):
    assert result.status == "ok", result.to_dict()
    return plain(result.data)


def call(s, actor, op, args, options=None):
    return s.app.invoke(s.agent_context(actor), op, args,
        {"idempotency_key": new_id(), **(options or {})})


def test_registry_declares_focus_and_campaign_review_roles_and_actions():
    registry = WorkspaceRegistry()
    assert [item.key for item in registry.all()] == ["campaign_review", "focus"]
    campaign = registry.get("campaign_review")
    assert campaign.supported_roles == frozenset({"business-agent", "global-marketing"})
    assert campaign.renderer == "campaign_review"
    assert set(campaign.presentation_actions) == {"workspace_present", "workspace_select", "workspace_attention"}
    focus = registry.get("focus")
    assert focus.supported_roles == frozenset({"ordubashy"})
    assert focus.renderer == "focus_board"
    assert "focus_get" not in focus.presentation_actions and "focus_show" in focus.presentation_actions
    assert [item.key for item in registry.for_role("business-agent")] == ["campaign_review"]
    assert [item.key for item in registry.for_role("global-marketing")] == ["campaign_review"]
    assert registry.for_role("marketing") == registry.for_role("outreach-specialist") == ()
    assert [item.key for item in registry.for_role("ordubashy")] == ["focus"]
    assert registry.for_role("founder") == ()
    assert len(WORKSPACE_TYPES) == 2


def test_workspace_types_lists_the_registry_and_filters_by_role(workspaces):
    s = workspaces
    every = ok(call(s, s.agent, "workspace_types", {}))
    assert {item["key"] for item in every["types"]} == {"focus", "campaign_review"}
    selected = ok(call(s, s.agent, "workspace_types", {"role_id": "business-agent"}))
    assert [item["key"] for item in selected["types"]] == ["campaign_review"]
    assert selected["role_id"] == "business-agent"


def test_presentation_state_is_stored_and_updated_with_a_revision(workspaces):
    s = workspaces
    created = ok(call(s, s.agent, "workspace_present", {"workspace": "campaign_review",
        "business_id": s.business, "campaign_id": "campaign-1", "stage": "leads",
        "status": "Reviewing leads"}))["workspace"]
    assert created["id"] and created["revision"] == 1 and created["campaign_id"] == "campaign-1"
    assert created["stage"] == "leads" and created["status"] == "Reviewing leads"
    selected = ok(call(s, s.agent, "workspace_select", {"workspace": "campaign_review",
        "business_id": s.business, "stage": "variants",
        "item": {"type": "draft", "id": "draft-1", "label": "First draft"}}))["workspace"]
    assert selected["revision"] == 2 and selected["stage"] == "variants"
    assert selected["item"] == {"type": "draft", "id": "draft-1", "label": "First draft"}
    assert selected["campaign_id"] == "campaign-1"
    flagged = ok(call(s, s.agent, "workspace_attention", {"workspace": "campaign_review",
        "business_id": s.business, "reason": "Choose the primary ICP", "urgent": True}))["workspace"]
    assert flagged["attention"] == {"reason": "Choose the primary ICP", "urgent": True, "blocker": False}
    with s.db.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM workspace_presentations").fetchone()[0] == 1
        assert uow.connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        assert uow.connection.execute("SELECT count(*) FROM signoffs").fetchone()[0] == 0
        assert uow.connection.execute("SELECT count(*) FROM focus_cards").fetchone()[0] == 0


def test_role_gate_and_focus_stays_on_its_own_operations(workspaces):
    s = workspaces
    denied = call(s, s.ordubashy, "workspace_present", {"workspace": "campaign_review",
        "business_id": s.business})
    assert denied.status == "error" and denied.error["code"] == "forbidden"
    refused = call(s, s.agent, "workspace_present", {"workspace": "focus", "business_id": s.business})
    assert refused.status == "error" and refused.error["code"] == "unsupported_workspace"
    # Focus keeps its own declared operations and their exact metadata.
    metadata = s.registry.metadata("focus_show")
    assert metadata.supports_signoff is False and metadata.effect_kind == "records"
    assert metadata.exposure == frozenset({"mcp", "dashboard", "worker"})
    assert s.registry.metadata("focus_get").effect_kind == "read"
    focus = WorkspaceRegistry().get("focus")
    assert focus.supported_roles == frozenset({"ordubashy"})
    assert {"focus_show", "focus_present", "focus_clear"} <= set(focus.presentation_actions)
    assert not {"workspace_present", "workspace_select", "workspace_attention"} & set(focus.presentation_actions)
    with s.db.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM workspace_presentations").fetchone()[0] == 0
        assert uow.connection.execute("SELECT count(*) FROM focus_cards").fetchone()[0] == 0


def test_workspace_business_is_bounded_by_membership_and_the_owner_can_read(workspaces):
    s = workspaces
    ok(call(s, s.agent, "workspace_present", {"workspace": "campaign_review",
        "business_id": s.business, "campaign_id": "campaign-1"}))
    mine = ok(call(s, s.agent, "workspace_get", {"workspace": "campaign_review",
        "business_id": s.business}))
    assert mine["workspace"]["agent_id"] == s.agent.id
    outside = call(s, s.agent, "workspace_get", {"workspace": "campaign_review",
        "business_id": s.other_business})
    assert outside.status == "error" and outside.error["code"] == "not_found"
    owner = ok(s.app.invoke(s.owner_context, "workspace_get", {"workspace": "campaign_review",
        "business_id": s.business, "agent_id": s.agent.id}))
    assert owner["workspace"]["campaign_id"] == "campaign-1"
    assert owner["workspace"]["agent_id"] == s.agent.id


def test_stale_presentation_revision_is_rejected(workspaces):
    s = workspaces
    ok(call(s, s.agent, "workspace_present", {"workspace": "campaign_review", "business_id": s.business}))
    ok(call(s, s.agent, "workspace_select", {"workspace": "campaign_review", "business_id": s.business,
        "stage": "leads"}, {"expected_revision": 1}))
    stale = call(s, s.agent, "workspace_select", {"workspace": "campaign_review",
        "business_id": s.business, "stage": "variants"}, {"expected_revision": 1})
    assert stale.status == "error" and stale.error["code"] == "revision_conflict"
    assert stale.error["current_revision"] == 2
    fresh = call(s, s.agent, "workspace_select", {"workspace": "campaign_review",
        "business_id": s.business, "stage": "variants"}, {"expected_revision": 2})
    assert fresh.status == "ok", fresh.to_dict()


def test_global_marketing_opens_campaign_review_for_a_selected_business(workspaces):
    s = workspaces
    marketing = add_global_marketing(s)
    ctx = InvocationContext(marketing, business_id=s.other_business, source="mcp")
    created = ok(s.app.invoke(ctx, "workspace_present", {"workspace": "campaign_review",
        "campaign_id": "campaign-9"}, {"idempotency_key": new_id()}))["workspace"]
    assert created["business_id"] == s.other_business and created["campaign_id"] == "campaign-9"
    mine = ok(s.app.invoke(ctx, "workspace_get", {"workspace": "campaign_review"}, {"idempotency_key": new_id()}))
    assert mine["business_id"] == s.other_business and mine["workspace"]["campaign_id"] == "campaign-9"
    owner = ok(s.app.invoke(s.owner_context, "workspace_get", {"workspace": "campaign_review",
        "business_id": s.other_business, "agent_id": marketing.id}))
    assert owner["workspace"]["campaign_id"] == "campaign-9" and owner["business_id"] == s.other_business


def test_global_marketing_is_denied_outside_its_business_grants(workspaces):
    s = workspaces
    marketing = add_global_marketing(s)
    outside = new_id()
    with s.db.transaction() as uow:
        (s.root / "three" / "hq").mkdir(parents=True)
        uow.repository("businesses").insert({"id": outside, "slug": "three", "name": "three",
            "workspace": str(s.root / "three"), "hq_path": str(s.root / "three" / "hq")})
    ctx = InvocationContext(marketing, business_id=outside, source="mcp")
    denied = s.app.invoke(ctx, "workspace_present", {"workspace": "campaign_review", "campaign_id": "x"},
        {"idempotency_key": new_id()})
    assert denied.status == "error" and denied.error["code"] == "forbidden"


def test_owner_reads_workspace_get_business_id_across_the_http_transport(tmp_path):
    from starlette.testclient import TestClient
    from test_service_transports import ORIGIN, ServiceFixture
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        service.login(client)
        headers = service.initialize(client, service.agent)
        presented = service.tool(client, headers, "workspace_present",
            {"workspace": "campaign_review", "campaign_id": "campaign-3", "idempotency_key": new_id()})
        assert presented["isError"] is False, presented
        result = client.post("/api/root/operations/workspace_get", headers=ORIGIN, json={
            "workspace": "campaign_review", "business_id": service.fixture.business_id, "agent_id": service.agent.id})
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["status"] == "ok", body
        assert body["data"]["workspace"]["campaign_id"] == "campaign-3"
        assert body["data"]["business_id"] == service.fixture.business_id
