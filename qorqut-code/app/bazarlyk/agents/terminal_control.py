"""Exact owner authorization and durable, at-most-once terminal dispatch."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
import logging
from threading import Event, Thread

from bazarlyk.application import OperationDescriptor
from bazarlyk.contracts import DomainError, new_id, utc_now, plain
from bazarlyk.domains.common import PlanBuilder, UUID, TEXT, schema, apply_records, ref
from bazarlyk.management.identity import require_protected_control

PLUGIN = 'agent-terminal-control'
PREFIX = 'agent_terminal_'
ACTIONS = ('start', 'restart', 'kill', 'ping')
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
    from bazarlyk.catalog.packages import declared_plugin_tools
    if package.id != PLUGIN or declared_plugin_tools(package) != TOOLS:
        raise DomainError('invalid_artifact', 'Terminal control requires the exact bounded tool contract')


class TerminalControl:
    def __init__(self, runtime):
        self.runtime, self.app, self.backend = runtime, runtime.application, None
        self.app.terminal_control = self
        self.stop_event, self.thread = Event(), None

    def message_target(self, ctx, identity, uow):
        """Recorded direct messages target an existing Dev in the selected business."""
        if ctx.principal_kind == 'human':
            self.owner(ctx, uow)
        else:
            sender = uow.connection.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=? AND status='active'",
                                            (ctx.principal_id,)).fetchone()
            if (ctx.principal_kind != 'agent' or not sender or ctx.delegation_assignment_id is not None
                    or json.loads(sender['profile_json'])['role']['id'] not in {'ordubashy', 'global-marketing'}):
                raise DomainError('forbidden', 'Direct Dev terminal messages require Ordubashy or Global Marketing')
        row = uow.connection.execute("SELECT * FROM agent_profiles WHERE agent_id=? AND status='active'", (identity,)).fetchone()
        if (not row or json.loads(row['profile_json'])['role']['id'] != 'dev'
                or row['home_business_id'] != ctx.business_id or ctx.business_id not in ctx.allowed_business_ids):
            raise DomainError('forbidden', 'Choose the active Dev in the selected authorized business')
        self.app.inbox.messages.peer(ctx, identity, uow)
        return dict(row)

    def queue_message(self, ctx, message, uow):
        target = self.message_target(ctx, message['recipient_id'], uow)
        binding = self.binding(target)
        self.backend.validate('ping', binding['terminal'])
        identity = message['id']
        sender = uow.repository('principals').get(ctx.principal_id)
        text = (f"Qorqut internal message {message['id']} from {json.dumps(sender['display_name'])} "
                f"({ctx.principal_id}). Business: {message['business_id']}.\n"
                "This is a coordination message; use its recorded sender identity and existing authority. The same message is in your inbox; "
                "do not execute it twice. Reply with agents_message_send(reply_to=the message ID) and acknowledge "
                "the inbox event once handled. Existing owner scope and holds still apply.\n\n" + message['body'])
        uow.repository('terminal_actions').insert({'id': identity, 'batch_id': new_id(),
            'proposer_id': ctx.principal_id, 'target_id': target['agent_id'], 'action': 'ping',
            'message': text, 'binding_json': {**binding, 'message_id': message['id']},
            'state': 'queued', 'detail': 'Queued for the existing Dev client; processing is unconfirmed'})

    def owner(self, ctx, uow):
        owner = uow.repository('installation').get('service')
        if ctx.principal_kind != 'human' or not owner or owner['owner_principal_id'] != ctx.principal_id:
            raise DomainError('forbidden', 'Only the installation owner can issue terminal commands')

    def actor(self, ctx, uow):
        if ctx.principal_kind == 'human':
            self.owner(ctx, uow)
            return
        row = uow.connection.execute('SELECT profile_json FROM agent_profiles WHERE agent_id=? AND status=\'active\'', (ctx.principal_id,)).fetchone()
        if (ctx.principal_kind != 'agent' or not row
                or json.loads(row['profile_json'])['role']['id'] not in {'ordubashy', 'global-marketing'}):
            raise DomainError('forbidden', 'Terminal control is reserved for Ordubashy, Global Marketing and the owner')

    def target(self, ctx, identity, uow, *, control=True):
        self.actor(ctx, uow)
        row = uow.connection.execute("SELECT * FROM agent_profiles WHERE agent_id=? AND status='active'", (identity,)).fetchone()
        if not row or identity == ctx.principal_id:
            raise DomainError('forbidden', 'Select another active registered agent')
        if control:
            from .global_peers import protect_peer_control
            protect_peer_control(ctx, identity, uow)
            # Every prepare, apply and queued dispatch rechecks the current actor.
            require_protected_control(uow, ctx.principal_id, ctx.principal_kind, [identity], allow_coordinators=True)
        if ctx.principal_kind == 'agent':
            sender = uow.connection.execute('SELECT profile_json FROM agent_profiles WHERE agent_id=?', (ctx.principal_id,)).fetchone()
            from .global_peers import marketing_peer
            if (json.loads(sender['profile_json'])['role']['id'] == 'global-marketing'
                    and json.loads(row['profile_json'])['role']['id'] != 'dev'
                    and (not marketing_peer(uow, ctx.principal_id) or row['home_business_id'] is None)):
                raise DomainError('forbidden', 'Global Marketing terminal control targets business Dev agents')
        business = row['home_business_id']
        if business is not None:
            if business not in ctx.allowed_business_ids:
                raise DomainError('forbidden', 'Agent business is outside current scope')
            b = uow.repository('businesses').get(business)
            if not b or b['archived_at'] is not None:
                raise DomainError('forbidden', 'Agent business is unavailable')
        return dict(row)

    def select_targets(self, ctx, args, uow):
        """Resolve once; never expand a business again after owner approval."""
        self.actor(ctx, uow)
        identities = set(args.get('target_agent_ids', []))
        if args.get('target_agent_id'):
            identities.add(args['target_agent_id'])
        for business_id in sorted(set(args.get('business_ids', []))):
            if business_id not in ctx.allowed_business_ids:
                raise DomainError('forbidden', 'Selected business is outside current scope')
            business = uow.repository('businesses').get(business_id)
            if not business or business['archived_at'] is not None:
                raise DomainError('forbidden', 'Selected business is unavailable')
            identities.update(row['agent_id'] for row in uow.connection.execute(
                "SELECT agent_id FROM agent_profiles WHERE home_business_id=? AND status='active' LIMIT ?",
                (business_id, MAX_TARGETS + 1)))
            if len(identities) > MAX_TARGETS:
                raise DomainError('validation_error', f'Select at most {MAX_TARGETS} agents')
        if not identities or len(identities) > MAX_TARGETS:
            raise DomainError('validation_error', f'Select between 1 and {MAX_TARGETS} active agents')
        # Invalid explicit IDs or unauthorized selections reject the whole request.
        return [self.target(ctx, identity, uow) for identity in sorted(identities)]

    def binding(self, row):
        if self.backend is None:
            raise DomainError('operation_unavailable', 'Terminal controller is not attached')
        return {'profile_revision': row['revision'], 'terminal': self.backend.snapshot(row)}

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
            self.backend.validate(action, binding['terminal'])
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
            effect_kind='read', supports_signoff=False, plugin_requirement=PLUGIN, read=self.status, **common))
        for action in ('start','restart','kill'):
            name = PREFIX+action
            self.app.registry.register(OperationDescriptor(name, selection_schema({'rationale':TEXT}, ['rationale']),
                plugin_requirement=PLUGIN, prepare=lambda c,a,o,u,n=name:self.prepare(n,c,a,o,u), apply=self.apply, **common))
        for action in ('ping','execute_command'):
            name = PREFIX+action
            self.app.registry.register(OperationDescriptor(name, schema({'command_id':UUID}, ['command_id']), supports_signoff=False,
                plugin_requirement=PLUGIN, prepare=lambda c,a,o,u,n=name:self.prepare(n,c,a,o,u), apply=self.apply, **common))
        self.app.registry.register(OperationDescriptor('terminal_command_issue', selection_schema({'action':{'enum':list(ACTIONS)},
            'message':{'type':'string','minLength':1,'maxLength':16000}}, ['action']),
            supports_signoff=False, owner_only=True, exposure={'dashboard'}, prepare=lambda c,a,o,u:self.prepare('terminal_command_issue',c,a,o,u), apply=self.apply, **common))
        return self

    def tick(self):
        admission = self.app.writer_admission
        with admission.writer() if admission else nullcontext():
            self.runtime.require_authority()
            return self._tick()

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
            uow.repository('terminal_actions').update(action['id'], {'state':'uncertain','detail':'Execution started; do not retry without inspection'}, expected_revision=action['revision'])
        try:
            from bazarlyk.global_chat.correlation import delivering
            with delivering(f"terminal-action:{action['id']}"):
                detail = self.backend.execute(target, action['action'], action['message'], action['binding_json']['terminal'])
            state = 'completed'
        except Exception:
            logging.getLogger(__name__).exception('Terminal action %s outcome uncertain', action['id'])
            state, detail = 'uncertain', 'Terminal execution could not be confirmed; inspect before issuing a new command'
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
        if self.thread: self.thread.join(timeout)
