"""Shared direct-instruction submissions into the ordinary delta/Signoff queue.

This path creates review records only. It grants neither extraction access nor
application authority. The native owner's original input hash authenticates the
instruction even after the transient input body has been scrubbed.
"""

from qorqut_agentic.behavior.tool_help import text as instruction_text
import hashlib
import json
from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

from qorqut_operations.contracts import DomainError, utc_now
from qorqut_agentic.errors import NotFoundError
from qorqut_agentic.overseer import validation

from qorqut.agents.global_scope import active_role_holder
from qorqut.agents.roles import LEARNING_APPLIER_ROLE
from qorqut.management.identity import installation_owner
from qorqut.native_chat import store
from qorqut.native_chat.contracts import InputRequest
from .authority import current_manager
from .provenance import source_ref


def _id(value):
    return str(uuid5(NAMESPACE_URL, 'qorqut:direct-instruction:' + value))


def _source_row(runtime, context, identity, uow):
    ctx = runtime.application.current_context(context, uow)
    owner = installation_owner(uow)
    if ctx.principal_kind == 'human':
        if ctx.principal_id != owner or ctx.source not in ('dashboard', 'worker'):
            raise DomainError('forbidden', 'The current owner is required')
        manager = None
    else:
        ctx, _ = current_manager(runtime, context, uow)
        manager = ctx.principal_id
    row = uow.connection.execute('''SELECT i.*, s.provider, s.conversation_id FROM native_inputs i
        JOIN native_sessions s ON s.id=i.session_id WHERE i.id=?''', (identity,)).fetchone()
    if (row is None or row['origin'] != 'owner' or row['input_type'] != 'chat'
            or row['principal_id'] != owner or row['sensitive'] or row['state'] != 'submitted'):
        raise DomainError('evidence_unavailable', 'A submitted, non-sensitive owner Chat instruction is required')
    overseer = active_role_holder(uow.connection, LEARNING_APPLIER_ROLE)
    if manager is not None and manager not in (row['agent_id'], overseer):
        raise DomainError('forbidden', 'The instruction belongs to another agent')
    return row


def authorize_source(runtime, context, ref):
    with runtime.database.read() as uow:
        row = _source_row(runtime, context, ref.get('record_id'), uow)
        expected = {'input_id': row['id'], 'agent_id': row['agent_id'], 'request_hash': row['request_hash'],
                    'provider': row['provider'], 'conversation_id': row['conversation_id']}
        if (ref.get('provider') != 'operations' or ref.get('kind') != 'owner_instruction'
                or ref.get('locator') != expected or ref.get('revision') != row['request_hash']
                or ref.get('actor_id') != row['principal_id'] or ref.get('actor_kind') != 'human'
                or ref.get('namespace') != {'kind': 'global', 'business_id': None}):
            raise DomainError('forbidden', 'The instruction reference does not match its canonical owner event')


def _instruction(runtime, context, text, source_id, uow):
    ctx, _ = current_manager(runtime, context, uow)
    if source_id:
        candidates = [source_id]
    else:
        candidates = [row[0] for row in uow.connection.execute('''SELECT id FROM native_inputs
            WHERE agent_id=? AND principal_id=? AND origin='owner' AND input_type='chat'
              AND sensitive=0 AND state='submitted'
            ORDER BY sequence DESC LIMIT 1000''', (ctx.principal_id, installation_owner(uow)))]
    for identity in candidates:
        row = _source_row(runtime, ctx, identity, uow)
        if row['agent_id'] != ctx.principal_id:
            raise DomainError('forbidden', 'Submit instructions from your own owner conversation')
        request = InputRequest(row['agent_id'], row['input_type'], row['source_key'], text,
            principal_id=row['principal_id'], expected_binding=row['session_id'])
        if store.request_hash(request) == row['request_hash']:
            return dict(row)
    raise DomainError('evidence_unavailable', 'Use the exact complete owner Chat message, including whitespace; no matching authenticated input was found')


