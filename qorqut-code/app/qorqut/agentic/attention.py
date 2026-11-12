"""Private, registration-scoped workflow synchronization receipts."""
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat

from qorqut_agentic.errors import AgenticError, InvalidRequestError
from qorqut_agentic.util import check_operation_id
from qorqut_operations.storage import safe_io


def binding(registration):
    path = Path(registration['workspace']) / '.qorqut/session-binding.json'
    try:
        value = json.loads(safe_io.read_bytes(path, limit=16384, private=True))
        if (not isinstance(value, dict) or value.get('registration_id') != registration['registration_id']
                or not isinstance(value.get('session_id'), str) or not value['session_id']
                or type(value.get('generation')) is not int or value['generation'] < 1):
            raise ValueError
        return {'consumer_id': value['session_id'], 'binding_generation': value['generation']}
    except (OSError, ValueError, TypeError, KeyError):
        raise AgenticError('A current native session binding is required', code='access_unavailable') from None


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                                    allow_nan=False).encode()).hexdigest()


@contextmanager
def receipt(registration, instance_id, actor_id, operation_id):
    """Hold one private receipt lock through dispatch and host synchronization."""
    key = fingerprint([instance_id, actor_id, operation_id])
    root = Path(registration['workspace']) / '.qorqut/workflow-attention'
    with safe_io.directory(root, create=True) as directory:
        if os.fstat(directory).st_mode & 0o077:
            raise AgenticError('Workflow receipt directory is not private', code='access_unavailable')
        descriptor = os.open(key + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise AgenticError('Workflow receipt lock is not private', code='access_unavailable')
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield Receipt(directory, key + '.json')
        finally:
            os.close(descriptor)


class Receipt:
    def __init__(self, directory, name):
        self.directory, self.name = directory, name

    def read(self):
        try:
            value = json.loads(safe_io._read(self.directory, self.name, limit=262144, private=True))
        except FileNotFoundError:
            return None
        except (ValueError, TypeError):
            raise AgenticError('Workflow synchronization receipt is invalid', code='access_unavailable') from None
        if not isinstance(value, dict) or value.get('phase') not in {'prepared', 'sync_pending', 'settled'}:
            raise AgenticError('Workflow synchronization receipt is invalid', code='access_unavailable')
        return value

    def write(self, value):
        data = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        if len(data) > 262144:
            raise InvalidRequestError('Workflow synchronization receipt exceeds its limit')
        temporary = '.workflow-' + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=self.directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.name, src_dir_fd=self.directory, dst_dir_fd=self.directory)
            os.fsync(self.directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.directory)
            except FileNotFoundError:
                pass

# Host-side operations: the engine remains the only authority for run controls.
from dataclasses import replace
import time

from qorqut_operations.application.plans import PlanBuilder, ref, schema
from qorqut_operations.application.registry import OperationDescriptor
from qorqut_operations.contracts import DomainError, canonical_bytes, plain, utc_now
from qorqut.management.identity import configured_ordubashy
from qorqut.transport.auth import AuthenticatedPrincipal, current_agent_credential

_SYNC_OPERATIONS = ('workflows.start', 'workflows.resume', 'workflows.pause', 'workflows.cancel', 'workflows.complete_step')
_TASK = schema({'type': {'const': 'task'}, 'id': {'type': 'string', 'minLength': 1, 'maxLength': 200},
                'business_id': {'type': ['string', 'null']}}, ('type', 'id', 'business_id'))
_BINDING = {'scope': {'const': 'global'}, 'operation': {'enum': list(_SYNC_OPERATIONS)},
            'operation_id': {'type': 'string', 'minLength': 1, 'maxLength': 200}, 'consumer_id': {'type': 'string', 'minLength': 1, 'maxLength': 200},
            'binding_generation': {'type': 'integer', 'minimum': 1}, 'run_id': {'type': 'string', 'minLength': 1, 'maxLength': 200}}
_CONTEXT_FIELDS = {'agent_id': {'type': 'string'}, 'registration_id': {'type': 'string'}, 'instance_id': {'type': 'string'},
                   'operation': _BINDING['operation'], 'operation_id': _BINDING['operation_id'],
                   'run_id': {'type': ['string', 'null']}, 'captured_at': {'type': 'string'},
                   'consumer_id': _BINDING['consumer_id'], 'binding_generation': _BINDING['binding_generation'],
                   'task': {'anyOf': [_TASK, {'type': 'null'}]}, 'membership_id': {'type': ['string', 'null']},
                   'membership_revision': {'type': ['integer', 'null'], 'minimum': 1},
                   'task_created_at': {'type': ['string', 'null']}, 'focus_generation': {'type': 'integer', 'minimum': 0},
                   'intent': {'type': 'string', 'maxLength': 1000}}
