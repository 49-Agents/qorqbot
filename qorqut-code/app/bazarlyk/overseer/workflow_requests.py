"""Canonical owner-only workflow-request Signoff reads."""
import hashlib
import json
from dataclasses import replace

from qorqut_operations.contracts import DomainError
from qorqut_operations.signoffs.inputs import validate_response
from bazarlyk.management.identity import installation_owner
from bazarlyk.native_chat import store
from .authority import current_overseer


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def response_revision(row):
    data = {'signoff_id': row['id'], 'proposal_revision': row['proposal_revision'],
            'payload_hash': row['payload_hash'], 'status_revision': row['status_revision']}
    return hashlib.sha256(canonical(data).encode('utf-8')).hexdigest()


def request_view(runtime, context, signoff_id):
    with runtime.database.read() as uow:
        ctx, _ = current_overseer(runtime, context, uow, capability='manage_agentic_layer')
        session = store.latest_session(uow.connection, ctx.principal_id)
        association = uow.connection.execute('SELECT * FROM overseer_workflow_requests WHERE signoff_id=?', (signoff_id,)).fetchone()
        if (association is None or session is None or association['agent_id'] != ctx.principal_id
                or association['conversation_id'] != session['conversation_id']):
            raise DomainError('not_found', 'The workflow request is unavailable')
        saved = json.loads(association['association_json'])
        ctx = replace(ctx, business_id=None, review_business_id=None)
        runtime.application.authorize(ctx, runtime.registry.get('signoffs_get'), (), uow, namespace='global')
        row = runtime.signoffs._row(ctx, signoff_id, uow)
        if (row['proposal_revision'] != association['proposal_revision'] or row['payload_hash'] != association['payload_hash']
                or row['request_key'] != association['request_key'] or row['proposer_principal_id'] != ctx.principal_id
                or row['review_business_id'] is not None or row['category'] != 'input_request'
                or row['preview_json'].get('input', {}).get('response_policy') != 'owner_only'):
            raise DomainError('evidence_unavailable', 'The canonical workflow request is inconsistent')
        response_ref, response = None, None
        if row['state'] == 'answered' and row['superseded_by_id'] is None:
            decision = row['decision_json'] or {}
            owner = installation_owner(uow)
            principal = uow.connection.execute('SELECT kind,active FROM principals WHERE id=?', (owner,)).fetchone()
            if (decision.get('kind') != 'respond' or decision.get('by') != owner or principal is None
                    or principal['kind'] != 'human' or not principal['active']):
                raise DomainError('forbidden', 'The workflow answer is not from the current owner')
            notes = ''
            if decision.get('message_id') is not None:
                runtime.application.authorize(ctx, runtime.registry.get('signoffs_notes_list'), (), uow, namespace='global')
                runtime.signoffs.notes.context(ctx, {'signoff_id': signoff_id}, uow)
                note = uow.connection.execute('SELECT body,created_by FROM signoff_notes WHERE id=? AND signoff_id=?',
                                              (decision['message_id'], signoff_id)).fetchone()
                if note is None or note['created_by'] != owner:
                    raise DomainError('evidence_unavailable', 'The canonical answer text is unavailable')
                notes = note['body']
            notes, choices = validate_response(row['preview_json']['input'], notes, decision.get('selected_choices') or [])
            if not isinstance(decision.get('at'), str) or not decision['at']:
                raise DomainError('evidence_unavailable', 'The canonical response timestamp is unavailable')
            response_ref = {'provider': 'qorqut', 'kind': 'signoff_response', 'id': signoff_id,
                            'revision': response_revision(row)}
            response = {'owner_principal_id': owner, 'selected_choice_ids': choices, 'notes': notes,
                        'answered_at': decision['at'], 'message_id': decision.get('message_id'),
                        'status_revision': row['status_revision'], 'proposal_revision': row['proposal_revision'],
                        'payload_hash': row['payload_hash']}
        return {'schema_version': 1, 'request': saved, 'status': row['state'], 'superseded_by_id': row['superseded_by_id'],
                'presentation': row['preview_json']['input'], 'response_reference': response_ref, 'response': response}


