"""A pending proposal survives a republish and still dies on a real authority change.

Owner-reviewed proposals were invalidated by values that say nothing about the
change under review: the proposer's profile revision, the role artifact's
version, and the provenance label naming which code branch granted the mode.
An installation republishes roles many times a day and adopts them into every
profile at once, so every proposal the owner had not yet reached died together,
and the replacements the owner approved could not apply either.
"""
import pytest
import yaml

from bazarlyk.application import Application
from qorqut_operations.contracts import DomainError
from test_platform_signoff_submission import platform, submit, decide, row  # noqa: F401


def state(platform, identity):
    return row(platform, identity)["state"]


def republished_role(fixture, agent, version="999.0.0"):
    """Publish a new version of the agent's role and adopt it, as a real republish does."""
    profile, _ = fixture.resolver.profile(agent.principal)
    role_id = profile.data["role"]["id"]
    kind, source, manifest_path = fixture.sources[role_id]
    assert kind == "role"
    manifest = source / manifest_path
    raw = manifest.read_text()
    body = yaml.safe_load(raw)
    body["version"] = version
    manifest.write_text(yaml.safe_dump(body))
    try:
        fixture.sources[role_id + "-next"] = ("role", source, manifest_path)
        fixture.publisher.sources[role_id + "-next"] = fixture.sources[role_id + "-next"]
        ref = fixture.publish(role_id + "-next")
    finally:
        # Restore the shared role source byte for byte.
        manifest.write_text(raw)
    fixture.update(agent.principal, {"role": ref})
    return ref


def test_unrelated_profile_rewrite_keeps_the_owners_pending_proposal_approvable(platform):
    fixture, _, _, _, agent = platform
    proposal = submit(platform)
    assert proposal.status == "signoff_required", proposal.to_dict()
    before = fixture.resolver.profile(agent.principal)[0].revision

    fixture.update(agent.principal, {"assignment_brief": "Own measurable progress with fresh evidence."})
    assert fixture.resolver.profile(agent.principal)[0].revision > before

    assert decide(platform, proposal).status == "accepted"
    assert state(platform, proposal.signoff["id"]) == "approved"


def test_role_republish_keeps_the_owners_pending_proposal_approvable(platform):
    """The incident itself: a new role version adopted into every profile."""
    fixture, _, _, _, agent = platform
    proposal = submit(platform)
    assert proposal.status == "signoff_required", proposal.to_dict()
    pinned = row(platform, proposal.signoff["id"])["policy_binding_json"]
    assert pinned["operations"]["signoffs_platform_submit"]["binding"]["role"]["version"] != "999.0.0"

    assert republished_role(fixture, agent)["version"] == "999.0.0"
    assert decide(platform, proposal).status == "accepted"
    assert state(platform, proposal.signoff["id"]) == "approved"


@pytest.mark.parametrize("change,code", [("deny", "policy_denied"), ("revoke_plugin", "forbidden"),
                                         ("deactivate", "inactive_agent")])
def test_real_authority_change_still_stops_the_approval(platform, change, code):
    fixture, _, _, _, agent = platform
    proposal = submit(platform)
    assert proposal.status == "signoff_required", proposal.to_dict()

    if change == "deny":
        fixture.update(agent.principal, {"tool_policy": {"overrides": {"signoffs_platform_submit": "deny"}}})
    elif change == "revoke_plugin":
        fixture.update(agent.principal, {"plugins": []})
    else:
        # A business keeps its one agent; a disabled identity still stops the approval.
        with pytest.raises(DomainError) as refused:
            fixture.update(agent.principal, {"status": "inactive"})
        assert refused.value.code == "roster_conflict"
        with fixture.db.transaction() as uow:
            uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (agent.principal.id,))

    outcome = decide(platform, proposal)
    assert outcome.status == "error", outcome.to_dict()
    assert outcome.error["code"] in {code, "stale_signoff"}, outcome.error
    assert state(platform, proposal.signoff["id"]) == "conflicted"


def test_changed_grant_configuration_still_stops_the_approval(platform):
    """Not through the binding: validate_targets re-derives the grant and rejects the payload."""
    fixture, _, _, _, agent = platform
    proposal = submit(platform)
    grant = fixture.resolver.profile(agent.principal)[0].data["plugins"][0]
    fixture.update(agent.principal, {"plugins": [{**grant, "config": {**grant["config"], "subreddits": ["selfhosted"]}}]})

    outcome = decide(platform, proposal)
    assert outcome.status == "error", outcome.to_dict()
    assert state(platform, proposal.signoff["id"]) == "conflicted"


