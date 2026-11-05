"""A read operation may declare one alternative Global capability; every other descriptor keeps its single gate."""

from dataclasses import replace

import pytest

from qorqut_operations.application import Application, CatalogContribution, OperationDescriptor, compose_catalog
from qorqut_operations.contracts import DomainError, InvocationContext, Principal
from qorqut_operations.policy import AgentPolicyBasis, PolicyResolver, PolicyRules
from support import AGENT, BUSINESS, HUMAN, database_at

SCHEMA = {"type": "object"}
GLOBAL = frozenset({"global"})
READ = dict(effect_kind="read", supports_signoff=False, read=lambda *_: {})
NOOP = dict(prepare=lambda *_: None, apply=lambda *_: None)


def read(name="jobs_probe", alternatives=("manage_probe",), **extra):
    fields = {"scope_kinds": GLOBAL | {"business"}, "global_capability": "manage_primary", **READ, **extra}
    return OperationDescriptor(name, SCHEMA, global_read_capability_alternatives=alternatives, **fields)


def registry(*descriptors):
    return compose_catalog([CatalogContribution("demo", (), descriptors)], include_batch=False).registry


class Agents:
    def refresh_invocation_context(self, ctx, uow):
        return ctx

    def policy_basis(self, ctx, uow):
        return AgentPolicyBasis({"jobs_probe": "allow", "plain_probe": "allow"}, {}, {"id": "role-a", "version": "1"}, 3)

    def plugin_declares(self, ctx, basis, descriptor, uow):
        return True


def agent(*capabilities, business=None):
    return InvocationContext(Principal(AGENT, "agent"), business_id=business,
                             allowed_business_ids=frozenset({BUSINESS}), profile_revision=3, source="mcp",
                             global_capabilities=frozenset(capabilities))


# -- the descriptor -------------------------------------------------------------------------------------------------------

def test_the_default_is_no_alternative_and_behaviour_is_unchanged():
    plain = OperationDescriptor("plain_probe", SCHEMA, scope_kinds=GLOBAL, global_capability="manage_primary", **READ)
    assert plain.global_read_capability_alternatives == frozenset()
    assert plain.global_capability_selected({"manage_primary"}) == "manage_primary"
    assert plain.global_capability_selected({"manage_probe"}) is None
    assert plain.global_capability_evidence({"manage_primary"}) == {"global_capability": "manage_primary"}


def test_one_rule_selects_the_primary_first_then_the_declared_alternative():
    descriptor = read(alternatives=frozenset({"manage_probe", "manage_other"}))
    assert descriptor.global_capability_selected({"manage_primary", "manage_probe"}) == "manage_primary"
    assert descriptor.global_capability_selected({"manage_probe"}) == "manage_probe"
    assert descriptor.global_capability_selected({"manage_other", "manage_probe"}) == "manage_other"
    assert descriptor.global_capability_selected({"read_global_context"}) is None
    assert descriptor.global_capability_selected(frozenset()) is None
    assert descriptor.global_capability_evidence({"manage_probe"}) == {
        "global_capability": "manage_primary",
        "global_capability_alternatives": ["manage_other", "manage_probe"],
        "global_capability_selected": "manage_probe"}


def test_business_policy_never_carries_the_alternatives():
    descriptor = read()
    assert descriptor.business_policy.global_capability is None
    assert descriptor.business_policy.global_read_capability_alternatives == frozenset()
    assert descriptor.global_read_capability_alternatives == {"manage_probe"}


