"""Where a prepared change is reviewed, and the trusted exceptions to "project content needs a project proposal".

A globally owned operation may not stage effects on project-scoped rows: project content requires a separate
project-owned proposal. A few v1 operations legitimately do (execution records of a global routine definition,
one owner-selected document in the unified journal). Each such
exception is a typed ``ReviewScopeException`` made by trusted composition and handed to ``Application``; there is
no other way to widen the rule, and an unlisted effect still fails closed with ``invalid_plan``."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from types import MappingProxyType
from typing import Mapping

from qorqut_operations.contracts import DomainError


@dataclass(frozen=True)
class BoundResource:
    """A resource that is allowed only when its identity equals a named operation argument.

    ``json_key`` selects the field of a composite (canonical JSON) resource ID; without it the plain
    resource ID is compared."""

    argument: str
    json_key: str | None = None

    def matches(self, resource, arguments) -> bool:
        wanted = arguments.get(self.argument)
        if wanted is None:
            return False
        if self.json_key is None:
            return resource.resource_id == wanted
        try:
            identity = json.loads(resource.resource_id)
        except (ValueError, TypeError):
            return False
        return isinstance(identity, dict) and identity.get(self.json_key) == wanted


@dataclass(frozen=True)
class ReviewScopeException:
    operations: frozenset[str]
    # Resource types that are an effect of the operation's global definition whatever their identity.
    resource_types: frozenset[str] = frozenset()
    # Resource types allowed only when bound to an operation argument.
    bound_resources: Mapping[str, BoundResource] = field(default_factory=dict)
    # Restrict to these principal kinds (None: any), and optionally refuse delegated workers.
    principal_kinds: frozenset[str] | None = None
    allow_delegated: bool = True

    def __post_init__(self):
        if not self.operations or any(not isinstance(name, str) for name in self.operations):
            raise ValueError("A review exception names its operations")
        bound = dict(self.bound_resources)
        if any(not isinstance(value, BoundResource) for value in bound.values()):
            raise ValueError("Bound resources must be BoundResource values")
        object.__setattr__(self, "operations", frozenset(self.operations))
        object.__setattr__(self, "resource_types", frozenset(self.resource_types))
        object.__setattr__(self, "bound_resources", MappingProxyType(bound))
        if self.principal_kinds is not None:
            object.__setattr__(self, "principal_kinds", frozenset(self.principal_kinds))

    def permits(self, context, plan, resource) -> bool:
        if plan.operation_name not in self.operations:
            return False
        if self.principal_kinds is not None and context.principal_kind not in self.principal_kinds:
            return False
        if not self.allow_delegated and context.delegation_assignment_id:
            return False
        if resource.resource_type in self.resource_types:
            return True
        bound = self.bound_resources.get(resource.resource_type)
        return bound is not None and bound.matches(resource, plan.arguments)


def review_scope(context, plan, exceptions=()):
    """The review destination of ``plan`` from its target ownership; project content in a global plan is
    refused unless a trusted exception permits that exact resource."""
    project = plan.target_namespace.removeprefix("project:") if plan.target_namespace.startswith("project:") else None
    scope = {"kind": "project", "project_id": project} if project else {"kind": "global"}
    for change in plan.changes:
        target = change.resource
        if target.project_id and project is None:
            if not any(exception.permits(context, plan, target) for exception in exceptions):
                raise DomainError("invalid_plan", "Project content requires a separate project-owned proposal")
    return scope
