"""Admission of host-verified invocations into the existing workflow runner."""
import hashlib
import json

from ..errors import AgenticError
from ..util import check_operation_id
from . import validation as v
from .records import canonical

_REFS = {'extraction': 'overseer/extract', 'review': 'overseer/review', 'application': 'overseer/apply'}


def start(ctx, workflows, kind, invocation, *, operation_id, gateway=None, arguments=None):
    if gateway is None:
        raise AgenticError('trusted invocation gateway is required', code='owner_input_required')
    if not isinstance(kind, str) or kind not in _REFS:
        v.invalid('kind')
    v.obj(invocation, 'invocation', ('reference',))
    ref = invocation['reference']
    v.obj(ref, 'reference', ('provider', 'kind', 'id', 'revision'))
    for field in ('provider', 'id', 'revision'):
        v.string(ref[field], field)
    if ref['kind'] not in ('owner_message', 'signoff_response', 'extraction_due'):
        v.invalid('reference.kind')
    resolved = gateway.resolve_invocation(kind, ref)
    v.obj(resolved, 'gateway invocation', ('invocation_key', 'reference', 'allowed_kinds', 'origin', 'owner_principal_id', 'conversation_id', 'executor', 'arguments', 'source_text', 'source_hash'))
    v.string(resolved['invocation_key'], 'invocation_key')
    v.string(resolved['conversation_id'], 'conversation_id')
    v.string(resolved['source_hash'], 'source_hash')
    if not isinstance(resolved['source_text'], str):
        v.invalid('source_text')
    if resolved['reference'] != ref or not isinstance(resolved['allowed_kinds'], list) or kind not in resolved['allowed_kinds']:
        raise AgenticError('invocation is not admitted for this workflow', code='owner_input_required')
    if resolved['origin'] == 'routine':
        if kind != 'extraction' or ref['kind'] != 'extraction_due' or resolved['owner_principal_id'] is not None or resolved['allowed_kinds'] != ['extraction']:
            raise AgenticError('routine invocation cannot authorize this workflow', code='owner_input_required')
    elif resolved['origin'] == 'owner':
        if ref['kind'] not in ('owner_message', 'signoff_response') or not resolved['owner_principal_id']:
            raise AgenticError('owner invocation is not admitted', code='owner_input_required')
    else:
        raise AgenticError('invocation origin is not admitted', code='owner_input_required')
    v.obj(resolved['executor'], 'executor', ('actor_id', 'actor_kind'))
    if resolved['executor']['actor_kind'] != 'agent':
        raise AgenticError('executor must be the admitted agent', code='owner_input_required')
    v.string(resolved['executor']['actor_id'], 'executor.actor_id')
    if not isinstance(resolved['arguments'], dict) or (arguments is not None and arguments != resolved['arguments']):
        raise AgenticError('caller arguments differ from admitted invocation', code='operation_conflict')
    check_operation_id(operation_id)
    if operation_id.lower().startswith('run:'):
        v.invalid('operation_id', 'uses the reserved invocation namespace')
    argument_values = resolved['arguments']
    if kind == 'review' and argument_values:
        v.invalid('arguments')
    if kind == 'extraction' and set(argument_values) - {'selection'}:
        v.invalid('arguments')
    if kind == 'application' and set(argument_values) - {'application_id'}:
        v.invalid('arguments')
    app_id = argument_values.get('application_id')
    if app_id is not None:
        v.uuid(app_id, 'application_id')
    identity = hashlib.sha256(canonical([resolved['invocation_key'], kind]).encode()).hexdigest()
    key = 'run:' + identity
    runner_op = 'overseer-run-' + identity
    fingerprint = hashlib.sha256(canonical({'kind': kind, 'gateway': resolved}).encode()).hexdigest()
    with ctx.store.lock(exclusive=True), ctx.store.transaction() as conn:
        ctx.guard_overseer_admission(conn, application_id=app_id)
        for op in (operation_id, key):
            row = conn.execute('SELECT fingerprint FROM overseer_operations WHERE operation_id=?', (op,)).fetchone()
            if row and row['fingerprint'] != fingerprint:
                raise AgenticError('invocation or operation ID reused with different content', code='operation_conflict')
        row = conn.execute('SELECT result FROM overseer_operations WHERE operation_id=?', (key,)).fetchone()
        reservation = json.loads(row['result']) if row else {'state': 'reserved', 'kind': kind, 'invocation': resolved, 'runner_operation_id': runner_op}
        if reservation['state'] != 'linked' and app_id is not None:
            app = conn.execute('''SELECT a.phase, a.accepted_revision, d.decision, d.accepted_revision AS delta_accepted_revision
                                  FROM delta_applications a JOIN deltas d ON d.id=a.delta_id WHERE a.id=?''', (app_id,)).fetchone()
            if app is None or app['phase'] != 'queued' or app['decision'] != 'accepted' or app['accepted_revision'] != app['delta_accepted_revision']:
                raise AgenticError('application is not available for selection', code='invalid_transition')
        for op in (operation_id, key):
            conn.execute('INSERT OR IGNORE INTO overseer_operations VALUES (?, ?, ?, ?)', (op, fingerprint, canonical(reservation), ctx.now()))
    if reservation['state'] == 'linked':
        return reservation
    runner_args = {name: {'value': value, 'source': 'admitted_invocation'} for name, value in argument_values.items()}
    run = workflows.start(_REFS[kind], resolved['executor'], arguments=runner_args, operation_id=runner_op)['run']
    ctx.hook('overseer_after_run_start', run_id=run['run_id'])
    with ctx.store.lock(exclusive=True), ctx.store.transaction() as conn:
        ctx.guard_overseer_admission(conn, application_id=app_id)
        old = json.loads(conn.execute('SELECT result FROM overseer_operations WHERE operation_id=?', (key,)).fetchone()['result'])
        if old['state'] == 'linked':
            reservation = old
        else:
            if app_id is not None:
                app = conn.execute('''SELECT a.*, d.decision, d.accepted_revision AS delta_accepted_revision
                                      FROM delta_applications a JOIN deltas d ON d.id=a.delta_id WHERE a.id=?''', (app_id,)).fetchone()
                if app is None or app['phase'] != 'queued' or app['run_id'] not in (None, run['run_id']) or app['decision'] != 'accepted' or app['accepted_revision'] != app['delta_accepted_revision']:
                    raise AgenticError('application is not available for selection', code='invalid_transition')
                conn.execute("UPDATE delta_applications SET run_id=?, invocation_ref=?, phase='planning', revision=revision+1, updated_at=? WHERE id=?",
                             (run['run_id'], canonical(ref), ctx.now(), app_id))
            reservation = {'state': 'linked', 'kind': kind, 'invocation': resolved, 'runner_operation_id': runner_op, 'run_id': run['run_id']}
            conn.execute('UPDATE overseer_operations SET result=? WHERE operation_id=?', (canonical(reservation), key))
        conn.execute('UPDATE overseer_operations SET result=? WHERE operation_id=?', (canonical(reservation), operation_id))
    return reservation


def admitted_run(conn, run_id, kind):
    for row in conn.execute("SELECT result FROM overseer_operations WHERE operation_id GLOB 'run:*'"):
        rec = json.loads(row['result'])
        if rec.get('state') == 'linked' and rec.get('kind') == kind and rec.get('run_id') == run_id:
            return rec
    return None
