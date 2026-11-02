"""Persistent attention board for the configured Ordubashy."""
from dataclasses import replace
import json

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PlanBuilder, page, schema, timestamp, utc_clock
from qorqut_operations.contracts import DomainError, InvocationOptions, Principal, new_id, plain
from qorqut_operations.discussion import emit_event
from bazarlyk.management.identity import configured_ordubashy, is_active_installation_owner

BOARD_TRANSITION_HINT = ('Make sure everything on your Attention board reflects reality: what you’re doing, '
                         'each task’s state, and who must act next.')

ID = {'type': 'string', 'minLength': 1, 'maxLength': 200}
REF = schema({'type': ID, 'id': ID, 'business_id': {'type': ['string', 'null']}}, ('type', 'id', 'business_id'))
TASK = schema({'type': {'const': 'task'}, 'id': ID, 'business_id': {'type': ['string', 'null']}}, ('type', 'id', 'business_id'))
REV = {'type': 'integer', 'minimum': 0}
CLOCK_FIELDS = frozenset({'board_mode', 'board_due_at', 'focus_due_at', 'last_agent_activity_at', 'last_owner_message_at', 'reminder_sequence'})
UNREADABLE = frozenset({'forbidden', 'policy_denied', 'invalid_policy', 'not_found', 'operation_unavailable'})


def task_recipients(uow, business_id, task, actor_id):
    """Current board membership is a routing reason, never a source grant."""
    agent = configured_ordubashy(uow, require_codex=False)
    if not agent or agent == actor_id:
        return set()
    found = uow.connection.execute(
        'SELECT 1 FROM attention_memberships WHERE agent_id=? AND task_business_id IS ? AND task_id=? AND task_created_at=?',
        (agent, business_id, task['id'], task['created_at'])).fetchone()
    return {agent} if found else set()


