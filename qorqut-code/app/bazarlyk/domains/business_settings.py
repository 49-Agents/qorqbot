"""Business settings authority; the record merge of a configuration update is ``qorqut_operations.projects.settings``."""

from bazarlyk.agents.engineering import has_ordubashy_settings_authority
from qorqut_operations.contracts import DomainError


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
