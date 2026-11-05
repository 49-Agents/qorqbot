"""Operation policy resolution: hard Operations rules plus trusted, contributed domain rules.

Precedence, in order (the first phase that produces a decision wins):

1. both permission maps must name known canonical operations with valid modes (otherwise ``invalid_policy``);
2. an unknown operation is denied (``fallback``);
3. an owner-only operation is denied to every agent and worker (``owner_only``);
4. ``PolicyRules.before_override`` rules, in order;
5. the agent's explicit override for the operation (``agent_override``);
6. ``PolicyRules.after_override`` rules;
7. the role/worker default for the operation (``role_default``);
8. ``PolicyRules.after_default`` rules;
9. deny (``fallback``).

The deployed v1 resolver interleaved its per-domain rules (Telegram, tables, products, email, inbox, ...) in this
same order; those rules are domain knowledge and are contributed by the retained domains as ``PolicyRules`` made by
trusted assembly code. Nothing here widens authority: a rule may return a decision or ``None`` and cannot see
or alter request data beyond the operation name and the permission maps."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from qorqut_operations.contracts import DomainError, PolicyDecision

MODES = ("allow", "signoff", "deny")

# (name, defaults, overrides, registry, *, role_id, scope, resolve) -> PolicyDecision | None
# ``resolve(other_operation, scope="business")`` re-enters the resolution for another operation under the same maps.
PolicyRule = Callable[..., "PolicyDecision | None"]


def _rule_tuple(name, rules):
    rules = tuple(rules)
    if any(not callable(rule) for rule in rules):
        raise ValueError(f"{name} must be callable policy rules")
    return rules


@dataclass(frozen=True)
class PolicyRules:
    before_override: tuple[PolicyRule, ...] = ()
    after_override: tuple[PolicyRule, ...] = ()
    after_default: tuple[PolicyRule, ...] = ()

    def __post_init__(self):
        for name in ("before_override", "after_override", "after_default"):
            object.__setattr__(self, name, _rule_tuple(name, getattr(self, name)))


def validate_modes(values, registry):
    if not isinstance(values, Mapping):
        raise DomainError("invalid_policy", "Permission rules must be an operation map")
    for name, mode in values.items():
        if not registry.known(name) or registry.canonical_name(name) != name:
            raise DomainError("invalid_policy", "Permission rules require known canonical operation IDs")
        metadata = registry.metadata(name)
        if mode not in MODES:
            raise DomainError("invalid_policy", "Unknown permission mode")
        if mode == "signoff" and not metadata.supports_signoff:
            raise DomainError("invalid_policy", "This operation cannot support exact-change signoff")
        if metadata.owner_only and mode != "deny":
            raise DomainError("invalid_policy", "Owner-only decisions cannot be granted to an agent or worker")


def resolve_operation_policy(operation_name, defaults, overrides, registry, *, rules: PolicyRules, role_id=None, scope="business"):
    """Resolve a canonical mode without inventing role defaults or scope grants.

    ``scope`` ("business" or "global") is passed to the contributed rules, which alone give it meaning."""
    if not isinstance(rules, PolicyRules):
        raise TypeError("Policy resolution requires the trusted PolicyRules of the composed domains")
    validate_modes(defaults, registry)
    validate_modes(overrides, registry)
    if not registry.known(operation_name):
        return PolicyDecision("deny", "fallback")
    name = registry.canonical_name(operation_name)
    if registry.metadata(name).owner_only:
        return PolicyDecision("deny", "owner_only")

    def resolve(other, scope="business"):
        return resolve_operation_policy(other, defaults, overrides, registry, rules=rules, role_id=role_id, scope=scope)

    def apply(phase):
        for rule in phase:
            decision = rule(name, defaults, overrides, registry, role_id=role_id, scope=scope, resolve=resolve)
            if decision is not None:
                if not isinstance(decision, PolicyDecision):
                    raise DomainError("invalid_policy", "A policy rule returned an invalid decision")
                return decision
        return None

    decision = apply(rules.before_override)
    if decision is not None:
        return decision
    if name in overrides:
        return PolicyDecision(overrides[name], "agent_override")
    decision = apply(rules.after_override)
    if decision is not None:
        return decision
    if name in defaults:
        return PolicyDecision(defaults[name], "role_default")
    decision = apply(rules.after_default)
    if decision is not None:
        return decision
    return PolicyDecision("deny", "fallback")
