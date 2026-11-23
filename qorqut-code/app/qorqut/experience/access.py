"""Who may use the Learning Applier's workflow tools, and through whose eyes the owner's conversations are read."""
from dataclasses import replace

from qorqut_operations.contracts import DomainError

from qorqut.agents.global_scope import active_role_holder
from qorqut.agents.roles import LEARNING_APPLIER_ROLE
from qorqut.management.identity import configured_ordubashy, installation_owner

CAPABILITY = 'manage_agentic_layer'


def at_global(app, ctx, uow):
    return app.current_context(replace(ctx, business_id=None, review_business_id=None), uow)


def applier(app, ctx, uow):
    """The active Learning Applier in person, at Global: never the owner, another agent or a delegated identity."""
    ctx = at_global(app, ctx, uow)
    if (ctx.principal_kind != 'agent' or ctx.delegation_assignment_id is not None
            or ctx.principal_id != active_role_holder(uow.connection, LEARNING_APPLIER_ROLE)):
        raise DomainError('forbidden', 'This tool belongs to the Learning Applier')
    return ctx


def filer(app, ctx, uow):
    """Any agent, at Global. Whoever does the work may file a point about how agents work for the Learning Applier's
    review; the owner does not file here, since everything the owner writes reaches the extraction by itself."""
    ctx = at_global(app, ctx, uow)
    if ctx.principal_kind != 'agent':
        raise DomainError('forbidden', 'Points for the Learning Applier\'s review are filed by agents')
    return ctx


def reader(app, ctx, uow):
    """Whoever may read observations, clusters and delta proposals: the Learning Applier, the owner, or Ordubashy in person."""
    ctx = at_global(app, ctx, uow)
    personal = ctx.delegation_assignment_id is None
    if personal and ctx.principal_kind == 'human' and ctx.principal_id == installation_owner(uow):
        return ctx
    if personal and ctx.principal_kind == 'agent' and ctx.principal_id in {
            configured_ordubashy(uow), active_role_holder(uow.connection, LEARNING_APPLIER_ROLE)} - {None}:
        return ctx
    raise DomainError('forbidden', 'Observations and delta proposals are read by the owner, Ordubashy and the Learning Applier')


def coordinator_context(app, uow):
    """Ordubashy's own Global context. The Learning Applier reads the owner's conversations and decisions exactly as Ordubashy
    may read them: the owner asked for every message, thread and Signoff decision to reach the extraction, and Ordubashy is
    the one agent that already reads them all. Refused when no coordinator is configured."""
    coordinator = configured_ordubashy(uow)
    if not coordinator:
        raise DomainError('operation_unavailable', 'No coordinator is configured, so the owner\'s conversations cannot be read')
    return app.policy.context_for(coordinator, business_id=None, review_business_id=None, uow=uow)
