"""Scoped Settings metadata and separately permissioned private-value reads."""

from qorqut_agentic.behavior.tool_help import text as instruction_text

from qorqut.agents.global_scope import active_project_participant
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, canonical_bytes
from qorqut_operations.application.plans import PAGE, page, schema
from .local import IDENTITY_KEYS, LocalFiles


def register_environment(runtime):
    local = LocalFiles(runtime)

    def get(ctx, args, options, uow):
        runtime.require_authority()
        if ctx.principal_kind == "human":
            local.require_environment_owner(ctx, uow)
        elif ctx.principal_kind != "agent":
            raise DomainError("forbidden", "Settings values require an authorized agent or owner")
        # The owner and agents reach active projects by the shared rule; agents are active supported globals.
        if ctx.project_id is not None and not active_project_participant(uow.connection, ctx.project_id, ctx.principal_id):
            raise DomainError("forbidden", "Settings values require active project access")
        if args["key"] in IDENTITY_KEYS:
            raise DomainError("forbidden", "Runtime identity bindings are not readable Settings")
        try:
            # Deliberately no Global fallback for a project-scoped secret read.
            values = {pair["key"]: pair["value"] for pair in local.environment_pairs(ctx.project_id)}
            values.update(local.private_environment(ctx.project_id,
                role=local.settings_role(ctx, uow), owner=ctx.principal_kind == "human"))
        except (OSError, UnicodeError):
            raise DomainError("configuration_unavailable", "Settings could not be read") from None
        key = args["key"]
        result = {"key": key, "found": key in values, "value": values.get(key),
                  "scope": "project" if ctx.project_id else "global"}
        if len(canonical_bytes(result)) > 16000:
            raise DomainError("result_too_large", "Settings value exceeds the retrieval limit")
        return result

    runtime.registry.register(OperationDescriptor("environment_get", schema({
        "key": {"type": "string", "maxLength": 200, "pattern": r"^[A-Za-z_][A-Za-z0-9_]*$"}}, ("key",)),
        effect_kind="read", supports_signoff=False, exposure={"mcp", "dashboard"},
        scope_kinds={"project", "global"}, global_capability="read_global_context", read=get,
        resolve_namespace=lambda ctx, args: "project:" + ctx.project_id if ctx.project_id else "global",
        description=instruction_text('environment-retrieve-one-settings-value-by-key-including-private')))

    def read(ctx, args, options, uow):
        runtime.require_authority()
        if ctx.project_id is not None and not active_project_participant(uow.connection, ctx.project_id, ctx.principal_id):
            raise DomainError("forbidden", "Settings metadata requires active project access")
        effective = {}
        try:
            owner_session = ctx.owner_session_project_id is not None
            scopes = ([(ctx.project_id, "project")] if owner_session and ctx.project_id is not None
                      else [(None, "global")] + ([(ctx.project_id, "project")] if ctx.project_id is not None else []))
            for project_id, origin in scopes:
                for pair in local.environment_pairs(project_id):
                    if pair["key"] not in IDENTITY_KEYS:
                        effective[pair["key"]] = {"name": pair["key"], "configured": bool(pair["value"]), "origin": origin}
            for key, value in local.private_environment(ctx.project_id,
                    role=local.settings_role(ctx, uow), owner=owner_session).items():
                effective[key] = {"name": key, "configured": bool(value),
                                  "origin": "project" if ctx.project_id else "global"}
        except (OSError, UnicodeError):
            raise DomainError("configuration_unavailable", "Settings metadata could not be read") from None
        names = args.get("names", sorted(effective))
        items = [effective.get(name, {"name": name, "configured": False, "origin": None}) for name in names]
        items, cursor = page(items, args, ctx.project_id, "environment_list", key=lambda item: (item["name"],), reverse=False)
        result = {"items": items, "next_cursor": cursor}
        if len(canonical_bytes(result)) > 24000:
            raise DomainError("result_too_large", "Narrow the variable names or reduce the page limit")
        return result

    runtime.registry.register(OperationDescriptor("environment_list", schema({**PAGE,
        "names": {"type": "array", "minItems": 1, "maxItems": 100, "uniqueItems": True,
            "items": {"type": "string", "maxLength": 200, "pattern": r"^[A-Za-z_][A-Za-z0-9_]*$"}}}),
        effect_kind="read", supports_signoff=False, exposure={"mcp", "dashboard"},
        scope_kinds={"project", "global"}, global_capability="read_global_context", read=read,
        resolve_namespace=lambda ctx, args: "project:" + ctx.project_id if ctx.project_id else "global",
        description=instruction_text('environment-list-effective-settings-variable-names-configured-nonempty-status')))


def bootstrap_environment(runtime, ctx):
    """Use the normal policy-checked metadata read; never fetch secret values."""
    result = runtime.application.invoke(ctx, "environment_list", {"limit": 30})
    if result.status != "ok":
        return {"status": "unavailable", "items": [], "has_more": False}
    data = result.to_dict()["data"]
    return {"status": "ok", "items": data["items"],
            "has_more": data.get("next_cursor") is not None}
