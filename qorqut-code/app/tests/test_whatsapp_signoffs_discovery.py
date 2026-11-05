"""Discovery of the owner-approved personal WhatsApp send: the effective-configuration rows that feed MCP `tools/list`
must agree with the call-time rule, which gives Signoff only to the Global Ordubashy that holds a grant."""

from types import SimpleNamespace

import pytest

from bazarlyk.agents.resolver import effective_tool_policy
from bazarlyk.composition.policy import GLOBAL_DISCOVERY_OPERATIONS, GLOBAL_TABLE_OPERATIONS
from bazarlyk.transport.mcp import RegisteredAgentCaller
from host_foundation_support import create_registered_global
from qorqut_operations.contracts import Principal
from whatsapp_signoff_support import Setup

OPERATION = "whatsapp_send"


@pytest.fixture
def wa(tmp_path):
    setup = Setup(tmp_path)
    try:
        yield setup
    finally:
        setup.close()


def rows_of(wa, agent=None):
    config = wa.runtime.resolver.resolve_agent_config(Principal(agent or wa.agent, "agent"))
    return {row["canonical_operation"]: row for row in config.data["tools"]}


def synthetic(wa, *, role="ordubashy", kind="ecosystem", capabilities=("read_global_context",), overrides=None, defaults=None):
    """The same effective-policy computation for a profile shape the harness cannot (or must not) create."""
    overrides = {OPERATION: "signoff"} if overrides is None else overrides
    profile = SimpleNamespace(
        data={"tool_policy": {"overrides": overrides}, "plugins": [], "role": {"id": role}}, revision=7,
        scope=SimpleNamespace(kind=kind, global_capabilities=frozenset(capabilities)))
    holder = SimpleNamespace(manifest={"default_tool_policy": defaults or {}}, ref={"id": role})
    return {row["canonical_operation"]: row for row in effective_tool_policy(profile, holder, wa.runtime.registry)}


def test_a_granted_global_ordubashy_discovers_the_proposal_as_signoff(wa):
    row = rows_of(wa)[OPERATION]
    assert row["available"] is True and row["effective_mode"] == "signoff" and row["supports_signoff"] is True
    assert row["scope_modes"] == {"business": "deny", "global": "signoff"}
    assert "denial_reason" not in row or row["denial_reason"] is None


def test_the_mcp_inventory_lists_it_for_that_agent_and_only_that_agent(wa):
    caller = RegisteredAgentCaller(wa.runtime, None)
    def listed(agent):
        inventory = caller.inventory(SimpleNamespace(delegation_assignment_id=None, principal=Principal(agent, "agent")))
        return {row["canonical_operation"] for row in inventory if row["available"]}
    assert OPERATION in listed(wa.agent)
    other = create_registered_global(wa.host, "learning-applier")
    wa.grant("signoff", agent=other)                   # a grant on another role is still no proposal right
    assert OPERATION not in listed(other)
    wa.grant(None)
    assert OPERATION not in listed(wa.agent)


@pytest.mark.parametrize("override", ["signoff", "allow"])
def test_an_allow_override_cannot_remove_the_approval(wa, override):
    wa.grant(override)
    row = rows_of(wa)[OPERATION]
    assert row["available"] is True and row["effective_mode"] == "signoff"
    assert row["scope_modes"] == {"business": "deny", "global": "signoff"}


@pytest.mark.parametrize("override", ["deny", None])
def test_a_denied_or_ungranted_ordubashy_does_not_discover_it(wa, override):
    wa.grant(override)
    row = rows_of(wa)[OPERATION]
    assert row["available"] is False and row["effective_mode"] == "deny" and row["denial_reason"] == "policy_denied"
    assert row["scope_modes"] == {"business": "deny", "global": "deny"}


def test_a_role_default_grant_is_honoured_like_an_override(wa):
    assert synthetic(wa, overrides={}, defaults={OPERATION: "signoff"})[OPERATION]["available"] is True
    assert synthetic(wa, overrides={OPERATION: "deny"}, defaults={OPERATION: "signoff"})[OPERATION]["available"] is False


@pytest.mark.parametrize("shape", [
    {"role": "learning-applier"}, {"role": "global-marketing"}, {"role": "business-agent"}, {"role": "founder"},
    {"kind": "business"},                                   # a business profile never has the Global rule
    {"kind": "business", "capabilities": ("read_global_context",)},
    {"capabilities": ()},                                   # an ecosystem profile that lost the Global read capability
    {"capabilities": ("manage_calendar",)},
])
def test_other_roles_business_profiles_and_capability_less_profiles_do_not_discover_it(wa, shape):
    row = synthetic(wa, **shape)[OPERATION]
    assert row["available"] is False and row["effective_mode"] == "deny"
    assert row.get("scope_modes", {}).get("global", "deny") == "deny"


def test_only_the_global_discovery_set_is_evaluated_in_both_scopes(wa):
    rows = rows_of(wa)
    assert {name for name, row in rows.items() if "scope_modes" in row} <= GLOBAL_DISCOVERY_OPERATIONS
    assert GLOBAL_DISCOVERY_OPERATIONS == GLOBAL_TABLE_OPERATIONS | {OPERATION}
    # The default scope is not broadened for an unrelated owner-approval tool, and the table rows keep their both-scope view.
    assert "scope_modes" not in rows["owner_action_propose"]
    assert "scope_modes" in rows["tables_list"] and rows["tables_list"]["scope_modes"].keys() == {"business", "global"}
    assert rows["owner_action_propose"]["effective_mode"] == "signoff"


def test_discovery_does_not_replace_the_bound_agent_gate_at_call_time(wa):
    assert rows_of(wa)[OPERATION]["available"] is True
    other = create_registered_global(wa.host, "learning-applier")
    wa.bind(other)                                          # the connector is bound to a different agent than Ordubashy
    refused = wa.propose()
    assert refused.status == "error" and refused.error["code"] == "forbidden"
    assert wa.connector.sends == []