def admitted_answer(runtime, context, reference, kind, arguments, response_interpretation):
    view = request_view(runtime, context, reference.get('id'))
    request, response = view['request'], view['response']
    if (view['response_reference'] != reference or response is None or request['kind'] != kind
            or request['arguments'] != arguments):
        raise DomainError('owner_input_required', 'The exact workflow request and current answer are required')
    choices, notes = response['selected_choice_ids'], response['notes']
    if choices not in ([], ['start']) or not choices and not notes.strip():
        raise DomainError('owner_input_required', 'This answer does not start the workflow')
    if response_interpretation is not None:
        if (not isinstance(response_interpretation, dict)
                or set(response_interpretation) != {'clarity', 'action', 'rationale'}
                or response_interpretation['clarity'] != 'explicit' or response_interpretation['action'] != 'start'
                or not isinstance(response_interpretation['rationale'], str)
                or not response_interpretation['rationale'].strip()
                or len(response_interpretation['rationale'].split()) > 50):
            raise DomainError('owner_input_required', 'An explicit start interpretation is required')
    elif notes != '' or choices != ['start']:
        raise DomainError('owner_input_required', 'The owner response requires an explicit interpretation')
    selected = request.get('selected_application')
    if selected is not None:
        from qorqut_agentic.overseer.views import Views
        current = Views(runtime.agentic_layer).get_delta_application(selected['delta_id'])
        if (current is None or current['id'] != selected['application_id']
                or current['accepted_revision'] != selected['accepted_revision']
                or current['acceptance_ref'] != selected['acceptance_ref']):
            raise DomainError('stale_revision', 'The selected application acceptance has changed')
    text = canonical({'presentation': {'request': request, 'form': view['presentation']}, 'response': response})
    return view, text, hashlib.sha256(text.encode('utf-8')).hexdigest()


