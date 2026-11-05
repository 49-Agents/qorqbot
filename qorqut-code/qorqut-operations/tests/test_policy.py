from dataclasses import replace

import pytest

from qorqut_operations.application import CatalogContribution, CatalogEntry, OperationDescriptor, compose_catalog
from qorqut_operations.contracts import DomainError, InvocationContext, PolicyDecision, Principal
from qorqut_operations.policy import (AgentPolicyBasis, PolicyResolver, PolicyRules, WorkerProfile,
    resolve_operation_policy)
from support import AGENT, BUSINESS, HUMAN, OTHER, WORKER, context, database_at

NOOP = dict(prepare=lambda *_: None, apply=lambda *_: None)


def make_registry():
    entries = (CatalogEntry("items_get", False, "read"), CatalogEntry("owner_decision", False, "records",
                                                                     frozenset({"dashboard"}), True))
    descriptors = (
        OperationDescriptor("items_create", {"type": "object"}, aliases=("create_work",), **NOOP),
        OperationDescriptor("items_plain", {"type": "object"}, supports_signoff=False, **NOOP),
        OperationDescriptor("items_plugin", {"type": "object"}, plugin_requirement="demo-plugin", **NOOP),
        OperationDescriptor("items_get", {"type": "object"}, effect_kind="read", supports_signoff=False,
                            read=lambda *_: {}),
        OperationDescriptor("items_global", {"type": "object"}, scope_kinds=frozenset({"global", "business"}),
                            global_capability="demo_global", **NOOP),
    )
    return compose_catalog([CatalogContribution("demo", (entries[1],), descriptors)], include_batch=False).registry


RULES = PolicyRules()


def test_default_override_fallback_and_canonical_alias():
    registry = make_registry()
    resolve = lambda *args, **kwargs: resolve_operation_policy(*args, rules=RULES, **kwargs)
    assert resolve("create_work", {"items_create": "allow"}, {"items_create": "signoff"}, registry).mode == "signoff"
    decision = resolve("items_create", {"items_create": "signoff"}, {"items_create": "allow"}, registry)
    assert (decision.mode, decision.provenance) == ("allow", "agent_override")
    assert resolve("items_create", {"items_create": "allow"}, {}, registry).provenance == "role_default"
    assert resolve("new_unknown", {}, {}, registry) == PolicyDecision("deny", "fallback")
    assert resolve("items_get", {}, {}, registry) == PolicyDecision("deny", "fallback")


@pytest.mark.parametrize("rule", [{"unknown": "allow"}, {"items_get": "signoff"}, {"items_create": "sometimes"},
                                  {"owner_decision": "allow"}, {"create_work": "allow"}, ["items_create"]])
def test_invalid_grants_are_rejected_in_either_map(rule):
    registry = make_registry()
    with pytest.raises(DomainError):
        resolve_operation_policy("items_create", {}, rule, registry, rules=RULES)
    with pytest.raises(DomainError):
        resolve_operation_policy("items_create", rule, {}, registry, rules=RULES)


def test_owner_only_is_denied_before_any_contributed_rule_or_grant():
    registry = make_registry()
    grant_everything = PolicyRules(before_override=(lambda *a, **k: PolicyDecision("allow", "rule"),))
    decision = resolve_operation_policy("owner_decision", {"owner_decision": "deny"}, {"owner_decision": "deny"},
                                        registry, rules=grant_everything)
    assert decision == PolicyDecision("deny", "owner_only")


def test_contributed_rules_run_in_their_phase_and_the_first_decision_wins():
    registry, seen = make_registry(), []
    def rule(label, mode=None):
        def apply(name, defaults, overrides, registry, *, role_id, scope, resolve):
            seen.append((label, name, role_id, scope))
            return PolicyDecision(mode, label) if mode else None
        return apply
    rules = PolicyRules(before_override=(rule("early"), rule("early-2")),
                        after_override=(rule("mid"),), after_default=(rule("late", "signoff"),))
    run = lambda defaults, overrides, **kw: resolve_operation_policy(
        "items_create", defaults, overrides, registry, rules=rules, role_id="role-a", **kw)
    assert run({}, {}).provenance == "late"
    assert [label for label, *_ in seen] == ["early", "early-2", "mid", "late"]
    seen.clear()
    assert run({"items_create": "allow"}, {}).provenance == "role_default"   # defaults precede the late rule
    assert [label for label, *_ in seen] == ["early", "early-2", "mid"]
    seen.clear()
    assert run({"items_create": "allow"}, {"items_create": "deny"}).provenance == "agent_override"
    assert [label for label, *_ in seen] == ["early", "early-2"]
    assert all(entry[1:] == ("items_create", "role-a", "business") for entry in seen)
    decisive = PolicyRules(before_override=(rule("first", "deny"),), after_override=(rule("never", "allow"),))
    assert resolve_operation_policy("items_create", {}, {"items_create": "allow"}, registry,
                                    rules=decisive).provenance == "first"   # a contributed deny precedes an override


