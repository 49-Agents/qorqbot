"""Canonical Project resolution and the one Project-record authority gate.

A Project record is one *existing* Project (`businesses` row), never a Task subtype, a shadow Story or a second
copy. Its canonical reference is ``{"type": "project", "id": PROJECT_UUID, "business_id": PROJECT_UUID}``: both
IDs must agree and name a real Project the caller may currently reach. The name and slug are read from the current
``businesses`` row, so a rename keeps the same conversation, links and Focus identity.

The new ``project_*`` operations belong to the installation owner and the exact configured active Ordubashy. That
is an identity gate checked on every call, independently of the transport and of policy: ordinary members, other
agents, historical identities and delegated workers are refused. Project reads never bypass the caller's ordinary
source authority: a Project's own scope is rechecked through the current active membership (humans) or the current
active-global reach (agents); every target read rechecks its own domain.
"""

from __future__ import annotations

from dataclasses import replace

from qorqut_operations.contracts import DomainError

from bazarlyk.agents.global_scope import active_business_participant
from bazarlyk.management.identity import private_participants

PROJECT_TYPE = "project"
# Only these already-supported private conversation sources get a Project inventory ``can_thread``. It is never
# inferred from a display label, a document kind or an extension's own item type.
THREAD_KINDS = frozenset({"task", "signoff", "project"})
UNAVAILABLE = frozenset({"forbidden", "not_found", "policy_denied", "operation_unavailable",
                         "business_archived", "context_refresh_required"})


def reference(project_id: str) -> dict:
    """The one canonical Project reference; both identifiers are the actual Project UUID."""
    return {"type": PROJECT_TYPE, "id": project_id, "business_id": project_id}


def business_row(ctx, project_id, uow, *, include_archived=False):
    """The stored Project row the caller may currently reach, with no domain read of the Project's records."""
    row = uow.repository("businesses").get(project_id)
    if row is None or not business_participant(ctx, project_id, uow):
        raise DomainError("not_found", "Project does not exist or is outside your current access")
    if row["archived_at"] is not None and not include_archived:
        raise DomainError("business_archived", "This project is archived and is not part of current work")
    return row


def business_participant(ctx, project_id, uow) -> bool:
    """Current reach of one Project: the active membership (humans) or active supported global reach (agents)."""
    return active_business_participant(uow.connection, project_id, ctx.principal_id) and project_id in ctx.allowed_business_ids


def project_id_from(args) -> str:
    """The one Project UUID every ``project_*`` call names, validated against the caller's selected scope."""
    project_id = args.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise DomainError("validation_error", "A project_id is required")
    return project_id


def selected_scope(ctx, args, project_id, uow):
    """The Project's own invocation context; a business route must agree with ``project_id``."""
    scope = args.get("scope", "business" if ctx.business_id is not None else "global")
    if scope not in ("business", "global"):
        raise DomainError("validation_error", "Scope is business or global")
    if ctx.business_id is not None and ctx.business_id != project_id:
        raise DomainError("validation_error", "The selected project must match project_id")
    return replace(ctx, business_id=project_id, review_business_id=project_id)


def role(ctx, uow) -> str:
    """The owner or the exact configured active Ordubashy; every Project record operation requires one of them."""
    owner, coordinator = private_participants(uow)
    if ctx.principal_kind == "human" and owner and ctx.principal_id == owner:
        return "owner"
    if ctx.principal_kind == "agent" and coordinator and ctx.principal_id == coordinator:
        return "ordubashy"
    raise DomainError("forbidden", "Project records belong to the installation owner and the configured Ordubashy")


def guard(ctx, application, uow, *, project_id, operation="project_record_get", include_archived=False):
    """The one nonrecursive Project read guard: identity, current reach, archive state and policy.

    Every surface that names a Project (the neutral item resolver, Focus and private-thread sources, directory labels,
    link targets and the ``project_*`` handlers) calls this exact function. It refreshes the caller once, then applies
    the owner/exact-Ordubashy identity gate, the Project's *current* membership or global reach and archived state, and
    the ordinary policy of the named read. No label, summary, count or card is produced before it succeeds.
    """
    current = application.current_context(ctx, uow)
    role(current, uow)
    row = business_row(current, project_id, uow, include_archived=include_archived)
    selected = replace(current, business_id=project_id, review_business_id=project_id)
    application.authorize(selected, application.registry.get(operation), (), uow, namespace="business:" + project_id)
    return selected, row


def allow(ctx, args, application, uow, *, project_id, operation, include_archived=False):
    """Identity gate, Project reach and the ordinary read/write gate of one ``project_*`` operation.

    The handler enforces all three for every transport: a business route, the root route with ``scope: "global"``,
    a direct invocation and a replayed receipt land here, and a business route must agree with ``project_id``."""
    current = application.current_context(ctx, uow)
    selected = selected_scope(current, args, project_id, uow)
    selected, row = guard(selected, application, uow, project_id=project_id, operation=operation,
                          include_archived=include_archived)
    return selected, row


def canonical_reference(item, *, require_scope=False):
    """Validate one public canonical Project reference in place: ``id`` and ``business_id`` are the same UUID.

    The neutral internal two-field vocabulary (``{type, id}``) may still resolve through the item resolver, which
    canonicalizes it to this full reference. Every public surface (link/get targets, Focus sources and private thread
    sources) passes ``require_scope=True`` so a null or omitted Project scope is refused, never inferred.
    """
    if not isinstance(item, dict) or item.get("type") != PROJECT_TYPE:
        return item
    identity, business = item.get("id"), item.get("business_id")
    if not isinstance(identity, str) or not identity:
        raise DomainError("validation_error", "A project reference needs its actual project id")
    if require_scope and business != identity:
        raise DomainError("validation_error", "A project reference must name the project as its own scope")
    if business is not None and business != identity:
        raise DomainError("validation_error", "A project reference must name the project as its own scope")
    return {**item, "business_id": identity}


def validate_public_reference(item):
    """The public Project form, or the unchanged item for every other kind."""
    return canonical_reference(item, require_scope=True)


def project_view(row) -> dict:
    """``project_record_get``'s Project: public operational fields of the existing record only."""
    config = row.get("config_json") or {}
    summary = config.get("one_liner") if isinstance(config, dict) else None
    return {"id": row["id"], "name": row["name"], "slug": row["slug"],
            "summary": " ".join(summary.split()) if isinstance(summary, str) else "",
            "revision": row["revision"], "archived": row["archived_at"] is not None,
            "source": reference(row["id"])}


def scope_name(uow, project_id) -> tuple[str | None, str]:
    """The own-scope slug and name of one item's business, or Global."""
    if project_id is None:
        return None, "Global"
    row = uow.repository("businesses").get(project_id)
    return (row["slug"], row["name"]) if row is not None else (None, project_id)


__all__ = ["PROJECT_TYPE", "THREAD_KINDS", "UNAVAILABLE", "allow", "business_participant", "business_row",
           "canonical_reference", "guard", "project_id_from",
           "project_view", "reference", "role", "scope_name", "selected_scope", "validate_public_reference"]