def _load(runtime, context, identity):
    saved = runtime.agentic_layer.overseer.materials.get(identity)
    if saved.get('kind') != 'direct_instruction_submission':
        raise DomainError('not_found', 'Direct instruction submission not found')
    with runtime.database.read() as uow:
        ctx, _ = current_manager(runtime, context, uow)
        if ctx.principal_id != saved['created_by'] and ctx.principal_id != active_role_holder(uow.connection, LEARNING_APPLIER_ROLE):
            raise DomainError('forbidden', 'Submission belongs to another manager')
    authorize_source(runtime, ctx, saved['source'])
    return saved


def binding_for(saved):
    return {'schema_version': 1, 'id': _id(saved['id'] + ':review'), 'purpose': 'review',
        'run_id': 'direct-instruction:' + saved['id'],
        'targets': {'delta_id': _id(saved['id'] + ':delta'), 'delta_revision': 1,
                    'application_id': None, 'plan_revision': None},
        'owner_scope': {'kind': 'global', 'business_id': None},
        'request_key': 'direct-instruction:' + saved['id'], 'request_ref': None,
        'choice_actions': {'accept': 'accept', 'reject': 'reject', 'revise': 'revise', 'later': 'later'},
        'direct_submission_id': saved['id']}


def authorize_binding(runtime, context, uow, binding, submission_id):
    ctx, profile = current_manager(runtime, context, uow)
    saved = _load(runtime, ctx, submission_id)
    expected = binding_for(saved)
    candidate = {**binding, 'request_ref': None}
    if candidate != expected:
        raise DomainError('forbidden', 'The binding is not this exact direct-instruction review')
    return ctx, profile


