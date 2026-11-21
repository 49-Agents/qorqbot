"""Project settings authority; the record merge of a configuration update is ``qorqut_operations.projects.settings``."""

from qorqut.agents.engineering import has_ordubashy_settings_authority
from qorqut_operations.contracts import DomainError
from qorqut_operations.project_lifecycle import owner_may_act


def require_settings_authority(ctx, uow):
    if not ctx.project_id:
        raise DomainError("scope_required", "Select a project to manage its settings")
    if has_ordubashy_settings_authority(ctx, uow):
        return
    if ctx.principal_kind == "human":
        if owner_may_act(uow.connection, ctx.principal_id, ctx.project_id):
            return
    raise DomainError("forbidden", "Project settings require its owner or scoped Ordubashy")
