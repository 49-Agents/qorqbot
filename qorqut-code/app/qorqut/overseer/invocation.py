"""Bind agent interpretation to a retained canonical owner request."""
import json

from qorqut_operations.contracts import DomainError
from qorqut.native_chat import store
from .authority import current_overseer


class InvocationGateway:
    def __init__(self, runtime, context, interpretation, response_interpretation=None):
        self.runtime, self.context, self.interpretation = runtime, context, interpretation
        self.response_interpretation = response_interpretation

    def resolve_invocation(self, kind, reference):
        if reference.get('provider') == 'qorqut' and reference.get('kind') == 'extraction_due':
            # The scheduled due branch is admitted by its own frozen host request, never by an owner envelope.
            if kind != 'extraction' or self.interpretation.get('kind') != 'extraction':
                raise DomainError('owner_input_required', 'A scheduled due reference authorizes extraction only')
            from .due import DueInvocationGateway
            return DueInvocationGateway(self.runtime, self.context, reference['id']).resolve_invocation(kind, reference)
        if kind != self.interpretation['kind'] or reference.get('provider') != 'qorqut' or reference.get('kind') not in ('owner_message', 'signoff_response'):
            raise DomainError('owner_input_required', 'This invocation is not an admitted owner request')
        if reference['kind'] == 'signoff_response':
            from .workflow_requests import admitted_answer
            view, source_text, source_hash = admitted_answer(self.runtime, self.context, reference, kind,
                                                              self.interpretation['arguments'], self.response_interpretation)
            association = view['request']
            request = {'source': {'owner_principal_id': view['response']['owner_principal_id'],
                                  'conversation_id': association['conversation_id']},
                       'source_text': source_text, 'source_hash': source_hash}
        else:
            request = self.runtime.overseer_retention.owner_request(self.context, reference.get('id'))
            if not request['eligible'] or request['reference'] != reference:
                raise DomainError('owner_input_required', 'The owner request is unavailable or not submitted')
        with self.runtime.database.read() as uow:
            ctx, _ = current_overseer(self.runtime, self.context, uow, capability='manage_agentic_layer')
            session = store.live_session(uow.connection, ctx.principal_id)
            profile = store.active_profile(uow.connection, ctx.principal_id, self.runtime.paths)
            owner = uow.connection.execute('SELECT desired_state FROM native_session_owners WHERE agent_id=?',
                                           (ctx.principal_id,)).fetchone()
            if reference['kind'] == 'signoff_response':
                saved = uow.connection.execute('''SELECT i.interpretation_json FROM overseer_workflow_interpretations i
                    JOIN overseer_workflow_requests r ON r.id=i.request_id WHERE r.signoff_id=? AND i.response_revision=?''',
                    (reference['id'], reference['revision'])).fetchone()
                expected = {'interpretation': self.interpretation, 'response_interpretation': self.response_interpretation}
            else:
                saved = uow.connection.execute('SELECT interpretation_json FROM overseer_invocations WHERE input_id=? AND kind=?',
                                               (reference['id'], kind)).fetchone()
                expected = self.interpretation
        if saved is None or json.loads(saved[0]) != expected:
            raise DomainError('owner_input_required', 'The exact interpretation has not been recorded')
        if session is None or profile is None or self.runtime.native_chat is None or (owner is not None and owner['desired_state'] in ('paused', 'stopped')):
            raise DomainError('owner_input_required', 'The continuing Overseer conversation is offline')
        status = self.runtime.native_chat.session_view(profile, session)
        if (not status['can_send'] or status['conversation_id'] != request['source']['conversation_id']
                or status['binding']['binding_id'] != session['id']):
            raise DomainError('owner_input_required', 'The continuing Overseer conversation is offline or changed')
        key = ('signoff-response:' + reference['id'] + ':' + reference['revision'] + ':' + kind
               if reference['kind'] == 'signoff_response' else 'owner-message:' + reference['id'] + ':' + kind)
        return {'invocation_key': key, 'reference': reference,
                'allowed_kinds': [kind], 'origin': 'owner', 'owner_principal_id': request['source']['owner_principal_id'],
                'conversation_id': request['source']['conversation_id'],
                'executor': {'actor_id': ctx.principal_id, 'actor_kind': 'agent'},
                'arguments': self.interpretation['arguments'], 'source_text': request['source_text'],
                'source_hash': request['source_hash']}


