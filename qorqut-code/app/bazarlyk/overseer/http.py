"""Private owner-only Overseer read routes; owner answers stay on Signoffs."""
import anyio
from qorqut_operations.contracts import DomainError, OperationResult
from qorqut_agentic.errors import AgenticError
from bazarlyk.transport.results import response
from .reads import OverseerReads


def matches(parts):
    return bool(parts) and parts[0] == 'overseer'


def _query(request, allowed):
    query = dict(request.query_params)
    if len(request.query_params.multi_items()) != len(query) or set(query) - set(allowed):
        raise DomainError('validation_error', 'Unknown Overseer query parameter')
    return query


def _number(value, *, default=None, minimum=1, maximum=None):
    if value is None: return default
    if not isinstance(value, str) or not value.isdecimal():
        raise DomainError('validation_error', 'Invalid numeric query value')
    number = int(value)
    if number < minimum or maximum is not None and number > maximum:
        raise DomainError('validation_error', 'Numeric query value is out of range')
    return number


async def route(runtime, request, context, parts, request_id):
    if request.method != 'GET':
        raise DomainError('not_found', 'Overseer mutation routes do not exist')
    def read():
        try:
            service = OverseerReads(runtime, context)
            page_params = ('limit', 'cursor')
            if parts == ['overseer', 'overview']:
                _query(request, ())
                return service.overview()
            if parts == ['overseer', 'batches']:
                q = _query(request, page_params)
                return service.batches(limit=_number(q.get('limit'), default=20, maximum=100), cursor=q.get('cursor'))
            if parts == ['overseer', 'deltas']:
                q = _query(request, (*page_params, 'decision'))
                return service.deltas(decision=q.get('decision'), limit=_number(q.get('limit'), default=20, maximum=100), cursor=q.get('cursor'))
            if parts == ['overseer', 'applications']:
                q = _query(request, (*page_params, 'queue', 'delta_id'))
                return service.applications(queue=q.get('queue'), delta_id=q.get('delta_id'),
                                            limit=_number(q.get('limit'), default=20, maximum=100), cursor=q.get('cursor'))
            if len(parts) >= 3 and parts[1] in ('batches', 'deltas', 'applications'):
                group, identity = parts[1:3]
                tail = parts[3:]
                if not tail:
                    if group == 'batches':
                        _query(request, ())
                        return service.batch(identity)
                    if group == 'deltas':
                        q = _query(request, ('revision',))
                        return service.delta_detail(identity, revision=_number(q.get('revision')))
                    q = _query(request, ('plan_revision',))
                    return service.application_detail(identity, plan_revision=_number(q.get('plan_revision')))
                if tail == ['discussion']:
                    q = _query(request, page_params)
                    return service.discussion({'batches': 'batch', 'deltas': 'delta', 'applications': 'application'}[group], identity,
                                              limit=_number(q.get('limit'), default=20, maximum=100), cursor=q.get('cursor'))
                routes = {('deltas', 'revisions'): service.revisions, ('deltas', 'decisions'): service.decisions,
                          ('applications', 'plans'): service.plans, ('applications', 'events'): service.events}
                if len(tail) == 1 and (group, tail[0]) in routes:
                    q = _query(request, page_params)
                    return routes[(group, tail[0])](identity, limit=_number(q.get('limit'), default=20, maximum=100), cursor=q.get('cursor'))
                if group == 'deltas' and len(tail) == 3 and tail[0] == 'evidence':
                    q = _query(request, ('revision',))
                    index = _number(tail[2], minimum=0)
                    return service.evidence(identity, tail[1], index, revision=_number(q.get('revision')))
            raise DomainError('not_found', 'Overseer read route is unavailable')
        except AgenticError as error:
            raise DomainError(error.code, str(error), details=error.details) from error
    value = await anyio.to_thread.run_sync(read)
    result = response(OperationResult.ok(value, request_id=request_id))
    result.headers['Cache-Control'] = 'private, no-store'
    return result