def propose(runtime, context, *, request_key, instruction_text, summary, change_direction,
            scope, target_refs, source_id=None):
    validation.scope(scope)
    with runtime.database.read() as uow:
        ctx, _ = current_manager(runtime, context, uow)
        if any(project not in ctx.allowed_business_ids for project in scope['project_ids']):
            raise DomainError('forbidden', 'The proposed scope is outside current access')
        row = _instruction(runtime, ctx, instruction_text, source_id, uow)
        holder = active_role_holder(uow.connection, LEARNING_APPLIER_ROLE)
    if not holder:
        raise DomainError('operation_unavailable', 'An Overseer must be registered for the shared review queue')
    identity = _id(ctx.principal_id + ':' + request_key)
    payload = {'instruction_text': instruction_text, 'source_id': row['id'], 'summary': summary,
               'change_direction': change_direction, 'scope': scope, 'target_refs': target_refs}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    layer = runtime.agentic_layer
    try:
        saved = _load(runtime, ctx, identity)
    except NotFoundError:
        saved = None
    if saved is not None and saved['request_hash'] != digest:
        raise DomainError('operation_conflict', 'The request key already identifies a different proposal')
    if saved is None:
        from .access import EvidenceAccess
        from .evidence import CaptureJournal
        source_scope = {'description': instruction_text('direct-explicit-owner-instruction-in-the-submitting-agent-s'),
            'project_ids': [], 'work_types': [], 'workflow_refs': [], 'conditions': [], 'exceptions': []}
        ref = source_ref(provider='operations', kind='owner_instruction', record_id=row['id'],
            revision=row['request_hash'], content={'text': instruction_text},
            episode_id=row['conversation_id'], actor_id=row['principal_id'], actor_kind='human',
            occurred_at=row['created_at'], producer_id=row['principal_id'], original_event_id='owner-input:' + row['id'],
            locator={'input_id': row['id'], 'agent_id': row['agent_id'], 'request_hash': row['request_hash'],
                     'provider': row['provider'], 'conversation_id': row['conversation_id']},
            scope=source_scope, namespace={'kind': 'global', 'business_id': None})
        journal = CaptureJournal(runtime.paths.overseer_archive, EvidenceAccess(runtime, ctx), overseer_ids={holder})
        ref = journal.observe('direct-instruction:' + identity, 'owner-input:' + row['id'], ref,
                              {'text': instruction_text})
        saved = {'schema_version': 1, 'id': identity, 'kind': 'direct_instruction_submission',
                 'created_by': ctx.principal_id, 'created_at': utc_now(), 'request_hash': digest,
                 'source': ref, 'proposal': payload}
        layer.overseer.materials.record(saved, 'direct-material:' + identity)
    p = saved['proposal']
    proposal_id, delta_id = _id(identity + ':proposal'), _id(identity + ':delta')
    # Legacy column name: this explicitly named submission is not an extraction
    # run, and creates neither a workflow run nor analysis coverage.
    proposal = {'schema_version': 1, 'id': proposal_id, 'extraction_run_id': 'direct-instruction:' + identity,
        'submission_kind': 'direct_instruction', 'submission_id': identity,
        'summary': p['summary'], 'change_direction': p['change_direction'],
        'observed_fact': p['instruction_text'], 'interpretation': 'Proposed implementation of an explicit owner instruction; not approved.',
        'scope': p['scope'], 'target_refs': p['target_refs'], 'origin_type': 'direct_instruction',
        'sources': [{'ref': saved['source'], 'role': 'supporting'}],
        'created_at': saved['created_at'], 'created_by': saved['created_by']}
    layer.overseer.proposals.record(proposal, 'direct-proposal:' + identity)
    delta = {'schema_version': 1, 'delta_id': delta_id, 'revision': 1, 'summary': p['summary'],
        'change_direction': p['change_direction'], 'scope': p['scope'], 'target_refs': p['target_refs'],
        'proposal_ids': [proposal_id], 'support_summary': {'proposal_count': 1, 'event_count': 1, 'episode_count': 1,
            'event_refs': [saved['source']['origin_event_id']], 'episode_refs': [saved['source']['episode_id']]},
        'uncertainties': [], 'ranking_reason': 'Submitted directly for owner review; priority was not assigned by extraction.',
        'created_at': saved['created_at'], 'created_by': saved['created_by']}
    layer.overseer.deltas.create(delta, 'direct-delta:' + identity)
    from .inputs import create_input_request
    form = {'title': p['summary'], 'explanation': 'Review this proposed standing change. Acceptance queues it for separate owner-led application.',
            'kind': 'question', 'selection_mode': 'single', 'response_policy': 'owner_only',
            'choices': [{'id': key, 'label': label, 'explanation': label} for key, label in
                        [('accept', 'Accept'), ('reject', 'Reject'), ('revise', 'Revise'), ('later', 'Later')]]}
    create_input_request(runtime, ctx, binding_for(saved), form, 'direct-input:' + identity, direct_submission=identity)
    return get(runtime, ctx, identity)


def get(runtime, context, submission_id):
    saved = _load(runtime, context, submission_id)
    layer = runtime.agentic_layer
    binding = layer.overseer.inputs.get(binding_for(saved)['id'], include_response=True)
    ref = binding['binding']['request_ref']
    signoff = runtime.signoffs.get(replace(context, business_id=None, review_business_id=None), ref['signoff_id']) if ref else None
    if signoff is not None and signoff.status != 'ok':
        raise DomainError('forbidden', 'The proposal Signoff is no longer available')
    return {'submission_id': submission_id, 'proposal_id': _id(submission_id + ':proposal'),
            'delta': layer.overseer.deltas.get(_id(submission_id + ':delta')),
            'binding': binding, 'signoff': signoff.to_dict()['data']['signoff'] if signoff else None,
            'active_files_changed': False}


def reconcile(runtime, context, *, submission_id, response, operation_id):
    from .inputs import OwnerInputGateway
    saved = _load(runtime, context, submission_id)
    binding = runtime.agentic_layer.overseer.inputs.get(binding_for(saved)['id'])
    def authority(rt, ctx, uow):
        return authorize_binding(rt, ctx, uow, binding, submission_id)
    result = runtime.agentic_layer.overseer.apply_owner_response(binding['id'], response,
        operation_id=operation_id, gateway=OwnerInputGateway(runtime, context, authority=authority))
    return {'result': result, **get(runtime, context, submission_id)}