def binding(*, profile_revision=7, role_version="2.20.2", mode="allow", provenance="agent_override",
            operation="signoffs_platform_submit", **extra):
    """A stored binding in the shape the previous release wrote."""
    return {"profile_revision": profile_revision, "policy_revision": profile_revision,
            "operations": {operation: {"mode": mode, "provenance": provenance, "binding": {
                "profile_revision": profile_revision, "global_capability": None, "plugin_requirement": None,
                "role": {"id": "marketing", "version": role_version, "digest": "sha256:" + "a" * 64}, **extra}}}}


@pytest.mark.parametrize("current,prepared,equal", [
    (binding(), binding(), True),
    (binding(profile_revision=9), binding(profile_revision=7), True),
    (binding(role_version="2.21.0"), binding(role_version="2.20.2"), True),
    (binding(provenance="role_default"), binding(provenance="agent_override"), True),
    (binding(mode="signoff"), binding(mode="allow"), False),
    (binding(plugin_grant_config={"mailboxes": ["a"]}), binding(plugin_grant_config={"mailboxes": ["b"]}), False),
    (binding(operation="other_effect"), binding(), False),
    (binding(), {**binding(), "job_id": "job-1"}, False),
])
def test_authority_comparison_ignores_counters_and_labels_only(current, prepared, equal):
    assert Application._binding_matches(current, prepared) is equal


def test_authority_comparison_does_not_mutate_its_arguments():
    stored = binding()
    Application._binding_matches(binding(), stored)
    assert stored == binding()
    assert stored["operations"]["signoffs_platform_submit"]["provenance"] == "agent_override"


@pytest.mark.parametrize("shape", [{}, {"operations": None}, {"operations": {"x": "not-a-dict"}}, []])
def test_authority_comparison_tolerates_unexpected_stored_shapes(shape):
    assert Application._binding_matches(shape, shape) is True
    assert Application._binding_matches(shape, binding()) is False


def profile_pin(fixture, agent, operation):
    """A proposal shaped like one staged by an earlier release: identity pinned by revision."""
    from qorqut_operations.contracts import ChangeSet, ExpectedTarget
    from bazarlyk.domains.common import ref
    profile, row = fixture.resolver.profile(agent.principal)
    resource = ref("agent_profiles", profile.agent_id, row["home_business_id"])
    return ChangeSet(operation_name=operation, operation_version=1, principal_id=profile.agent_id,
        target_namespace="business:" + fixture.business_id, arguments={}, affected_scope={"kind": "business",
        "business_id": fixture.business_id}, expected_targets=(ExpectedTarget(resource, revision=row["revision"]),),
        changes=(), preview={"title": "Pinned identity"}), resource


@pytest.mark.parametrize("operation,survives", [("signoffs_platform_submit", True), ("tasks_update", False)])
def test_only_an_operation_that_re_derives_identity_ignores_its_revision(platform, operation, survives):
    """Declaring re-derivation is per operation; it is not a blanket exemption."""
    from qorqut_operations.contracts import DomainError
    fixture, app, _, _, agent = platform
    plan, _ = profile_pin(fixture, agent, operation)
    fixture.update(agent.principal, {"assignment_brief": "Rewritten while the proposal waited."})

    with fixture.db.read(business_id=fixture.business_id) as uow:
        if survives:
            app.check_targets(fixture.ctx, plan, uow)
        else:
            with pytest.raises(DomainError) as error:
                app.check_targets(fixture.ctx, plan, uow)
            assert error.value.code == "revision_conflict"


def test_declared_re_derivation_still_notices_a_row_the_plan_writes(platform):
    """Only rows the plan reads are exempt; a written row keeps exact pinning."""
    from qorqut_operations.contracts import DomainError, PreparedChange
    from dataclasses import replace
    fixture, app, _, _, agent = platform
    plan, resource = profile_pin(fixture, agent, "signoffs_platform_submit")
    plan = replace(plan, changes=(PreparedChange(kind="record", resource=resource, action="update", values={}),))
    fixture.update(agent.principal, {"assignment_brief": "Rewritten while the proposal waited."})

    with fixture.db.read(business_id=fixture.business_id) as uow, pytest.raises(DomainError) as error:
        app.check_targets(fixture.ctx, plan, uow)
    assert error.value.code == "revision_conflict"
