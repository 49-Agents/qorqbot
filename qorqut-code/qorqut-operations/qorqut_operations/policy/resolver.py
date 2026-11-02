"""Principal-aware authority resolution over the extracted rules.

Human, worker and agent decisions are the deployed v1 ones. The agent's stored profile, pinned role and plugin
packages belong to the host's agent registry, so they arrive through the typed ``AgentAuthority`` port instead of
being read here."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from qorqut_operations.business_lifecycle import active_business_ids, require_active_business
from qorqut_operations.contracts import DomainError, InvocationContext, PolicyDecision, Principal, freeze, plain
from .rules import PolicyRules, resolve_operation_policy, validate_modes


@dataclass(frozen=True)
class AgentPolicyBasis:
    """What the host's agent registry says about one agent right now."""

    defaults: Mapping[str, str]  # the pinned role's default tool policy
    overrides: Mapping[str, str]  # the agent profile's explicit tool-policy overrides
    role: Mapping[str, Any]  # the profile's role reference; must carry "id"
    revision: int  # the profile revision this basis was read at


class AgentAuthority(Protocol):
    def refresh_invocation_context(self, context: InvocationContext, uow) -> InvocationContext: ...

    def policy_basis(self, context: InvocationContext, uow) -> AgentPolicyBasis: ...

    def plugin_declares(self, context: InvocationContext, basis: AgentPolicyBasis, descriptor, uow) -> bool:
        """True only when the profile holds the descriptor's required plugin grant and that plugin
        package declares this very tool."""


@dataclass(frozen=True)
class WorkerProfile:
    """Trusted registered program grants; these never come from tool arguments."""

    operations: Mapping[str, str]
    business_ids: frozenset[str] = field(default_factory=frozenset)
    global_capabilities: frozenset[str] = field(default_factory=frozenset)
    revision: int = 1

    def __post_init__(self):
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("Worker policy revision must be positive")
        object.__setattr__(self, "operations", freeze(self.operations))
        object.__setattr__(self, "business_ids", frozenset(self.business_ids))
        object.__setattr__(self, "global_capabilities", frozenset(self.global_capabilities))


