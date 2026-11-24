"""Ordinary project notes: the registered kind and the explicit project every write to one names."""

from qorqut_operations.contracts import DomainError

KIND = "project_owned_note"
TARGET = "target_project_id"


def require_target(ctx, args, project_id):
    """Writes name their project explicitly; a stale selection cannot redirect them."""
    target = args.get(TARGET)
    if target is None:
        raise DomainError("validation_error", "Project notes require target_project_id")
    if target != ctx.project_id or target != project_id:
        raise DomainError("forbidden", "target_project_id differs from the selected or owning project")
    return target


def require_project_access(ctx, before, kind=None, project_id=None):
    """Every write path to a project note stays inside the selected project the principal may reach.

    The operation grant is checked by the operation (or composed effect) that writes; this adds no author rule.
    """
    if (before or {}).get("kind") != KIND and kind != KIND:
        return
    owner = (before or {}).get("project_id") or project_id
    if owner is None or owner != ctx.project_id or owner not in ctx.allowed_project_ids:
        raise DomainError("forbidden", "Project notes change only inside their selected project")
