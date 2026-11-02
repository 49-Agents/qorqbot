"""Frozen coverage, private material references, input bindings and review batches."""
import copy
import json
import re

from ..errors import AgenticError, NotFoundError
from . import validation as v
from .deltas import _delta, _revision
from .records import canonical, get_record, mutate


def blocking_inputs(conn, delta_id, resolving_ids=()):
    for row in conn.execute('SELECT id, record FROM overseer_inputs'):
        binding = json.loads(row['record'])
        if binding['targets']['delta_id'] != delta_id or row['id'] in resolving_ids:
            continue
        cleared = conn.execute('SELECT 1 FROM overseer_input_resolutions WHERE input_id=?', (row['id'],)).fetchone()
        resolved_here = conn.execute("SELECT 1 FROM overseer_owner_responses WHERE binding_id=? AND json_extract(result, '$.action')='clarify'", (row['id'],)).fetchone()
        ambiguous = conn.execute("SELECT 1 FROM overseer_owner_responses WHERE binding_id=? AND json_extract(result, '$.status')='needs_clarification'", (row['id'],)).fetchone()
        if not cleared and ((binding['purpose'] == 'clarify' and not resolved_here) or ambiguous):
            return True
    return False


class ImmutableRecords:
    def __init__(self, ctx, table, name):
        self.ctx, self.table, self.name = ctx, table, name

    def record(self, record: dict, operation_id: str):
        v.obj(record, self.name, ('schema_version', 'id'))
        v.version(record)
        v.uuid(record['id'], 'id')
        def write(conn):
            if conn.execute(f'SELECT 1 FROM {self.table} WHERE id=?', (record['id'],)).fetchone():
                raise AgenticError('record already exists', code='operation_conflict')
            conn.execute(f'INSERT INTO {self.table} VALUES (?, ?)', (record['id'], canonical(record)))
            return record
        return mutate(self.ctx, operation_id, self.name + '.record', record, write)

    def get(self, id: str):
        return get_record(self.ctx, self.table, id)


