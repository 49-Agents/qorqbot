"""Scoped Settings metadata and separately permissioned private-value reads."""

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
        if ctx.business_id is not None:
            member = uow.repository("business_memberships", business_id=ctx.business_id).get(ctx.principal_id)
            if not member or not member["active"]:
                raise DomainError("forbidden", "Settings values require active business membership")
        if args["key"] in IDENTITY_KEYS:
            raise DomainError("forbidden", "Runtime identity bindings are not readable Settings")
        try:
            # Deliberately no Global fallback for a business-scoped secret read.
            values = {pair["key"]: pair["value"] for pair in local.environment_pairs(ctx.business_id)}
            values.update(local.private_environment(ctx.business_id,
                role=local.settings_role(ctx, uow), owner=ctx.principal_kind == "human"))
        except (OSError, UnicodeError):
            raise DomainError("configuration_unavailable", "Settings could not be read") from None
        key = args["key"]
        result = {"key": key, "found": key in values, "value": values.get(key),
                  "scope": "business" if ctx.business_id else "global"}
        if len(canonical_bytes(result)) > 16000:
            raise DomainError("result_too_large", "Settings value exceeds the retrieval limit")
        return result

    runtime.registry.register(OperationDescriptor("environment_get", schema({
        "key": {"type": "string", "maxLength": 200, "pattern": r"^[A-Za-z_][A-Za-z0-9_]*$"}}, ("key",)),
        effect_kind="read", supports_signoff=False, exposure={"mcp", "dashboard"},
        scope_kinds={"business", "global"}, global_capability="read_global_context", read=get,
        resolve_namespace=lambda ctx, args: "business:" + ctx.business_id if ctx.business_id else "global",
        description="Retrieve one Settings value by key, including private context or credentials, only when "
            "environment_get is explicitly granted. Role-private values also require your registered role; "
            "they are never inherited across scopes. Business scope reads only that business's own Settings, "
            "without Global inheritance; Global scope reads Global Settings. Missing returns found:false/value:null; "
            "empty returns found:true/value:''. Values enter model context and may be saved in transcripts. "
            "Retrieve only what the task needs; never repeat secrets in messages, logs or tasks. "
            "Retrieval does not authorize payments or other external actions."))

    def read(ctx, args, options, uow):
        runtime.require_authority()
        if ctx.business_id is not None:
            member = uow.repository("business_memberships", business_id=ctx.business_id).get(ctx.principal_id)
            if not member or not member["active"]:
                raise DomainError("forbidden", "Settings metadata requires active business membership")
        effective = {}
        try:
            owner_session = ctx.owner_session_business_id is not None
            scopes = ([(ctx.business_id, "business")] if owner_session and ctx.business_id is not None
                      else [(None, "global")] + ([(ctx.business_id, "business")] if ctx.business_id is not None else []))
            for business_id, origin in scopes:
                for pair in local.environment_pairs(business_id):
                    if pair["key"] not in IDENTITY_KEYS:
                        effective[pair["key"]] = {"name": pair["key"], "configured": bool(pair["value"]), "origin": origin}
            for key, value in local.private_environment(ctx.business_id,
                    role=local.settings_role(ctx, uow), owner=owner_session).items():
                effective[key] = {"name": key, "configured": bool(value),
                                  "origin": "business" if ctx.business_id else "global"}
        except (OSError, UnicodeError):
            raise DomainError("configuration_unavailable", "Settings metadata could not be read") from None
        names = args.get("names", sorted(effective))
        items = [effective.get(name, {"name": name, "configured": False, "origin": None}) for name in names]
        items, cursor = page(items, args, ctx.business_id, "environment_list", key=lambda item: (item["name"],), reverse=False)
        result = {"items": items, "next_cursor": cursor}
        if len(canonical_bytes(result)) > 24000:
            raise DomainError("result_too_large", "Narrow the variable names or reduce the page limit")
        return result

    runtime.registry.register(OperationDescriptor("environment_list", schema({**PAGE,
        "names": {"type": "array", "minItems": 1, "maxItems": 100, "uniqueItems": True,
            "items": {"type": "string", "maxLength": 200, "pattern": r"^[A-Za-z_][A-Za-z0-9_]*$"}}}),
        effect_kind="read", supports_signoff=False, exposure={"mcp", "dashboard"},
        scope_kinds={"business", "global"}, global_capability="read_global_context", read=read,
        resolve_namespace=lambda ctx, args: "business:" + ctx.business_id if ctx.business_id else "global",
        description="List effective Settings variable names, configured (nonempty) status and origin (global/business). "
            "No values are returned. Role-private bindings are visible only to allowed registered roles in their scope. "
            "Optional names checks specific bindings; origin:null means absent or unavailable to your role. "
            "Business overrides, including empty overrides, win over Global. Follow next_cursor. "
            "Configured does not prove credentials are valid or that a tool can use them. "
            "Pass variable names to supported operations such as metrics source endpoint_env/token_env; "
            "the existing runtime resolves values. This tool does not grant script execution or secret access."))


def bootstrap_environment(runtime, ctx):
    """Use the normal policy-checked metadata read; never fetch secret values."""
    result = runtime.application.invoke(ctx, "environment_list", {"limit": 30})
    if result.status != "ok":
        return {"status": "unavailable", "items": [], "has_more": False}
    data = result.to_dict()["data"]
    return {"status": "ok", "items": data["items"],
            "has_more": data.get("next_cursor") is not None}
