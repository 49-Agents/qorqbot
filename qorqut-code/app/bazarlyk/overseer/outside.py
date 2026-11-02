"""Authenticated record-only outside observations."""
from datetime import datetime, timezone

from qorqut_agentic.errors import NotFoundError
from .access import EvidenceAccess
from .authority import current_overseer


def record(runtime, context, args):
    with runtime.database.read() as uow:
        ctx, _ = current_overseer(runtime, context, uow, capability='manage_agentic_layer')
        provider = runtime.overseer._provider(ctx, uow)
    component = runtime.agentic_layer.overseer.outside
    # Serialize timestamp selection with the immutable insert so retries agree.
    with runtime.agentic_layer._ctx.store.lock(exclusive=True):
        try:
            previous = component.get(args['id'])
        except NotFoundError:
            previous = None
        timestamp = previous['created_at'] if previous else datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        value = {'schema_version': 1, 'id': args['id'], 'summary': args['summary'], 'sources': args['sources'],
                 'suggested_improvement': args.get('suggested_improvement'), 'created_at': timestamp,
                 'created_by': ctx.principal_id}
        from qorqut_agentic.overseer.outside import validate
        validate(value)
        # Verify exact captured metadata, visibility, integrity and excluded origin.
        for link in value['sources']:
            provider.get_source(link['ref'])
        return component.record(value, args['operation_id'])


def authorize(runtime, context, record):
    access = EvidenceAccess(runtime, context)
    for link in record['sources']:
        access.authorize_source(link['ref'])
    return record
