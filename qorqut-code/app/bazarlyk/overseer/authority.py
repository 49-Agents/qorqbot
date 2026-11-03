"""Current authenticated Overseer identity checks used by host adapters."""
from pathlib import Path
import time

from qorqut_operations.contracts import DomainError

from bazarlyk.agents.global_scope import active_role_holder
from bazarlyk.agents.roles import LEARNING_APPLIER_ROLE
from bazarlyk.transport.auth import AuthenticatedPrincipal, current_agent_credential


def current_manager(runtime, context, uow):
    """Shared proposal authority, without granting the Overseer role or evidence reader."""
    from bazarlyk.management.identity import configured_ordubashy
    ctx = runtime.application.current_context(context, uow)
    if (ctx.principal_kind != 'agent' or ctx.source != 'mcp' or not ctx.session_registration_id
            or ctx.delegation_binding_id is not None or ctx.delegation_assignment_id is not None):
        raise DomainError('forbidden', 'A current full manager registration is required')
    profile, row = runtime.resolver.profile(ctx.principal, connection=uow.connection)
    if (profile.agent_id not in {configured_ordubashy(uow),
                                active_role_holder(uow.connection, LEARNING_APPLIER_ROLE)}
            or 'manage_agentic_layer' not in profile.scope.global_capabilities):
        raise DomainError('forbidden', 'Current agentic management authority is required')
    registration = uow.connection.execute('SELECT * FROM machine_registrations WHERE id=?',
                                          (ctx.session_registration_id,)).fetchone()
    if (registration is None or registration['principal_id'] != profile.agent_id
            or Path(registration['workspace']) != Path(row['workspace'])):
        raise DomainError('registration_conflict', 'Registration does not match the current manager')
    current_agent_credential(uow.connection, AuthenticatedPrincipal(
        ctx.principal, registration['credential_id'], registration['id']), now=time.time())
    return ctx, profile


def current_overseer(runtime, context, uow, *, capability=None):
    """Resolve the current holder and its actual native credential on every use."""
    ctx = runtime.application.current_context(context, uow)
    if (ctx.principal_kind != 'agent' or ctx.source != 'mcp' or not ctx.session_registration_id
            or ctx.delegation_binding_id is not None or ctx.delegation_assignment_id is not None):
        raise DomainError('forbidden', 'A current full Overseer native registration is required')
    profile, row = runtime.resolver.profile(ctx.principal, connection=uow.connection)
    if profile.agent_id != active_role_holder(uow.connection, LEARNING_APPLIER_ROLE):
        raise DomainError('forbidden', 'The active Overseer role holder is required')
    registration = uow.connection.execute('SELECT * FROM machine_registrations WHERE id=?',
                                          (ctx.session_registration_id,)).fetchone()
    if (registration is None or registration['principal_id'] != profile.agent_id
            or Path(registration['workspace']) != Path(row['workspace'])):
        raise DomainError('registration_conflict', 'Native registration does not match the current Overseer profile')
    current_agent_credential(uow.connection, AuthenticatedPrincipal(
        ctx.principal, registration['credential_id'], registration['id']), now=time.time())
    if ctx.inbox_session_id is not None or ctx.inbox_generation is not None:
        from .continuation import selected_session
        selected_session(runtime, ctx, uow)
    if capability is not None and capability not in profile.scope.global_capabilities:
        raise DomainError('forbidden', 'The current Overseer capability is required')
    return ctx, profile