@pytest.mark.parametrize("build", [
    # A mutation, a composed effect and a platform action can never widen their gate.
    lambda: OperationDescriptor("probe_write", SCHEMA, scope_kinds=GLOBAL, global_capability="manage_primary",
                                global_read_capability_alternatives=frozenset({"manage_probe"}), **NOOP),
    lambda: OperationDescriptor("probe_compound", SCHEMA, scope_kinds=GLOBAL, global_capability="manage_primary",
                                effect_kind="compound", derived_effects=("probe_write",),
                                global_read_capability_alternatives=frozenset({"manage_probe"}), **NOOP),
    lambda: OperationDescriptor("probe_platform", SCHEMA, scope_kinds=GLOBAL, global_capability="manage_primary",
                                signoff_category="platform_action", effect_kind="compound",
                                global_read_capability_alternatives=frozenset({"manage_probe"}), **NOOP),
    lambda: read(derived_effects=("probe_write",)),
    lambda: read(supports_signoff=True),
    lambda: read(owner_only=True),
    # The primary stays required, on a Global-capable operation, and is not repeated.
    lambda: read(global_capability=None),
    lambda: read(scope_kinds=frozenset({"business"})),
    lambda: read(alternatives=("manage_primary",)),
    lambda: read(alternatives=("",)),
    lambda: read(alternatives="manage_probe"),  # a bare string is not a set of names
])
def test_alternatives_are_rejected_outside_plain_reads_with_a_primary(build):
    with pytest.raises(ValueError):
        build()


# -- the engine and the resolver share it ---------------------------------------------------------------------------------

def test_namespace_admits_the_primary_or_a_declared_alternative_only(tmp_path):
    descriptor = read(resolve_namespace=lambda context, arguments: "global")
    app = Application(database_at(tmp_path), registry(descriptor))
    for held in ({"manage_primary"}, {"manage_probe"}, {"manage_primary", "manage_probe"}):
        assert app.namespace(agent(*held), descriptor, {}) == "global"
    for held in (set(), {"read_global_context"}, {"manage_fastmail_lookalike"}):
        with pytest.raises(DomainError, match="Global capability is not granted"):
            app.namespace(agent(*held), descriptor, {})
    legacy = replace(descriptor, global_read_capability_alternatives=frozenset())
    with pytest.raises(DomainError, match="Global capability is not granted"):
        app.namespace(agent("manage_probe"), legacy, {})


def test_policy_resolution_uses_the_same_rule_and_binds_alternatives_only_when_declared(tmp_path):
    database = database_at(tmp_path)
    with database.transaction() as uow:
        uow.repository("principals").insert({"id": AGENT, "kind": "agent"})
        uow.repository("installation").insert({"id": "service", "installation_id": "11111111-1111-4111-8111-111111111111",
                                               "owner_principal_id": HUMAN})
    plain = OperationDescriptor("plain_probe", SCHEMA, scope_kinds=GLOBAL, global_capability="manage_primary", **READ)
    descriptor = read()
    resolver = PolicyResolver(registry(descriptor, plain), Agents(), rules=PolicyRules())
    with database.read() as uow:
        decision = resolver.resolve(agent("manage_probe"), descriptor, (), uow)
        assert decision.mode == "allow"
        assert decision.binding["global_capability"] == "manage_primary"
        assert tuple(decision.binding["global_capability_alternatives"]) == ("manage_probe",)
        assert decision.binding["global_capability_selected"] == "manage_probe"
        assert resolver.resolve(agent("manage_primary"), descriptor, (), uow).binding[
            "global_capability_selected"] == "manage_primary"
        with pytest.raises(DomainError, match="Global capability is not granted"):
            resolver.resolve(agent("read_global_context"), descriptor, (), uow)
        # A descriptor without alternatives keeps exactly its former gate and binding.
        with pytest.raises(DomainError, match="Global capability is not granted"):
            resolver.resolve(agent("manage_probe"), plain, (), uow)
        assert resolver.resolve(agent("manage_primary"), plain, (), uow).binding == {
            "profile_revision": 3, "role": {"id": "role-a", "version": "1"},
            "global_capability": "manage_primary", "plugin_requirement": None}
        # Business scope never asks for the Global gate at all.
        business = agent(business=BUSINESS)
        assert resolver.resolve(business, descriptor.business_policy, (), uow).binding["global_capability"] is None
        # The installation owner's Global decision names the same evidence.
        owner = InvocationContext(Principal(HUMAN, "human"), allowed_business_ids=frozenset({BUSINESS}),
                                  source="dashboard", global_capabilities=frozenset({"manage_probe"}))
        owned = resolver.resolve(owner, descriptor, (), uow)
        assert owned.provenance == "installation_owner" and owned.binding["global_capability_selected"] == "manage_probe"
