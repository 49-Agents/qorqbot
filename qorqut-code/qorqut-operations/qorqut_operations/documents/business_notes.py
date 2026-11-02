"""Ordinary project notes: the registered kind and the explicit project every write to one names."""

from qorqut_operations.contracts import DomainError

KIND = "business_owned_note"
TARGET = "target_business_id"


def require_target(ctx, args, business_id):
    """Writes name their business explicitly; a stale selection cannot redirect them."""
    target = args.get(TARGET)
    if target is None:
        raise DomainError("validation_error", "Project notes require target_business_id")
    if target != ctx.business_id or target != business_id:
        raise DomainError("forbidden", "target_business_id differs from the selected or owning project")
    return target


def require_project_access(ctx, before, kind=None, business_id=None):
    """Every write path to a project note stays inside the selected project the principal may reach.

    The operation grant is checked by the operation (or composed effect) that writes; this adds no author rule.
    """
    if (before or {}).get("kind") != KIND and kind != KIND:
        return
    owner = (before or {}).get("business_id") or business_id
    if owner is None or owner != ctx.business_id or owner not in ctx.allowed_business_ids:
        raise DomainError("forbidden", "Project notes change only inside their selected project")
