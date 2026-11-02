"""Authenticated host operations over the portable Overseer layer."""
from qorqut_operations.application.registry import OperationDescriptor
from qorqut_operations.contracts import DomainError
from qorqut_agentic.overseer.views import Views

from .access import EvidenceAccess
from .authority import current_overseer
from .coverage import AnalysisCoverage
from .evidence import EvidenceProvider
from .inputs import OwnerInputGateway, create_input_request
from .invocation import start_from_owner


_OBJ = {'type': 'object'}
_STR = {'type': 'string', 'minLength': 1, 'maxLength': 200}
_UUID = {'type': 'string', 'format': 'uuid'}
_PAGE = {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}, 'cursor': {'type': 'string', 'maxLength': 4096}}
_TASK_REF = {'type': 'object', 'additionalProperties': False,
    'required': ['provider', 'kind', 'id', 'revision', 'namespace'], 'properties': {
        'provider': {'const': 'operations'}, 'kind': {'const': 'task'}, 'id': _UUID, 'revision': _STR,
        'namespace': {'type': 'object', 'additionalProperties': False, 'required': ['kind', 'business_id'],
                      'properties': {'kind': {'enum': ['global', 'business']}, 'business_id': {'type': ['string', 'null']}}}}}


def _schema(properties, required=()):
    return {'type': 'object', 'additionalProperties': False, 'properties': properties, 'required': list(required)}


READS = {
    'agentic_change_get': ({'submission_id': _UUID}, ('submission_id',)),
    'overseer_evidence_list': ({'run_id': _STR, 'selection': _OBJ, **_PAGE}, ('run_id', 'selection')),
    'overseer_episode_get': ({'run_id': _STR, 'episode_ref': _OBJ, 'frontier': _OBJ,
        'cursor': {'type': 'string', 'maxLength': 4096}, 'max_chars': {'type': 'integer', 'minimum': 1}},
        ('run_id', 'episode_ref', 'frontier')),
    'overseer_proposals_list': (_PAGE, ()),
    'overseer_outside_delta_get': ({'id': _UUID}, ('id',)),
    'overseer_outside_deltas_list': (_PAGE, ()),
    'overseer_delta_get': ({'id': _UUID, 'revision': {'type': 'integer', 'minimum': 1}}, ('id',)),
    'overseer_deltas_list': ({**_PAGE, 'decision': {'enum': ['undecided', 'accepted', 'rejected']}}, ()),
    'overseer_application_get': ({'id': _UUID, 'plan_revision': {'type': 'integer', 'minimum': 1}}, ('id',)),
    'overseer_application_recovery_status': ({'application_id': _UUID}, ('application_id',)),
    'overseer_owner_request_get': ({'reference': _OBJ}, ('reference',)),
    'overseer_owner_requests_list': (_PAGE, ()),
    # P07: due extraction requests. Reads never reserve, acknowledge or start work.
    'overseer_due_list': (_PAGE, ()),
    'overseer_due_get': ({'reference': _OBJ}, ('reference',)),
}
START = {'invocation': _OBJ, 'interpretation': _OBJ, 'operation_id': _STR,
         'response_interpretation': {'type': ['object', 'null']}}
