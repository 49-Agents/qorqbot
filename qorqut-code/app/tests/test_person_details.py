"""Person fields and configurable affiliation permissions."""
from qorqut_operations.contracts import InvocationContext, InvocationOptions, new_id
from test_service_transports import ServiceFixture


def call(s, op, args, *, actor=None, revision=None):
    ctx = s.fixture.ctx if actor is None else InvocationContext(actor, source="mcp", business_id=s.fixture.business_id,
        review_business_id=s.fixture.business_id, allowed_business_ids=frozenset({s.fixture.business_id}))
    return s.runtime.application.invoke(ctx, op, args, InvocationOptions(expected_revision=revision, idempotency_key=new_id()))


def person(s, identity):
    r = call(s, "context_items_get", {"item": {"type": "person", "id": identity}})
    assert r.status == "ok", r.to_dict()
    return r.data["item"]


def approve(s, proposal, *, stale=False):
    with s.fixture.db.read(business_id=s.fixture.business_id) as uow:
        row = uow.repository("signoffs").get(proposal.signoff["id"])
    r = s.runtime.signoffs.review(s.fixture.ctx, row["id"], decision="approve", proposal_revision=row["proposal_revision"],
        payload_hash=row["payload_hash"], expected_status_revision=row["status_revision"], idempotency_key=new_id())
    if stale:
        assert r.error["code"] == "stale_signoff", r.to_dict()
        return
    assert r.status == "accepted", r.to_dict()
    s.runtime.signoffs.run_one(row["id"])


def test_person_defaults_contact_partial_updates_and_owner_identity(tmp_path):
    s = ServiceFixture(tmp_path, full=True)
    created = call(s, "context_people_create", {"name": "External person", "contact": {"email": "person@example.org", "phone": "+123"}, "summary": "Met through customer interviews"})
    assert created.status == "ok", created.to_dict()
    identity = created.data["document"]["id"]
    assert person(s, identity)["affiliation"] == "external"
    assert person(s, s.fixture.owner.id)["affiliation"] == "internal"
    edited = call(s, "context_people_update", {"document_id": identity, "changes": {"contact": {"notes": "Email first"}}}, revision=1)
    assert edited.status == "ok", edited.to_dict()
    assert dict(person(s, identity)["contact"]) == {"email": "person@example.org", "phone": "+123", "notes": "Email first"}
    owner = person(s, s.fixture.owner.id)
    edited = call(s, "context_people_update", {"document_id": s.fixture.owner.id, "changes": {"contact": {"email": "owner@example.org"}, "summary": "Founder"}}, revision=owner["revision"])
    assert edited.status == "ok", edited.to_dict()
    assert person(s, s.fixture.owner.id)["summary"] == "Founder"
    assert call(s, "context_people_update", {"document_id": s.fixture.owner.id, "changes": {"summary": "Stale"}}, revision=owner["revision"]).error["code"] == "revision_conflict"


def test_business_agent_affiliation_respects_allow_override(tmp_path):
    s = ServiceFixture(tmp_path, full=True)
    identity = call(s, "context_people_create", {"name": "Assistant"}).data["document"]["id"]
    s.fixture.update(s.agent, {"tool_policy": {"overrides": {"context_people_update": "allow", "context_people_affiliation_set": "allow"}}})
    proposal = call(s, "context_people_update", {"document_id": identity, "changes": {"affiliation": "internal"}}, actor=s.agent, revision=1)
    assert proposal.status == "ok", proposal.to_dict()
    assert person(s, identity)["affiliation"] == "internal"
    # Direct owner edits remain immediate.
    changed = call(s, "context_people_affiliation_set", {"document_id": identity, "affiliation": "external"}, revision=2)
    assert changed.status == "ok", changed.to_dict()
    assert person(s, identity)["affiliation"] == "external"


