"""Delegated-worker authorization: live parent authority intersected with one exact saved assignment binding.

The checks are the deployed v1 ones, unchanged. Only the stored rows and the host's vocabulary (worker control
operations, record-selector argument names, role support) come from the injected ``DelegationProvider``; with no
provider every delegated call is refused.

The one addition is a typed ``DelegatedCreationGroup`` made by trusted assembly code: the records one operation creates
together under identities allocated while it prepares (a queue input and its companion binding) cannot be named by an
assignment in advance, so a *complete* group of expected-absent targets sharing one new UUID is exempt from the assignment's
record-ID membership and from nothing else."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from qorqut_operations.contracts import OPERATION_NAME, DomainError, ExpectedTarget


@dataclass(frozen=True)
class DelegatedCreationGroup:
    """One operation and the resource types it creates together, as one new record each, under one fresh identity.

    The operation's name must equal the descriptor's exactly and the resource types are at least two distinct names. A group
    is authority vocabulary supplied by the trusted host; no caller argument, assignment or tool policy can install one."""

    operation_name: str
    resource_types: frozenset[str]

    def __post_init__(self):
        if not isinstance(self.operation_name, str) or not OPERATION_NAME.fullmatch(self.operation_name):
            raise ValueError("A delegated creation group names one operation")
        types = self.resource_types
        if isinstance(types, (str, bytes)) or not hasattr(types, "__iter__"):
            raise ValueError("A delegated creation group lists its resource types")
        listed = list(types)
        if (len(listed) < 2 or len(set(listed)) != len(listed)
                or any(not isinstance(name, str) or not OPERATION_NAME.fullmatch(name) for name in listed)):
            raise ValueError("A delegated creation group names at least two distinct resource types")
        object.__setattr__(self, "resource_types", frozenset(listed))


def creation_groups(groups) -> tuple[DelegatedCreationGroup, ...]:
    """Validate the trusted assembly input: typed groups, at most one rule per operation."""
    groups = tuple(groups)
    if any(type(group) is not DelegatedCreationGroup for group in groups):
        raise TypeError("Delegated creation groups are typed contributions made by trusted assembly code")
    names = [group.operation_name for group in groups]
    if len(set(names)) != len(names):
        raise ValueError("An operation has at most one delegated creation group")
    return groups


def _canonical_uuid(value) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def _is_complete_group(groups, operation_name, targets, namespace) -> bool:
    """Whether the whole expected-target list of this operation is exactly one matching creation group.

    Every target is a typed ``ExpectedTarget`` for a distinct declared type and no other, expected absent, with no revision or
    hash, the same canonical new UUID, and the admission's own business (or Global) scope. A resource namespace is absent or
    exactly the canonical admission namespace. Anything else keeps the assignment's record boundary."""
    group = next((item for item in groups if item.operation_name == operation_name), None)
    if group is None or not isinstance(namespace, str):
        return False
    if namespace == "global":
        business_id = None
    elif namespace.startswith("business:"):
        business_id = namespace.removeprefix("business:")
    else:
        return False
    targets = tuple(targets)
    if (len(targets) != len(group.resource_types) or any(not isinstance(target, ExpectedTarget) for target in targets)
            or {target.resource.resource_type for target in targets} != group.resource_types):
        return False
    identities = {target.resource.resource_id for target in targets}
    if len(identities) != 1 or not _canonical_uuid(next(iter(identities))):
        return False
    return all(not target.exists and target.revision is None and target.content_hash is None
               and target.resource.business_id == business_id and target.resource.namespace in (None, namespace)
               for target in targets)


