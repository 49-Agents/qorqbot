"""Who may manage another agent's inbox subscriptions.

This is the subscription-specific counterpart of ``worker_access.require_worker_manager`` and is used only by the inbox's
managed-subject hook. The installation owner keeps exactly the shared worker rule. The configured Ordubashy may additionally
manage every other eligible active agent, whatever the source type of the subscription, except the configured Overseer
(the active ``learning-applier`` role holder). The target's own source access is still decided as that target by the
inbox, and the target keeps its ordinary ability to unsubscribe. Nothing here widens ``require_worker_manager``, which
also protects profile, permission and terminal management.
"""

from __future__ import annotations

from qorqut_operations.contracts import DomainError

from .global_scope import active_global, active_role_holder
from .roles import LEARNING_APPLIER_ROLE
from .worker_access import require_worker_manager


def require_subscription_manager(ctx, uow, target_agent_id) -> None:
    """Accept the caller to manage ``target_agent_id``'s subscriptions, or raise ``forbidden``.

    Ordubashy is resolved from the stored configured coordinator identity (never a display name) and must still be a
    current Ordubashy. Every other caller, the installation owner included, is judged by the shared worker rule."""
    from bazarlyk.management.identity import configured_ordubashy
    if ctx.principal_kind == "agent":
        coordinator = configured_ordubashy(uow, require_codex=False)
        if coordinator and ctx.principal_id == coordinator:
            require_subscription_target(uow, coordinator, target_agent_id)
            return
    require_worker_manager(ctx, uow, target_agent_id)


def require_subscription_target(uow, manager_id, target_agent_id) -> None:
    """The target must be an active supported global agent other than the manager and not the Overseer.

    The stored profile role and the single active Overseer registration both exclude it, so a renamed display name or a
    second registration cannot reopen it; an ambiguous Overseer configuration fails closed through ``active_role_holder``."""
    profile = active_global(uow.connection, target_agent_id)
    if profile is None:
        raise DomainError("forbidden", "Only an active agent can have its subscriptions managed here")
    if target_agent_id == manager_id:
        raise DomainError("forbidden", "Manage your own subscriptions without naming yourself")
    if (profile["role"]["id"] == LEARNING_APPLIER_ROLE
            or target_agent_id == active_role_holder(uow.connection, LEARNING_APPLIER_ROLE)):
        raise DomainError("forbidden", "The Overseer's subscriptions are not managed here")