_CONTEXT_ENVELOPE = schema({'version': {'const': 1}, 'payload': schema(_CONTEXT_FIELDS, tuple(_CONTEXT_FIELDS)),
                            'signature': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}, ('version', 'payload', 'signature'))


def signing_key(directory):
    """Create or read the independent host key without following symlinks."""
    with safe_io.directory(directory, create=True) as parent:
        lock = os.open('attention-workflow-context.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                       0o600, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode) or os.fstat(lock).st_mode & 0o077:
                raise DomainError('authority_invalid', 'Workflow context key lock is not private')
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                fd = os.open('attention-workflow-context.key', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=parent)
            except FileExistsError:
                fd = None
            if fd is not None:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(secrets.token_bytes(48)); stream.flush(); os.fsync(stream.fileno())
                os.fsync(parent)
            value = safe_io._read(parent, 'attention-workflow-context.key', limit=4096, private=True)
            if len(value) < 32:
                raise DomainError('authority_invalid', 'Workflow context signing material is invalid')
            return value
        finally:
            os.close(lock)


def sign_context(key, payload):
    signature = hmac.new(key, b'qorqut-attention-workflow-context-v1\0' + canonical_bytes(payload), hashlib.sha256).hexdigest()
    return {'version': 1, 'payload': payload, 'signature': signature}


def verify_context(key, envelope, operation, operation_id, run_id):
    if (not isinstance(envelope, dict) or set(envelope) != {'version', 'payload', 'signature'}
            or type(envelope['version']) is not int or envelope['version'] != 1):
        raise DomainError('forbidden', 'Invalid workflow context envelope')
    payload = envelope['payload']
    if not isinstance(payload, dict) or set(payload) != set(_CONTEXT_FIELDS):
        raise DomainError('forbidden', 'Invalid workflow context payload')
    expected = sign_context(key, payload)['signature']
    if (not isinstance(envelope['signature'], str) or not hmac.compare_digest(envelope['signature'], expected)
            or payload['operation'] != operation or payload['operation_id'] != operation_id
            or (payload['run_id'] != run_id if operation != 'workflows.start' else payload['run_id'] is not None)):
        raise DomainError('forbidden', 'Workflow context does not match this operation')
    return payload


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def workflow_evidence(layer, operation, operation_id, run_id, actor):
    """Read bounded, exact-run facts from the pinned Agentic receipt and run tables."""
    event_types = {'workflows.start': {'started'}, 'workflows.resume': {'resume'}, 'workflows.pause': {'pause'},
                   'workflows.cancel': {'cancel'}, 'workflows.complete_step': {'step_completed', 'verification_failed'}}
    scope = 'start' if operation == 'workflows.start' else run_id
    try:
        with layer.workflows.ctx.store.reader() as connection:
            connection.execute('BEGIN')
            run = connection.execute('SELECT workflow_ref,actor,control,selection FROM runs WHERE run_id=? '
                                     'AND length(workflow_ref)<=512 AND length(actor)<=4096 AND length(selection)<=16384', (run_id,)).fetchone()
            receipt = connection.execute("SELECT json_extract(result,'$.run.run_id'), "
                "json_extract(result,'$.run.events[#-1].type'), json_extract(result,'$.run.events[#-1].actor'), "
                "json_extract(result,'$.run.events[#-1].payload.operation_id'), json_extract(result,'$.run.events[#-1].ordinal'), "
                "json_extract(result,'$.run.events[#-2].type'), json_extract(result,'$.run.events[#-2].actor'), "
                "json_extract(result,'$.run.events[#-2].payload.operation_id'), json_extract(result,'$.run.events[#-2].ordinal') FROM workflow_ops "
                "WHERE scope=? AND operation_id=? AND result IS NOT NULL AND length(result)<=8388608 AND json_valid(result)",
                (scope, operation_id)).fetchone()
            last = connection.execute('SELECT ordinal FROM run_events WHERE run_id=? ORDER BY ordinal DESC LIMIT 1',
                                      (run_id,)).fetchone()
        if run is None or receipt is None or last is None or receipt[0] != run_id:
            raise ValueError
        run_actor, selection = json.loads(run['actor']), json.loads(run['selection'])
        if (run_actor != actor or not isinstance(selection, dict)
                or (selection.get('project_id') is not None and not isinstance(selection['project_id'], str))):
            raise ValueError
        candidates = ((receipt[1], receipt[2], receipt[3], receipt[4]), (receipt[5], receipt[6], receipt[7], receipt[8]))
        matched = [event_ordinal for kind, event_actor, op_id, event_ordinal in candidates
                   if kind in event_types[operation] and _json(event_actor) == actor and op_id == operation_id]
        if len(matched) != 1 or type(matched[0]) is not int:
            raise ValueError
        if run['control'] not in {'running', 'paused', 'completed', 'cancelled'} or type(last[0]) is not int or last[0] < 1:
            raise ValueError
        return {'run_id': run_id, 'workflow_ref': run['workflow_ref'], 'state': run['control'],
                'ordinal': matched[0], 'current_ordinal': last[0], 'project_id': selection.get('project_id')}
    except (sqlite3.Error, KeyError, TypeError, ValueError, AgenticError):
        raise DomainError('workflow_unavailable', 'Verified workflow evidence is unavailable') from None


class WorkflowAttention:
    def __init__(self, runtime, *, key=None):
        self.runtime, self.app = runtime, runtime.application
        self.service = runtime.application.ordubashy_attention
        self.instance_id = runtime.agentic_layer.instance_id
        self.key = key if key is not None else signing_key(runtime.paths.runtime)
        if not isinstance(self.key, bytes) or len(self.key) < 32:
            raise DomainError('authority_invalid', 'Workflow context signing material is invalid')

    def register(self):
        common = dict(supports_signoff=False, exposure=frozenset({'mcp'}), scope_kinds=frozenset({'global'}),
                      global_capability='read_global_context', resolve_namespace=lambda c, a: 'global')
        self.app.registry.register(OperationDescriptor('attention_workflow_context',
            schema({**_BINDING, 'task': _TASK, 'intent': {'type': 'string', 'maxLength': 1000}},
                   ('scope', 'operation', 'operation_id', 'consumer_id', 'binding_generation')),
            effect_kind='read', read=self.context, **common))
        self.app.registry.register(OperationDescriptor('attention_workflow_sync',
            schema({'scope': _BINDING['scope'], 'operation': _BINDING['operation'], 'operation_id': {'type': 'string', 'minLength': 1, 'maxLength': 200},
                    'run_id': _BINDING['run_id'], 'context': _CONTEXT_ENVELOPE},
                   ('scope', 'operation', 'operation_id', 'run_id', 'context')),
            prepare=self.prepare, apply=self.apply, replay_validator=self.replay_validator, **common))
        return self

    def caller(self, ctx, consumer, generation, uow):
        ctx = self.app.current_context(replace(ctx, business_id=None, review_business_id=None), uow)
        if (ctx.principal_kind != 'agent' or ctx.source != 'mcp' or not ctx.session_registration_id
                or ctx.delegation_binding_id is not None or ctx.delegation_assignment_id is not None
                or configured_ordubashy(uow) != ctx.principal_id):
            raise DomainError('forbidden', 'A current full native Ordubashy registration is required')
        registration = uow.connection.execute('SELECT * FROM machine_registrations WHERE id=?', (ctx.session_registration_id,)).fetchone()
        profile, profile_row = self.runtime.resolver.profile(ctx.principal, connection=uow.connection)
        if (registration is None or registration['principal_id'] != ctx.principal_id or profile.agent_id != ctx.principal_id
                or Path(registration['workspace']) != Path(profile_row['workspace'])):
            raise DomainError('registration_conflict', 'Native registration is unavailable')
        current_agent_credential(uow.connection, AuthenticatedPrincipal(ctx.principal, registration['credential_id'],
                                                                         registration['id']), now=time.time())
        bound = replace(ctx, inbox_session_id=consumer, inbox_generation=generation)
        row = self.runtime.inbox.session_owner(bound, consumer, uow)
        owner = _json(row['binding_json'])
        if (not isinstance(owner, dict) or owner.get('registration_id') != ctx.session_registration_id
                or owner.get('agent_id') != ctx.principal_id or owner.get('session_id') != consumer
                or owner.get('workspace') != registration['workspace']):
            raise DomainError('registration_conflict', 'Native inbox binding belongs to another registration')
        return bound

    @staticmethod
    def association(uow, agent_id, run_id):
        row = uow.connection.execute('SELECT * FROM attention_workflows WHERE agent_id=? AND run_id=?', (agent_id, run_id)).fetchone()
        return dict(row) if row is not None else None

    def context(self, ctx, args, options, uow):
        args = plain(args)
        ctx = self.caller(ctx, args['consumer_id'], args['binding_generation'], uow)
        op, run_id = args['operation'], args.get('run_id')
        try:
            if self.runtime.agentic_layer.instance_id != self.instance_id:
                raise DomainError('workflow_unavailable', 'Workflow instance changed')
            check_operation_id(args['operation_id'])
            if op != 'workflows.start':
                check_operation_id(run_id, 'run_id')
            if op in ('workflows.start', 'workflows.resume'):
                scope = 'start' if op == 'workflows.start' else run_id
                with self.runtime.agentic_layer.workflows.ctx.store.reader() as engine:
                    if engine.execute('SELECT 1 FROM workflow_ops WHERE scope=? AND operation_id=? LIMIT 1',
                                      (scope, args['operation_id'])).fetchone():
                        raise DomainError('workflow_context_unavailable', 'This workflow operation already has a receipt; reuse its original context')
        except (sqlite3.Error, AgenticError):
            raise DomainError('workflow_unavailable', 'Workflow preflight evidence is unavailable') from None
        if op == 'workflows.start' and run_id is not None:
            raise DomainError('validation_error', 'A new workflow cannot supply a run id')
        association = self.association(uow, ctx.principal_id, run_id) if run_id else None
        if op not in ('workflows.start', 'workflows.resume', 'workflows.pause', 'workflows.cancel') and association is None:
            raise DomainError('not_found', 'Workflow attention association is unavailable')
        if op == 'workflows.resume' and association is None and 'task' not in args:
            raise DomainError('validation_error', 'Resuming an unassociated run requires an Attention Task')
        state = self.service.state(uow, ctx.principal_id)
        focused_task = _json(state.get('work_task_json'))
        task = args.get('task') or (_json(association['work_task_json']) if association else None) or focused_task
        intent = args.get('intent', state.get('intent', '') if task == focused_task else '')
        if association is None and op in ('workflows.pause', 'workflows.cancel'):
            task, intent = None, ''
        membership = None
        task_created_at = None
        if op not in ('workflows.pause', 'workflows.cancel'):
            if task is None:
                raise DomainError('not_found', 'Workflow Task membership is unavailable')
            selected = self.service.authorized_task(uow, ctx, ctx.principal_id, task)
            if selected['task'].get('task_type', 'task') != 'task':
                raise DomainError('validation_error', 'Workflow work requires an ordinary Task')
            membership, task_created_at = selected['membership'], selected['task']['created_at']
        if op in ('workflows.start', 'workflows.resume'):
            # The Task says what is being done (its next_step); an Intent sentence is optional.
            if not isinstance(intent, str) or len(intent.strip()) > 1000 or task is None:
                raise DomainError('validation_error', 'Select an Attention Task before starting or resuming')
        elif op in ('workflows.pause', 'workflows.cancel') and association is not None:
            # Control after revocation never discloses a Task projection.
            membership = self.service.task_membership(uow, ctx.principal_id, task) if task else None
            task_created_at = membership['task_created_at'] if membership else None
        payload = {'agent_id': ctx.principal_id, 'registration_id': ctx.session_registration_id,
                'instance_id': self.instance_id,
                'operation': op, 'operation_id': args['operation_id'], 'run_id': run_id, 'captured_at': utc_now(),
                'consumer_id': args['consumer_id'], 'binding_generation': args['binding_generation'], 'task': task,
                'membership_id': membership['id'] if membership else None,
                'membership_revision': membership['revision'] if membership else None,
                'task_created_at': task_created_at, 'focus_generation': state['focus_generation'], 'intent': intent.strip() if isinstance(intent, str) else ''}
        return sign_context(self.key, payload)

    def prepare(self, ctx, args, options, uow):
        args = plain(args)
        snapshot = verify_context(self.key, args['context'], args['operation'], args['operation_id'], args['run_id'])
        ctx = self.caller(ctx, snapshot['consumer_id'], snapshot['binding_generation'], uow)
        if (snapshot['agent_id'] != ctx.principal_id or snapshot['registration_id'] != ctx.session_registration_id
                or snapshot['instance_id'] != self.instance_id):
            raise DomainError('forbidden', 'Workflow context does not match the native registration')
        layer = self.runtime.agentic_layer
        if layer.instance_id != self.instance_id:
            raise DomainError('workflow_unavailable', 'Workflow instance changed')
        evidence = workflow_evidence(layer, args['operation'], args['operation_id'], args['run_id'],
                                     {'actor_id': ctx.principal_id, 'actor_kind': 'agent'})
        project_id = evidence['project_id']
        if (args['operation'] not in ('workflows.pause', 'workflows.cancel')
                and project_id is not None and project_id not in ctx.allowed_business_ids):
            raise DomainError('forbidden', 'Workflow routing is no longer authorized')
        existing = self.association(uow, ctx.principal_id, args['run_id'])
        if args['operation'] not in ('workflows.start', 'workflows.resume', 'workflows.pause', 'workflows.cancel') and existing is None:
            raise DomainError('not_found', 'Workflow attention association is unavailable')
        builder = PlanBuilder(ctx, 'attention_workflow_sync', args, 'Synchronize workflow attention', namespace='global',
                              affected_scope={'kind': 'global'})
        builder.record('attention_workflows', {'agent_id': ctx.principal_id, 'run_id': args['run_id']}, existing,
                       {'evidence': evidence}, action='sync_workflow_attention', business_id=None, namespace='global')
        attention_row = uow.repository('agent_attention').get(ctx.principal_id)
        builder.expect(ref('agent_attention', ctx.principal_id, None, 'global'), attention_row)
        if snapshot['membership_id']:
            member = uow.repository('attention_memberships').get(snapshot['membership_id'])
            builder.expect(ref('attention_memberships', snapshot['membership_id'], None, 'global'), member)
        return builder.build()

    def apply(self, ctx, plan, uow):
        args = plain(plan.arguments)
        snapshot = verify_context(self.key, args['context'], args['operation'], args['operation_id'], args['run_id'])
        ctx = self.caller(ctx, snapshot['consumer_id'], snapshot['binding_generation'], uow)
        if (snapshot['agent_id'] != ctx.principal_id or snapshot['registration_id'] != ctx.session_registration_id
                or snapshot['instance_id'] != self.instance_id):
            raise DomainError('forbidden', 'Workflow context does not match the native registration')
        changes = [change for change in plan.changes if change.action == 'sync_workflow_attention']
        if len(changes) != 1:
            raise DomainError('invalid_plan', 'Workflow evidence is unavailable')
        evidence = plain(changes[0].values)['evidence']; run_id = args['run_id']
        if (args['operation'] not in ('workflows.pause', 'workflows.cancel') and evidence.get('project_id') is not None
                and evidence['project_id'] not in ctx.allowed_business_ids):
            raise DomainError('forbidden', 'Workflow routing is no longer authorized')
        row = self.association(uow, ctx.principal_id, run_id)
        if row and evidence['ordinal'] <= row['last_event_ordinal']:
            return {'outcome': 'attention_skipped', 'reason': 'stale_event'}
        op, task = args['operation'], snapshot['task']
        if row is None and op in ('workflows.pause', 'workflows.cancel'):
            return {'outcome': 'attention_skipped', 'reason': 'association_absent'}
        member = uow.repository('attention_memberships').get(snapshot['membership_id']) if snapshot['membership_id'] else None
        valid = bool(member and member['id'] == snapshot['membership_id'] and member['agent_id'] == ctx.principal_id
                     and member['task_id'] == task['id'] and member['task_created_at'] == snapshot['task_created_at']) if task else False
        if valid:
            task = {'type': 'task', 'id': member['task_id'], 'business_id': member['task_business_id']}
            try:
                selected = self.service.authorized_task(uow, ctx, ctx.principal_id, task)
                valid = selected['task']['created_at'] == snapshot['task_created_at']
            except DomainError:
                valid = False
        if not valid and op in ('workflows.start', 'workflows.resume'):
            return {'outcome': 'attention_skipped', 'reason': 'membership_changed'}
        if row is None and not valid:
            return {'outcome': 'attention_skipped', 'reason': 'membership_changed'}
        now = utc_now()
        state = evidence['state']; terminal = state in ('completed', 'cancelled')
        paused = state == 'paused'
        focus_state = self.service.state(uow, ctx.principal_id)
        focus_current = focus_state['focus_generation'] == snapshot['focus_generation']
        attach = bool(valid and op in ('workflows.start', 'workflows.resume') and not terminal and not paused
                      and evidence.get('current_ordinal') == evidence['ordinal'] and focus_current)
        if row is None:
            uow.connection.execute('INSERT INTO attention_workflows(agent_id,run_id,revision,consumer_id,work_task_json,workflow_ref,workflow_name,last_event_ordinal,paused,detached_at,updated_at) VALUES(?,?,1,?,?,?,?,?,?,?,?)',
                (ctx.principal_id, run_id, snapshot['consumer_id'], json.dumps(task), evidence['workflow_ref'], evidence['workflow_ref'], evidence['ordinal'], int(paused), None if attach else now, now))
        else:
            uow.connection.execute('UPDATE attention_workflows SET consumer_id=?,workflow_ref=?,last_event_ordinal=?,paused=?,updated_at=?,revision=revision+1 WHERE agent_id=? AND run_id=?',
                (snapshot['consumer_id'], evidence['workflow_ref'], evidence['ordinal'], int(paused), now, ctx.principal_id, run_id))
        if terminal or paused:
            if row and row['blocked_since']:
                uow.connection.execute("UPDATE attention_reminders SET state=CASE WHEN emission_started_at IS NOT NULL THEN 'uncertain' ELSE 'cancelled' END,updated_at=?,revision=revision+1 WHERE agent_id=? AND kind='workflow_detach' AND workflow_run_id=? AND blocked_since=? AND state IN ('pending','claimed')",
                                       (now, ctx.principal_id, run_id, row['blocked_since']))
            uow.connection.execute('UPDATE attention_workflows SET detached_at=?,blocked_since=NULL,blocker_key=NULL,blocker_reason=\'\',resume_context=\'\' WHERE agent_id=? AND run_id=?', (now, ctx.principal_id, run_id))
            state_row = self.service.state(uow, ctx.principal_id)
            focus = _json(state_row.get('focus_json'))
            if isinstance(focus, dict) and focus.get('type') == 'workflow' and focus.get('id') == run_id:
                self.service.clear_focus(uow, ctx.principal_id, 'workflow_paused' if paused else 'workflow_finished')
        elif attach:
            if op == 'workflows.resume':
                # The actual resolver refuses detached associations and requires the selected Task.
                # Both changes are in the same writer transaction as the generation-checked focus.
                uow.connection.execute('UPDATE attention_workflows SET work_task_json=?,detached_at=NULL WHERE agent_id=? AND run_id=?',
                                       (json.dumps(task), ctx.principal_id, run_id))
            focused = self.service.set_focus(uow, ctx, ctx.principal_id, focus={'type': 'workflow', 'id': run_id, 'business_id': None},
                                             task=task, intent=snapshot['intent'], expected_focus_generation=snapshot['focus_generation'])
            if not focused:
                raise DomainError('revision_conflict', 'Focus changed during workflow synchronization')
        elif op in ('workflows.start', 'workflows.resume') and not terminal and not paused:
            return {'outcome': 'attention_skipped', 'reason': 'focus_changed'}
        return {'outcome': 'attention_synchronized', 'run_id': run_id}

    def replay_validator(self, ctx, args, replay, uow):
        args = plain(args)
        context = verify_context(self.key, args['context'], args['operation'], args['operation_id'], args['run_id'])
        ctx = self.caller(ctx, context['consumer_id'], context['binding_generation'], uow)
        if (context['agent_id'] != ctx.principal_id or context['registration_id'] != ctx.session_registration_id
                or context['instance_id'] != self.instance_id):
            raise DomainError('forbidden', 'Workflow context does not match the native registration')
        if args['operation'] not in ('workflows.pause', 'workflows.cancel'):
            member = uow.repository('attention_memberships').get(context['membership_id']) if context['membership_id'] else None
            if (not member or member['agent_id'] != ctx.principal_id
                    or member['task_id'] != context['task']['id']
                    or member['task_created_at'] != context['task_created_at']):
                raise DomainError('not_found', 'Workflow Task membership is unavailable')
            task = {'type': 'task', 'id': member['task_id'], 'business_id': member['task_business_id']}
            self.service.authorized_task(uow, ctx, ctx.principal_id, task)


def register_workflow_attention(runtime):
    existing = getattr(runtime, 'workflow_attention', None)
    if existing is not None:
        if isinstance(existing, WorkflowAttention) and existing.runtime is runtime:
            return existing
        raise DomainError('invalid_configuration', 'Conflicting workflow attention attachment')
    runtime.workflow_attention = WorkflowAttention(runtime).register()
    return runtime.workflow_attention