def test_rules_can_resolve_another_operation_under_the_same_maps():
    registry = make_registry()
    alias_of_plain = PolicyRules(after_default=(
        lambda name, d, o, r, *, role_id, scope, resolve: resolve("items_plain") if name == "items_create" else None,))
    decision = resolve_operation_policy("items_create", {}, {"items_plain": "allow"}, registry, rules=alias_of_plain)
    assert decision == PolicyDecision("allow", "agent_override")


def test_rules_are_required_typed_and_cannot_return_other_values():
    registry = make_registry()
    with pytest.raises(TypeError):
        resolve_operation_policy("items_create", {}, {}, registry)  # no implicit rules
    for bad in (None, {}, [], (lambda: None,)):
        with pytest.raises(TypeError, match="PolicyRules"):
            resolve_operation_policy("items_create", {}, {}, registry, rules=bad)
    with pytest.raises(ValueError, match="callable"):
        PolicyRules(before_override=("not a rule",))
    returns_dict = PolicyRules(before_override=(lambda *a, **k: {"mode": "allow"},))
    with pytest.raises(DomainError, match="invalid decision"):
        resolve_operation_policy("items_create", {}, {}, registry, rules=returns_dict)
    with pytest.raises(TypeError, match="PolicyRules"):
        PolicyResolver(registry, rules=None)


# -- principal-aware resolution -----------------------------------------------------------------------------------------

class FakeAgents:
    def __init__(self):
        self.bases = 0
        self.defaults, self.overrides, self.declared = {"items_create": "signoff", "items_plugin": "allow"}, {}, True

    def refresh_invocation_context(self, ctx, uow):
        return ctx

    def policy_basis(self, ctx, uow):
        self.bases += 1
        return AgentPolicyBasis(self.defaults, self.overrides, {"id": "role-a", "version": "1"}, 3)

    def plugin_declares(self, ctx, basis, descriptor, uow):
        return self.declared


def seed_principals(database):
    with database.transaction() as uow:
        uow.repository("principals").insert({"id": AGENT, "kind": "agent"})
        uow.repository("principals").insert({"id": WORKER, "kind": "system"})
        uow.repository("business_memberships", business_id=BUSINESS).insert({"principal_id": WORKER})
        uow.repository("installation").insert({"id": "service", "installation_id": "11111111-1111-4111-8111-111111111111",
                                               "owner_principal_id": HUMAN})


def agent_context(**kwargs):
    return InvocationContext(Principal(AGENT, "agent"), business_id=BUSINESS,
                             allowed_business_ids=frozenset({BUSINESS}), profile_revision=3, source="mcp", **kwargs)


def worker_context(**kwargs):
    return InvocationContext(Principal(WORKER, "system"), business_id=BUSINESS, allowed_business_ids=frozenset({BUSINESS}),
                             source="worker", **kwargs)


def test_human_decisions_follow_installation_and_membership_authority(tmp_path):
    database = database_at(tmp_path)
    seed_principals(database)
    registry = make_registry()
    resolver = PolicyResolver(registry, rules=RULES)
    with database.read() as uow:
        decision = resolver.resolve(context(), registry.get("items_create"), (), uow)
        assert (decision.mode, decision.provenance) == ("allow", "owner")
        assert decision.binding["membership_revision"] == 1
        # A global-capability operation without a selected business needs the installation owner.
        ctx = replace(context(), business_id=None, global_capabilities=frozenset({"demo_global"}))
        assert resolver.resolve(ctx, registry.get("items_global"), (), uow).provenance == "installation_owner"
        assert resolver.resolve(replace(ctx, global_capabilities=frozenset()), registry.get("items_plain"), (), uow
                                ).provenance == "owner_required"
        with pytest.raises(DomainError, match="Global capability"):
            resolver.resolve(replace(ctx, global_capabilities=frozenset()), registry.get("items_global"), (), uow)
    with database.transaction() as uow:
        uow.repository("business_memberships", business_id=BUSINESS).update(HUMAN, {"is_owner": False}, expected_revision=1)
    with database.read() as uow:
        write = resolver.resolve(context(), registry.get("items_create"), (), uow)
        read = resolver.resolve(context(), registry.get("items_get"), (), uow)
        assert (write.mode, write.provenance) == ("deny", "owner_required")
        assert (read.mode, read.provenance) == ("allow", "business_member")