class Attention:
    def __init__(self, runtime):
        self.runtime, self.app = runtime, runtime.application
        self.inbox = runtime.focus.inbox
        self.clock = getattr(self.inbox, 'clock', utc_clock)

    def register(self):
        common = dict(supports_signoff=False, scope_kinds=frozenset({'global'}),
                      global_capability='read_global_context', resolve_namespace=lambda c, a: 'global')
        shared = {'scope': {'const': 'global'}, 'agent_id': ID}
        self.app.registry.register(OperationDescriptor('attention_get', schema({**shared,
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}, 'cursor': {'type': 'string', 'maxLength': 2000}}),
            **common, effect_kind='read', read=lambda c,a,o,u: self.read(c,a,u)))
        ops = {
            'attention_add': ({'task': TASK, 'expected_revision': REV}, ('task',)),
            'attention_remove': ({'membership_id': ID, 'expected_membership_revision': REV,
                'owner_requested': {'type': 'boolean'}, 'rationale': {'type': 'string', 'maxLength': 4000}}, ('membership_id', 'expected_membership_revision')),
            'attention_focus_set': ({'focus': REF, 'task': TASK, 'intent': {'type': 'string', 'maxLength': 1000},
                'expected_revision': REV}, ('focus', 'task', 'intent', 'expected_revision')),
            'attention_focus_clear': ({'expected_revision': REV}, ('expected_revision',)),
            'attention_workflow_update': ({'run_id': ID, 'expected_revision': REV,
                'state': {'enum': ['blocked', 'ready', 'detached']}, 'blocker_key': ID,
                'reason': {'type': 'string', 'maxLength': 2000}, 'resume_context': {'type': 'string', 'maxLength': 4000},
                'related_refs': {'type': 'array', 'maxItems': 100, 'items': REF}}, ('run_id', 'expected_revision', 'state')),
        }
        for name, (fields, required) in ops.items():
            self.app.registry.register(OperationDescriptor(name, schema({**shared, **fields}, required), **common,
                prepare=lambda c,a,o,u,op=name:self.prepare(op,c,a,u), apply=self.apply))
        self.app.ordubashy_attention = self
        return self

    def context(self, ctx, args, uow):
        ctx = self.app.current_context(replace(ctx, business_id=None, review_business_id=None), uow)
        agent = configured_ordubashy(uow, require_codex=False)
        if not agent or (args.get('agent_id') and args['agent_id'] != agent):
            raise DomainError('not_found', 'Configured Ordubashy is unavailable')
        if ctx.principal_kind == 'agent' and ctx.principal_id == agent:
            if 'agent_id' in args: raise DomainError('forbidden', 'Only the owner can select the coordinator')
            if ctx.delegation_assignment_id is not None: raise DomainError('forbidden', 'Delegated identities cannot change Attention')
            owner = self.inbox.session_owner(ctx, ctx.inbox_session_id, uow)
            if not ctx.session_registration_id or json.loads(owner['binding_json']).get('registration_id') != ctx.session_registration_id:
                raise DomainError('inbox_generation_stale', 'Native registration does not match the current binding')
        elif not is_active_installation_owner(uow, ctx.principal_id, ctx.principal_kind):
            raise DomainError('forbidden', 'Attention requires the installation owner or Ordubashy')
        return ctx, agent

    def state(self, uow, agent_id):
        row = uow.connection.execute('SELECT * FROM agent_attention WHERE agent_id=?', (agent_id,)).fetchone()
        data = dict(row) if row else {'agent_id': agent_id, 'revision': 0, 'focus_json': None, 'work_task_json': None,
            'intent': '', 'focus_generation': 0, 'focus_started_at': None, 'last_agent_activity_at': None,
            'last_owner_message_at': None, 'board_mode': 'empty', 'board_due_at': None, 'focus_due_at': None,
            'reminder_sequence': 0, 'updated_at': None}
        for key in ('focus_json', 'work_task_json'):
            if isinstance(data[key], str): data[key] = json.loads(data[key])
        return data

    def ensure(self, uow, agent):
        uow.connection.execute('INSERT OR IGNORE INTO agent_attention(agent_id,updated_at) VALUES(?,?)', (agent, timestamp(self.clock)))

    def update_clock(self, uow, agent_id, changes):
        if set(changes) - CLOCK_FIELDS: raise ValueError('Unknown attention clock field')
        if not changes: return
        self.ensure(uow, agent_id)
        uow.connection.execute('UPDATE agent_attention SET '+','.join(k+'=?' for k in changes)+' WHERE agent_id=?', [*changes.values(), agent_id])

    def membership_event(self, uow, agent, membership_id, ref, created_at, actor, kind, now, *, owner_requested=False, rationale=''):
        uow.connection.execute('INSERT INTO attention_membership_events(id,agent_id,membership_id,task_business_id,task_id,task_created_at,actor_id,event_type,owner_requested,rationale,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (new_id(), agent, membership_id, ref['business_id'], ref['id'], created_at, actor, kind, int(owner_requested), rationale, now))

    def task_membership(self, uow, agent_id, task_ref):
        row = uow.connection.execute('SELECT * FROM attention_memberships WHERE agent_id=? AND task_business_id IS ? AND task_id=?',
            (agent_id, task_ref['business_id'], task_ref['id'])).fetchone()
        return dict(row) if row else None

    def authorized_task(self, uow, ctx, agent_id, task_ref):
        ctx, current = self.context(ctx, {}, uow)
        if current != agent_id: raise DomainError('forbidden', 'Attention coordinator changed')
        task = self.task(ctx, task_ref, uow)
        membership = self.task_membership(uow, agent_id, task_ref)
        if not membership or membership['task_created_at'] != task['created_at']:
            raise DomainError('not_found', 'Task is not on Attention board')
        if task.get('task_type') != 'task' or task.get('archived'):
            raise DomainError('validation_error', 'Board work requires an unarchived ordinary Task')
        return {'task': task, 'membership': membership}

    def set_focus(self, uow, ctx, agent_id, *, focus, task, intent, expected_focus_generation):
        state = self.state(uow, agent_id)
        if state['focus_generation'] != expected_focus_generation: return False
        args = {'focus':focus, 'task':task, 'intent':intent, 'expected_revision':state['revision']}
        ctx, current, state = self.validate('attention_focus_set', ctx, args, uow)
        if current != agent_id: raise DomainError('forbidden', 'Attention coordinator changed')
        self._set_focus(uow, agent_id, state, args, timestamp(self.clock))
        return True

    def _set_focus(self, uow, agent, state, args, now):
        changed = state['focus_json'] != args['focus'] or state['work_task_json'] != args['task']
        intent = args['intent'].strip()
        if changed or state['intent'] != intent:
            self.ensure(uow,agent)
            if changed: self.cancel_reminders(uow,agent,now)
            uow.connection.execute('UPDATE agent_attention SET focus_json=?,work_task_json=?,intent=?,focus_generation=focus_generation+?,focus_started_at=?,focus_due_at=?,board_due_at=NULL,updated_at=?,revision=revision+1 WHERE agent_id=?',
                (json.dumps(args['focus']),json.dumps(args['task']),intent,int(changed),now if changed else state['focus_started_at'],None if changed else state['focus_due_at'],now,agent))

    def clear_focus(self, uow, agent_id, reason):
        state = self.state(uow, agent_id)
        if not state['focus_json']: return
        now = timestamp(self.clock)
        uow.connection.execute("UPDATE agent_attention SET focus_json=NULL,work_task_json=NULL,intent='',focus_generation=focus_generation+1,focus_started_at=NULL,focus_due_at=NULL,board_due_at=NULL,board_mode='empty',updated_at=?,revision=revision+1 WHERE agent_id=?", (now, agent_id))
        self.cancel_reminders(uow, agent_id, now, focus_only=True)

    def cancel_reminders(self, uow, agent, now, *, focus_only=False):
        uow.connection.execute("UPDATE attention_reminders SET state=CASE WHEN emission_started_at IS NOT NULL THEN 'uncertain' ELSE 'cancelled' END,updated_at=?,revision=revision+1 WHERE agent_id=? AND state IN ('pending','claimed')" + (" AND kind IN ('focus','workflow_detach')" if focus_only else ''), (now, agent))

    def task(self, ctx, ref, uow, *, lifecycle_only=False):
        agent = configured_ordubashy(uow, require_codex=False)
        if ctx.principal_kind == 'human':
            peer = self.app.policy.context_for(agent, business_id=None, review_business_id=None, uow=uow)
            # The coordinator's reach must be checked, but its thread/history is
            # not part of the owner's response. Authorize the same Task only.
            self.task(peer, ref, uow, lifecycle_only=True)
        selected = self.app.current_context(replace(ctx, business_id=ref['business_id'], review_business_id=ref['business_id']), uow)
        descriptor = self.app.registry.get('tasks_get')
        if ref['business_id'] is not None: descriptor = replace(descriptor, global_capability=None)
        self.app.authorize(selected, descriptor, (), uow, namespace='business:'+ref['business_id'] if ref['business_id'] else 'global')
        args = {'task_id': ref['id'], **({'scope': 'global'} if ref['business_id'] is None else {})}
        if lifecycle_only:
            from qorqut_operations.tasks.scope import task_context
            selected = task_context(selected, args, uow)
            task = uow.repository('tasks', business_id=selected.business_id).get(ref['id'])
            if task is None:
                raise DomainError('not_found', 'Task is unavailable')
            return task
        return descriptor.read(selected, args, InvocationOptions(), uow)['task']

    def resolve(self, ctx, agent, ref, uow):
        if ref['type'] == 'workflow':
            row = uow.connection.execute('SELECT * FROM attention_workflows WHERE agent_id=? AND run_id=?', (agent, ref['id'])).fetchone()
            if ref['business_id'] is not None or not row or row['detached_at'] or row['paused']:
                raise DomainError('not_found', 'Workflow association is unavailable')
            self.task(ctx, json.loads(row['work_task_json']), uow, lifecycle_only=True)
            return {**ref, 'label': row['workflow_name']}
        if ctx.principal_kind == 'human':
            peer = self.app.policy.context_for(agent, business_id=None, review_business_id=None, uow=uow)
            self.runtime.focus.resolve(peer, ref, uow)
        return self.runtime.focus.resolve(ctx, ref, uow)

    def board_mode(self, uow, agent_id):
        # Timer checks run inside write transactions. Do not hydrate task threads,
        # relationships and worker projections just to inspect lifecycle state.
        from qorqut_operations.tasks.scope import task_context
        try: ctx = self.app.policy.context_for(agent_id, business_id=None, review_business_id=None, uow=uow)
        except DomainError: return 'empty'
        scopes = {}
        states = set()
        for row in uow.connection.execute('SELECT * FROM attention_memberships WHERE agent_id=?', (agent_id,)):
            business_id = row['task_business_id']
            try:
                if business_id not in scopes:
                    scopes[business_id] = None
                    selected = self.app.current_context(replace(ctx, business_id=business_id, review_business_id=business_id), uow)
                    descriptor = self.app.registry.get('tasks_get')
                    if business_id is not None: descriptor = replace(descriptor, global_capability=None)
                    self.app.authorize(selected, descriptor, (), uow, namespace='business:'+business_id if business_id else 'global')
                    selected = task_context(selected, {'scope': 'global'} if business_id is None else {}, uow)
                    scopes[business_id] = uow.repository('tasks', business_id=selected.business_id)
                repository = scopes[business_id]
                if repository is None:
                    continue
                task = repository.get(row['task_id'])
                if task and not task.get('archived') and task.get('created_at') == row['task_created_at']:
                    if task['status'] in {'open', 'working'}:
                        return 'normal'
                    states.add(task['status'])
            except DomainError as error:
                if error.code not in UNREADABLE: raise
        return 'reassess' if states & {'blocked','waiting'} else 'empty'

    def reminder_eligible(self, uow, agent_id, *, consumer_id, binding_generation, focus_generation, kind):
        """Recheck current identity, binding and source authority before delivery."""
        try:
            if configured_ordubashy(uow, require_codex=False) != agent_id: return False
            row = uow.connection.execute('SELECT * FROM native_session_owners WHERE agent_id=?', (agent_id,)).fetchone()
            if not row or row['consumer_id'] != consumer_id or row['generation'] != binding_generation or row['desired_state'] in ('paused','stopped'): return False
            binding = json.loads(row['binding_json'])
            registration_id = binding.get('registration_id')
            registration = uow.repository('machine_registrations').get(registration_id) if registration_id else None
            if (not registration or registration['principal_id'] != agent_id or binding.get('agent_id') != agent_id
                    or binding.get('session_id') != consumer_id or binding.get('workspace') != registration['workspace']): return False
            ctx = self.inbox.context(self.inbox.resolver.runtime_context(Principal(agent_id, 'agent')), uow)
            ctx = replace(ctx, inbox_session_id=consumer_id, inbox_generation=binding_generation, session_registration_id=registration_id)
            self.inbox.session_owner(ctx, consumer_id, uow)
            for operation in ('attention_get', 'inbox_list'):
                if self.app.policy.resolve(ctx, self.app.registry.get(operation), (), uow).mode != 'allow': return False
            state = self.state(uow, agent_id)
            if kind in ('board_normal', 'board_reassess'):
                return not state['focus_json'] and self.board_mode(uow, agent_id) == kind.removeprefix('board_')
            if kind not in ('focus','workflow_detach') or not state['focus_json'] or state['focus_generation'] != focus_generation: return False
            self.authorized_task(uow, ctx, agent_id, state['work_task_json'])
            focus = state['focus_json']
            self.resolve(ctx, agent_id, focus, uow)
            if focus['type'] == 'workflow':
                workflow = uow.connection.execute('SELECT consumer_id,paused,detached_at,blocked_since,blocker_key FROM attention_workflows WHERE agent_id=? AND run_id=?', (agent_id,focus['id'])).fetchone()
                if not workflow or workflow['consumer_id'] != consumer_id or workflow['paused'] or workflow['detached_at']: return False
                if kind == 'workflow_detach': return bool(workflow['blocked_since'] and workflow['blocker_key'])
            return kind == 'focus'
        except DomainError:
            return False

    def related_event(self, uow, agent_id, event):
        event = dict(event)
        state = self.state(uow, agent_id)
        if not state['focus_json']: return True
        installation = uow.repository('installation').get('service')
        owner = (installation or {}).get('owner_principal_id')
        if event.get('actor_id') == owner and event.get('source_type') == 'owner_conversations': return True
        if event.get('source_type') == 'agent_messages':
            message = uow.connection.execute('SELECT current_sender_id,current_recipient_id FROM agent_messages WHERE id=?', (event.get('source_id'),)).fetchone()
            if message and message['current_sender_id'] == owner and message['current_recipient_id'] == agent_id: return True
        refs = [state['focus_json'], state['work_task_json']]
        focus = state['focus_json']
        if focus['type'] == 'workflow':
            row = uow.connection.execute('SELECT related_refs_json FROM attention_workflows WHERE agent_id=? AND run_id=?', (agent_id,focus['id'])).fetchone()
            if row: refs.extend(json.loads(row[0]))
        if event.get('source_type') == 'operational_assignments':
            row = uow.connection.execute('SELECT business_id,scope_json,links_json FROM operational_assignments WHERE id=? AND parent_principal_id=?', (event.get('source_id'),agent_id)).fetchone()
            if row:
                links = json.loads(row['links_json'])
                scope = json.loads(row['scope_json'])
                def matches(ref):
                    return (ref and ref['type'] == 'task' and ref['id'] in links.get('tasks', []) and
                        (ref['business_id'] == row['business_id'] or
                         (row['business_id'] is None and ref['business_id'] in scope.get('allowed_business_ids', []))))
                if any(matches(ref) for ref in refs): return True
        if event.get('source_type') == 'agent_messages':
            message = uow.connection.execute('SELECT delegation_assignment_id FROM agent_messages WHERE id=?', (event.get('source_id'),)).fetchone()
            if message and message['delegation_assignment_id']:
                return self.related_event(uow, agent_id, {'source_type':'operational_assignments','source_id':message['delegation_assignment_id']})
        source_map = {'tasks':'task', 'actions':'action', 'signoffs':'signoff', 'documents':'material', 'data_tables':'table'}
        kind = source_map.get(event.get('source_type'), event.get('source_type'))
        work = state['work_task_json']
        if kind == 'task' and work and work['business_id'] == event.get('business_id'):
            if uow.connection.execute("SELECT 1 FROM relationships WHERE business_id IS ? AND from_type='task' AND from_id=? AND role='blocked_by' AND to_type='task' AND to_id=?", (work['business_id'],work['id'],event.get('source_id'))).fetchone(): return True
        return any(ref and ref['type'] == kind and ref['id'] == event.get('source_id') and ref['business_id'] == event.get('business_id') for ref in refs)

    def workflow_summary(self, uow, agent, run_id):
        """Narrow read-only projection of the exact focused run; never raw arguments, outputs or events."""
        row = uow.connection.execute('SELECT workflow_name,blocked_since,blocker_reason,paused,detached_at FROM attention_workflows WHERE agent_id=? AND run_id=?', (agent, run_id)).fetchone()
        unavailable = {'unavailable': True, 'run_id': run_id}
        layer = getattr(self.runtime, 'agentic_layer', None)
        if row is None or layer is None: return unavailable
        try:
            run = layer.workflows.get(run_id)['run']
            if run.get('run_id') != run_id or run.get('actor') != {'actor_id': agent, 'actor_kind': 'agent'}: return unavailable
            steps = [{'id': str(step['id'])[:200], 'label': str(step['id'])[:200], 'status': str(step.get('state'))[:40]} for step in run['steps']]
            current = next((step for step in steps if step['status'] == 'active'), None) or next((step for step in steps if step['status'] != 'completed'), None)
            if current and run['state'] in ('running', 'paused', 'waiting_arguments'): current['current'] = True
            blocked = bool(row['blocked_since']) and run['state'] not in ('completed', 'cancelled')
            return {'run_id': run_id, 'name': row['workflow_name'], 'state': str(run['state'])[:40], 'paused': bool(row['paused']),
                'detached': bool(row['detached_at']), 'steps': steps, 'blocked': blocked,
                'blocker_reason': (row['blocker_reason'] or '')[:1000] if blocked else ''}
        except Exception: return unavailable

    def read(self, ctx, args, uow):
        ctx, agent = self.context(ctx, args, uow)
        state = self.state(uow, agent)
        items = []
        for row in uow.connection.execute('SELECT * FROM attention_memberships WHERE agent_id=?', (agent,)):
            ref = {'type':'task','id':row['task_id'],'business_id':row['task_business_id']}
            try:
                # Ordering and membership checks need lifecycle fields, not
                # every Task's notes, related records and worker projections.
                task = self.task(ctx, ref, uow, lifecycle_only=True)
                if task.get('created_at') != row['task_created_at']: continue
            except DomainError as error:
                if error.code not in UNREADABLE: raise
                continue
            items.append({'membership_id':row['id'], 'membership_revision':row['revision'], 'task_ref':ref, 'task':task, 'added_at':row['created_at']})
        def ordering(item):
            task = item['task']
            status = task.get('status')
            rank = {'blocked':0,'working':1,'open':2,'waiting':3,'done':4,'cancelled':5}.get(status, 6)
            completed = task.get('latest_completed_at') or '' if status == 'done' else ''
            # Fixed-width UTC timestamps sort in reverse through their inverted character values.
            inverted = ''.join(chr(127-ord(char)) for char in completed) if completed else '\x7f'
            return (rank, inverted, item['added_at'], item['membership_id'])
        selected, next_cursor = page(items, args, None, 'attention_get:'+agent, key=ordering, reverse=False)
        # Preserve the full Task response contract, but hydrate only this page.
        for item in selected:
            item['task'] = self.task(ctx, item['task_ref'], uow)
        focus = state['focus_json']
        intent, task_ref = state['intent'], state['work_task_json']
        if focus:
            try:
                self.task(ctx, task_ref, uow, lifecycle_only=True)
                focus = self.resolve(ctx, agent, focus, uow)
            except DomainError as error:
                if error.code not in UNREADABLE: raise
                focus, task_ref, intent = {'unavailable': True}, None, ''
        installation = uow.repository('installation').get('service')
        workflow = None
        if state['focus_json'] and state['focus_json'].get('type') == 'workflow' and not focus.get('unavailable'):
            workflow = self.workflow_summary(uow, agent, state['focus_json']['id'])
        return {'agent_id':agent, 'owner_id':installation['owner_principal_id'], 'revision':state['revision'],
            'attention':{'state':'focused' if state['focus_json'] else 'unfocused','focus':focus,'task':task_ref,'intent':intent,
                'focus_generation':state['focus_generation'],'focus_started_at':state['focus_started_at'],'updated_at':state['updated_at'],
                **({'workflow': workflow} if workflow else {})},
            'items':selected, 'next_cursor':next_cursor}

    def prepare(self, op, ctx, args, uow):
        self.validate(op, ctx, args, uow)
        return PlanBuilder(ctx, op, args, 'Update Attention board', namespace='global', affected_scope={'kind':'global'}).build()

    def validate(self, op, ctx, args, uow):
        ctx, agent = self.context(ctx, args, uow)
        state = self.state(uow, agent)
        if 'expected_revision' in args and args['expected_revision'] != state['revision']:
            raise DomainError('revision_conflict', 'Attention changed; reread it', current_revision=state['revision'])
        if op in ('attention_add','attention_focus_set'):
            task = self.task(ctx, args['task'], uow)
            if task.get('task_type') != 'task' or task.get('archived'):
                raise DomainError('validation_error', 'Board work requires an unarchived ordinary Task')
            if op == 'attention_focus_set':
                membership = self.task_membership(uow, agent, args['task'])
                if not membership or membership['task_created_at'] != task['created_at']: raise DomainError('not_found', 'Task is not on Attention board')
                if not isinstance(args['intent'], str) or not args['intent'].strip() or len(args['intent']) > 1000: raise DomainError('validation_error', 'Intent must contain at most 1000 characters')
                self.resolve(ctx, agent, args['focus'], uow)
                if args['focus']['type'] == 'workflow':
                    association = uow.connection.execute('SELECT work_task_json FROM attention_workflows WHERE agent_id=? AND run_id=?', (agent,args['focus']['id'])).fetchone()
                    if json.loads(association[0]) != args['task']: raise DomainError('validation_error', 'Workflow belongs to another work Task')
        if op == 'attention_remove':
            row = uow.connection.execute('SELECT * FROM attention_memberships WHERE id=? AND agent_id=?', (args['membership_id'],agent)).fetchone()
            if not row: raise DomainError('not_found', 'Membership is unavailable')
            if row['revision'] != args['expected_membership_revision']: raise DomainError('revision_conflict', 'Membership changed; reread it', current_revision=row['revision'])
            ref = {'type':'task','id':row['task_id'],'business_id':row['task_business_id']}
            task = self.task(ctx, ref, uow, lifecycle_only=True)
            if task['created_at'] != row['task_created_at']: raise DomainError('not_found', 'Task is unavailable')
            if ctx.principal_kind == 'agent' and task['status'] == 'blocked': raise DomainError('forbidden', 'Blocked work cannot be discarded')
            if ctx.principal_kind == 'agent' and task['status'] == 'done' and (args.get('owner_requested') is not True or not args.get('rationale','').strip()):
                raise DomainError('forbidden', 'Done removal requires an owner instruction and rationale')
        if op == 'attention_workflow_update':
            row = uow.connection.execute('SELECT * FROM attention_workflows WHERE agent_id=? AND run_id=?', (agent,args['run_id'])).fetchone()
            if not row: raise DomainError('not_found', 'Workflow association is unavailable')
            self.task(ctx, json.loads(row['work_task_json']), uow)
            if args['state'] == 'blocked' and (not args.get('blocker_key','').strip() or not args.get('reason','').strip()): raise DomainError('validation_error', 'Blocked requires a blocker key and reason')
            for ref in args.get('related_refs', []): self.resolve(ctx, agent, ref, uow)
        return ctx, agent, state

    def task_update_hint(self, ctx, args, operation):
        """Advisory only: never mutate the board or require Attention session ownership."""
        if ctx.principal_kind != 'agent':
            return None
        status = (args.get('status', 'done') if operation == 'tasks_complete'
                  else args.get('changes', {}).get('status'))
        if status == 'waiting':
            return 'Waiting is for an external party. Otherwise, check state and Workers.'
        if status not in ('working', 'done'):
            return None
        with self.runtime.database.read() as uow:
            agent = configured_ordubashy(uow, require_codex=False)
            if ctx.principal_id != agent:
                return None
            if status == 'working':
                return BOARD_TRANSITION_HINT
            state = self.state(uow, agent)
            ref = {'type': 'task', 'id': args.get('task_id'), 'business_id': ctx.business_id}
            if state['focus_json'] and ref in (state['focus_json'], state['work_task_json']):
                return 'Clear or change your focus.'
        return None

    def add_result(self, ctx, task, uow):
        result = self.read(ctx, {}, uow)
        if ctx.principal_kind == 'agent':
            result['agent_hint'] = BOARD_TRANSITION_HINT
            if not task.get('worker_ids'):
                result['agent_hint'] += ' Set who must act next.'
        return result

    def apply(self, ctx, plan, uow):
        args, op = plain(plan.arguments), plan.operation_name
        ctx, agent, state = self.validate(op, ctx, args, uow)
        now = timestamp(self.clock)
        if op == 'attention_add':
            task = self.task(ctx,args['task'],uow)
            if self.task_membership(uow,agent,args['task']): return self.add_result(ctx,task,uow)
            self.ensure(uow,agent)
            membership_id = new_id()
            uow.connection.execute('INSERT INTO attention_memberships(id,agent_id,task_business_id,task_id,task_created_at,created_at) VALUES(?,?,?,?,?,?)', (membership_id,agent,args['task']['business_id'],args['task']['id'],task['created_at'],now))
            self.membership_event(uow,agent,membership_id,args['task'],task['created_at'],ctx.principal_id,'added',now)
            uow.connection.execute('UPDATE agent_attention SET revision=revision+1,updated_at=? WHERE agent_id=?',(now,agent))
            emit_event(uow,business_id=args['task']['business_id'],source_type='tasks',source_id=args['task']['id'],source_revision=task['revision'],actor_id=ctx.principal_id,event_type='attention_added',summary='Added to Attention board',required={agent})
        elif op == 'attention_remove':
            row = uow.connection.execute('SELECT * FROM attention_memberships WHERE id=?',(args['membership_id'],)).fetchone()
            ref = {'type':'task','id':row['task_id'],'business_id':row['task_business_id']}
            task = self.task(ctx,ref,uow,lifecycle_only=True)
            self.membership_event(uow,agent,row['id'],ref,row['task_created_at'],ctx.principal_id,'removed',now,owner_requested=args.get('owner_requested',False),rationale=args.get('rationale',''))
            uow.connection.execute('DELETE FROM attention_memberships WHERE id=?',(row['id'],))
            uow.connection.execute("UPDATE attention_workflows SET detached_at=?,updated_at=?,revision=revision+1 WHERE agent_id=? AND detached_at IS NULL AND json_extract(work_task_json,'$.id')=? AND json_extract(work_task_json,'$.business_id') IS ?", (now,now,agent,ref['id'],ref['business_id']))
            if state['work_task_json'] == ref: self.clear_focus(uow,agent,'membership_removed')
            uow.connection.execute('UPDATE agent_attention SET revision=revision+1,updated_at=? WHERE agent_id=?',(now,agent))
            emit_event(uow,business_id=ref['business_id'],source_type='tasks',source_id=ref['id'],source_revision=task['revision'],actor_id=ctx.principal_id,event_type='attention_removed',summary='Removed from Attention board',required={agent})
        elif op == 'attention_focus_clear': self.clear_focus(uow,agent,'explicit')
        elif op == 'attention_focus_set': self._set_focus(uow,agent,state,args,now)
        elif op == 'attention_workflow_update':
            row = uow.connection.execute('SELECT * FROM attention_workflows WHERE agent_id=? AND run_id=?',(agent,args['run_id'])).fetchone()
            mode = args['state']; key = args.get('blocker_key','').strip()
            since = (row['blocked_since'] if row['blocker_key'] == key and row['blocked_since'] else now) if mode == 'blocked' else None
            uow.connection.execute('UPDATE attention_workflows SET blocked_since=?,blocker_key=?,blocker_reason=?,resume_context=?,related_refs_json=?,detached_at=?,updated_at=?,revision=revision+1 WHERE agent_id=? AND run_id=?',
                (since,key if mode == 'blocked' else None,args.get('reason','').strip() if mode == 'blocked' else '',args.get('resume_context',row['resume_context']),json.dumps(args.get('related_refs',json.loads(row['related_refs_json']))),now if mode == 'detached' else row['detached_at'],now,agent,args['run_id']))
            current_workflow = state['focus_json'] == {'type':'workflow','id':args['run_id'],'business_id':None}
            episode_changed = row['blocked_since'] and (mode != 'blocked' or row['blocker_key'] != key)
            if episode_changed:
                uow.connection.execute("UPDATE attention_reminders SET state=CASE WHEN emission_started_at IS NOT NULL THEN 'uncertain' ELSE 'cancelled' END,updated_at=?,revision=revision+1 WHERE agent_id=? AND kind='workflow_detach' AND workflow_run_id=? AND blocked_since=? AND state IN ('pending','claimed')", (now,agent,args['run_id'],row['blocked_since']))
            if mode == 'detached' and current_workflow: self.clear_focus(uow,agent,'workflow_detached')
            else:
                self.ensure(uow,agent)
                uow.connection.execute('UPDATE agent_attention SET revision=revision+1,updated_at=? WHERE agent_id=?',(now,agent))
        if op == 'attention_add':
            return self.add_result(ctx,task,uow)
        if op == 'attention_remove':
            return {'agent_id': agent, 'membership_id': args['membership_id'],
                    'removed': True, 'revision': self.state(uow, agent)['revision']}
        result = self.read(ctx,{},uow)
        if op == 'attention_focus_set' and ctx.principal_kind == 'agent':
            result['agent_hint'] = BOARD_TRANSITION_HINT
        return result


def register_attention(runtime):
    existing = getattr(runtime.application, 'ordubashy_attention', None)
    return existing if existing is not None else Attention(runtime).register()
