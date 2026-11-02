"""Trusted host response reconciliation; no owner-decision CLI entry point."""
import hashlib
import json
from uuid import uuid4

from ..errors import AgenticError, NotFoundError
from . import validation as v
from .deltas import _delta, _read
from .records import canonical, mutate

_ACTIONS = {
    'review': {'accept', 'reject', 'revise', 'later'},
    'plan': {'proceed', 'revise', 'later'},
    'reopen': {'reopen', 'later'},
    'clarify': {'clarify', 'later'},
    'select': {'select', 'later'},
    'close_satisfied': {'close_satisfied', 'later'},
    'failure': {'retry_checks', 'repair', 'revert', 'leave', 'later'},
}


def _response_action(binding, result, interpretation):
    choices = result['selected_choice_ids']
    notes = result['notes']
    actions = binding['choice_actions']
    mapped = [actions.get(choice) for choice in choices]
    if any(action is None for action in mapped) or (len(choices) > 1 and binding['purpose'] != 'select'):
        return None
    if len(set(mapped)) > 1:
        return None
    chosen = mapped[0] if mapped else None
    if not notes.strip() and chosen is not None and interpretation is None:
        return chosen
    if not isinstance(interpretation, dict) or set(interpretation) != {'clarity', 'action', 'rationale'}:
        return None
    if interpretation['clarity'] != 'explicit' or not isinstance(interpretation['rationale'], str) or not interpretation['rationale'].strip():
        return None
    action = interpretation['action']
    if not isinstance(action, str) or action not in _ACTIONS[binding['purpose']] or (chosen is not None and action != chosen):
        return None
    return action


def _canonical_response(binding, response, gateway):
    v.obj(response, 'response', ('reference', 'interpretation'))
    ref = response['reference']
    v.obj(ref, 'reference', ('provider', 'signoff_id', 'proposal_revision', 'payload_hash', 'status_revision'))
    request = binding['request_ref']
    if request is None or any(ref.get(k) != request.get(k) for k in ('provider', 'signoff_id', 'proposal_revision', 'payload_hash')):
        raise AgenticError('response does not match the binding request', code='stale_revision')
    if type(ref['status_revision']) is not int or ref['status_revision'] < 1:
        v.invalid('status_revision')
    result = gateway.resolve_response(binding, ref)
    v.obj(result, 'gateway result', ('reference', 'state', 'superseded_by_id', 'owner_principal_id', 'response_key', 'message_ref', 'selected_choice_ids', 'notes', 'answered_at'))
    if result['reference'] != ref or result['state'] != 'answered' or result['superseded_by_id'] is not None:
        raise AgenticError('canonical response is stale or not answered', code='stale_revision')
    v.string(result['owner_principal_id'], 'owner_principal_id')
    v.string(result['response_key'], 'response_key')
    v.string(result['answered_at'], 'answered_at')
    v.array(result['selected_choice_ids'], 'selected_choice_ids')
    for choice in result['selected_choice_ids']:
        v.string(choice, 'selected_choice_id')
    if not isinstance(result['notes'], str):
        v.invalid('notes')
    return result


