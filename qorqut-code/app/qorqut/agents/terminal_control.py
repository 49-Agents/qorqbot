"""Exact owner authorization and durable, at-most-once control of an agent's managed session (turn on and open, restart, turn off,
direct message). The tool names keep their ``agent_terminal_`` prefix: they are the contract Ordubashy and the stored plans use."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import logging
from threading import Event, Thread

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, new_id, utc_now, plain
from qorqut_operations.application.plans import PlanBuilder, UUID, TEXT, schema, apply_records, ref
from qorqut.management.identity import require_protected_control
from .global_scope import active_global
from .roles import ORDUBASHY_ROLE
from .worker_access import require_worker_manager

DESCRIPTIONS = {
    'status': 'Whether the agent is on or off, asleep or open, and working.',
    'start': 'Turn the agent on and open its session.',
    'restart': "Close the agent's session and open it again in the same conversation.",
    'kill': "Close the agent's session and turn it off until the owner messages it or you start it.",
    'ping': 'Send the owner-issued direct message.',
    'execute_command': 'Run the owner-issued session command, such as compacting.',
}
PLUGIN = 'agent-terminal-control'
PREFIX = 'agent_terminal_'
ACTIONS = ('start', 'restart', 'kill', 'ping')
# The session intent an explicit lifecycle action records for its target's native binding, when it has one.
DESIRED_STATES = {'start': 'running', 'restart': 'running', 'kill': 'stopped'}
TOOLS = {PREFIX + x for x in (*ACTIONS, 'status', 'execute_command')}
MAX_TARGETS = 100
SELECTORS = {
    'target_agent_id': UUID,
    'target_agent_ids': {'type':'array', 'items':UUID, 'minItems':1, 'maxItems':MAX_TARGETS},
    'business_ids': {'type':'array', 'items':UUID, 'minItems':1, 'maxItems':MAX_TARGETS},
}


def selection_schema(extra=None, required=()):
    value = schema({**SELECTORS, **(extra or {})}, required)
    value['anyOf'] = [{'required':[name]} for name in SELECTORS]
    return value



def validate_package(package):
    from qorqut.catalog.packages import declared_plugin_tools
    if package.id != PLUGIN or declared_plugin_tools(package) != TOOLS:
        raise DomainError('invalid_artifact', 'Terminal control requires the exact bounded tool contract')


# A managed worker is closed (restart) and has to be gone before it is opened again; the wait for that.
RESTART_CLOSE_SECONDS = 30.0
RESTART_POLL_SECONDS = 0.5


def execution_outcome(error):
    """The recorded state of an action whose execution raised: ``failed`` when nothing was delivered (the input arbiter refused the
    message before the first input, or the session could not be opened), otherwise ``uncertain``: an effect may have happened and
    is never replayed."""
    if isinstance(error, DomainError) and error.code == 'not_delivered':
        return 'failed', error.message
    return 'uncertain', 'The action could not be confirmed; inspect before issuing a new command'


class TerminalControl:
    def __init__(self, runtime):
        self.runtime, self.app = runtime, runtime.application
        self.app.terminal_control = self
        self.stop_event, self.thread = Event(), None

    def message_target(self, ctx, identity, uow):
        """Recorded direct messages targeted a business Dev. Businesses have no resident agents, and a
        business-home identity is never dispatched to whatever its stored status; inbox delivery remains."""
        if ctx.principal_kind == 'human':
            self.owner(ctx, uow)
        raise DomainError('forbidden', 'Direct messages targeted retired project Dev agents; use inbox delivery')

    def queue_message(self, ctx, message, uow):
        """Direct messages to a business Dev are retired: the target check refuses every caller."""
        self.message_target(ctx, message['recipient_id'], uow)

    def owner(self, ctx, uow):
        owner = uow.repository('installation').get('service')
        if ctx.principal_kind != 'human' or not owner or owner['owner_principal_id'] != ctx.principal_id:
            raise DomainError('forbidden', 'Only the installation owner can issue terminal commands')

    def actor(self, ctx, uow):
        if ctx.principal_kind == 'human':
            self.owner(ctx, uow)
            return
        data = active_global(uow.connection, ctx.principal_id) if ctx.principal_kind == 'agent' else None
        if data is None or data['role']['id'] != ORDUBASHY_ROLE:
            raise DomainError('forbidden', 'Terminal control is reserved for Ordubashy and the owner')

    def target(self, ctx, identity, uow, *, control=True):
        self.actor(ctx, uow)
        row = uow.connection.execute("SELECT * FROM agent_profiles WHERE agent_id=? AND status='active'", (identity,)).fetchone()
        if not row or identity == ctx.principal_id:
            raise DomainError('forbidden', 'Select another active registered agent')
        # A business-home identity is historical and never controlled or dispatched to, whatever its stored status.
        if row['home_business_id'] is not None or active_global(uow.connection, identity) is None:
            raise DomainError('forbidden', 'Select an active global agent; business-home identities are historical')
        if control:
            # Every prepare, apply and queued dispatch rechecks the current actor.
            require_protected_control(uow, ctx.principal_id, ctx.principal_kind, [identity], allow_coordinators=True)
        if ctx.principal_kind == 'agent':
            # Ordubashy reaches worker terminals only; the owner keeps every global.
            require_worker_manager(ctx, uow, identity)
        return dict(row)

    def select_targets(self, ctx, args, uow):
        """Resolve once; never expand a business again after owner approval."""
        self.actor(ctx, uow)
        identities = set(args.get('target_agent_ids', []))
        if args.get('target_agent_id'):
            identities.add(args['target_agent_id'])
        for business_id in sorted(set(args.get('business_ids', []))):
            if business_id not in ctx.allowed_business_ids:
                raise DomainError('forbidden', 'Selected project is outside current scope')
            business = uow.repository('businesses').get(business_id)
            if not business or business['archived_at'] is not None:
                raise DomainError('forbidden', 'Selected project is unavailable')
            # Businesses have no resident agents, so a business selection adds no targets.
        if not identities or len(identities) > MAX_TARGETS:
            raise DomainError('validation_error', f'Select between 1 and {MAX_TARGETS} active agents')
        # Invalid explicit IDs or unauthorized selections reject the whole request.
        return [self.target(ctx, identity, uow) for identity in sorted(identities)]

    def binding(self, row):
        return {'profile_revision': row['revision'], 'terminal': self.snapshot(row)}

    def snapshot(self, row):
        """The observation a plan is prepared against: ``absent`` without a verified session, otherwise ``present`` with that
        session's identity. It changes only when the session or the selected conversation does, never with the turn state, so an
        owner command prepared against it stays valid until then. (The ``terminal`` key and the field names are kept: stored plans
        and owner commands carry them.)"""
        from qorqut.managed_runtime.manager import managed_of
        from qorqut.native_chat import selection
        with self.runtime.database.read() as uow:
            chosen = selection.selected(uow.connection, row['agent_id'])
        revision = chosen.revision if chosen is not None else None
        managed = managed_of(self.runtime)
        identity = managed.identity(row['agent_id']) if managed is not None else None
        if not identity:
            return {'state': 'absent', 'managed': True, 'client': 'claude', 'selection_revision': revision}
        return {'state': 'present', 'managed': True, 'client': 'claude', 'command': 'claude', 'selection_revision': revision,
                'session_id': identity.get('conversation_id'), 'foreground_pid': identity.get('pid'),
                'foreground_started': identity.get('started'), 'owner': identity.get('owner')}

    def _opener(self):
        native = getattr(self.runtime, 'native_chat', None)
        opener = getattr(native, 'opener', None)
        if opener is None or opener.managed is None:
            raise DomainError('operation_unavailable', 'Managed sessions are unavailable')
        return opener

    def session_state(self, row):
        """Where a managed agent's session stands: ``on`` (not turned off), ``open``, ``asleep`` (on and not open: the next input
        opens it) and ``working`` (a turn is running); the one status Ordubashy reads for a worker."""
        opener = self._opener()
        found = opener.managed.snapshot(row['agent_id'])
        found = found if isinstance(found, dict) else {}
        is_open = bool(found.get('open'))
        off = opener.is_off(row['agent_id'])
        return {'on': not off, 'off': off, 'open': is_open, 'asleep': not is_open and not off,
                'working': is_open and found.get('phase') in ('working', 'blocked'), 'phase': found.get('phase'),
                'activity': found.get('activity')}

    def _execute(self, target, action, message, input_key):
        """The meaning of the four actions.

        ping is a direct message through the shared input arbiter like every input: it opens an asleep agent that is on (waiting for
        the open, outside the agent's lock) and is then delivered once; an agent that is off is not opened and the message is
        reported not delivered.

        The target's intent was recorded before this ran (``DESIRED_STATES``: start and restart turn it on, kill turns it off).
        start opens the session (and waits for it to bind); restart closes it, waits for its host to be gone, and opens it again
        (the same conversation resumes); kill closes it for good until a start. A session that cannot be opened is a failed action
        (nothing was delivered), never an uncertain one."""
        opener = self._opener()
        agent_id = target['agent_id']
        if action == 'ping':
            return self._ping(agent_id, message, input_key)
        if action == 'kill':
            opener.managed.close(agent_id, 'now')
            return 'Managed session closed; the agent stays off until it is started again'
        from qorqut.native_chat import store
        with self.runtime.database.read() as uow:
            profile = store.active_profile(uow.connection, agent_id, self.runtime.paths)
        if profile is None:
            raise DomainError('not_delivered', 'The agent is not active')
        opener.turn_on(agent_id)
        if action == 'restart' and opener.managed.close(agent_id, 'graceful'):
            self._wait_closed(opener, agent_id)
        try:
            opener.wake(profile, 'ordubashy', wait=True, force=True)
        except DomainError as error:
            raise DomainError('not_delivered', f'The session could not be opened ({error.code}): {error.message}') from None
        return 'Managed session restarted' if action == 'restart' else 'Managed session open'

    def _ping(self, agent_id, message, key):
        from qorqut.native_chat.contracts import (InputRequest, NativeChatError, STATE_SUBMITTED, STATE_UNCERTAIN, TYPE_DIRECT,
                                                  managed_menu_command)
        if managed_menu_command(message) is not None:
            # The same refusal owner Chat gives: a command that opens a menu has nothing to show in a headless session.
            raise DomainError('not_delivered', 'That command opens a menu a managed session cannot show')
        try:
            result = self.runtime.native_chat.arbiter.submit(InputRequest(agent_id, TYPE_DIRECT, key, message))
        except NativeChatError as error:
            raise DomainError('not_delivered', error.message) from None
        if result.state == STATE_SUBMITTED:
            return 'Direct message submitted to the agent; processing is not confirmed'
        if result.state == STATE_UNCERTAIN:
            raise DomainError('operation_failed', 'Direct message outcome could not be confirmed')
        raise DomainError('not_delivered', f"Direct message was not delivered: {result.reason or 'the session is not ready'}")

    def _wait_closed(self, opener, agent_id):
        import time
        deadline = time.monotonic() + RESTART_CLOSE_SECONDS
        while opener.managed.handle(agent_id) is not None:
            if time.monotonic() >= deadline:
                raise DomainError('not_delivered', 'The session did not close in time, so it was not opened again')
            time.sleep(RESTART_POLL_SECONDS)

    def command(self, ctx, identity, uow):
        row = uow.repository('terminal_commands').get(identity, required=True)
        owner = uow.repository('installation').get('service')
        principal = uow.repository('principals').get(row['issuer_id'])
        if (not owner or row['issuer_id'] != owner['owner_principal_id'] or not principal or not principal['active']
                or row['consumed_by'] or row['expires_at'] <= utc_now()):
            raise DomainError('forbidden', 'Owner command is expired, consumed, or no longer authorized')
        for target in row['targets_json']:
            self.target(ctx, target['target_id'], uow)
        return row

    def prepare(self, name, ctx, args, options, uow):
        self.actor(ctx, uow)
        builder = PlanBuilder(ctx, name, args, 'Control agent terminals', namespace='global', affected_scope={'kind':'global'})
        command = None
        if name in {PREFIX+'execute_command', PREFIX+'ping'}:
            command = self.command(ctx, args['command_id'], uow)
            action, message = command['action'], command['message']
            rows = [self.target(ctx, item['target_id'], uow) for item in command['targets_json']]
            if name == PREFIX+'ping' and action != 'ping':
                raise DomainError('forbidden', 'Direct ping requires an explicit owner ping command')
        else:
            action = args['action'] if name == 'terminal_command_issue' else name.removeprefix(PREFIX)
            message = args.get('message')
            rows = self.select_targets(ctx, args, uow)
        if action == 'ping':
            if not message or not message.strip() or any(ord(c)<32 and c not in '\n\t' for c in message):
                raise DomainError('validation_error', 'Direct ping requires nonblank text without terminal control characters')
        elif message is not None:
            raise DomainError('validation_error', 'Only direct ping accepts a message')
        targets = []
        for row in rows:
            binding = self.binding(row)
            builder.expect(ref('agent_profiles', row['agent_id'], row['home_business_id']), row)
            targets.append({'target_id':row['agent_id'], 'binding':binding})
            builder.effects.append(f"{action.capitalize()} {row['name']} ({row['agent_id']})")
        if command and targets != command['targets_json']:
            raise DomainError('revision_conflict', 'A target session changed since the owner command')
        builder.title = (f"{action.capitalize()} agent {rows[0]['name']}" if len(rows)==1 else f"{action.capitalize()} {len(rows)} agents")
        if action == 'ping':
            builder.title += ' directly'
        if name == 'terminal_command_issue':
            self.owner(ctx, uow)
            identity = new_id()
            builder.record('terminal_commands', identity, None, {'id':identity, 'issuer_id':ctx.principal_id,
                'targets_json':targets, 'action':action, 'message':message,
                'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat().replace('+00:00','Z'),
                'consumed_by':None}, business_id=None)
        else:
            batch_id = new_id()
            if command:
                builder.record('terminal_commands', command['id'], command, {'consumed_by':batch_id}, business_id=None)
            for target in targets:
                identity = new_id()
                builder.record('terminal_actions', identity, None, {'id':identity, 'batch_id':batch_id,
                    'proposer_id':ctx.principal_id, 'target_id':target['target_id'], 'action':action,
                    'message':message, 'binding_json':target['binding'], 'state':'queued', 'detail':None}, business_id=None)
        return builder.build()

    def apply(self, ctx, plan, uow):
        self.actor(ctx, uow)
        for change in plan.changes:
            if change.resource.resource_type not in {'terminal_actions','terminal_commands'}:
                continue
            values = plain(change.values)
            if change.action == 'insert':
                targets = values['targets_json'] if change.resource.resource_type == 'terminal_commands' else [
                    {'target_id':values['target_id'], 'binding':values['binding_json']}]
                for target in targets:
                    row = self.target(ctx, target['target_id'], uow)
                    if self.binding(row) != target['binding']:
                        raise DomainError('revision_conflict', 'Target session changed before application')
            elif change.resource.resource_type == 'terminal_commands':
                self.command(ctx, change.resource.resource_id, uow)
        records = apply_records(ctx, plan, uow)
        actions = [row for row in records if 'batch_id' in row]
        return {'records':records, **({'batch_id':actions[0]['batch_id']} if actions else {})}

    def status(self, ctx, args, options, uow):
        self.actor(ctx, uow)
        if args.get('batch_id'):
            actions = [dict(row) for row in uow.connection.execute(
                'SELECT id,target_id,state,detail FROM terminal_actions WHERE batch_id=? ORDER BY target_id,id', (args['batch_id'],))]
            if not actions:
                raise DomainError('not_found', 'Terminal batch was not found')
            for action in actions:
                self.target(ctx, action['target_id'], uow, control=False)
            return {'batch_id':args['batch_id'], 'actions':actions}
        row = self.target(ctx, args['target_agent_id'], uow, control=False)
        result = {'target_agent_id':row['agent_id'], **self.binding(row)}
        try:
            result['session'] = self.session_state(row)
        except DomainError:
            result['session'] = None
        if args.get('action_id'):
            action = uow.repository('terminal_actions').get(args['action_id'], required=True)
            if action['target_id'] != row['agent_id']:
                raise DomainError('not_found', 'Action belongs to another agent')
            result['action'] = action
        return result

    def register(self):
        common = dict(scope_kinds={'global'}, global_capability='read_global_context', resolve_namespace=lambda c,a:'global')
        self.app.registry.register(OperationDescriptor(PREFIX+'status', {**schema({'target_agent_id':UUID,'action_id':UUID,'batch_id':UUID}),
                'oneOf':[{'required':['target_agent_id'], 'not':{'required':['batch_id']}},
                         {'required':['batch_id'], 'not':{'anyOf':[{'required':['target_agent_id']},{'required':['action_id']}]}}]},
            effect_kind='read', supports_signoff=False, plugin_requirement=PLUGIN, read=self.status,
            description=DESCRIPTIONS['status'], **common))
        for action in ('start','restart','kill'):
            name = PREFIX+action
            self.app.registry.register(OperationDescriptor(name, selection_schema({'rationale':TEXT}, ['rationale']),
                plugin_requirement=PLUGIN, prepare=lambda c,a,o,u,n=name:self.prepare(n,c,a,o,u), apply=self.apply,
                description=DESCRIPTIONS[action], **common))
        for action in ('ping','execute_command'):
            name = PREFIX+action
            self.app.registry.register(OperationDescriptor(name, schema({'command_id':UUID}, ['command_id']), supports_signoff=False,
                plugin_requirement=PLUGIN, prepare=lambda c,a,o,u,n=name:self.prepare(n,c,a,o,u), apply=self.apply,
                description=DESCRIPTIONS[action], **common))
        self.app.registry.register(OperationDescriptor('terminal_command_issue', selection_schema({'action':{'enum':list(ACTIONS)},
            'message':{'type':'string','minLength':1,'maxLength':16000}}, ['action']),
            supports_signoff=False, owner_only=True, exposure={'dashboard'}, prepare=lambda c,a,o,u:self.prepare('terminal_command_issue',c,a,o,u), apply=self.apply, **common))
        return self

    def tick(self):
        admission = self.app.writer_admission
        with admission.writer() if admission else nullcontext():
            self.runtime.require_authority()
            # An idle controller holds no write unit: only a visible queued action enters the transaction below, which
            # selects again and repeats every authorization, binding and uncertain-before-effect step.
            if not self._has_queued():
                return False
            return self._tick()

    def _has_queued(self):
        with self.runtime.database.read() as uow:
            return uow.connection.execute("SELECT 1 FROM terminal_actions WHERE state='queued' LIMIT 1").fetchone() is not None

    def _tick(self):
        with self.runtime.database.transaction() as uow:
            self.app.ensure_writes_open(uow)
            row = uow.connection.execute("SELECT id FROM terminal_actions WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if not row: return False
            actions = uow.repository('terminal_actions')
            action = actions.get(row['id'])
            # Before a batch's first effect, every saved target's authorization is rechecked: a
            # request that reaches an unauthorized target fails whole, never partly run.
            # A changed terminal binding still fails only its own action.
            started = uow.connection.execute("SELECT 1 FROM terminal_actions WHERE batch_id=? AND state!='queued' LIMIT 1",
                                             (action['batch_id'],)).fetchone()
            checked = [action] if started else [actions.get(item['id']) for item in uow.connection.execute(
                "SELECT id FROM terminal_actions WHERE batch_id=? AND state='queued' ORDER BY created_at,id", (action['batch_id'],))]
            failing = None
            try:
                for failing in checked:
                    current = self._authorized(failing, uow)
                    if failing['id'] == action['id']:
                        target = current
            except DomainError as exc:
                for item in checked:
                    detail = exc.message if item['id'] == failing['id'] else 'Another target in this batch failed authorization before execution'
                    actions.update(item['id'], {'state':'failed','detail':detail}, expected_revision=item['revision'])
                return True
            try:
                binding = {k:v for k,v in action['binding_json'].items() if k != 'message_id'}
                if self.binding(target) != binding:
                    raise DomainError('revision_conflict', 'Target session changed before execution')
            except DomainError as exc:
                actions.update(action['id'], {'state':'failed','detail':exc.message}, expected_revision=action['revision'])
                return True
            # Never replay after a crash, timeout or uncertain external write.
            if action['action'] in DESIRED_STATES:
                # The explicit start/stop intent is durable before its effect, so nothing restarts a stopped session.
                uow.connection.execute('UPDATE native_session_owners SET desired_state=? WHERE agent_id=?',
                                       (DESIRED_STATES[action['action']], target['agent_id']))
            uow.repository('terminal_actions').update(action['id'], {'state':'uncertain','detail':'Execution started; do not retry without inspection'}, expected_revision=action['revision'])
        try:
            detail = self._execute(target, action['action'], action['message'], f"terminal-action:{action['id']}")
            state = 'completed'
        except Exception as exc:
            state, detail = execution_outcome(exc)
            if state == 'uncertain':
                logging.getLogger(__name__).exception('Terminal action %s outcome uncertain', action['id'])
        with self.runtime.database.transaction() as uow:
            row = uow.repository('terminal_actions').get(action['id'])
            uow.repository('terminal_actions').update(action['id'], {'state':state,'detail':detail}, expected_revision=row['revision'])
        return True

    def _authorized(self, action, uow):
        """The saved action's current proposer permission and target authorization."""
        if action['binding_json'].get('message_id'):
            message = uow.connection.execute('SELECT * FROM agent_messages WHERE id=?',
                (action['binding_json']['message_id'],)).fetchone()
            if (not message or action['action'] != 'ping' or message['sender_id'] != action['proposer_id']
                    or message['recipient_id'] != action['target_id']
                    or message['current_sender_id'] != message['sender_id']
                    or message['current_recipient_id'] != message['recipient_id']):
                raise DomainError('forbidden', 'Terminal message participants changed')
            ctx = self.app.policy.context_for(action['proposer_id'], business_id=message['business_id'], review_business_id=None, uow=uow)
            self.app.authorize(ctx, self.app.registry.get('agents_message_send'), [], uow,
                               namespace='business:' + message['business_id'])
            return self.message_target(ctx, action['target_id'], uow)
        ctx = self.app.policy.context_for(action['proposer_id'], business_id=None, review_business_id=None, uow=uow)
        descriptor = self.app.registry.get(PREFIX + ('ping' if action['action']=='ping' else action['action']))
        decision = self.app.authorize(ctx, descriptor, [], uow, namespace='global')
        if decision.mode == 'deny': raise DomainError('forbidden', 'Terminal permission was revoked')
        return self.target(ctx, action['target_id'], uow)

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.stop_event.clear()
        self.thread = Thread(target=self._loop, name='agent-terminal-control', daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop_event.wait(1):
            try: self.tick()
            except Exception: logging.getLogger(__name__).exception('Terminal controller tick failed')

    def stop(self, timeout=5):
        self.stop_event.set()
        thread = self.thread
        if thread is None: return
        thread.join(timeout)
        if thread.is_alive(): raise TimeoutError('Terminal control is still draining')
        self.thread = None
