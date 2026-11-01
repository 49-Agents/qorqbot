"""Business settings authority and partial configuration updates."""

from copy import deepcopy

from bazarlyk.agents.engineering import has_ordubashy_settings_authority
from qorqut_operations.contracts import DomainError


def require_settings_authority(ctx, uow):
    if not ctx.business_id:
        raise DomainError("scope_required", "Select a business to manage its settings")
    if has_ordubashy_settings_authority(ctx, uow):
        return
    if ctx.principal_kind == "human":
        member = uow.repository("business_memberships", business_id=ctx.business_id).get(ctx.principal_id)
        if member and member["active"] and member["is_owner"]:
            return
    raise DomainError("forbidden", "Business settings require its owner or scoped Ordubashy")


def merge_settings(previous, changes):
    value = deepcopy(previous) if isinstance(previous, dict) else {}
    for key, item in changes.items():
        if item is None:
            value.pop(key, None)
        elif isinstance(item, dict):
            value[key] = merge_settings(value.get(key), item)
        else:
            value[key] = deepcopy(item)
    return value