def test_resolution_refuses_targets_outside_current_scope_and_inactive_business(tmp_path):
    from qorqut_operations.contracts import ResourceRef
    database = database_at(tmp_path)
    registry = make_registry()
    resolver = PolicyResolver(registry, rules=RULES)
    with database.read() as uow:
        with pytest.raises(DomainError, match="outside current scope"):
            resolver.resolve(context(), registry.get("items_create"), (ResourceRef("items", "x", OTHER),), uow)
    with database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at='now', archived_by=? WHERE id=?", (HUMAN, BUSINESS))
    with database.read() as uow:
        with pytest.raises(DomainError):
            resolver.resolve(context(), registry.get("items_create"), (), uow)


def test_agent_decisions_come_from_the_injected_authority_with_bound_basis(tmp_path):
    database = database_at(tmp_path)
    seed_principals(database)
    registry, agents = make_registry(), FakeAgents()
    resolver = PolicyResolver(registry, agents, rules=RULES)
    with database.read() as uow:
        decision = resolver.resolve(agent_context(), registry.get("items_create"), (), uow)
        assert decision.mode == "signoff" and decision.provenance == "role_default"
        assert decision.binding == {"profile_revision": 3, "role": {"id": "role-a", "version": "1"},
                                    "global_capability": None, "plugin_requirement": None}
        resolver.resolve(agent_context(), registry.get("items_plain"), (), uow)
        assert agents.bases == 1   # the read unit's snapshot is reused
        agents.declared = False
        assert resolver.resolve(agent_context(), registry.get("items_plugin"), (), uow) == PolicyDecision("deny", "plugin_unavailable")
    with database.transaction() as uow:
        resolver.resolve(agent_context(), registry.get("items_plain"), (), uow)
        resolver.resolve(agent_context(), registry.get("items_plain"), (), uow)
    assert agents.bases == 3   # a writer reloads on every decision
    agents.overrides = {"items_create": "allow"}
    with database.read() as uow:
        assert resolver.resolve(agent_context(), registry.get("items_create"), (), uow).provenance == "agent_override"
        with pytest.raises(DomainError, match="not installed"):
            PolicyResolver(registry, rules=RULES).resolve(agent_context(), registry.get("items_create"), (), uow)
    class BadAgents(FakeAgents):
        def policy_basis(self, ctx, uow):
            return {"defaults": {}}
    with database.read() as uow:
        with pytest.raises(DomainError, match="invalid policy basis"):
            PolicyResolver(registry, BadAgents(), rules=RULES).resolve(agent_context(), registry.get("items_create"), (), uow)


def test_worker_decisions_use_only_the_registered_profile(tmp_path):
    database = database_at(tmp_path)
    seed_principals(database)
    registry = make_registry()
    profiles = {WORKER: WorkerProfile({"items_create": "allow", "items_plain": "allow"}, frozenset({BUSINESS}),
                                      frozenset({"demo_global"}), 5)}
    resolver = PolicyResolver(registry, worker_profiles=profiles, rules=RULES)
    with database.read() as uow:
        decision = resolver.resolve(worker_context(), registry.get("items_create"), (), uow)
        assert decision.mode == "allow" and decision.provenance == "worker_profile"
        assert decision.binding["worker_revision"] == 5
        assert resolver.resolve(worker_context(), registry.get("items_get"), (), uow).mode == "deny"
        with pytest.raises(DomainError, match="internal worker"):
            resolver.resolve(replace(worker_context(), source="mcp"), registry.get("items_create"), (), uow)
        with pytest.raises(DomainError, match="No registered worker policy"):
            PolicyResolver(registry, rules=RULES).resolve(worker_context(), registry.get("items_create"), (), uow)
        # A grant that the catalog cannot honour is invalid, not silently ignored.
        bad = PolicyResolver(registry, worker_profiles={WORKER: WorkerProfile({"items_get": "signoff"}, frozenset({BUSINESS}))},
                             rules=RULES)
        with pytest.raises(DomainError, match="exact-change signoff"):
            bad.resolve(worker_context(), registry.get("items_create"), (), uow)
    with pytest.raises(ValueError, match="revision"):
        WorkerProfile({}, revision=0)