def validate_binding(record):
    v.obj(record, 'binding', ('schema_version', 'id', 'purpose', 'run_id', 'targets', 'owner_scope', 'request_key', 'request_ref', 'choice_actions'))
    v.version(record)
    v.uuid(record['id'], 'id')
    v.string(record['run_id'], 'run_id')
    v.string(record['request_key'], 'request_key')
    if record['purpose'] not in ('clarify', 'review', 'select', 'plan', 'failure', 'reopen', 'close_satisfied'):
        v.invalid('purpose')
    targets = record['targets']
    v.obj(targets, 'targets', ('delta_id', 'delta_revision', 'application_id', 'plan_revision'))
    for key in ('delta_id', 'application_id'):
        if targets[key] is not None:
            v.uuid(targets[key], key)
    for key in ('delta_revision', 'plan_revision'):
        if targets[key] is not None and (type(targets[key]) is not int or targets[key] < 1):
            v.invalid(key)
    if record['purpose'] in ('review', 'reopen', 'close_satisfied') and (targets['delta_id'] is None or targets['delta_revision'] is None):
        v.invalid('targets')
    if record['purpose'] in ('plan', 'failure') and (targets['delta_id'] is None or targets['delta_revision'] is None or targets['application_id'] is None or targets['plan_revision'] is None):
        v.invalid('targets')
    if record['purpose'] in ('plan', 'failure', 'close_satisfied'):
        if targets['application_id'] is None or type(targets.get('application_revision')) is not int or targets['application_revision'] < 1:
            v.invalid('targets.application_revision')
    v.obj(record['owner_scope'], 'owner_scope', ('kind', 'business_id'))
    scope = record['owner_scope']
    if scope['kind'] not in ('global', 'business') or (scope['kind'] == 'global' and scope['business_id'] is not None):
        v.invalid('owner_scope')
    if scope['kind'] == 'business':
        v.uuid(scope['business_id'], 'owner_scope.business_id')
    if record['request_ref'] is not None:
        ref = record['request_ref']
        v.obj(ref, 'request_ref', ('provider', 'signoff_id', 'proposal_revision', 'payload_hash'))
        v.string(ref['provider'], 'provider')
        v.uuid(ref['signoff_id'], 'signoff_id')
        if type(ref['proposal_revision']) is not int or ref['proposal_revision'] < 1:
            v.invalid('proposal_revision')
        v.string(ref['payload_hash'], 'payload_hash')
    if not isinstance(record['choice_actions'], dict):
        v.invalid('choice_actions')
    allowed = {'review': {'accept', 'reject', 'revise', 'later'}, 'plan': {'proceed', 'revise', 'later'},
               'reopen': {'reopen', 'later'}, 'clarify': {'clarify', 'later'}, 'select': {'select', 'later'},
               'close_satisfied': {'close_satisfied', 'later'}, 'failure': {'retry_checks', 'repair', 'revert', 'leave', 'later'}}
    for key, action in record['choice_actions'].items():
        v.string(key, 'choice_id')
        v.string(action, 'choice_action')
        if action not in allowed[record['purpose']]:
            v.invalid('choice_actions')
    if 'choice_targets' in record:
        if record['purpose'] != 'select' or not isinstance(record['choice_targets'], dict):
            v.invalid('choice_targets')
        for choice, target in record['choice_targets'].items():
            if choice not in record['choice_actions'] or record['choice_actions'][choice] != 'select':
                v.invalid('choice_targets')
            v.obj(target, 'choice_target', ('application_id',))
            v.uuid(target['application_id'], 'choice_target.application_id')
    resolutions = record.get('resolves_input_ids', [])
    v.array(resolutions, 'resolves_input_ids')
    for input_id in resolutions:
        v.uuid(input_id, 'resolves_input_ids')
    if len(set(resolutions)) != len(resolutions) or record['id'] in resolutions:
        v.invalid('resolves_input_ids')
    if record['purpose'] == 'close_satisfied':
        evidence = record.get('satisfaction_evidence')
        v.obj(evidence, 'satisfaction_evidence', ('observed_at', 'active_commit', 'resource_hashes', 'source_refs', 'check_receipt_refs', 'explanation'))
        v.string(evidence['observed_at'], 'satisfaction_evidence.observed_at')
        v.string(evidence['explanation'], 'satisfaction_evidence.explanation')
        if not isinstance(evidence['active_commit'], str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', evidence['active_commit']):
            v.invalid('satisfaction_evidence.active_commit')
        if not isinstance(evidence['resource_hashes'], dict):
            v.invalid('satisfaction_evidence.resource_hashes')
        for path, digest in evidence['resource_hashes'].items():
            v.string(path, 'satisfaction_evidence.resource_hashes')
            if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
                v.invalid('satisfaction_evidence.resource_hashes')
        v.array(evidence['source_refs'], 'satisfaction_evidence.source_refs')
        v.array(evidence['check_receipt_refs'], 'satisfaction_evidence.check_receipt_refs')
        for item in evidence['source_refs'] + evidence['check_receipt_refs']:
            if not ((isinstance(item, str) and item.strip()) or (isinstance(item, dict) and item)):
                v.invalid('satisfaction_evidence', 'requires concrete references')
        if not (evidence['resource_hashes'] or evidence['source_refs'] or evidence['check_receipt_refs']):
            v.invalid('satisfaction_evidence', 'requires concrete evidence')


class Inputs:
    def __init__(self, ctx):
        self.ctx = ctx

    def bind(self, record: dict, operation_id: str):
        validate_binding(record)
        def write(conn):
            old = conn.execute('SELECT record FROM overseer_inputs WHERE id=?', (record['id'],)).fetchone()
            if old:
                saved = json.loads(old['record'])
                previous, requested = copy.deepcopy(saved), copy.deepcopy(record)
                previous.pop('request_ref'); requested.pop('request_ref')
                if previous != requested or (saved['request_ref'] is not None and saved['request_ref'] != record['request_ref']):
                    raise AgenticError('binding cannot be repointed', code='operation_conflict')
                if saved['request_ref'] is None and record['request_ref'] is not None:
                    conn.execute('UPDATE overseer_inputs SET record=? WHERE id=?', (canonical(record), record['id']))
                    return record
                return saved
            for row in conn.execute('SELECT record FROM overseer_inputs'):
                if json.loads(row['record'])['request_key'] == record['request_key']:
                    raise AgenticError('request key already bound', code='operation_conflict')
            for input_id in record.get('resolves_input_ids', []):
                prior = conn.execute('SELECT record FROM overseer_inputs WHERE id=?', (input_id,)).fetchone()
                if prior is None:
                    raise NotFoundError('resolved input binding not found')
                previous = json.loads(prior['record'])
                if previous['owner_scope'] != record['owner_scope']:
                    raise AgenticError('input resolution scope differs', code='invalid_transition')
                for key in ('delta_id', 'application_id'):
                    old_target, new_target = previous['targets'].get(key), record['targets'].get(key)
                    if old_target != new_target:
                        raise AgenticError('input resolution target differs', code='invalid_transition')
            targets = record['targets']
            if targets['delta_id'] is not None:
                row = _delta(conn, targets['delta_id'])
                if targets['delta_revision'] != row['current_revision']:
                    raise AgenticError('delta revision changed', code='stale_revision')
            if targets['application_id'] is not None:
                app = conn.execute('SELECT delta_id,plan_revision,revision FROM delta_applications WHERE id=?', (targets['application_id'],)).fetchone()
                if app is None:
                    raise NotFoundError('application not found')
                if app['delta_id'] != targets['delta_id'] or app['plan_revision'] != targets['plan_revision'] or (targets.get('application_revision') is not None and app['revision'] != targets['application_revision']):
                    raise AgenticError('plan revision changed', code='stale_revision')
            conn.execute('INSERT INTO overseer_inputs VALUES (?, ?)', (record['id'], canonical(record)))
            return record
        return mutate(self.ctx, operation_id, 'inputs.bind', record, write)

    def get(self, id: str, include_response: bool = False):
        binding = get_record(self.ctx, 'overseer_inputs', id)
        if not include_response:
            return binding
        with self.ctx.store.reader() as conn:
            answer = conn.execute('SELECT digest, record, result FROM overseer_owner_responses WHERE binding_id=?', (id,)).fetchone()
            resolution = conn.execute('SELECT resolving_input_id, response_key FROM overseer_input_resolutions WHERE input_id=?', (id,)).fetchone()
        return {'binding': binding, 'response': json.loads(answer['record']) if answer else None,
                'canonical_digest': answer['digest'] if answer else None,
                'result': json.loads(answer['result']) if answer else None,
                'resolution': dict(resolution) if resolution else None}


class Review:
    def __init__(self, ctx):
        self.ctx = ctx

    def save_batch(self, record: dict, operation_id: str):
        v.obj(record, 'batch', ('schema_version', 'id', 'run_id', 'items', 'presentation_ref', 'created_at'))
        v.version(record)
        v.uuid(record['id'], 'id')
        v.string(record['run_id'], 'run_id')
        v.string(record['created_at'], 'created_at')
        v.array(record['items'], 'items')
        if len(record['items']) > 5:
            v.invalid('items', 'must contain at most five items')
        seen = set()
        for item in record['items']:
            v.obj(item, 'item', ('delta_id', 'revision', 'ranking_reason'))
            v.uuid(item['delta_id'], 'delta_id')
            if item['delta_id'] in seen:
                v.invalid('items', 'cannot repeat a delta')
            seen.add(item['delta_id'])
            if type(item['revision']) is not int or item['revision'] < 1 or not isinstance(item['ranking_reason'], str) or len(item['ranking_reason'].split()) > 50:
                v.invalid('item')
        def write(conn):
            if conn.execute('SELECT 1 FROM overseer_batches WHERE id=?', (record['id'],)).fetchone():
                raise AgenticError('batch already exists', code='operation_conflict')
            for item in record['items']:
                delta = _delta(conn, item['delta_id'])
                if delta['current_revision'] != item['revision']:
                    raise AgenticError('delta revision changed', code='stale_revision')
                rev = _revision(conn, item['delta_id'], item['revision'])
                if delta['decision'] != 'undecided' or not rev['proposal_ids']:
                    raise AgenticError('delta is not actionable', code='invalid_transition')
                if blocking_inputs(conn, item['delta_id']):
                    raise AgenticError('delta awaits clarification', code='owner_input_required')
            conn.execute('INSERT INTO overseer_batches VALUES (?, ?)', (record['id'], canonical(record)))
            return record
        return mutate(self.ctx, operation_id, 'review.save_batch', record, write)

    def get_batch(self, id: str):
        return get_record(self.ctx, 'overseer_batches', id)
