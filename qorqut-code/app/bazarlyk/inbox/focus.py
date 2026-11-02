"""Ordubashy's owner-facing board. References and handoffs, never authority."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, InvocationOptions, new_id, plain
from qorqut_operations.application.plans import PlanBuilder, schema, timestamp
from bazarlyk.agents.global_scope import active_global
from bazarlyk.agents.roles import ORDUBASHY_ROLE
from bazarlyk.domains.items.vocabulary import ITEM_TYPES
from qorqut_operations.items import UNAVAILABLE

UUID = {'type': 'string', 'format': 'uuid'}
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 2000}
# Products are not a Focus item type.
ITEM = schema({'type': {'enum': [kind for kind in ITEM_TYPES if kind != 'product']}, 'id': {'type': 'string', 'minLength': 1, 'maxLength': 200},
    'business_id': {'anyOf': [UUID, {'type': 'null'}]}, 'reason': TEXT, 'needs_input': {'type': 'boolean'}},
    ('type', 'id', 'business_id', 'reason'))


class FocusService:
    def __init__(self, inbox, signoffs):
        self.inbox, self.app, self.signoffs = inbox, inbox.app, signoffs
        self.items = inbox.domains.items
        self.desktop_url = lambda: None

    def register(self):
        common = dict(supports_signoff=False, scope_kinds=frozenset({'global'}),
            global_capability='read_global_context', resolve_namespace=lambda c, a: 'global')
        shared = {'scope': {'const': 'global'}, 'consumer_id': UUID,
            'expected_focus_revision': {'type': 'integer', 'minimum': 0}}
        operations = {
            'focus_show': ({'status': {'type': 'string', 'maxLength': 2000},
                'items': {'type': 'array', 'maxItems': 20, 'items': ITEM}}, ('status', 'items'),
                'Replace your ordered Focus cards with current object references and why they matter. Keeps any pending browser input. Use your session inbox consumer_id and freshly read revision.'),
            'focus_clear': ({}, (), 'Clear Focus and explicitly cancel pending browser input. Does not complete tasks, dismiss signoffs or approve actions.'),
            'focus_browser_input': ({'prompt': TEXT, 'app': {'type': 'string', 'minLength': 1, 'maxLength': 200},
                'tab': {'type': 'string', 'minLength': 1, 'maxLength': 500}}, ('prompt', 'app', 'tab'),
                'Ask the owner for browser input on the identified app/tab, after finishing the current browser command. Pauses automation until owner Done. No URL or approval authority; describe the actual tab from your last browser observation.'),
        }
        for name, (fields, required, description) in operations.items():
            self.app.registry.register(OperationDescriptor(name, schema({**shared, **fields},
                ('consumer_id', 'expected_focus_revision', *required)), **common, description='Ordubashy only: ' + description,
                prepare=lambda c, a, o, u, op=name: self.prepare(op, c, a, u), apply=self.apply))
        from .attention import AttentionBoard
        self.board = AttentionBoard(self).register()
        return self

    def context(self, ctx, uow):
        ctx = self.app.current_context(replace(ctx, business_id=None, review_business_id=None), uow)
        profile = active_global(uow.connection, ctx.principal_id) if ctx.principal_kind == 'agent' else None
        if profile is None or profile['role']['id'] != ORDUBASHY_ROLE:
            raise DomainError('forbidden', 'Focus is available only to active ecosystem Ordubashy')
        return ctx

    def row(self, ctx, uow):
        return uow.repository('agent_focus').get(ctx.principal_id) or {
            'agent_id': ctx.principal_id, 'revision': 0, 'consumer_id': None, 'status': '',
            'items_json': [], 'browser_json': None, 'activity_json': None, 'updated_at': None}

    def session(self, ctx, consumer, uow):
        self.inbox.session_owner(ctx, consumer, uow)
        if not self.inbox.startup_owned(ctx.principal_id, consumer, uow):
            raise DomainError('inbox_consumer_required', 'Use the consumer_id supplied by this prepared session startup')

    def resolve(self, ctx, item, uow):
        business = item['business_id']
        if business is not None and (business not in ctx.allowed_business_ids
                or not self.inbox.business_member(ctx, business, uow)):
            raise DomainError('not_found', 'Focus item is unavailable')
        selected = replace(ctx, business_id=business, review_business_id=business)
        if item['type'] == 'email':
            email = self.inbox.domains.email
            # Ordubashy's Global mailbox is read through the Global email operation.
            self.items.authorize(selected, 'email_conversation_get' if business is None else email.read_operation(selected), uow)
            email.discussion_access(selected, uow)
            row = email.conversation(uow, business, item['id'])
            resolved = {'label': row['subject'], 'revision': row['revision'],
                'resource_type': 'email_conversations', 'resource_id': item['id']}
        elif item['type'] == 'outreach':
            if business is None or not self.inbox.business_member(ctx, business, uow):
                raise DomainError('not_found', 'Focus item is unavailable')
            row = uow.connection.execute('SELECT * FROM outreach_events WHERE business_id IS ? AND id=?',
                (business, item['id'])).fetchone()
            if row is None:
                raise DomainError('not_found', 'Focus item is unavailable')
            from bazarlyk.integrations.outreach import deep_link
            resolved = {'label': row['summary'], 'revision': row['record_revision'],
                'resource_type': 'outreach_events', 'resource_id': item['id'], 'deep_link': deep_link(item['id'])}
        elif item['type'] == 'signoff':
            self.items.authorize(selected, 'signoffs_get', uow)
            row = self.signoffs._row(selected, item['id'], uow)
            if row['review_business_id'] != business:
                raise DomainError('not_found', 'Choose the signoff’s actual review scope')
            resolved = {'label': row['preview_json']['title'], 'state': row['state'], 'revision': row['status_revision'], 'category': row['category'],
                'resource_type': 'signoffs', 'resource_id': item['id']}
        else:
            resolved = self.items.resolve(selected, {'type': item['type'], 'id': item['id']}, uow, allow_busy=True)
        biz = uow.repository('businesses').get(business) if business else None
        card = {**item, 'label': resolved.get('label', item['id']), 'state': resolved.get('state'),
            'revision': resolved['revision'], 'resource_type': resolved['resource_type'],
            'category': resolved.get('category'), 'summary': resolved.get('summary', ''),
            'business_slug': biz['slug'] if biz else None, 'business_name': biz['name'] if biz else 'Global'}
        if item['type'] == 'task':
            card['task_type'] = resolved.get('task_type', 'task')
        if resolved.get('deep_link'):
            card['deep_link'] = resolved['deep_link']
        return card

    def validate(self, op, ctx, args, uow):
        self.board.context(ctx,uow)
        raise DomainError('upgrade_required', 'Use focus_present, focus_edit, focus_move and focus_set_archived; legacy browser Done remains available')
    def prepare(self, op, ctx, args, uow):
        ctx, _ = self.validate(op, ctx, args, uow)
        return PlanBuilder(ctx, op, args, 'Update Ordubashy Focus', namespace='global', affected_scope={'kind': 'global'}).build()

    def write(self, ctx, row, changes, uow):
        repo = uow.repository('agent_focus')
        if not repo.get(ctx.principal_id):
            return repo.insert({'agent_id': ctx.principal_id, 'revision': 1, **changes})
        return repo.update(ctx.principal_id, changes, expected_revision=row['revision'])

    def apply(self, ctx, plan, uow):
        args = plain(plan.arguments)
        ctx, row = self.validate(plan.operation_name, ctx, args, uow)
        changes = {'consumer_id': args['consumer_id'], 'updated_at': timestamp(self.inbox.clock)}
        if plan.operation_name == 'focus_show':
            changes.update(status=args['status'].strip(), items_json=args['items'])
        elif plan.operation_name == 'focus_clear':
            changes.update(status='', items_json=[], browser_json=None)
        else:
            changes['browser_json'] = {'request_id': new_id(), 'state': 'pending',
                **{key: args[key].strip() for key in ('prompt', 'app', 'tab')}, 'created_at': changes['updated_at']}
        self.write(ctx, row, changes, uow)
        return self.snapshot(ctx, uow)

    def snapshot(self, ctx, uow, owner=None):
        row, cards = self.row(ctx, uow), []
        for item in row['items_json']:
            try:
                card = self.resolve(ctx, item, uow)
                if owner:
                    self.resolve(owner, item, uow)
                cards.append(card)
            except DomainError as error:
                if error.code not in UNAVAILABLE:
                    raise
                # Do not retain formerly visible source labels or agent-authored reasons.
                cards.append({'unavailable': True})
        session_owner = uow.connection.execute('SELECT consumer_id FROM native_session_owners WHERE agent_id=?', (ctx.principal_id,)).fetchone()
        return {'revision': row['revision'], 'status': row['status'], 'items': cards,
            'browser': row['browser_json'], 'activity': row['activity_json'], 'browser_busy': self.busy(row),
            'updated_at': row['updated_at'], 'stale': bool(row['updated_at'] and (not session_owner
                or session_owner['consumer_id'] != row['consumer_id']))}

    def owner_context(self, owner, agent_id, uow, *, shared=False):
        owner = self.app.current_context(replace(owner, business_id=None, review_business_id=None), uow)
        installation = uow.repository('installation').get('service')
        if owner.principal_kind != 'human' or not installation or installation['owner_principal_id'] != owner.principal_id:
            raise DomainError('forbidden', 'Only the installation owner can read or respond to Focus')
        agent = self.app.policy.context_for(agent_id, business_id=None, review_business_id=None, uow=uow)
        if shared:
            from bazarlyk.agents.global_peers import marketing_peer
            if marketing_peer(uow, agent_id):
                return owner, self.board.context(agent, uow)
        return owner, self.context(agent, uow)

    def owner_snapshot(self, owner, agent_id):
        with self.app.database.read() as uow:
            owner, ctx = self.owner_context(owner, agent_id, uow, shared=True)
            from .attention import AttentionBoard
            return AttentionBoard(self).read(owner, {}, uow)

    def owner_done(self, owner, agent_id, request_id, note=''):
        if not isinstance(note, str) or len(note) > 4000 or not isinstance(request_id, str):
            raise DomainError('validation_error', 'Provide the browser request ID and a note of at most 4000 characters')
        with self.app.database.transaction() as uow:
            self.app.ensure_writes_open(uow)
            owner, ctx = self.owner_context(owner, agent_id, uow)
            row = self.row(ctx, uow)
            browser = row['browser_json']
            if not browser or browser['request_id'] != request_id:
                raise DomainError('revision_conflict', 'This browser input request was replaced or cancelled; refresh Focus')
            if browser['state'] == 'done':
                return {'completed': True, 'request_id': request_id}  # Lost-response retry: no second message.
            args = {'scope': 'global', 'to_agent_id': agent_id, 'message':
                f"Browser input completed by owner [Focus {request_id}].\nApp: {browser['app']}\nTab: {browser['tab']}\n"
                f"Requested input: {browser['prompt']}\nOwner note: {note.strip() or '(none)'}\n"
                'This is input completion, not approval for any protected action. Inspect the page before continuing.'}
            messages = self.inbox.messages
            plan = messages.prepare(owner, args, InvocationOptions(), uow)
            messages.apply(owner, plan, uow)
            self.write(ctx, row, {'browser_json': {**browser, 'state': 'done', 'note': note.strip(),
                'completed_at': timestamp(self.inbox.clock)}, 'updated_at': timestamp(self.inbox.clock)}, uow)
            return {'completed': True, 'request_id': request_id}

    def owner_document(self, owner, agent_id, document_id):
        with self.app.database.read() as uow:
            owner, ctx = self.owner_context(owner, agent_id, uow, shared=True)
            items=list(self.row(ctx,uow)['items_json'])
            from .attention import decode
            for stored in uow.connection.execute('SELECT * FROM focus_cards'):
                card=decode(stored)
                items.extend([ref for ref in [card['source'],card['context'],(card['reference'] or {}).get('target'),*card['references']] if ref and 'id' in ref])
            for item in items:
                if item['id'] != document_id:
                    continue
                card = self.resolve(ctx, item, uow)
                self.resolve(owner, item, uow)
                if card['resource_type'] == 'documents':
                    store = self.inbox.domains.documents.store
                    selected = replace(owner, business_id=item['business_id'])
                    return {'document': store.view(store.get(selected, document_id, uow), uow)}
            raise DomainError('not_found', 'Document is not on the current Focus board')

    def busy(self, row):
        activity = row['activity_json']
        return bool(activity and activity['phase'] in {'running', 'active'}
                    and activity['until'] > timestamp(self.inbox.clock))

    def browser_activity(self, ctx, consumer_id, command, command_id, phase):
        from bazarlyk.bootstrap.browser import COMMANDS
        import uuid
        try:
            consumer_id, command_id = str(uuid.UUID(consumer_id)), str(uuid.UUID(command_id))
        except (ValueError, TypeError, AttributeError):
            raise DomainError('validation_error', 'Browser consumer and command IDs must be UUIDs')
        if not isinstance(command, str) or command not in COMMANDS or not isinstance(phase, str) or phase not in {'running', 'active', 'finished', 'failed'}:
            raise DomainError('validation_error', 'Invalid browser activity')
        with self.app.database.transaction() as uow:
            self.app.ensure_writes_open(uow)
            ctx = self.context(ctx, uow)
            self.session(ctx, consumer_id, uow)
            descriptor = self.app.registry.get('focus_present')
            if self.app.policy.resolve(ctx, descriptor, (), uow).mode != 'allow':
                raise DomainError('forbidden', 'Focus browser access is not granted')
            row = self.row(ctx, uow)
            if phase == 'running':
                if row['browser_json'] and row['browser_json']['state'] == 'pending':
                    raise DomainError('input_pending', 'Browser is with the owner. Wait for Done; do not automate or detach')
                activity = row['activity_json']
                if self.busy(row) and activity.get('consumer_id') != consumer_id:
                    raise DomainError('browser_busy', 'Another browser command is still running')
            elif not row['activity_json'] or row['activity_json']['command_id'] != command_id:
                raise DomainError('revision_conflict', 'Browser command activity was superseded')
            now = timestamp(self.inbox.clock)
            until = (datetime.fromisoformat(now.replace('Z', '+00:00')) + timedelta(seconds=60)).astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
            activity = {'command': command, 'command_id': command_id, 'consumer_id': consumer_id,
                        'phase': phase, 'at': now, 'until': until}
            if not uow.repository('agent_focus').get(ctx.principal_id):
                uow.repository('agent_focus').insert({'agent_id': ctx.principal_id, 'revision': 1, 'updated_at': None})
            # Telemetry does not change the presentation CAS revision or owner request binding.
            import json
            uow.connection.execute('UPDATE agent_focus SET activity_json=? WHERE agent_id=?', (json.dumps(activity), ctx.principal_id))
            return {'recorded': True}


def valid_desktop_url(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(c) < 33 for c in value):
        return None
    try:
        url = urlsplit(value)
        if url.scheme in {'http', 'https'} and url.hostname and not url.username and not url.password:
            _ = url.port
            return value
    except ValueError:
        pass
    return None