def create_request(runtime, context, request_key, kind, arguments, question, operation_id, source_ref=None):
    from uuid import UUID, uuid5
    from qorqut_operations.contracts import plain, utc_now
    from qorqut_operations.signoffs.inputs import validate_form
    from qorqut_agentic.overseer.views import Views
    from qorqut_agentic.util import check_operation_id
    from .archive import PrivateArchive
    from .retention import _digest

    if (not isinstance(request_key, str) or not request_key.strip() or len(request_key) > 200
            or kind not in ('extraction', 'review', 'application') or not isinstance(arguments, dict)
            or not isinstance(question, str) or not question.strip() or len(question) > 8000):
        raise DomainError('validation_error', 'A bounded workflow request is required')
    check_operation_id(operation_id)
    allowed = {'review': set(), 'extraction': {'selection'}, 'application': {'application_id'}}
    if set(arguments) - allowed[kind]:
        raise DomainError('validation_error', 'Unexpected workflow arguments')
    selected = None
    if kind == 'application' and arguments.get('application_id') is not None:
        app_id = arguments['application_id']
        try:
            if str(UUID(app_id)) != app_id: raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise DomainError('validation_error', 'A canonical application ID is required') from None
        with runtime.database.read() as uow:
            prior = uow.connection.execute('SELECT association_json FROM overseer_workflow_requests WHERE request_key=?',
                                            (request_key,)).fetchone()
        if prior is not None:
            previous = json.loads(prior[0])
            if any(previous.get(key) != value for key, value in {
                    'kind': kind, 'arguments': arguments, 'question': question, 'source_ref': source_ref}.items()):
                raise DomainError('operation_conflict', 'The request key was used for another workflow request')
            selected = previous['selected_application']
        else:
            app = runtime.agentic_layer.overseer.applications.get(app_id)
            current = Views(runtime.agentic_layer).get_delta_application(app['delta_id'])
            if current is None or current['id'] != app_id or app['phase'] != 'queued':
                raise DomainError('invalid_transition', 'The selected application is unavailable')
            selected = {'application_id': app_id, 'delta_id': current['delta_id'],
                        'accepted_revision': current['accepted_revision'], 'acceptance_ref': current['acceptance_ref']}
    if source_ref is not None:
        if not isinstance(source_ref, dict) or source_ref.get('kind') != 'owner_message':
            raise DomainError('validation_error', 'A current-conversation owner request is required')
        provenance = runtime.overseer_retention.owner_request(context, source_ref.get('id'))
        if provenance['reference'] != source_ref or provenance['sensitive']:
            raise DomainError('evidence_unavailable', 'The provenance request is unavailable')
    with runtime.database.read() as uow:
        ctx, _ = current_overseer(runtime, context, uow, capability='manage_agentic_layer')
        session = store.latest_session(uow.connection, ctx.principal_id)
        if session is None:
            raise DomainError('owner_input_required', 'The continuing conversation is unavailable')
        agent, conversation = ctx.principal_id, session['conversation_id']
    identity = str(uuid5(UUID('ea46e22e-539d-41c4-a53d-a15a09d3fd14'), canonical([agent, conversation, request_key])))
    particulars = {'workflow': kind, 'arguments': arguments, 'selected_application': selected}
    explanation = ('Start the requested workflow with these exact details: ' + canonical(particulars)
                   + '. Starting review does not approve deltas; starting application does not agree a plan.')
    if len(explanation) > 8000:
        raise DomainError('validation_error', 'The exact workflow arguments exceed the Signoff presentation limit')
    form = validate_form({'title': 'Start ' + kind + ' workflow?', 'explanation': question, 'kind': 'question',
        'selection_mode': 'single', 'selection_required': True, 'response_policy': 'owner_only',
        'choices': [{'id': 'start', 'label': 'Start workflow', 'explanation': explanation},
                    {'id': 'later', 'label': 'Later', 'explanation': 'Do not start this workflow now.'}]})
    fixed = {'id': identity, 'request_key': request_key, 'agent_id': agent, 'conversation_id': conversation,
             'namespace': {'kind': 'global', 'business_id': None}, 'kind': kind, 'arguments': arguments,
             'question': question, 'source_ref': source_ref, 'selected_application': selected}
    fingerprint = _digest(fixed)
    with runtime.database.transaction(operation='overseer.workflow-request-reserve') as uow:
        runtime.application.ensure_writes_open(uow)
        current_overseer(runtime, context, uow, capability='manage_agentic_layer')
        prior = uow.connection.execute('SELECT * FROM overseer_workflow_request_ops WHERE operation_id=?', (operation_id,)).fetchone()
        if prior is not None and (prior['request_id'] != identity or prior['fingerprint'] != fingerprint):
            raise DomainError('operation_conflict', 'The operation ID was used for another request')
        by_key = uow.connection.execute('SELECT * FROM overseer_workflow_requests WHERE request_key=?', (request_key,)).fetchone()
        if by_key is not None:
            existing = json.loads(by_key['association_json'])
            if any(existing.get(key) != value for key, value in fixed.items()):
                raise DomainError('operation_conflict', 'The request key was used for another workflow request')
        else:
            existing = None
        if prior is None:
            uow.connection.execute('INSERT INTO overseer_workflow_request_ops VALUES (?, ?, ?)', (operation_id, identity, fingerprint))
    if existing is not None:
        view = request_view(runtime, context, by_key['signoff_id'])
        return {'schema_version': 1, 'request': existing, 'response_reference': view['response_reference']}
    ctx = replace(ctx, business_id=None, review_business_id=None)
    with runtime.database.read() as uow:
        ctx, _ = current_overseer(runtime, context, uow, capability='manage_agentic_layer')
        ctx = replace(ctx, business_id=None, review_business_id=None)
        runtime.application.authorize(ctx, runtime.registry.get('signoffs_get'), (), uow, namespace='global')
        rows = uow.connection.execute('SELECT id FROM signoffs WHERE proposer_principal_id=? AND review_business_id IS NULL AND request_key=? LIMIT 2',
                                      (agent, request_key)).fetchall()
        if len(rows) > 1: raise DomainError('operation_conflict', 'The request key has multiple canonical Signoffs')
        row = runtime.signoffs._row(ctx, rows[0]['id'], uow) if rows else None
        if row is not None and (row['category'] != 'input_request' or row['preview_json'].get('input') != form
                or uow.connection.execute('SELECT 1 FROM overseer_signoffs WHERE signoff_id=?', (row['id'],)).fetchone()):
            raise DomainError('operation_conflict', 'The existing Signoff does not match this workflow request')
    if row is None:
        result = runtime.application.invoke(ctx, 'signoffs_input_create', {'request_key': request_key, 'form': form, 'scope': 'global'},
                                            {'idempotency_key': 'overseer-workflow:' + identity})
        if result.status != 'ok':
            error = plain(result.error) if result.error else {'code': 'operation_unavailable', 'message': 'Signoff was not created'}
            raise DomainError(error['code'], error['message'], details=error.get('details'))
        signoff_id = result.data['signoff']['id']
    else:
        signoff_id = row['id']
    with runtime.database.read() as uow:
        ctx, _ = current_overseer(runtime, context, uow, capability='manage_agentic_layer')
        ctx = replace(ctx, business_id=None, review_business_id=None)
        runtime.application.authorize(ctx, runtime.registry.get('signoffs_get'), (), uow, namespace='global')
        row = runtime.signoffs._row(ctx, signoff_id, uow)
        if (row['proposer_principal_id'] != agent or row['request_key'] != request_key or row['review_business_id'] is not None
                or row['category'] != 'input_request' or row['preview_json'].get('input') != form):
            raise DomainError('operation_conflict', 'The canonical Signoff does not match this request')
        request_ref = {'provider': 'qorqut_signoff', 'signoff_id': signoff_id,
                       'proposal_revision': row['proposal_revision'], 'payload_hash': row['payload_hash']}
    source = {'provider': 'operations', 'kind': 'signoff', 'namespace': {'kind': 'global', 'business_id': None},
              'record_id': signoff_id, 'locator': {'signoff_id': signoff_id}}
    run_ref = {'provider': 'qorqut_overseer', 'agent_id': agent, 'conversation_id': conversation, 'run_id': None,
               'namespace': {'kind': 'global', 'business_id': None}}
    def allow_source(ref):
        if ref != source: raise DomainError('forbidden', 'The Signoff is not the requested presentation')
        with runtime.database.read() as uow:
            current, _ = current_overseer(runtime, context, uow, capability='manage_agentic_layer')
            current = replace(current, business_id=None, review_business_id=None)
            runtime.application.authorize(current, runtime.registry.get('signoffs_get'), (), uow, namespace='global')
            exact = runtime.signoffs._row(current, signoff_id, uow)
            if (exact['proposal_revision'], exact['payload_hash']) != (request_ref['proposal_revision'], request_ref['payload_hash']):
                raise DomainError('stale_revision', 'The Signoff presentation changed')
    archive = PrivateArchive(runtime.paths.overseer_archive, allow_source,
        lambda ref, action: runtime.overseer_retention.authorize_retention(context, ref, action=action))
    presentation = archive.capture_retention(run_ref, [{'kind': 'workflow_request_presentation', 'form': form,
                                                       'request_ref': request_ref}], [source], 'workflow-presentation:' + identity)
    association = {**fixed, 'request_ref': request_ref, 'presentation_ref': presentation}
    with runtime.database.transaction(operation='overseer.workflow-request-association') as uow:
        runtime.application.ensure_writes_open(uow)
        current, _ = current_overseer(runtime, context, uow, capability='manage_agentic_layer')
        current = replace(current, business_id=None, review_business_id=None)
        runtime.application.authorize(current, runtime.registry.get('signoffs_get'), (), uow, namespace='global')
        exact = runtime.signoffs._row(current, signoff_id, uow)
        latest = store.latest_session(uow.connection, agent)
        if (latest is None or latest['conversation_id'] != conversation or exact['proposal_revision'] != request_ref['proposal_revision']
                or exact['payload_hash'] != request_ref['payload_hash'] or exact['preview_json'].get('input') != form):
            raise DomainError('stale_revision', 'The continuing request changed before association')
        previous = uow.connection.execute('SELECT association_json FROM overseer_workflow_requests WHERE id=? OR request_key=?', (identity, request_key)).fetchone()
        if previous is not None and previous[0] != canonical(association):
            raise DomainError('operation_conflict', 'The workflow request is already associated differently')
        if previous is None:
            uow.connection.execute('INSERT INTO overseer_workflow_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (identity, request_key, agent, conversation, canonical(association), signoff_id,
                 request_ref['proposal_revision'], request_ref['payload_hash']))
        event = {'request_id': identity, 'signoff_id': signoff_id}
        uow.connection.execute('''INSERT OR IGNORE INTO overseer_retention_events
            (event_key,kind,source_json,cursor_json,material_ref_json,state,updated_at)
            VALUES (?, 'workflow_signoff', ?, NULL, NULL, 'pending', ?)''',
            ('workflow-signoff:' + identity, canonical(event), utc_now()))
    view = request_view(runtime, context, signoff_id)
    return {'schema_version': 1, 'request': association, 'response_reference': view['response_reference']}