class PolicyResolver:
    def __init__(self, registry, agent_resolver=None, *, rules: PolicyRules, worker_profiles=None,
                 job_consequence_verifier=None, human_global_capabilities=None, job_plan_validator=None):
        if not isinstance(rules, PolicyRules):
            raise TypeError("A policy resolver needs the trusted PolicyRules of the composed domains")
        self.registry = registry
        self.rules = rules
        self.agent_resolver = agent_resolver
        self.worker_profiles = worker_profiles if worker_profiles is not None else {}
        # W7 verifies frozen job outputs. A truthy label or source field is not proof.
        self.job_consequence_verifier = job_consequence_verifier
        self.human_global_capabilities = human_global_capabilities
        self.job_plan_validator = job_plan_validator

    def validate_plan(self, context, descriptor, plan, uow):
        if context.job_id is not None:
            if self.job_plan_validator is None:
                raise DomainError("policy_denied", "Exact job output validation is not installed")
            self.job_plan_validator(context, descriptor, plan, uow)

    def worker_profile(self, principal_id, uow):
        provider = self.worker_profiles
        profile = provider(principal_id, uow) if callable(provider) else provider.get(principal_id)
        if not isinstance(profile, WorkerProfile):
            raise DomainError("policy_denied", "No registered worker policy grants this operation")
        validate_modes(profile.operations, self.registry)
        return profile

    @staticmethod
    def _principal(principal_id, uow):
        row = uow.repository("principals").get(principal_id)
        if row is None or not row["active"]:
            raise DomainError("invalid_principal", "Principal is missing or inactive")
        return Principal(row["id"], row["kind"], row["display_name"])

    def refresh_context(self, context, uow):
        # A read transaction has one authority snapshot. Reuse only an exact
        # immutable input context in that unit, including scope and session /
        # delegation provenance. Writes always revalidate after each change.
        memo = getattr(uow, "memo", None) if not getattr(uow, "writable", True) else None
        key = ("refreshed_context", id(self), context)
        if memo is not None and key in memo:
            return memo[key]
        refreshed = self._refresh_context(context, uow)
        if memo is not None:
            memo[key] = refreshed
        return refreshed

    def _refresh_context(self, context, uow):
        principal = self._principal(context.principal_id, uow)
        if principal.kind != context.principal_kind:
            raise DomainError("invalid_principal", "Principal kind changed")
        require_active_business(uow.connection, context.business_id)
        context = replace(context, principal=principal)
        if principal.kind == "agent":
            if self.agent_resolver is None:
                raise DomainError("policy_denied", "Agent profile resolver is not installed")
            return self.agent_resolver.refresh_invocation_context(context, uow)
        if principal.kind == "system":
            profile = self.worker_profile(principal.id, uow)
            if context.source != "worker":
                raise DomainError("forbidden", "Worker principals require the internal worker interface")
            memberships = frozenset(row[0] for row in uow.connection.execute(
                "SELECT business_id FROM business_memberships WHERE principal_id = ? AND active = 1", (principal.id,)))
            context = replace(context, allowed_business_ids=profile.business_ids & memberships,
                              global_capabilities=profile.global_capabilities,
                              profile_revision=profile.revision, policy_revision=profile.revision)
        if principal.kind == "human":
            memberships = frozenset(row[0] for row in uow.connection.execute(
                "SELECT business_id FROM business_memberships WHERE principal_id = ? AND active = 1", (principal.id,)))
            context = replace(context, allowed_business_ids=context.allowed_business_ids & memberships)
        if principal.kind == "human" and self.human_global_capabilities is not None:
            provider = self.human_global_capabilities
            capabilities = provider(principal.id, uow) if callable(provider) else provider.get(principal.id, ())
            context = replace(context, global_capabilities=frozenset(capabilities))
        if principal.kind != "human":
            context = replace(context, allowed_business_ids=context.allowed_business_ids & active_business_ids(uow.connection))
        if context.review_business_id and context.review_business_id not in context.allowed_business_ids:
            raise DomainError("forbidden", "Review project is no longer authorized")
        return context.select_business(context.business_id)

    def context_for(self, principal_id, *, business_id, review_business_id, uow,
                    initiated_by_principal_id=None, request_id=None, job_id=None, source=None):
        """Reconstitute an approval's proposer from current authoritative configuration."""
        actor = self._principal(principal_id, uow)
        values = {"source": source or {"agent": "mcp", "system": "worker", "human": "dashboard"}[actor.kind],
                  "business_id": business_id, "review_business_id": review_business_id,
                  "initiated_by_principal_id": initiated_by_principal_id}
        if request_id is not None:
            values["request_id"] = request_id
        if job_id is not None:
            values["job_id"] = job_id
        if actor.kind == "human":
            values["business_id"] = business_id or review_business_id
            values["allowed_business_ids"] = frozenset(row[0] for row in uow.connection.execute(
                "SELECT business_id FROM business_memberships WHERE principal_id = ? AND active = 1", (actor.id,)))
        return self.refresh_context(InvocationContext(actor, **values), uow)

    def _agent_decision(self, context, descriptor, uow):
        if self.agent_resolver is None:
            return PolicyDecision("deny", "fallback")
        # A read unit sees one snapshot, so the caller's profile and pinned role
        # cannot change while it is open; readers that decide many operations
        # (briefings, list visibility) reuse them. Writers always reload.
        memo = getattr(uow, "memo", None) if not getattr(uow, "writable", True) else None
        key = ("agent_policy_basis", context.principal_id)
        basis = memo.get(key) if memo is not None else None
        if basis is None:
            basis = self.agent_resolver.policy_basis(context, uow)
            if not isinstance(basis, AgentPolicyBasis):
                raise DomainError("invalid_policy", "Agent resolver returned an invalid policy basis")
            if memo is not None:
                memo[key] = basis
        decision = resolve_operation_policy(descriptor.name, basis.defaults, basis.overrides, self.registry,
                                            rules=self.rules, role_id=basis.role["id"],
                                            scope="global" if descriptor.global_capability and context.business_id is None else "business")
        if descriptor.plugin_requirement and not self.agent_resolver.plugin_declares(context, basis, descriptor, uow):
            return PolicyDecision("deny", "plugin_unavailable")
        return PolicyDecision(decision.mode, decision.provenance, {
            "profile_revision": basis.revision, "role": plain(basis.role),
            **descriptor.global_capability_evidence(context.global_capabilities),
            "plugin_requirement": descriptor.plugin_requirement,
        })

    def resolve(self, context, descriptor, targets, uow):
        context = self.refresh_context(context, uow)
        for target in targets:
            resource = getattr(target, "resource", target)
            if resource.business_id and resource.business_id not in context.allowed_business_ids:
                raise DomainError("forbidden", "Prepared resource is outside current scope")
        if descriptor.global_capability and descriptor.global_capability_selected(context.global_capabilities) is None:
            raise DomainError("forbidden", "Global capability is not granted")
        if descriptor.owner_only:
            return PolicyDecision("deny", "owner_only")
        if context.principal_kind == "human":
            if descriptor.name == "business_archive":
                installation = uow.repository("installation").get("service")
                return PolicyDecision("signoff" if installation and installation["owner_principal_id"] == context.principal_id else "deny", "business_archive_owner_approval")
            if descriptor.global_capability and context.business_id is None:
                installation = uow.repository("installation").get("service")
                if installation and installation["owner_principal_id"] == context.principal_id:
                    return PolicyDecision("allow", "installation_owner", {
                        "installation_revision": installation["revision"],
                        **descriptor.global_capability_evidence(context.global_capabilities)})
                return PolicyDecision("deny", "installation_owner_required")
            member = (uow.repository("business_memberships", business_id=context.business_id).get(context.principal_id)
                      if context.business_id else None)
            if not member or not member["active"] or (descriptor.effect_kind != "read" and not member["is_owner"]):
                return PolicyDecision("deny", "owner_required")
            return PolicyDecision("allow", "owner" if member["is_owner"] else "business_member", {"membership_revision": member["revision"],
                                  **descriptor.global_capability_evidence(context.global_capabilities)})
        if context.principal_kind == "agent":
            return self._agent_decision(context, descriptor, uow)
        profile = self.worker_profile(context.principal_id, uow)
        decision = resolve_operation_policy(descriptor.name, profile.operations, {}, self.registry, rules=self.rules)
        binding = {"worker_revision": profile.revision, **descriptor.global_capability_evidence(context.global_capabilities)}
        job_id = context.job_id
        verified = (self.job_consequence_verifier(context, descriptor, targets, uow)
                    if self.job_consequence_verifier is not None and (job_id or context.initiated_by_principal_id) else None)
        if verified is not None:
            digest = verified.get("definition_digest") if isinstance(verified, Mapping) else None
            if (not isinstance(verified, Mapping) or not job_id or verified.get("job_id") != job_id
                    or not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
                raise DomainError("invalid_policy", "Job consequence proof must bind this trusted job and its definition")
            binding["job_consequence"] = plain(verified)
        elif context.initiated_by_principal_id:
            historical = uow.repository("principals").get(context.initiated_by_principal_id)
            if historical is None:
                raise DomainError("invalid_principal", "Initiating principal is missing")
            if historical["kind"] == "agent":
                initiator = self._principal(context.initiated_by_principal_id, uow)
                original = self.context_for(initiator.id, business_id=context.business_id,
                    review_business_id=context.review_business_id, uow=uow)
                other = self.resolve(original, descriptor, targets, uow)
                binding["initiator"] = {"principal_id": initiator.id, "mode": other.mode,
                                         "binding": plain(other.binding)}
                mode = "deny" if "deny" in (decision.mode, other.mode) else (
                    "signoff" if "signoff" in (decision.mode, other.mode) else "allow")
                decision = PolicyDecision(mode, "worker_and_initiator")
        return PolicyDecision(decision.mode, "worker_and_initiator" if "initiator" in binding else "worker_profile", binding)
