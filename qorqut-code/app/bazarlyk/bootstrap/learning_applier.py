"""Explicit standalone learning-applier provisioning; never launches a session."""

from qorqut_operations.contracts import DomainError, InvocationOptions, Principal, canonical_hash, plain
from .preparation import prepare_agent

CAPABILITIES = ("read_global_context", "manage_shared_instructions", "publish_roles", "publish_plugins")
ASSIGNMENT = "Work with the owner to select and implement learning deltas across authorized businesses and shared repositories. Maintain work notes and states; commit and push each delta, then guide optional session-end publication."


def prepare_learning_applier(runtime, owner_context, *, role_ref, allowed_business_ids, endpoint, claude_version=None, versions=None):
    if owner_context.principal_kind != "human":
        raise DomainError("forbidden", "Learning-applier preparation requires the owner")
    if role_ref.get("id") != "learning-applier":
        raise DomainError("validation_error", "Select the published learning-applier role")
    scope = {"kind": "ecosystem", "allowed_business_ids": sorted(set(allowed_business_ids)), "global_capabilities": sorted(CAPABILITIES)}
    if not scope["allowed_business_ids"] or not set(scope["allowed_business_ids"]) <= owner_context.allowed_business_ids:
        raise DomainError("forbidden", "Select explicitly authorized businesses")
    arguments = {"name": "learning-applier", "display_name": "Learning Applier", "role": plain(role_ref),
                 "scope": scope, "assignment_brief": ASSIGNMENT, "plugins": []}
    # Creation is authorized even on retry; preparation never silently changes an
    # existing profile's pins, permissions or assigned business scope.
    with runtime.database.read() as uow:
        ctx = runtime.application.current_context(owner_context, uow)
        if not set(scope["allowed_business_ids"]) <= ctx.allowed_business_ids or not set(CAPABILITIES) <= ctx.global_capabilities:
            raise DomainError("forbidden", "Learning-applier scope exceeds current owner grants")
        descriptor = runtime.registry.get("agents_create")
        namespace = runtime.application.namespace(ctx, descriptor, arguments)
        runtime.application.authorize(ctx, descriptor, (), uow, namespace=namespace)
        existing = uow.connection.execute("SELECT * FROM agent_profiles WHERE home_business_id IS NULL AND name='learning-applier'").fetchone()
    if existing:
        from qorqut_operations.storage.repositories import decode_row
        row = decode_row(existing)
        profile = row["profile_json"]
        if (row["status"] != "active" or profile["role"] != plain(role_ref)
                or profile["scope"] != scope or profile["plugins"] or profile["assignment_brief"] != ASSIGNMENT
                or profile["tool_policy"]["overrides"]):
            raise DomainError("bootstrap_conflict", "Existing learning-applier profile differs; review its configuration explicitly")
        identity = row["agent_id"]
    else:
        result = runtime.application.invoke_or_raise(owner_context, "agents_create", arguments,
            InvocationOptions(idempotency_key="prepare-learning-applier:" + canonical_hash(arguments)))
        if result.status != "ok":
            return result.to_dict()
        identity = result.data["agent_id"]
    if versions is None and claude_version:
        versions = {"claude-code": claude_version}
    return prepare_agent(runtime.resolver, Principal(identity, "agent"), endpoint=endpoint, versions=versions)