def _transition(conn, binding, result, action, now):
    targets = binding['targets']
    delta_id, delta_revision = targets['delta_id'], targets['delta_revision']
    app_id, plan_revision = targets['application_id'], targets['plan_revision']
    delta = _delta(conn, delta_id) if delta_id else None
    if delta and delta['current_revision'] != delta_revision:
        return {'status': 'stale_revision', 'action': None}
    app = None
    if app_id is not None:
        app = conn.execute('SELECT * FROM delta_applications WHERE id=?', (app_id,)).fetchone()
        if app is None or app['delta_id'] != delta_id or app['plan_revision'] != plan_revision or (targets.get('application_revision') is not None and app['revision'] != targets['application_revision']):
            return {'status': 'stale_revision', 'action': None}
    if action is None:
        return {'status': 'needs_clarification', 'action': None}
    purpose = binding['purpose']
    reference = {'response_key': result['response_key'], 'reference': result['reference']}
    if purpose in ('review', 'plan') and action in ('accept', 'reject', 'proceed'):
        from .support import blocking_inputs
        if blocking_inputs(conn, delta_id, binding.get('resolves_input_ids', [])):
            return {'status': 'needs_clarification', 'action': None}
    if purpose == 'review' and action in ('accept', 'reject'):
        if delta['decision'] != 'undecided':
            return {'status': 'invalid_transition', 'action': None}
        if action == 'accept':
            if conn.execute("SELECT 1 FROM delta_applications WHERE delta_id=? AND phase != 'complete'", (delta_id,)).fetchone():
                return {'status': 'invalid_transition', 'action': None}
            application_id = str(uuid4())
            conn.execute('UPDATE deltas SET decision=?, accepted_revision=?, acceptance_ref=?, updated_at=? WHERE id=?',
                         ('accepted', delta_revision, canonical(reference), now, delta_id))
            conn.execute('''INSERT INTO delta_applications
                (id,delta_id,accepted_revision,acceptance_ref,run_id,invocation_ref,phase,plan_revision,waiting_input_ref,external_tasks,merge,checks,created_at,updated_at)
                VALUES (?,?,?,?,NULL,NULL,'queued',NULL,NULL,?,NULL,?,?,?)''',
                         (application_id, delta_id, delta_revision, canonical(reference), '[]', canonical({'state': 'not_run', 'receipts': []}), now, now))
            return {'status': 'applied', 'action': action, 'delta': _read(conn, delta_id), 'application_id': application_id}
        conn.execute("UPDATE deltas SET decision='rejected', updated_at=? WHERE id=?", (now, delta_id))
    elif purpose == 'reopen' and action == 'reopen':
        if delta['decision'] != 'rejected':
            return {'status': 'invalid_transition', 'action': None}
        conn.execute("UPDATE deltas SET decision='undecided', updated_at=? WHERE id=?", (now, delta_id))
    elif purpose == 'plan' and action == 'proceed':
        if not app or delta['decision'] != 'accepted' or app['phase'] not in ('queued', 'planning'):
            return {'status': 'invalid_transition', 'action': None}
        saved = conn.execute('SELECT record FROM overseer_plan_revisions WHERE application_id=? AND revision=?', (app_id, plan_revision)).fetchone()
        if saved is None or json.loads(saved['record'])['delta_revision'] != delta_revision:
            return {'status': 'stale_revision', 'action': None}
        run = conn.execute('SELECT control FROM runs WHERE run_id=?', (app['run_id'],)).fetchone()
        if run is None or run['control'] != 'running':
            return {'status': 'invalid_transition', 'action': None}
        conn.execute('UPDATE deltas SET accepted_revision=?, acceptance_ref=?, updated_at=? WHERE id=?',
                     (delta_revision, canonical(reference), now, delta_id))
        conn.execute("UPDATE delta_applications SET accepted_revision=?, acceptance_ref=?, agreement_ref=?, phase='planning', revision=revision+1, updated_at=? WHERE id=?",
                     (delta_revision, canonical(reference), canonical(reference), now, app_id))
    elif purpose == 'select' and action == 'select':
        from .runs import admitted_run
        choices = result['selected_choice_ids']
        targets_by_choice = binding.get('choice_targets', {})
        if len(choices) != 1 or choices[0] not in targets_by_choice:
            return {'status': 'needs_clarification', 'action': None}
        selected = targets_by_choice[choices[0]].get('application_id')
        run = admitted_run(conn, binding['run_id'], 'application')
        app = conn.execute('''SELECT a.*, d.decision, d.accepted_revision AS delta_accepted_revision
                              FROM delta_applications a JOIN deltas d ON d.id=a.delta_id WHERE a.id=?''', (selected,)).fetchone()
        if run is None or app is None or app['phase'] != 'queued' or app['run_id'] is not None or app['decision'] != 'accepted' or app['accepted_revision'] != app['delta_accepted_revision']:
            return {'status': 'invalid_transition', 'action': None}
        conn.execute("UPDATE delta_applications SET run_id=?, invocation_ref=?, phase='planning', revision=revision+1, updated_at=? WHERE id=?",
                     (binding['run_id'], canonical(run['invocation']['reference']), now, selected))
        return {'status': 'applied', 'action': action, 'application_id': selected}
    elif purpose == 'close_satisfied' and action == 'close_satisfied':
        if not app or delta['decision'] != 'accepted' or app['accepted_revision'] != delta_revision or app['merge'] is not None or app['phase'] not in ('queued', 'planning'):
            return {'status': 'invalid_transition', 'action': None}
        conn.execute("UPDATE delta_applications SET phase='complete', outcome='already_satisfied', satisfaction_evidence=?, revision=revision+1, updated_at=? WHERE id=?",
                     (canonical({'evidence': binding['satisfaction_evidence'], 'owner_response': reference}), now, app_id))
    return {'status': 'applied', 'action': action, 'delta': _read(conn, delta_id) if delta_id else None}


def apply(ctx, binding_id, response, *, operation_id, gateway=None):
    if gateway is None:
        raise AgenticError('trusted owner input gateway is required', code='owner_input_required')
    v.uuid(binding_id, 'binding_id')
    with ctx.store.reader() as conn:
        row = conn.execute('SELECT record FROM overseer_inputs WHERE id=?', (binding_id,)).fetchone()
    if row is None:
        raise NotFoundError('binding not found')
    binding = json.loads(row['record'])
    result = _canonical_response(binding, response, gateway)
    interpretation = response['interpretation']
    digest = hashlib.sha256(canonical(result).encode()).hexdigest()
    full = {'canonical': result, 'interpretation': interpretation}
    def write(conn):
        old = conn.execute('SELECT binding_id, digest, record, result FROM overseer_owner_responses WHERE response_key=?', (result['response_key'],)).fetchone()
        if old:
            if old['binding_id'] != binding_id or old['digest'] != digest or json.loads(old['record']) != full:
                raise AgenticError('canonical response identity reused with different content', code='operation_conflict')
            return json.loads(old['result'])
        if conn.execute('SELECT 1 FROM overseer_owner_responses WHERE binding_id=?', (binding_id,)).fetchone():
            raise AgenticError('binding already has a canonical response', code='operation_conflict')
        current = conn.execute('SELECT record FROM overseer_inputs WHERE id=?', (binding_id,)).fetchone()
        if not current or json.loads(current['record']) != binding:
            raise AgenticError('binding changed during response reconciliation', code='stale_revision')
        action = _response_action(binding, result, interpretation)
        outcome = _transition(conn, binding, result, action, ctx.now())
        conn.execute('INSERT INTO overseer_owner_responses VALUES (?, ?, ?, ?, ?)',
                     (result['response_key'], binding_id, digest, canonical(full), canonical(outcome)))
        if outcome['status'] == 'applied' and outcome['action'] not in (None, 'later', 'revise'):
            for old_id in binding.get('resolves_input_ids', []):
                conn.execute('INSERT OR IGNORE INTO overseer_input_resolutions VALUES (?, ?, ?)',
                             (old_id, binding_id, result['response_key']))
        return outcome
    outcome = mutate(ctx, operation_id, 'owner_response', {'binding_id': binding_id, 'response': response, 'canonical_digest': digest}, write)
    if outcome['status'] in ('stale_revision', 'invalid_transition'):
        raise AgenticError('owner response cannot change current state', code=outcome['status'])
    return outcome
