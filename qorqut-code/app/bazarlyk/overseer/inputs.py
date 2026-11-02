"""Trusted reconciliation of canonical Operations input Signoff answers."""
from dataclasses import replace
import hashlib
import json

from qorqut_operations.contracts import DomainError
from qorqut_operations.signoffs.inputs import validate_response

from .authority import current_overseer


class OwnerInputGateway:
    """Re-read the real Signoff with the authenticated Overseer's current access."""

    def __init__(self, runtime, context, *, authority=None):
        self.runtime, self.context = runtime, context
        self.authority = authority or current_overseer

    def resolve_response(self, binding, reference):
        runtime = self.runtime
        scope = binding['owner_scope']
        business = scope['business_id']
        namespace = 'business:' + business if business else 'global'
        with runtime.database.read() as uow:
            ctx, _ = self.authority(runtime, self.context, uow)
            if business and business not in ctx.allowed_business_ids:
                raise DomainError('forbidden', 'The Signoff review scope is unavailable')
            ctx = replace(ctx, business_id=business, review_business_id=business)
            runtime.application.authorize(ctx, runtime.registry.get('signoffs_get'), (), uow, namespace=namespace)
            row = runtime.signoffs._row(ctx, reference['signoff_id'], uow)
            saved = uow.connection.execute('SELECT * FROM overseer_signoffs WHERE binding_id=?', (binding['id'],)).fetchone()
            if (saved is None or saved['request_key'] != binding['request_key']
                    or saved['signoff_id'] != row['id'] or saved['review_business_id'] != business
                    or saved['proposal_revision'] != row['proposal_revision'] or saved['payload_hash'] != row['payload_hash']):
                raise DomainError('stale_revision', 'The Signoff is not the saved binding request')
            if (scope['kind'] != ('business' if business else 'global') or row['category'] != 'input_request'
                    or row['review_business_id'] != business or row['request_key'] != binding['request_key']
                    or row['state'] != 'answered' or row['superseded_by_id'] is not None
                    or reference != {'provider': 'qorqut_signoff', 'signoff_id': row['id'],
                                     'proposal_revision': row['proposal_revision'], 'payload_hash': row['payload_hash'],
                                     'status_revision': row['status_revision']}):
                raise DomainError('stale_revision', 'The canonical Signoff answer is stale or unavailable')
            form = row['preview_json'].get('input', {})
            if (form.get('response_policy') != 'owner_only'
                    or {c['id'] for c in form.get('choices', [])} != set(binding['choice_actions'])):
                raise DomainError('invalid_transition', 'The Signoff question no longer matches its binding')
            decision = row['decision_json'] or {}
            owner = decision.get('by')
            principal = uow.connection.execute('SELECT kind,active FROM principals WHERE id=?', (owner,)).fetchone()
            if decision.get('kind') != 'respond' or principal is None or principal['kind'] != 'human' or not principal['active']:
                raise DomainError('forbidden', 'The answer was not made by an active owner')
            if business:
                member = uow.connection.execute('SELECT active,is_owner FROM business_memberships WHERE business_id=? AND principal_id=?',
                                                (business, owner)).fetchone()
                if member is None or not member['active'] or not member['is_owner']:
                    raise DomainError('forbidden', 'The responder no longer has owner authority for this scope')
            else:
                installation = uow.connection.execute("SELECT owner_principal_id FROM installation WHERE id='service'").fetchone()
                if installation is None or installation[0] != owner:
                    raise DomainError('forbidden', 'The responder is not the current installation owner')
            message_id = decision.get('message_id')
            notes = ''
            if message_id is not None:
                runtime.application.authorize(ctx, runtime.registry.get('signoffs_notes_list'), (), uow, namespace=namespace)
                runtime.signoffs.notes.context(ctx, {'signoff_id': row['id']}, uow)
                note = uow.connection.execute('SELECT body,created_by FROM signoff_notes WHERE id=? AND signoff_id=?',
                                              (message_id, row['id'])).fetchone()
                if note is None or note['created_by'] != owner:
                    raise DomainError('evidence_unavailable', 'The canonical answer text is unavailable')
                notes = note['body']
            notes, choices = validate_response(form, notes, decision.get('selected_choices') or [])
            if not isinstance(decision.get('at'), str) or not decision['at']:
                raise DomainError('evidence_unavailable', 'The canonical answer timestamp is unavailable')
            key = hashlib.sha256(json.dumps([row['id'], row['proposal_revision'], row['payload_hash'], row['status_revision']],
                                            separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
            return {'reference': reference, 'state': row['state'], 'superseded_by_id': None,
                    'owner_principal_id': owner, 'response_key': key,
                    'message_ref': {'provider': 'operations', 'kind': 'signoff_note', 'id': message_id} if message_id else None,
                    'selected_choice_ids': choices, 'notes': notes, 'answered_at': decision['at']}


def create_input_request(runtime, context, binding, form, operation_id, *, direct_submission=None):
    """Bind once, create/recover the real Signoff, then attach its exact identity."""
    from copy import deepcopy
    from qorqut_operations.contracts import plain
    from qorqut_operations.signoffs.inputs import validate_form
    from qorqut_agentic.errors import AgenticError
    from qorqut_agentic.overseer.views import Views
    from bazarlyk.native_chat import store

    if not isinstance(binding, dict) or not isinstance(form, dict):
        raise DomainError('validation_error', 'A binding and input form are required')
    binding = deepcopy(binding)
    expected_ref = binding.get('request_ref')
    binding['request_ref'] = None
    identity, request_key = binding['id'], binding['request_key']
    scope = binding['owner_scope']
    business = scope['business_id']
    if scope['kind'] != ('business' if business else 'global'):
        raise DomainError('validation_error', 'Invalid review scope')
    form = validate_form({**form, 'response_policy': 'owner_only'})
    if {choice['id'] for choice in form['choices']} != set(binding['choice_actions']):
        raise DomainError('validation_error', 'Every choice must match the saved binding')
    layer = runtime.agentic_layer
    def authorize(uow):
        if direct_submission is not None:
            from .direct import authorize_binding
            return authorize_binding(runtime, context, uow, binding, direct_submission)
        return current_overseer(runtime, context, uow, capability='manage_agentic_layer')

    # The narrow direct-submission authority is internal, never accepted in the
    # public Overseer input request. Validate it before material is read.
    with runtime.database.read() as uow:
        authorize(uow)
    targets = binding['targets']
    if binding['purpose'] == 'close_satisfied':
        if (not callable(getattr(layer.overseer.applications, 'inspect_satisfaction', None))
                or not isinstance(binding.get('satisfaction_inspection_id'), str)
                or not binding.get('satisfaction_inspection_id')):
            raise DomainError('operation_unavailable', 'A saved satisfaction inspection is required')
    if binding.get('batch_id') is not None:
        batch = layer.overseer.review.get_batch(binding['batch_id'])
        if (batch['run_id'] != binding['run_id'] or not any(
                item['delta_id'] == targets['delta_id'] and item['revision'] == targets['delta_revision']
                for item in batch['items'])):
            raise DomainError('invalid_transition', 'The binding is not an item in this saved batch')
    shown = {'targets': targets}
    if binding.get('evidence_refs'):
        shown['evidence_refs'] = binding['evidence_refs']
    if binding.get('failure_evidence'):
        shown['failure_evidence'] = binding['failure_evidence']
        shown['failure_check_run'] = binding.get('failure_check_run')
    if targets['delta_id'] is not None:
        delta = layer.overseer.deltas.get(targets['delta_id'], revision=targets['delta_revision'])
        runtime.overseer._check_delta(context, delta)
        shown['delta'] = delta['revision']
    from .access import EvidenceAccess, linked_source_refs
    source_refs = linked_source_refs(binding)
    if source_refs:
        access = EvidenceAccess(runtime, context)
        for ref in source_refs:
            access.authorize_source(ref)
    if targets['application_id'] is not None and targets['plan_revision'] is not None:
        application = layer.overseer.applications.get(targets['application_id'], plan_revision=targets['plan_revision'])
        shown['plan'] = application['plan']
    if binding['purpose'] == 'close_satisfied' and binding.get('satisfaction_evidence') is not None:
        shown['satisfaction_evidence'] = binding['satisfaction_evidence']
        shown['satisfaction_inspection_id'] = binding['satisfaction_inspection_id']
    explanation = form['explanation'] + '\n\nBound material:\n' + json.dumps(shown, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if len(explanation) > 8000:
        raise DomainError('validation_error', 'The exact bound material exceeds the Signoff explanation limit')
    form['explanation'] = explanation
    namespace = 'business:' + business if business else 'global'
    with runtime.database.read() as uow:
        ctx, _ = authorize(uow)
        if business and business not in ctx.allowed_business_ids:
            raise DomainError('forbidden', 'The requested review scope is unavailable')
        ctx = replace(ctx, business_id=business, review_business_id=business)
        session = store.latest_session(uow.connection, ctx.principal_id)
        admission = Views(layer).get_admitted_run(binding['run_id'])
        invocation = admission and admission.get('invocation', {})
        if direct_submission is None and (session is None or not admission or invocation.get('conversation_id') != session['conversation_id']
                or invocation.get('executor') != {'actor_id': ctx.principal_id, 'actor_kind': 'agent'}):
            raise DomainError('owner_input_required', 'A matching admitted Overseer workflow is required')
        runtime.application.authorize(ctx, runtime.registry.get('signoffs_get'), (), uow, namespace=namespace)
        rows = uow.connection.execute('SELECT id FROM signoffs WHERE proposer_principal_id=? AND review_business_id IS ? AND request_key=? LIMIT 2',
                                      (ctx.principal_id, business, request_key)).fetchall()
        if len(rows) > 1:
            raise DomainError('operation_conflict', 'More than one canonical Signoff uses this request key')
        existing = runtime.signoffs._row(ctx, rows[0]['id'], uow) if rows else None
        if existing is not None and (existing['category'] != 'input_request' or existing['preview_json'].get('input') != form):
            raise DomainError('operation_conflict', 'The saved Signoff question differs from this binding')
    try:
        layer.overseer.inputs.bind(binding, operation_id)
    except AgenticError as error:
        if error.code != 'operation_conflict':
            raise
        saved_binding = layer.overseer.inputs.get(identity)
        if {**saved_binding, 'request_ref': None} != binding or saved_binding['request_ref'] is None:
            raise
        layer.overseer.inputs.bind(saved_binding, operation_id)
    if existing is None:
        args = {'request_key': request_key, 'form': form, **({'scope': 'global'} if business is None else {})}
        result = runtime.application.invoke(ctx, 'signoffs_input_create', args,
                                            {'idempotency_key': 'overseer-input:' + identity})
        if result.status != 'ok':
            error = plain(result.error) if result.error else {'code': 'operation_unavailable', 'message': 'Signoff was not created'}
            raise DomainError(error['code'], error['message'], details=error.get('details'))
        signoff_id = result.data['signoff']['id']
    else:
        signoff_id = existing['id']
    with runtime.database.transaction(operation='overseer.input-association') as uow:
        runtime.application.ensure_writes_open(uow)
        current, _ = authorize(uow)
        current = replace(current, business_id=business, review_business_id=business)
        runtime.application.authorize(current, runtime.registry.get('signoffs_get'), (), uow, namespace=namespace)
        row = runtime.signoffs._row(current, signoff_id, uow)
        if (row['proposer_principal_id'] != current.principal_id or row['request_key'] != request_key
                or row['review_business_id'] != business or row['category'] != 'input_request'
                or row['preview_json'].get('input') != form):
            raise DomainError('operation_conflict', 'Canonical Signoff does not match the requested binding')
        reference = {'provider': 'qorqut_signoff', 'signoff_id': row['id'],
                     'proposal_revision': row['proposal_revision'], 'payload_hash': row['payload_hash']}
        if expected_ref is not None and expected_ref != reference:
            raise DomainError('stale_revision', 'The requested Signoff binding has changed')
        prior = uow.connection.execute('SELECT * FROM overseer_signoffs WHERE binding_id=?', (identity,)).fetchone()
        if prior is not None:
            if (prior['signoff_id'], prior['proposal_revision'], prior['payload_hash'], prior['review_business_id'], prior['request_key'],
                prior['purpose'], prior['run_id'], prior['delta_id'], prior['delta_revision'], prior['application_id'], prior['plan_revision'], prior['batch_id']) != (
                row['id'], row['proposal_revision'], row['payload_hash'], business, request_key, binding['purpose'], binding['run_id'],
                targets['delta_id'], targets['delta_revision'], targets['application_id'], targets['plan_revision'], binding.get('batch_id')):
                raise DomainError('operation_conflict', 'The binding already names a different Signoff')
        else:
            uow.connection.execute('INSERT INTO overseer_signoffs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (identity, request_key, row['id'], row['proposal_revision'], row['payload_hash'], business, binding['purpose'], binding['run_id'],
                 targets['delta_id'], targets['delta_revision'], targets['application_id'], targets['plan_revision'], binding.get('batch_id')))
        from qorqut_operations.contracts import utc_now
        event = json.dumps({'binding_id': identity, 'signoff_id': row['id']}, sort_keys=True, separators=(',', ':'))
        uow.connection.execute('''INSERT OR IGNORE INTO overseer_retention_events
            (event_key,kind,source_json,cursor_json,material_ref_json,state,updated_at)
            VALUES (?, 'signoff', ?, NULL, NULL, 'pending', ?)''', ('signoff:' + identity, event, utc_now()))
    bound = {**binding, 'request_ref': reference}
    layer.overseer.inputs.bind(bound, 'input-bind-request:' + identity)
    return {'binding': bound, 'signoff_id': signoff_id}
