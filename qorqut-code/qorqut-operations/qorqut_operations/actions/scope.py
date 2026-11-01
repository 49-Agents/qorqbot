"""Who may read, record and change an Action. Storage namespace owns an Action; business links only widen reading.

These are the retained v1 rules, moved as they were: a business agent records for its home business; the owner and
granted global agents use Global storage; a Global business-operational Action names the businesses allowed to read
its shared text. Recorder, owner and grants decide who may change one, never a link or a tag."""

from __future__ import annotations

from qorqut_operations.actions.models import CATEGORIES, GLOBAL_AGENT_CATEGORIES
from qorqut_operations.business_lifecycle import active_business_ids
from qorqut_operations.contracts import DomainError


def is_owner(ctx, uow) -> bool:
    if ctx.principal_kind != "human":
        return False
    installation = uow.repository("installation").get("service")
    return bool(installation) and installation["owner_principal_id"] == ctx.principal_id


def reader_scope(ctx, uow) -> dict:
    """Which memberships a read may see; storage namespace never grants scope."""
    if ctx.home_business_id is not None:
        return {"kind": "business", "businesses": frozenset({ctx.home_business_id}), "global_storage": False}
    if ctx.business_id is not None:
        # A non-root business invocation (for example a business member) sees that business only.
        return {"kind": "business", "businesses": frozenset({ctx.business_id}), "global_storage": False}
    if ctx.principal_kind == "human":
        if not is_owner(ctx, uow):
            raise DomainError("forbidden", "Global actions require the installation owner or an authorized global agent")
        return {"kind": "owner", "businesses": frozenset(ctx.allowed_business_ids), "global_storage": True}
    if ctx.principal_kind == "agent" and "read_global_context" in ctx.global_capabilities:
        return {"kind": "ecosystem", "businesses": frozenset(ctx.allowed_business_ids), "global_storage": True}
    raise DomainError("forbidden", "Actions require an owner, a business agent or an authorized global agent")


def action_namespace(ctx, args) -> str:
    """Business agents record in their home business; owner and ecosystem invocations use Global storage."""
    if ctx.home_business_id is not None:
        return "business:" + ctx.home_business_id
    if ctx.business_id and "read_global_context" not in ctx.global_capabilities:
        return "business:" + ctx.business_id
    return "global"


def change_namespace(ctx, args) -> str:
    """Changing an Action happens in the Action's own owning namespace, which its canonical key names."""
    key = args.get("action_id")
    parts = key.split("/") if isinstance(key, str) else ()
    if len(parts) == 3 and parts[0] == "business":
        return "business:" + parts[1]
    if len(parts) == 2 and parts[0] == "global":
        return "global"
    return action_namespace(ctx, args)


def links(uow, key) -> list[str]:
    return [row[0] for row in uow.connection.execute(
        "SELECT business_id FROM action_businesses WHERE action_key=? ORDER BY business_id", (key,))]


def visible(scope, row, row_links) -> bool:
    if scope["kind"] == "owner":
        return True
    if scope["kind"] == "business":
        return bool(set(row_links) & scope["businesses"])
    return row["category"] == "global-operational" or (
        row["category"] == "business-operational" and bool(set(row_links) & scope["businesses"]))


def evidence_visible(scope, row, row_links) -> bool:
    """Evidence (refs, related objects, storage identity) follows access, not the storage namespace.

    A business-stored row's evidence belongs to that business. A Global-stored row's evidence is shown to non-owners
    only when they may access every linked business, so a shared action never exposes another business's opaque
    references."""
    if scope["kind"] == "owner":
        return True
    if row["business_id"] is not None:
        return row["business_id"] in scope["businesses"]
    if not scope["global_storage"]:
        return False
    return set(row_links) <= scope["businesses"]


def scope_clause(scope):
    """Authorization as SQL: memberships, never the storage namespace, decide which actions a reader sees."""
    if scope["kind"] == "owner":
        return "1", []
    allowed = sorted(scope["businesses"])
    marks = ",".join("?" for _ in allowed) or "NULL"
    linked = f"EXISTS (SELECT 1 FROM action_businesses m WHERE m.action_key=a.action_key AND m.business_id IN ({marks}))"
    if scope["kind"] == "business":
        return linked, allowed
    return f"(a.category='global-operational' OR (a.category='business-operational' AND {linked}))", allowed


def classify(ctx, uow, category, business_ids, *, owner_category_optional=True):
    """Resolve ``(category, businesses)`` for the caller; any inaccessible business rejects the whole call."""
    requested = list(dict.fromkeys(business_ids or []))
    home = ctx.home_business_id or (ctx.business_id if action_namespace(ctx, {}) != "global" else None)
    if home is not None:
        if category not in (None, "business-operational"):
            raise DomainError("forbidden", "Business actions are business-operational for your home business")
        if requested and requested != [home]:
            raise DomainError("forbidden", "Business agents cannot select other or additional businesses")
        return "business-operational", [home]
    if ctx.principal_kind == "agent":
        if category is None:
            raise DomainError("validation_error", "Global agents must choose global-operational or business-operational",
                              field_errors={"category": "required"})
        if category not in GLOBAL_AGENT_CATEGORIES:
            raise DomainError("forbidden", "Global agents may record only global-operational or business-operational actions")
    elif ctx.principal_kind != "human" or not owner_category_optional:
        raise DomainError("forbidden", "Actions are recorded by the owner or registered agents")
    if category == "business-operational":
        if not requested:
            raise DomainError("validation_error", "Business-operational actions need at least one business",
                              field_errors={"business_ids": "required"})
        active = active_business_ids(uow.connection)
        if any(business not in ctx.allowed_business_ids or business not in active for business in requested):
            raise DomainError("forbidden", "Every selected business must be currently authorized and active")
        return category, requested
    if requested:
        raise DomainError("validation_error", "Only business-operational actions name businesses",
                          field_errors={"business_ids": "not allowed for this category"})
    return category, []


def may_change(ctx, uow, row) -> bool:
    """Correction and withdrawal belong to the Action's original recorder in its owning scope, or to the owner."""
    if row is None or row["business_id"] != ctx.business_id:
        return False
    return row["recorded_by"] == ctx.principal_id or is_owner(ctx, uow)


__all__ = ["CATEGORIES", "action_namespace", "change_namespace", "classify", "evidence_visible", "is_owner", "links", "may_change", "reader_scope",
           "scope_clause", "visible"]
