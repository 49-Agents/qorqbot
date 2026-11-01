"""Delegated-worker authorization: live parent authority intersected with one exact saved assignment binding.

The checks are the deployed v1 ones, unchanged. Only the stored rows and the host's vocabulary (worker control
operations, record-selector argument names, role support) come from the injected ``DelegationProvider``; with no
provider every delegated call is refused."""

from __future__ import annotations

from qorqut_operations.contracts import DomainError


def authorize_delegation(provider, context, descriptor, targets, uow, *, namespace=None, arguments=None):
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
