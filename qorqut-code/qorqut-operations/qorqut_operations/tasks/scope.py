"""Which owning namespace a Task operation works in, and the context that implies."""

from __future__ import annotations

from dataclasses import replace

from qorqut_operations.contracts import DomainError
from qorqut_operations.global_agents import is_global_agent


def task_namespace(ctx, args):
    if args.get("scope") == "global" or (not ctx.business_id and args.get("scope") != "business"):
        return "global"
    return "business:" + (ctx.business_id or "")


def task_context(ctx, args, uow):
    """The context a Task operation runs in: unchanged for a business, restricted for Global (owner or granted global agent)."""
    if task_namespace(ctx, args) != "global":
        return ctx
    if ctx.principal_kind == "human":
        installation = uow.repository("installation").get("service")
        allowed = installation and installation["owner_principal_id"] == ctx.principal_id
    else:
        allowed = ctx.principal_kind == "agent" and is_global_agent(uow.connection, ctx.principal_id)
    if not allowed or "read_global_context" not in ctx.global_capabilities:
        raise DomainError("forbidden", "Global tasks require the owner or an authorized global agent")
    return replace(ctx, business_id=None, review_business_id=None)
