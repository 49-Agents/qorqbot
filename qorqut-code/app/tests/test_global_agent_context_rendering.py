"""Active mandatory context renders only a supported global, and its inbox intake wording names only the coordinator.

Synthetic only: a tmp instance with the registered globals; no model, provider, network, native process or live data."""

import dataclasses

import pytest
from qorqut_operations.contracts import DomainError, Principal, plain

from bazarlyk.agents.context import compose_context
from bazarlyk.agents.models import AgentProfile
from host_foundation_support import build_instance, create_registered_global, register_sources

ROLES = ("ordubashy", "learning-applier")


def section(rendered, title):
    body = rendered.split(f"## {title}\n", 1)[1]
    return " ".join(body.split("\n## ", 1)[0].replace("`", "").split())


def test_active_context_renders_only_a_supported_global_and_global_email_intake(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first",))
    register_sources(instance, ids["first"], "first")
    identities = {role: create_registered_global(instance, role) for role in ROLES}
    instance.runtime.freeze_registry()
    resolver = instance.runtime.resolver
    principal = Principal(identities["ordubashy"], "agent")
    rendered = resolver.resolve_agent_config(principal, business_id=ids["first"]).to_dict()["mandatory_context"]["rendered"]
    identity = section(rendered, "Identity And Assignment")
    assert "Scope: ecosystem;" in identity and "Business:" not in identity
    assert "Target: First" in section(rendered, "Selected Project")
    inbox = section(rendered, "Agent inbox")
    assert ("New incoming project email notifies the configured intake coordinator, the active Ordubashy, when that agent "
            "may read the project's email, whoever else is assigned.") in inbox
    assert "Each existing assignee who may read it keeps its own notification." in inbox
    assert "reads the email before routing it, then takes ownership or assigns it with email_assign" in inbox
    assert "business agent" not in inbox.lower()
    # Every host-generated startup label that names the organization calls it a Project; only strategy keeps its own wording.
    assert "active authorized projects, listed under Projects. Archived projects are excluded" in identity
    assert "\n## Projects\n" in rendered and "## Businesses" not in rendered and "listed under Businesses" not in rendered
    assert "stage: " in section(rendered, "Selected Project") and "project revision:" in section(rendered, "Selected Project")
    orientation = section(rendered, "Work Orientation")
    assert "Project tasks stay in their project." in orientation and "same-project records" in orientation
    for stale in ("business revision", "Business tasks", "same-business", "Global/business origin", "Selected-business"):
        assert stale not in rendered, stale
    assert "Projects have no resident agents. Global agents work across every active project." in inbox
    # Direct composition refuses a business-home identity and a retired role instead of rendering either as a global.
    profile, _ = resolver.profile(principal)
    role = resolver.packages_for_profile(profile)[0]
    homed = AgentProfile({**plain(profile.data), "scope": {"kind": "business", "business_id": ids["first"]}})
    retired = dataclasses.replace(role, manifest={**plain(role.manifest), "id": "business-agent"})
    for candidate, package in ((homed, role), (profile, retired)):
        with pytest.raises(DomainError) as refused:
            compose_context(candidate, package, {}, effective_config_id="sha256:0", workspace="/unused", business=None)
        assert refused.value.code == "invalid_profile", refused.value
