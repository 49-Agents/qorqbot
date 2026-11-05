"""Ordubashy's work briefing no longer directs it to business-agent or Dev directories: the current global agents are Ordubashy, Learning Applier and workers.

Synthetic only: a tmp instance with the registered globals; no model, provider, network, native process or live data."""

from qorqut_operations.contracts import InvocationOptions, Principal, new_id, plain

from host_foundation_support import build_instance, create_registered_global, create_worker, register_sources

ROLES = ("ordubashy", "learning-applier")
OVERVIEW = "project overview for your manager role"


def briefing(instance, agent, business):
    ctx = instance.runtime.resolver.invocation_context(Principal(agent, "agent"), business_id=business)
    result = instance.runtime.application.invoke(ctx, "agent_config_get", {}, InvocationOptions(idempotency_key=new_id()))
    assert result.status == "ok", result.error
    return plain(result.data)["work_briefing"]["rendered"]


def test_ordubashy_briefing_has_no_business_agent_or_dev_directory(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first",))
    register_sources(instance, ids["first"], "first")
    identities = {role: create_registered_global(instance, role) for role in ROLES}
    identities["colleague"] = create_worker(instance, "colleague")
    instance.runtime.freeze_registry()
    created = instance.invoke(instance.human(ids["first"]), "tasks_create", {"title": "Unassigned business work"})
    assert created.status == "ok", created.error
    for business in (ids["first"], None):
        rendered = briefing(instance, identities["ordubashy"], business)
        assert "## Team" in rendered and "Business agent" not in rendered and "Dev directory" not in rendered, business
        assert 'role_id="business-agent"' not in rendered and 'role_id="dev"' not in rendered
    # The business overview of unassigned work stays with Ordubashy, the one global manager role.
    assert OVERVIEW in briefing(instance, identities["ordubashy"], ids["first"])
    assert OVERVIEW not in briefing(instance, identities["colleague"], ids["first"])
