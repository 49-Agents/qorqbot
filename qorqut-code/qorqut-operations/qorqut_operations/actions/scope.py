"""Who may read, record and change an Action. Storage namespace owns an Action; project links only widen reading.

These are the retained v1 rules, moved as they were: a project-selected caller without ``read_global_context`` records
for that project; the owner and granted global agents use Global storage; a Global project-operational Action names the
projects allowed to read its shared text. Only global agents act, so no caller has a home project here; Actions that
project-home agents recorded remain history in their owning scope. Recorder, owner and grants decide who may change
one, never a link or a tag."""

from __future__ import annotations

from qorqut_operations.actions.models import CATEGORIES, GLOBAL_AGENT_CATEGORIES
from qorqut_operations.project_lifecycle import active_project_ids
from qorqut_operations.contracts import DomainError
from qorqut_operations.global_agents import is_coordinator


def is_owner(ctx, uow) -> bool:
    if ctx.principal_kind != "human":
        return False
    installation = uow.repository("installation").get("service")
    return bool(installation) and installation["owner_principal_id"] == ctx.principal_id


def reader_scope(ctx, uow) -> dict:
    """Which memberships a read may see; storage namespace never grants scope."""
    if ctx.project_id is not None:
        # A project-selected invocation (a project member, or a global agent working in that project) sees that
        # project only.
        return {"kind": "project", "projects": frozenset({ctx.project_id}), "global_storage": False}
    if ctx.principal_kind == "human":
        if not is_owner(ctx, uow):
            raise DomainError("forbidden", "Global actions require the installation owner or an authorized global agent")
        return {"kind": "owner", "projects": frozenset(ctx.allowed_project_ids), "global_storage": True}
    if ctx.principal_kind == "agent" and "read_global_context" in ctx.global_capabilities:
        return {"kind": "ecosystem", "projects": frozenset(ctx.allowed_project_ids), "global_storage": True}
    raise DomainError("forbidden", "Actions require an owner, a project member or an authorized global agent")


def action_namespace(ctx, args) -> str:
    """A project-selected caller without ``read_global_context`` records in that project; owner and granted global
    invocations use Global storage."""
    if ctx.project_id and "read_global_context" not in ctx.global_capabilities:
        return "project:" + ctx.project_id
    return "global"


def change_namespace(ctx, args) -> str:
    """Changing an Action happens in its owning namespace: the selected project, or Global when none is selected."""
    return "project:" + ctx.project_id if ctx.project_id else "global"


def links(uow, action_id) -> list[str]:
    return [row[0] for row in uow.connection.execute(
        "SELECT project_id FROM action_projects WHERE action_id=? ORDER BY project_id", (action_id,))]


def visible(scope, row, row_links) -> bool:
    if scope["kind"] == "owner":
        return True
    if scope["kind"] == "project":
        return bool(set(row_links) & scope["projects"])
    return row["category"] == "global-operational" or (
        row["category"] == "project-operational" and bool(set(row_links) & scope["projects"]))


def evidence_visible(scope, row, row_links) -> bool:
    """Evidence (refs, related objects, storage identity) follows access, not the storage namespace.

    A project-stored row's evidence belongs to that project. A Global-stored row's evidence is shown to non-owners
    only when they may access every linked project, so a shared action never exposes another project's opaque
    references."""
    if scope["kind"] == "owner":
        return True
    if row["project_id"] is not None:
        return row["project_id"] in scope["projects"]
    if not scope["global_storage"]:
        return False
    return set(row_links) <= scope["projects"]


def scope_clause(scope):
    """Authorization as SQL: memberships, never the storage namespace, decide which actions a reader sees."""
    if scope["kind"] == "owner":
        return "1", []
    allowed = sorted(scope["projects"])
    marks = ",".join("?" for _ in allowed) or "NULL"
    linked = f"EXISTS (SELECT 1 FROM action_projects m WHERE m.action_id=a.id AND m.project_id IN ({marks}))"
    if scope["kind"] == "project":
        return linked, allowed
    return f"(a.category='global-operational' OR (a.category='project-operational' AND {linked}))", allowed


def classify(ctx, uow, category, project_ids, *, owner_category_optional=True):
    """Resolve ``(category, projects)`` for the caller; any inaccessible project rejects the whole call."""
    requested = list(dict.fromkeys(project_ids or []))
    home = ctx.project_id if action_namespace(ctx, {}) != "global" else None
    if home is not None:
        if category not in (None, "project-operational"):
            raise DomainError("forbidden", "Project actions are project-operational for the selected project")
        if requested and requested != [home]:
            raise DomainError("forbidden", "Project-stored actions cannot select other or additional projects")
        return "project-operational", [home]
    if ctx.principal_kind == "agent":
        if category is None:
            raise DomainError("validation_error", "Global agents must choose global-operational or project-operational",
                              field_errors={"category": "required"})
        if category not in GLOBAL_AGENT_CATEGORIES:
            raise DomainError("forbidden", "Global agents may record only global-operational or project-operational actions")
    elif ctx.principal_kind != "human" or not owner_category_optional:
        raise DomainError("forbidden", "Actions are recorded by the owner or registered agents")
    if category == "project-operational":
        if not requested:
            raise DomainError("validation_error", "Project-operational actions need at least one project",
                              field_errors={"project_ids": "required"})
        active = active_project_ids(uow.connection)
        if any(project not in ctx.allowed_project_ids or project not in active for project in requested):
            raise DomainError("forbidden", "Every selected project must be currently authorized and active")
        return category, requested
    if requested:
        raise DomainError("validation_error", "Only project-operational actions name projects",
                          field_errors={"project_ids": "not allowed for this category"})
    return category, []


def classification_from_task(category, project_ids, task_project_id):
    """``(category, project_ids)`` with what a Global recorder left out taken from the Task its Action links.

    The owner and global agents record in Global whatever project the work was for, so the Task says which it was: with no
    category, a project Task makes the Action project-operational and a Global Task global-operational; a
    project-operational Action that names no project is for the Task's project. What the caller passed is never changed,
    and ``classify`` authorizes the result exactly as if the caller had passed all of it."""
    if category is None:
        category = "global-operational" if task_project_id is None else "project-operational"
    if category == "project-operational" and not project_ids and task_project_id is not None:
        project_ids = [task_project_id]
    return category, project_ids


def may_change(ctx, uow, row) -> bool:
    """The original recorder, owner or configured coordinator can maintain an accessible Action in its owning scope."""
    if row is None or row["project_id"] != ctx.project_id:
        return False
    return (row["recorded_by"] == ctx.principal_id or is_owner(ctx, uow)
            or ctx.principal_kind == 'agent' and not ctx.delegation_assignment_id
            and is_coordinator(uow.connection, ctx.principal_id))


__all__ = ["CATEGORIES", "action_namespace", "change_namespace", "classification_from_task", "classify", "evidence_visible", "is_owner", "links",
           "may_change", "reader_scope", "scope_clause", "visible"]