WRITE = {
    'agentic_change_propose': ({'request_key': _STR,
        'instruction_text': {'type': 'string', 'minLength': 1, 'maxLength': 17000},
        'source_id': _UUID, 'summary': {'type': 'string', 'minLength': 1, 'maxLength': 160},
        'change_direction': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
        'scope': _OBJ, 'target_refs': {'type': 'array', 'items': _STR, 'maxItems': 20}},
        ('request_key', 'instruction_text', 'summary', 'change_direction', 'scope', 'target_refs')),
    'agentic_change_reconcile': ({'submission_id': _UUID, 'response': _OBJ, 'operation_id': _STR},
        ('submission_id', 'response', 'operation_id')),
    'overseer_outside_delta_record': ({'id': _UUID, 'summary': {'type': 'string', 'minLength': 1, 'maxLength': 8000},
        'sources': {'type': 'array', 'minItems': 1, 'maxItems': 100, 'items': _OBJ},
        'suggested_improvement': {'type': ['string', 'null'], 'minLength': 1, 'maxLength': 8000}, 'operation_id': _STR},
        ('id', 'summary', 'sources', 'operation_id')),

    'overseer_extract_request': (START, ('invocation', 'interpretation', 'operation_id')),
    'overseer_review_start': (START, ('invocation', 'interpretation', 'operation_id')),
    'overseer_application_start': (START, ('invocation', 'interpretation', 'operation_id')),
    'overseer_input_request': ({'binding': _OBJ, 'form': _OBJ, 'operation_id': _STR}, ('binding', 'form', 'operation_id')),
    'overseer_invocation_request': ({'request_key': _STR, 'kind': {'enum': ['extraction', 'review', 'application']},
        'arguments': _OBJ, 'question': {'type': 'string', 'minLength': 1, 'maxLength': 8000},
        'operation_id': _STR, 'source_ref': {'type': ['object', 'null']}},
        ('request_key', 'kind', 'arguments', 'question', 'operation_id')),
    'overseer_input_reconcile': ({'binding_id': _UUID, 'response': _OBJ, 'operation_id': _STR}, ('binding_id', 'response', 'operation_id')),
    'overseer_development_task_create': ({'application_id': _UUID, 'requirement_id': _STR, 'task': _OBJ,
        'expected_revision': {'type': 'integer', 'minimum': 1}, 'operation_id': _STR},
        ('application_id', 'requirement_id', 'task', 'expected_revision', 'operation_id')),
    'overseer_development_task_link': ({'application_id': _UUID, 'requirement_id': _STR, 'task_ref': _TASK_REF,
        'expected_revision': {'type': 'integer', 'minimum': 1}, 'operation_id': _STR},
        ('application_id', 'requirement_id', 'task_ref', 'expected_revision', 'operation_id')),
    'overseer_development_tasks_reconcile': ({'application_id': _UUID,
        'expected_revision': {'type': 'integer', 'minimum': 1}, 'operation_id': _STR},
        ('application_id', 'expected_revision', 'operation_id')),
}
for suffix in ('prepare', 'prepare_merge', 'publish', 'recover', 'record_checks', 'replan'):
    properties = {'application_id': _UUID, 'expected_revision': {'type': 'integer', 'minimum': 1}, 'operation_id': _STR}
    required = ['application_id', 'expected_revision', 'operation_id']
    if suffix == 'record_checks': properties['phase'] = {'enum': ['pre_merge', 'post_merge']}; required.append('phase')
    if suffix == 'replan': properties['reason'] = {'type': 'string', 'minLength': 1, 'maxLength': 2000}; required.append('reason')
    WRITE['overseer_application_' + suffix] = (properties, required)
WRITE['overseer_application_inspect_satisfaction'] = ({
    'application_id': _UUID, 'resource_paths': {'type': 'array', 'items': _STR, 'maxItems': 100, 'uniqueItems': True},
    'source_refs': {'type': 'array', 'items': _OBJ, 'maxItems': 100},
    'check_receipt_refs': {'type': 'array', 'items': _OBJ, 'maxItems': 100},
    'explanation': {'type': 'string', 'minLength': 1, 'maxLength': 8000},
    'expected_revision': {'type': 'integer', 'minimum': 1}, 'operation_id': _STR},
    ('application_id', 'explanation', 'expected_revision', 'operation_id'))