def test_worker_job_proof_must_bind_the_trusted_job_and_definition(tmp_path):
    database = database_at(tmp_path)
    seed_principals(database)
    registry = make_registry()
    profiles = {WORKER: WorkerProfile({"items_create": "allow"}, frozenset({BUSINESS}))}
    job = "00000000-0000-4000-8000-0000000000aa"
    job_context = worker_context(job_id=job)
    digest = "a" * 64
    def resolver(proof, validator=None):
        return PolicyResolver(registry, worker_profiles=profiles, rules=RULES, job_consequence_verifier=lambda *a: proof,
                              job_plan_validator=validator)
    with database.read() as uow:
        good = resolver({"job_id": job, "definition_digest": digest}).resolve(job_context, registry.get("items_create"), (), uow)
        assert good.binding["job_consequence"] == {"job_id": job, "definition_digest": digest}
        for proof in ({"job_id": "other", "definition_digest": digest}, {"job_id": job, "definition_digest": "short"},
                      {"job_id": job}, "truthy"):
            with pytest.raises(DomainError, match="Job consequence proof"):
                resolver(proof).resolve(job_context, registry.get("items_create"), (), uow)
        with pytest.raises(DomainError, match="Exact job output validation"):
            resolver(None).validate_plan(job_context, registry.get("items_create"), None, uow)
        calls = []
        resolver(None, lambda *args: calls.append(args)).validate_plan(job_context, registry.get("items_create"), "plan", uow)
        assert len(calls) == 1
        resolver(None).validate_plan(worker_context(), registry.get("items_create"), None, uow)   # not a job: nothing to validate


def test_worker_started_by_an_agent_is_the_intersection_of_both_authorities(tmp_path):
    database = database_at(tmp_path)
    seed_principals(database)
    registry, agents = make_registry(), FakeAgents()
    profiles = {WORKER: WorkerProfile({"items_create": "allow", "items_plain": "allow"}, frozenset({BUSINESS}))}
    resolver = PolicyResolver(registry, agents, worker_profiles=profiles, rules=RULES)
    started = worker_context(initiated_by_principal_id=AGENT)
    with database.read() as uow:
        # The agent's own authority for items_create is signoff; the worker alone would be allowed.
        decision = resolver.resolve(started, registry.get("items_create"), (), uow)
        assert (decision.mode, decision.provenance) == ("signoff", "worker_and_initiator")
        assert decision.binding["initiator"]["principal_id"] == AGENT
        agents.overrides = {"items_plain": "deny"}
        assert resolver.resolve(started, registry.get("items_plain"), (), uow).mode == "deny"
        with pytest.raises(DomainError, match="Initiating principal is missing"):
            resolver.resolve(worker_context(initiated_by_principal_id=OTHER), registry.get("items_create"), (), uow)


def test_context_for_reconstitutes_authority_from_current_state(tmp_path):
    database = database_at(tmp_path)
    seed_principals(database)
    resolver = PolicyResolver(make_registry(), FakeAgents(), rules=RULES,
                              worker_profiles={WORKER: WorkerProfile({}, frozenset({BUSINESS}))},
                              human_global_capabilities={HUMAN: ("demo_global",)})
    with database.read() as uow:
        human = resolver.context_for(HUMAN, business_id=None, review_business_id=BUSINESS, uow=uow)
        assert human.source == "dashboard" and human.business_id == BUSINESS
        assert human.allowed_business_ids == {BUSINESS, OTHER} and human.global_capabilities == {"demo_global"}
        assert resolver.context_for(WORKER, business_id=BUSINESS, review_business_id=BUSINESS, uow=uow).source == "worker"
        with pytest.raises(DomainError, match="missing or inactive"):
            resolver.context_for("00000000-0000-4000-8000-0000000000ff", business_id=None,
                                 review_business_id=None, uow=uow)