def authorize_delegation(provider, context, descriptor, targets, uow, *, namespace=None, arguments=None, creation_groups=()):
    if provider is None:
        raise DomainError("forbidden", "Delegated execution is not installed")
    operation_name = descriptor.name if hasattr(descriptor, "name") else descriptor

    binding = provider.binding(uow, context.delegation_binding_id)
    if (binding is None or binding["assignment_id"] != context.delegation_assignment_id
            or binding["parent_principal_id"] != context.principal_id
            or binding["execution_generation"] != context.delegation_generation
            or binding["revoked_at"] is not None):
        raise DomainError("forbidden", "Delegated worker binding is missing, revoked, or mismatched")
    role_ref = context.role_artifact_ref
    if (role_ref is None or not provider.supports_role(role_ref)
            or not provider.required_capabilities(role_ref) <= context.global_capabilities
            or "manage_delegated_assignments" not in context.global_capabilities):
        raise DomainError("forbidden", "Delegated execution requires a compatible global peer role and explicit capabilities")
    # Reuse the authentication invariant even for long-running calls: expiry,
    # assignment state, generation, principal, registration, and lease are
    # checked again at prepare and apply, not only when MCP authenticated.
    live = provider.authenticate_binding(uow, binding["credential_hash"])
    if live is None or live["id"] != binding["id"]:
        raise DomainError("forbidden", "Delegated worker binding is no longer live")
    assignment = provider.assignment(uow, context.delegation_assignment_id)
    if assignment is None:
        raise DomainError("forbidden", "Delegated assignment is missing")
    worker_control = (context.delegation_assignment_id is not None
                      and operation_name in provider.worker_operations)
    if operation_name not in assignment["allowed_operations_json"] and not worker_control:
        raise DomainError("forbidden", "Operation is outside the approved assignment")
    if (getattr(descriptor, "owner_only", False)
            or getattr(descriptor, "signoff_category", "internal_change") == "platform_action"):
        raise DomainError("forbidden", "Delegated workers cannot execute owner-only or externally consequential operations")
    scope = assignment["scope_json"]
    if assignment["business_id"] is not None:
        if worker_control:
            if (assignment["business_id"] not in context.allowed_business_ids
                    or namespace not in {"global", "business:" + assignment["business_id"]}):
                raise DomainError("forbidden", "Worker control operation is outside its assigned business")
        elif namespace != "business:" + assignment["business_id"] or context.business_id != assignment["business_id"]:
            raise DomainError("forbidden", "Delegated operation is outside its exact business scope")
    elif namespace != "global":
        business_id = namespace.removeprefix("business:") if isinstance(namespace, str) else None
        agent_config_scope = (operation_name == "agent_config_get"
                              and namespace == "agent:" + (context.agent_id or ""))
        if not agent_config_scope and (not isinstance(namespace, str)
                or not namespace.startswith("business:")
                or business_id not in scope.get("allowed_business_ids", ())
                or business_id not in context.allowed_business_ids):
            raise DomainError("forbidden", "Delegated operation is outside its approved global business scope")
    if namespace == "global" and scope["kind"] != "global" and not worker_control:
        raise DomainError("forbidden", "Business assignments cannot access global operations")
    record_ids = scope.get("record_ids", {})
    # Only a complete creation group of the trusted assembly skips the record-ID membership below; every check above and the
    # business boundary of each target still apply to it.
    new_group = not worker_control and _is_complete_group(creation_groups, operation_name, targets, namespace)
    for target in targets:
        resource = getattr(target, "resource", target)
        if worker_control:
            if (resource.resource_type != "operational_assignments"
                    or resource.resource_id != assignment["id"]):
                raise DomainError("forbidden", "Worker control operations are bound to their own assignment")
            continue
        if resource.business_id is not None:
            allowed_businesses = ({assignment["business_id"]} if assignment["business_id"] else
                set(scope.get("allowed_business_ids", ())) & set(context.allowed_business_ids))
            if resource.business_id not in allowed_businesses:
                raise DomainError("forbidden", "Prepared target is outside the assignment business scope")
        if new_group:
            continue
        permitted = record_ids.get(resource.resource_type)
        if permitted is None or resource.resource_id not in permitted:
            raise DomainError("forbidden", "Prepared record is outside the assignment record boundary")
    if arguments is not None:
        if worker_control and operation_name == "delegated_assignment_get":
            if arguments.get("assignment_id") != assignment["id"]:
                raise DomainError("forbidden", "Worker may read only its own assignment")
            return assignment
        # Any explicit record selector is checked before prepare as well as
        # against prepared targets. Broad reads need dedicated filtered
        # handlers; ordinary writes are checked again on their exact plan.
        selectors = provider.selector_kinds(operation_name)
        candidates = [(selectors[key], value) for key, value in arguments.items()
                      if key in selectors and isinstance(value, str)]
        scoped_context_reads = provider.context_scoped_reads
        if operation_name == "agent_config_get":
            requested_business = arguments.get("business_id")
            if requested_business is not None:
                if assignment["business_id"] is not None:
                    in_scope = requested_business == assignment["business_id"]
                else:
                    in_scope = (requested_business in scope.get("allowed_business_ids", ())
                                and requested_business in context.allowed_business_ids)
                if (not in_scope or (isinstance(namespace, str) and namespace.startswith("business:")
                                     and namespace != "business:" + requested_business)):
                    raise DomainError("forbidden", "Agent configuration business is outside the assignment scope")
        links = assignment["links_json"]
        linked_threads = set(links.get("message_threads", ()))
        message_threads = set(record_ids.get("message_threads", ())) | linked_threads
        for kind, value in candidates:
            if kind == "message_threads" and value not in message_threads:
                if provider.thread_has_assignment_message(uow, value, assignment["id"]):
                    message_threads.add(value)
        if ((getattr(descriptor, "effect_kind", None) == "read" and not candidates
             and operation_name not in scoped_context_reads)
                or any((value not in message_threads if kind == "message_threads"
                        else record_ids.get(kind) is None or value not in record_ids[kind])
                       for kind, value in candidates)):
            raise DomainError("forbidden", "Delegated reads require an explicit in-boundary record selector")
    return assignment
