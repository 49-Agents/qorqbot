"""Who is a global worker and who may administer one.

The one definition every worker-targeted operation uses (profile and permission edits, terminal lifecycle, inbox
subscriptions, current work). Only the installation owner and the configured Ordubashy manage a worker; a worker never
administers itself or a peer, and Learning Applier is never a worker.
"""

from __future__ import annotations

from qorqut_operations.contracts import DomainError

from .global_scope import active_global
from .roles import GLOBAL_WORKER_ROLE


def is_worker(connection, agent_id) -> bool:
    """True when `agent_id` is an active registered global worker (the shared current-global eligibility decides)."""
    data = active_global(connection, agent_id)
    return data is not None and data["role"]["id"] == GLOBAL_WORKER_ROLE


def require_worker_manager(ctx, uow, target_agent_id) -> None:
    """Accept the installation owner or the configured Ordubashy for an active worker target; refuse anyone else."""
    from qorqut.management.identity import configured_ordubashy, is_active_installation_owner
    if not is_worker(uow.connection, target_agent_id):
        raise DomainError("forbidden", "Only an active global worker can be managed here")
    if is_active_installation_owner(uow, ctx.principal_id, ctx.principal_kind):
        return
    if ctx.principal_kind == "agent" and ctx.principal_id == configured_ordubashy(uow):
        return
    raise DomainError("forbidden", "Only the installation owner or Ordubashy can manage a global worker")
