"""Business settings authority and partial configuration updates."""

from collections.abc import Mapping
from copy import deepcopy

from bazarlyk.agents.engineering import has_ordubashy_settings_authority
from qorqut_operations.contracts import DomainError, canonical_bytes

# Inert attribution written once by earlier creations (or migration); nothing treats them as authority any more.
# No settings write adds, changes or removes them, whoever the caller is.
RESERVED_KEYS = ("setup_origin_agent_id", "founding_document_id", "setup_legacy_followups")


def require_settings_authority(ctx, uow):
    if not ctx.business_id:
        raise DomainError("scope_required", "Select a project to manage its settings")
    if has_ordubashy_settings_authority(ctx, uow):
        return
    if ctx.principal_kind == "human":
        member = uow.repository("business_memberships", business_id=ctx.business_id).get(ctx.principal_id)
        if member and member["active"] and member["is_owner"]:
            return
    raise DomainError("forbidden", "Project settings require its owner or scoped Ordubashy")


def protect_reserved_settings(proposed, previous):
    """Reject any difference in the reserved creation fields between the stored and the proposed configuration.

    Presence is compared as well as value, so an absent field and a null one are different, and a field that is unchanged is
    allowed so ordinary settings round-trip."""
    proposed = proposed if isinstance(proposed, Mapping) else {}
    previous = previous if isinstance(previous, Mapping) else {}
    for key in RESERVED_KEYS:
        if (key in proposed) != (key in previous) or (key in proposed and canonical_bytes(proposed[key]) != canonical_bytes(previous[key])):
            raise DomainError("forbidden", f"The project setting {key} is written once when the project is created and is not editable",
                              details={"key": key})


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