class OverseerOperations:
    def __init__(self, runtime):
        import threading
        self.runtime = runtime
        self.executor_ready = False
        self._evidence_scan_lock = threading.Lock()

    def register(self):
        for name, (properties, required) in READS.items():
            capability = 'read_agent_experience' if name in ('overseer_evidence_list', 'overseer_episode_get') else 'manage_agentic_layer'
            self.runtime.registry.register(OperationDescriptor(name, _schema(properties, required), effect_kind='read',
                supports_signoff=False, exposure=frozenset({'mcp'}), scope_kinds=frozenset({'global'}),
                global_capability=capability, resolve_namespace=lambda c, a: 'global',
                description=('Read your submitted instruction, exact review Signoff and decision; performs no application.'
                             if name == 'agentic_change_get' else 'Read authorized Overseer records and current source availability.'),
                read=lambda c, a, o, u, n=name: self.safe_read(n, c, a, u)))
        from .due import DueRecord, OUTPUT as DUE_OUTPUT, ARGUMENTS as DUE_ARGUMENTS
        due = DueRecord(self.runtime)
        self.runtime.registry.register(OperationDescriptor(DUE_OUTPUT, DUE_ARGUMENTS, effect_kind='records',
            supports_signoff=False, exposure=frozenset({'worker'}), scope_kinds=frozenset({'global'}),
            global_capability='manage_agentic_layer', resolve_namespace=lambda c, a: 'global',
            prepare=due.prepare, apply=due.apply,
            description='Worker-only output of the scheduled extraction program: record its one derived due occurrence.'))
        for name, (properties, required) in WRITE.items():
            if name == 'overseer_development_task_create':
                from qorqut_operations.contracts import plain
                task_schema = plain(self.runtime.registry.get('tasks_create').input_schema)
                task_schema['properties']['business_id'] = {'type': ['string', 'null'], 'format': 'uuid'}
                properties = {**properties, 'task': task_schema}
            self.runtime.registry.register(OperationDescriptor(name, _schema(properties, required), effect_kind='compound',
                supports_signoff=False, exposure=frozenset({'mcp'}), scope_kinds=frozenset({'global'}),
                global_capability='manage_agentic_layer', transport_only=True, external_read=True,
                description=({'agentic_change_propose': 'Submit an explicit owner Chat instruction as a proposed delta with an owner-only review Signoff. Supply the exact full original message, proposed change, scope (description, project_ids, work_types, workflow_refs, conditions, exceptions), and catalog target references. Reuse request_key on retry. Creates no active edit; acceptance queues separate application.',
                              'agentic_change_reconcile': 'Read the actual owner answer to your exact proposal Signoff and update the shared delta queue. Response contains reference (provider, signoff_id, proposal_revision, payload_hash, status_revision) and interpretation (null for a choice without notes; otherwise clarity/action/rationale). Never applies active files.'}.get(name,
                             'Run an authorized Overseer bridge operation with its exact saved binding.'))))
        from .executor import configure
        from qorqut_agentic.errors import AgenticError
        try:
            self.executor_ready = configure(self.runtime)
        except AgenticError:
            # Unprovisioned executor roots do not block unrelated host features.
            pass
        return self

    def safe_read(self, name, context, args, uow):
        from qorqut_agentic.errors import AgenticError
        try:
            return self.read(name, context, args, uow)
        except AgenticError as error:
            raise DomainError(error.code, str(error), details=error.details) from error

    def _provider(self, context, uow):
        ctx, _ = current_overseer(self.runtime, context, uow, capability='read_agent_experience')
        try:
            return EvidenceProvider(self.runtime, ctx, self.runtime.paths.overseer_archive, EvidenceAccess(self.runtime, ctx),
                                    overseer_ids={ctx.principal_id}, coverage=AnalysisCoverage(self.runtime.agentic_layer))
        except DomainError as error:
            if error.code == 'invalid_configuration':
                raise DomainError('overseer_storage_unavailable', 'Private Overseer storage requires offline provisioning') from error
            raise

    def _check_delta(self, context, delta):
        access = EvidenceAccess(self.runtime, context)
        for proposal_id in delta['revision']['proposal_ids']:
            proposal = self.runtime.agentic_layer.overseer.proposals.get(proposal_id)
            for source in proposal['sources']:
                access.authorize_source(source['ref'])
        return delta

    def _evidence_delivery(self, name, context, args, uow):
        import hashlib
        import json
        ctx, _ = current_overseer(self.runtime, context, uow, capability='read_agent_experience')
        from .continuation import require_run
        _, session = require_run(self.runtime, ctx, uow, args['run_id'], active=True)
        provider = self._provider(ctx, uow)
        if name == 'overseer_evidence_list':
            request = {'selection': args['selection'], 'cursor': args.get('cursor'), 'limit': args.get('limit', 50)}
            result = provider.list_changes(**request)
            refs = result['items']
        else:
            request = {'episode_ref': args['episode_ref'], 'frontier': args['frontier'], 'cursor': args.get('cursor'),
                       'max_chars': args.get('max_chars', 160000)}
            result = provider.get_episode(**request)
            request['frontier'] = result['frontier']
            refs = result['source_refs']
        run_ref = {'provider': 'qorqut_overseer', 'agent_id': ctx.principal_id, 'conversation_id': session['conversation_id'],
                   'run_id': args['run_id'], 'namespace': {'kind': 'global', 'business_id': None}}
        records = [{'kind': 'evidence_delivery', 'operation': name, 'request': request, 'result': result,
                    'delivery_state': 'prepared_for_delivery'}]
        raw = json.dumps({'run_ref': run_ref, 'records': records, 'source_refs': refs}, sort_keys=True,
                         separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
        operation = 'presentation:' + hashlib.sha256(raw).hexdigest()
        presentation_ref = provider.capture_presentation(run_ref, records, refs, operation)
        return {**result, 'presentation_ref': presentation_ref}

    def read(self, name, context, args, uow):
        if name == 'agentic_change_get':
            from .direct import get
            return get(self.runtime, context, **args)
        if name in ('overseer_evidence_list', 'overseer_episode_get'):
            # Retried transports must not race inventory cursors or publish a
            # duplicate page presentation. Refuse before provider/admission work.
            if not self._evidence_scan_lock.acquire(blocking=False):
                raise DomainError('evidence_busy',
                    'Evidence reading is still in progress; this request did not advance progress. '
                    'Automatically retry this exact request after waits of 5, 10, 20, 30 and 30 seconds, '
                    'with at most five retries and a 120-second total budget from the first busy response. '
                    'Keep the same run, selection, frontier, cursor and limits; do not restart the inventory. '
                    'Stop retrying on any different error. If still busy when the budget is exhausted, '
                    'save progress and report a service fault, not a request for owner permission to resume.',
                    retryable=True, details={'retry_delays_seconds': [5, 10, 20, 30, 30],
                                            'max_retries': 5, 'retry_budget_seconds': 120,
                                            'request_dispatched': False})
            try:
                return self._evidence_delivery(name, context, args, uow)
            finally:
                self._evidence_scan_lock.release()
        ctx, _ = current_overseer(self.runtime, context, uow)
        layer = self.runtime.agentic_layer
        if name in ('overseer_outside_delta_get', 'overseer_outside_deltas_list'):
            ctx, _ = current_overseer(self.runtime, ctx, uow, capability='manage_agentic_layer')
            from .outside import authorize
            if name == 'overseer_outside_delta_get':
                return authorize(self.runtime, ctx, layer.overseer.outside.get(args['id']))
            page = layer.overseer.outside.list(limit=args.get('limit', 50), cursor=args.get('cursor'))
            for item in page['items']:
                authorize(self.runtime, ctx, item)
            return page

        if name == 'overseer_proposals_list':
            page = layer.overseer.proposals.list(limit=args.get('limit', 20), cursor=args.get('cursor'))
            access = EvidenceAccess(self.runtime, ctx)
            for proposal in page['items']:
                for source in proposal['sources']: access.authorize_source(source['ref'])
            return page
        if name == 'overseer_delta_get':
            return self._check_delta(ctx, layer.overseer.deltas.get(args['id'], revision=args.get('revision')))
        if name == 'overseer_deltas_list':
            page = Views(layer).list_deltas(decision=args.get('decision'), limit=args.get('limit', 20), cursor=args.get('cursor'))
            for item in page['items']: self._check_delta(ctx, item)
            return page
        if name == 'overseer_application_get':
            application = layer.overseer.applications.get(args['id'], plan_revision=args.get('plan_revision'))
            self._check_delta(ctx, layer.overseer.deltas.get(application['delta_id']))
            return application
        if name == 'overseer_application_recovery_status':
            application = layer.overseer.applications.get(args['application_id'])
            self._check_delta(ctx, layer.overseer.deltas.get(application['delta_id']))
            method = getattr(layer.overseer.applications, 'recovery_status', None)
            if not callable(method): raise DomainError('operation_unavailable', 'The application executor is not installed')
            return method(args['application_id'])
        if name == 'overseer_owner_request_get':
            reference = args['reference']
            if reference.get('kind') == 'signoff_response' or reference.get('provider') == 'qorqut_signoff':
                from .workflow_requests import request_view
                result = request_view(self.runtime, ctx, reference.get('id') if reference.get('kind') == 'signoff_response'
                                      else reference.get('signoff_id'))
                expected = result['response_reference'] if reference.get('kind') == 'signoff_response' else result['request']['request_ref']
                if expected != reference:
                    raise DomainError('stale_revision', 'Workflow response reference changed')
                return result
            result = self.runtime.overseer_retention.owner_request(ctx, reference.get('id'))
            if result['reference'] != reference: raise DomainError('stale_revision', 'Owner request reference changed')
            return result
        if name == 'overseer_owner_requests_list':
            return self.owner_requests(ctx, args, uow)
        if name == 'overseer_due_list':
            from .due import list_requests
            return list_requests(self.runtime, ctx, uow, limit=args.get('limit', 20), cursor=args.get('cursor'))
        if name == 'overseer_due_get':
            from .due import get_request
            return get_request(self.runtime, ctx, uow, args['reference'])
        raise DomainError('operation_unavailable', 'Unknown Overseer read')

    def owner_requests(self, ctx, args, uow):
        import base64
        import json
        from bazarlyk.native_chat import store
        from .workflow_requests import request_view
        session = store.latest_session(uow.connection, ctx.principal_id)
        if session is None:
            return {'schema_version': 1, 'items': [], 'next_cursor': None, 'complete': True,
                    'availability': 'unavailable', 'gaps': [{'code': 'conversation_unavailable'}]}
        after = ('', '', '')
        cursor = args.get('cursor')
        if cursor is not None:
            try:
                value = json.loads(base64.b64decode(cursor.encode('ascii'), altchars=b'-_', validate=True))
                if (set(value) != {'agent', 'provider', 'conversation', 'after'} or value['agent'] != ctx.principal_id
                        or value['provider'] != session['provider'] or value['conversation'] != session['conversation_id']
                        or not isinstance(value['after'], list) or len(value['after']) != 3
                        or not all(isinstance(v, str) for v in value['after'])):
                    raise ValueError('cursor')
                after = tuple(value['after'])
            except (ValueError, TypeError, AttributeError):
                raise DomainError('validation_error', 'Invalid owner request cursor') from None
        limit = args.get('limit', 20)
        rows = uow.connection.execute('''SELECT * FROM (
            SELECT i.created_at AS at,i.id AS id,'chat' AS kind FROM native_inputs i
            JOIN native_sessions s ON s.id=i.session_id JOIN installation x ON x.id='service'
            LEFT JOIN telegram_owner_inputs t ON i.source_key='telegram:'||t.id AND t.agent_id=i.agent_id
            WHERE i.agent_id=? AND s.provider=? AND s.conversation_id=? AND i.origin='owner'
              AND ((i.input_type='chat' AND i.principal_id=x.owner_principal_id)
                   OR (i.input_type='telegram' AND i.principal_id IS NULL AND t.owner_principal_id=x.owner_principal_id)
                   OR (i.input_type='telegram' AND i.principal_id=x.owner_principal_id AND t.owner_principal_id=x.owner_principal_id))
            UNION ALL
            SELECT s.created_at AS at,s.id AS id,'workflow_signoff' AS kind FROM overseer_workflow_requests r
            JOIN signoffs s ON s.id=r.signoff_id WHERE r.agent_id=? AND r.conversation_id=?
            ) WHERE (at,id,kind)>(?,?,?) ORDER BY at,id,kind LIMIT ?''',
            (ctx.principal_id, session['provider'], session['conversation_id'], ctx.principal_id,
             session['conversation_id'], *after, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        items = []
        for row in rows:
            if row['kind'] == 'workflow_signoff':
                items.append(request_view(self.runtime, ctx, row['id']))
                continue
            native = uow.connection.execute('''SELECT i.*,o.input_id AS retained FROM native_inputs i
                LEFT JOIN overseer_owner_inputs o ON o.input_id=i.id WHERE i.id=?''', (row['id'],)).fetchone()
            if native['retained'] is not None:
                items.append(self.runtime.overseer_retention.owner_request(ctx, native['id']))
            else:
                items.append({'reference': None, 'source_text': None, 'source_hash': None,
                    'source': {'native_input_id': native['id'], 'owner_principal_id': native['principal_id'], 'agent_id': ctx.principal_id,
                               'provider': session['provider'], 'conversation_id': session['conversation_id'], 'session_id': native['session_id'],
                               'source_kind': native['input_type'], 'source_key': native['source_key'], 'accepted_at': native['created_at']},
                    'sensitive': bool(native['sensitive']), 'material_ref': None,
                    'delivery': {'state': native['state'], 'transport_reported_sent': native['state'] == 'submitted',
                                 'processing_confirmed': False, 'native_entry_ref': None},
                    'eligible': False, 'gaps': [{'code': 'sensitive_input_redacted' if native['sensitive'] else 'historical_input_unavailable'}]})
        next_cursor = None
        if more:
            value = {'agent': ctx.principal_id, 'provider': session['provider'], 'conversation': session['conversation_id'],
                     'after': [rows[-1]['at'], rows[-1]['id'], rows[-1]['kind']]}
            next_cursor = base64.urlsafe_b64encode(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).decode()
        return {'schema_version': 1, 'items': items, 'next_cursor': next_cursor, 'complete': not more,
                'availability': 'available', 'gaps': []}

    def mutate(self, name, context, args):
        if name in ('agentic_change_propose', 'agentic_change_reconcile'):
            from . import direct
            return (direct.propose if name == 'agentic_change_propose' else direct.reconcile)(self.runtime, context, **args)
        if name == 'overseer_outside_delta_record':
            from .outside import record
            return record(self.runtime, context, args)

        kinds = {'overseer_extract_request': 'extraction', 'overseer_review_start': 'review', 'overseer_application_start': 'application'}
        if name in kinds:
            return start_from_owner(self.runtime, context, kinds[name], args['invocation'], args['interpretation'], args['operation_id'],
                                    args.get('response_interpretation'))
        if name == 'overseer_input_request':
            return create_input_request(self.runtime, context, args['binding'], args['form'], args['operation_id'])
        if name == 'overseer_invocation_request':
            from .workflow_requests import create_request
            return create_request(self.runtime, context, **args)
        if name == 'overseer_input_reconcile':
            from .continuation import require_run
            binding = self.runtime.agentic_layer.overseer.inputs.get(args['binding_id'])
            with self.runtime.database.read() as uow:
                ctx, _ = current_overseer(self.runtime, context, uow, capability='manage_agentic_layer')
                require_run(self.runtime, ctx, uow, binding['run_id'])
            from .executor import acting_as
            with acting_as(context):
                return self.runtime.agentic_layer.overseer.apply_owner_response(args['binding_id'], args['response'],
                    operation_id=args['operation_id'], gateway=OwnerInputGateway(self.runtime, context))
        if name.startswith('overseer_development_task'):
            if not self.executor_ready:
                from .executor import configure
                self.executor_ready = configure(self.runtime)
            from .executor import development_task
            return development_task(self.runtime, context, name, args)
        if name.startswith('overseer_application_'):
            from .continuation import require_run
            application = self.runtime.agentic_layer.overseer.applications.get(args['application_id'])
            with self.runtime.database.read() as uow:
                ctx, _ = current_overseer(self.runtime, context, uow, capability='manage_agentic_layer')
                require_run(self.runtime, ctx, uow, application['run_id'], kind='application')
            method = getattr(self.runtime.agentic_layer.overseer.applications, name.removeprefix('overseer_application_'), None)
            if not callable(method): raise DomainError('operation_unavailable', 'The application executor is not installed')
            if not self.executor_ready:
                from .executor import configure
                self.executor_ready = configure(self.runtime)
            options = dict(args); application_id = options.pop('application_id')
            from .executor import acting_as, satisfy_input_need
            with acting_as(context):
                result = method(application_id, **options)
                return satisfy_input_need(self.runtime, context, result)
        raise DomainError('operation_unavailable', 'Unknown Overseer operation')