def start_from_owner(runtime, context, kind, invocation, interpretation, operation_id, response_interpretation=None):
    if (not isinstance(interpretation, dict) or set(interpretation) != {'kind', 'arguments', 'rationale'}
            or interpretation['kind'] != kind or kind not in ('extraction', 'review', 'application')
            or not isinstance(interpretation['arguments'], dict)
            or not isinstance(interpretation['rationale'], str) or not interpretation['rationale'].strip()
            or len(interpretation['rationale'].split()) > 50):
        raise DomainError('validation_error', 'A bounded interpretation of the requested workflow is required')
    if not isinstance(invocation, dict) or set(invocation) != {'reference'} or not isinstance(invocation['reference'], dict):
        raise DomainError('validation_error', 'A canonical invocation reference is required')
    ref = invocation['reference']
    if set(ref) != {'provider', 'kind', 'id', 'revision'} or ref['provider'] != 'qorqut' or ref['kind'] not in (
            'owner_message', 'signoff_response', 'extraction_due'):
        raise DomainError('validation_error', 'A canonical owner request reference is required')
    if ref['kind'] == 'extraction_due':
        if kind != 'extraction' or response_interpretation is not None:
            raise DomainError('owner_input_required', 'A scheduled due reference authorizes extraction only')
        from .due import admit_due_extraction
        return admit_due_extraction(runtime, context, invocation, interpretation, operation_id)
    if ref['kind'] == 'signoff_response':
        from .workflow_requests import admitted_answer, canonical
        view, _, _ = admitted_answer(runtime, context, ref, kind, interpretation['arguments'], response_interpretation)
        encoded = canonical({'interpretation': interpretation, 'response_interpretation': response_interpretation})
        with runtime.database.transaction(operation='overseer.workflow-interpretation') as uow:
            runtime.application.ensure_writes_open(uow)
            current_overseer(runtime, context, uow, capability='manage_agentic_layer')
            request_id = view['request']['id']
            saved = uow.connection.execute('SELECT interpretation_json FROM overseer_workflow_interpretations WHERE request_id=? AND response_revision=?',
                                           (request_id, ref['revision'])).fetchone()
            if saved is not None and saved[0] != encoded:
                raise DomainError('operation_conflict', 'The answer already has a different interpretation')
            if saved is None:
                uow.connection.execute('INSERT INTO overseer_workflow_interpretations VALUES (?, ?, ?)', (request_id, ref['revision'], encoded))
        gateway = InvocationGateway(runtime, context, interpretation, response_interpretation)
        return runtime.agentic_layer.overseer.start_run(kind, invocation, operation_id=operation_id, gateway=gateway,
                                                       arguments=interpretation['arguments'])
    if response_interpretation is not None:
        raise DomainError('validation_error', 'Response interpretation applies only to a Signoff answer')
    request = runtime.overseer_retention.owner_request(context, ref['id'])
    if not request['eligible'] or request['reference'] != ref:
        raise DomainError('owner_input_required', 'The owner request is unavailable or not submitted')
    encoded = json.dumps(interpretation, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    with runtime.database.transaction(operation='overseer.invocation') as uow:
        runtime.application.ensure_writes_open(uow)
        current_overseer(runtime, context, uow, capability='manage_agentic_layer')
        saved = uow.connection.execute('SELECT interpretation_json FROM overseer_invocations WHERE input_id=? AND kind=?',
                                       (ref['id'], kind)).fetchone()
        if saved is not None and saved[0] != encoded:
            raise DomainError('operation_conflict', 'The owner request already has a different interpretation')
        if saved is None:
            uow.connection.execute('INSERT INTO overseer_invocations VALUES (?, ?, ?)', (ref['id'], kind, encoded))
    gateway = InvocationGateway(runtime, context, interpretation)
    return runtime.agentic_layer.overseer.start_run(kind, invocation, operation_id=operation_id, gateway=gateway,
                                                   arguments=interpretation['arguments'])