def test_affiliation_deny_blocks_every_person_write(tmp_path):
    s = ServiceFixture(tmp_path, full=True)
    identity = call(s, "context_people_create", {"name": "Assistant"}).data["document"]["id"]
    s.fixture.update(s.agent, {"tool_policy": {"overrides": {"context_people_create": "allow", "context_people_update": "allow", "context_document_update": "allow", "context_people_affiliation_set": "deny"}}})
    for op, args in [
        ("context_people_update", {"document_id": identity, "changes": {"affiliation": "internal"}}),
        ("context_people_affiliation_set", {"document_id": identity, "affiliation": "internal"}),
        ("context_document_update", {"document_id": identity, "body": "---\nname: Assistant\naffiliation: internal\n---\n"}),
        ("context_people_create", {"name": "Fake internal", "affiliation": "internal"}),
    ]:
        result = call(s, op, args, actor=s.agent, revision=None if op.endswith("create") else 1)
        if op == "context_document_update":
            assert result.status == "ok", result.to_dict()
            assert person(s, identity)["affiliation"] == "external"
            continue
        assert result.status == "error", result.to_dict()
        assert result.error["code"] == "policy_denied", result.to_dict()
    assert person(s, identity)["affiliation"] == "external"
    assert call(s, "context_people_update", {"document_id": identity, "changes": {"summary": "Normal contact edit"}}, actor=s.agent, revision=person(s, identity)["revision"]).status == "ok"


def test_affiliation_proposal_cannot_overwrite_newer_edit_or_survive_role_change(tmp_path):
    s = ServiceFixture(tmp_path, full=True)
    # Ordubashy proposes: a business agent's role is fixed.
    s.fixture.update(s.manager, {"tool_policy": {"overrides": {"context_people_affiliation_set": "signoff"}}})
    identity = call(s, "context_people_create", {"name": "Assistant"}).data["document"]["id"]
    proposal = call(s, "context_people_affiliation_set", {"document_id": identity, "affiliation": "internal"}, actor=s.manager, revision=1)
    assert proposal.status == "signoff_required", proposal.to_dict()
    assert call(s, "context_people_update", {"document_id": identity, "changes": {"summary": "New owner information"}}, revision=1).status == "ok"
    approve(s, proposal, stale=True)
    assert person(s, identity)["affiliation"] == "external"
    fresh = call(s, "context_people_affiliation_set", {"document_id": identity, "affiliation": "internal"}, actor=s.manager, revision=2)
    assert fresh.status == "signoff_required", fresh.to_dict()
    s.fixture.update(s.manager, {"role": s.fixture.refs["founder"], "tool_policy": {"overrides": {}}})
    approve(s, fresh, stale=True)
    assert person(s, identity)["affiliation"] == "external"


def test_human_person_affiliation_signoff_does_not_change_membership_permissions(tmp_path):
    s = ServiceFixture(tmp_path, full=True)
    s.fixture.update(s.agent, {"tool_policy": {"overrides": {"context_people_affiliation_set": "signoff"}}})
    identity = s.fixture.owner.id
    before = person(s, identity)
    proposal = call(s, "context_people_affiliation_set", {"document_id": identity, "affiliation": "external"}, actor=s.agent, revision=before["revision"])
    assert proposal.status == "signoff_required", proposal.to_dict()
    assert person(s, identity)["affiliation"] == "internal"
    approve(s, proposal)
    assert person(s, identity)["affiliation"] == "external"
    with s.fixture.db.read(business_id=s.fixture.business_id) as uow:
        assert uow.repository("business_memberships").get(identity)["is_owner"]
    assert call(s, "context_people_affiliation_set", {"document_id": identity, "affiliation": "internal"}, revision=before["revision"]+1).status == "ok"


def test_affiliation_uses_role_defaults_and_overrides_without_role_exceptions():
    from bazarlyk.application.catalog import builtin_registry
    from bazarlyk.policy.resolver import resolve_operation_policy
    registry = builtin_registry()
    op = "context_people_affiliation_set"
    for defaults, overrides, expected in [
        ({}, {}, "deny"),
        ({op: "allow"}, {}, "allow"),
        ({op: "allow"}, {op: "signoff"}, "signoff"),
        ({op: "allow"}, {op: "deny"}, "deny"),
    ]:
        for role in ("business-agent", "ordubashy", "founder", "global-marketing", "custom-role"):
            decision = resolve_operation_policy(op, defaults, overrides, registry, role_id=role)
            assert decision.mode == expected
            assert decision.provenance == ("agent_override" if overrides else "role_default" if defaults else "fallback")
