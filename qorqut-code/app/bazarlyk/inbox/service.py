"""Scoped inbox reads, explicit acknowledgements and the current native session owner.

Nothing here polls or leases. New work is announced by the native-chat nudge backend, which reads this service's pending view and
authorization; a read or a nudge never acknowledges anything."""
import json
from dataclasses import replace

from qorqut_operations.actions import action_key, one_line
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, Principal, new_id, plain
from qorqut_operations.application.plans import PlanBuilder, schema, utc_clock, timestamp
from qorqut_operations.discussion import ThreadItem, set_follow
from qorqut_operations.tasks.scope import task_context
from .events import ITEM_TYPES
from .messages import Messages

SOURCES = ('tasks', 'actions', 'signoffs', 'documents', 'email_conversations', 'outreach_events')
# Tasks and Actions are followed through the discussion's follow state (a personal, sticky preference), not through
# a subscription row; every other source keeps its subscription.
THREAD_SOURCES = {'tasks': 'task', 'actions': 'action'}
VISIBLE_TYPES = {**ITEM_TYPES, 'tasks': 'task', 'actions': 'action'}
CONSUMER = {'type': 'string', 'format': 'uuid'}
SCOPE = {'scope': {'enum': ['business', 'global']}}
TARGET = {'source_type': {'enum': list(SOURCES)}, 'source_id': {'type': 'string', 'minLength': 1, 'maxLength': 200}}


class InboxService:
    def __init__(self, application, domains, *, resolver=None, clock=utc_clock):
        self.app, self.domains, self.clock = application, domains, clock
        self.resolver = resolver
        self.messages = Messages(self)
        # Where an agent's inbox notice stands (the nudge backend's read-only status); the host replaces this when it wires the backend.
        self.delivery = lambda agent_id: {'state': 'unknown', 'reason': 'nudge_backend_not_running', 'last_attempt_at': None}

    def owner_agent_context(self, owner, agent_id):
        with self.app.database.read() as uow:
            owner = self.app.current_context(owner, uow)
            installation = uow.repository('installation').get('service')
            profile = uow.connection.execute('SELECT * FROM agent_profiles WHERE agent_id=?', (agent_id,)).fetchone()
            if not profile:
                raise DomainError('not_found', 'Agent is unavailable')
            member = uow.connection.execute('SELECT is_owner,active FROM business_memberships WHERE business_id=? AND principal_id=?',
                (profile['home_business_id'], owner.principal_id)).fetchone()
            if owner.principal_kind != 'human' or not (
                installation and installation['owner_principal_id'] == owner.principal_id or member and member['is_owner'] and member['active']):
                raise DomainError('forbidden', 'Only this agent’s owner may inspect its inbox')
        return self.resolver.invocation_context(Principal(agent_id, 'agent'))

    def owner_document(self, owner, agent_id, document_id):
        ctx = self.owner_agent_context(owner, agent_id)
        with self.app.database.read() as uow:
            ctx = self.context(ctx, uow)
            references = uow.connection.execute("SELECT business_id FROM inbox_subscriptions WHERE agent_id=? AND source_type='documents' AND source_id=? "
                "UNION SELECT e.business_id FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id "
                "WHERE d.agent_id=? AND e.source_type='documents' AND e.source_id=?",
                (agent_id, document_id, agent_id, document_id)).fetchall()
            if len(references) != 1 or not self.visible(ctx, 'documents', document_id, references[0][0], uow):
                raise DomainError('not_found', 'Inbox document is unavailable')
            selected = ctx.select_business(references[0][0])
            store = self.domains.documents.store
            row = store.get(selected, document_id, uow)
            return {'document': store.view(row, uow)}

    def owner_snapshot(self, owner, agent_id, *, after=0):
        """The agent's pending items and subscriptions as its owner may inspect them, and ``delivery``: where the backend's
        advisory notice stands ({state, reason, last_attempt_at}, the same meaning as ``agents[].inbox`` of the activity list).
        It is read only after the owner's authorization above, never says the agent read anything, and replaces the retired
        watcher, lease and poll fields."""
        ctx = self.owner_agent_context(owner, agent_id)
        with self.app.database.read() as uow:
            ctx = self.context(ctx, uow)
            batch = self.pending(ctx, uow, after=after)
            snapshot = {**batch, 'subscriptions': self.subscriptions(ctx, uow)}
        return {**snapshot, 'delivery': self.delivery(agent_id)}

    def register(self):
        common = dict(supports_signoff=False, scope_kinds=frozenset({'business', 'global'}), global_capability='read_global_context',
            resolve_namespace=lambda ctx, args: 'global' if ctx.business_id is None else 'business:' + ctx.business_id)
        operations = {
            'inbox_list': ({'consumer_id': CONSUMER, 'after': {'type': 'integer', 'minimum': 0},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}, (),
                'Read a batch of your pending updates. For a fresh reconciliation omit after (or use 0); use next_cursor as after only to continue that bounded pass. A notification watermark does not prove earlier events were handled. Reading never acknowledges. Your authenticated agent identity selects the inbox; consumer_id is accepted for older clients but is unnecessary.'),
            'inbox_subscriptions_list': ({}, (), 'List authorized subscriptions, including automatic owner-task follows for Ordubashy. An ID identifies a removable manual follow; id=null means automatic-only. Removing a manual follow never disables automatic following.'),
            'inbox_acknowledge': ({'consumer_id': CONSUMER, 'event_ids': {'type': 'array', 'minItems': 1, 'maxItems': 100,
                'uniqueItems': True, 'items': {'type': 'integer', 'minimum': 1}}, 'outcome': {'type': 'string', 'maxLength': 2000}},
                ('event_ids',), 'Acknowledge exact handled events; a read or a nudge never clears them.'),
            'inbox_subscribe': ({**TARGET, 'include_related': {'type': 'boolean'}}, ('source_type', 'source_id'),
                'Follow an authorized item. A Task or Action follow is the same preference as tasks_follow/actions_follow and is removed the same way. Only a Task follow can include its directly related work (its child Tasks and the items it is linked to, one hop); that routing lasts while the Task is followed, with no recursive following or time expiry. Remove it with inbox_unsubscribe.'),
            'inbox_unsubscribe': ({'subscription_id': {'type': 'string', 'format': 'uuid'}}, ('subscription_id',),
                'Remove your explicit subscription. A Task or Action follow is removed with tasks_follow/actions_follow instead; automatic owner-task following is independent. Already delivered updates remain pending until acknowledged.'),
        }
        for name, (fields, required, description) in operations.items():
            args = schema({**SCOPE, **fields}, required)
            if name in {'inbox_list', 'inbox_subscriptions_list'}:
                descriptor = OperationDescriptor(name, args, **common, effect_kind='read', description=description,
                    read=lambda ctx, a, opt, uow, op=name: self.read(op, ctx, a, uow))
            else:
                descriptor = OperationDescriptor(name, args, **common, description=description,
                    prepare=lambda ctx, a, opt, uow, op=name: self.prepare(op, ctx, a, uow), apply=self.apply)
            self.app.registry.register(descriptor)
        self.messages.register(common)
        self.app.inbox = self
        return self

    def context(self, ctx, uow):
        ctx = self.app.current_context(ctx, uow)
        if ctx.principal_kind != 'agent':
            raise DomainError('forbidden', 'An agent can consume only its own inbox')
        return ctx

    def visible(self, ctx, source_type, identity, business, uow, *, email_access_cache=None):
        """Use current domain authorization; unavailable objects disclose no summary."""
        if source_type == 'owner_conversations':
            # Private rows are not an inbox item type and cannot be subscribed
            # to; only the exact bound peer (configured Ordubashy or granted
            # Marketing) sees its own delivery, and only while the bound source
            # itself is still readable.
            from .private_conversations import visible_conversation
            return visible_conversation(self.app, ctx, identity, uow)
        if source_type == 'outreach_events':
            # A subscription names the campaign and can precede the first event.
            return business is not None and self.business_member(ctx, business, uow)
        if source_type not in VISIBLE_TYPES: return False
        # Request records are historical only; current canonical source reads
        # use Signoffs and no longer accept the retired requests table.
        if source_type == 'requests': return False
        try:
            selected = ctx.select_business(business)
            if business is not None and not self.business_member(ctx, business, uow):
                return False
            if business is None and 'read_global_context' not in selected.global_capabilities:
                return False
            if business is None and source_type == 'tasks':
                selected = task_context(replace(selected, business_id=None), {'scope': 'global'}, uow)
                descriptor = self.app.registry.get('tasks_get')
                self.app.authorize(selected, descriptor, (), uow, namespace='global')
                return bool(uow.repository('tasks', business_id=None).get(identity))
            if source_type in {'principals', 'agent_profiles', 'context_entities'}:
                self.domains.items.resolve(selected,
                    {'type': ITEM_TYPES[source_type], 'id': identity}, uow, allow_busy=True)
                return True
            if source_type == 'email_conversations' and business is None:
                # Ordubashy's Global mailbox: the configured coordinator and owner only.
                selected = replace(selected, business_id=None, review_business_id=None)
                self.app.authorize(selected, self.app.registry.get('email_conversation_get'), (), uow, namespace='global')
                self.email_discussion_access(selected, uow, email_access_cache)
                return bool(self.domains.email.conversation(uow, None, identity))
            if source_type == 'email_conversations':
                self.app.authorize(selected, self.app.registry.get(self.domains.email.read_operation(selected)), (), uow, namespace='business:'+business)
                self.email_discussion_access(selected, uow, email_access_cache)
                return bool(self.domains.email.conversation(uow, business, identity))
            self.domains.related.get(selected, uow, 'action_logs' if source_type == 'actions' else source_type, identity, allow_busy=True)
            return True
        except DomainError as error:
            if error.code in {'not_found', 'forbidden', 'policy_denied', 'busy', 'document_unavailable', 'operation_unavailable', 'invalid_assignee'}:
                return False
            raise

    def email_discussion_access(self, ctx, uow, cache=None):
        """Reuse permission validation only within one read snapshot and recipient.

        Conversation existence and each event's visibility are still checked
        separately. Never retain this cache across nudge passes or transactions.
        """
        if cache is None:
            return self.domains.email.discussion_access(ctx, uow)
        key = (id(uow), ctx.principal_id, ctx.business_id, ctx.profile_revision)
        if key not in cache:
            try:
                self.domains.email.discussion_access(ctx, uow)
            except DomainError as error:
                cache[key] = error
            else:
                cache[key] = None
        if cache[key] is not None:
            raise cache[key]

    @staticmethod
    def business_member(ctx, business, uow):
        return uow.connection.execute('SELECT 1 FROM business_memberships WHERE business_id=? AND principal_id=? AND active=1',
            (business, ctx.principal_id)).fetchone() is not None

    def startup_owned(self, agent_id, consumer, uow):
        return uow.connection.execute(
            "SELECT 1 FROM session_registrations WHERE principal_id=? AND session_id=? "
            "AND json_extract(metadata_json,'$.inbox_startup_claim')=1 LIMIT 1",
            (agent_id, consumer)).fetchone() is not None

    def current_owner(self, ctx, consumer, uow):
        """The agent's native session owner row, checked against the exact session and generation the caller carries."""
        row = uow.connection.execute('SELECT * FROM native_session_owners WHERE agent_id=?', (ctx.principal_id,)).fetchone()
        if row and row['consumer_id'] != consumer:
            raise DomainError('inbox_session_superseded', 'Another verified session owns this inbox; retire the stale helper')
        if row and (ctx.inbox_session_id != consumer or type(ctx.inbox_generation) is not int
                    or ctx.inbox_generation != row['generation']):
            raise DomainError('inbox_generation_stale', 'Caller native binding is missing or stale')
        if not row and ctx.inbox_session_id is not None:
            raise DomainError('inbox_generation_stale', 'Caller native binding is stale')
        return row

    def session_owner(self, ctx, consumer, uow):
        """The current owner row of this exact native session. Ownership is the binding itself; it never expires with time."""
        row = self.current_owner(ctx, consumer, uow)
        if not row or row['consumer_id'] != consumer:
            raise DomainError('inbox_consumer_required', 'Start this session through the verified native binding first')
        return row

    def session_context(self, ctx, uow):
        ctx = self.context(ctx, uow)
        self.app.ensure_writes_open(uow)
        descriptor = self.app.registry.get('inbox_list')
        if ctx.business_id is not None:
            descriptor = replace(descriptor, global_capability=None)
        if self.app.policy.resolve(ctx, descriptor, (), uow).mode != 'allow':
            raise DomainError('forbidden', 'Inbox access is not granted')
        return ctx

    def start_session(self, ctx, registration_id, session_id, event, *, binding=None,
                      old_state='unknown', expected_generation=None):
        """One transaction; native verification happens in the trusted transport adapter."""
        import uuid
        try:
            consumer = str(uuid.UUID(session_id))
        except (ValueError, TypeError, AttributeError):
            raise DomainError('validation_error', 'Inbox session_id must be a UUID')
        with self.app.database.transaction() as uow:
            ctx = self.session_context(ctx, uow)
            registration = uow.repository('machine_registrations').get(registration_id)
            if not registration or registration['principal_id'] != ctx.principal_id:
                raise DomainError('forbidden', 'Startup registration belongs to another agent')
            row = uow.connection.execute('SELECT * FROM native_session_owners WHERE agent_id=?', (ctx.principal_id,)).fetchone()
            generation = row['generation'] if row else 0   # 0 only says that no owner exists yet; it is never stored
            if expected_generation is not None and (type(expected_generation) is not int or expected_generation != generation):
                raise DomainError('inbox_generation_stale', 'Inbox ownership changed during verification')
            same = row and row['consumer_id'] == consumer
            rebound = False
            if not same and self.startup_owned(ctx.principal_id, consumer, uow):
                raise DomainError('inbox_session_superseded', 'This session was replaced; retire its helpers')
            if binding is None:
                if not same:
                    raise DomainError('inbox_binding_required', 'Startup requires a verified native process and selection')
            else:
                if (binding.get('session_id') != consumer or binding.get('registration_id') != registration_id
                        or binding.get('agent_id') != ctx.principal_id or binding.get('workspace') != registration['workspace']):
                    raise DomainError('forbidden', 'Native binding differs from the scoped registration')
                if same and json.loads(row['binding_json']).get('owner') != binding.get('owner'):
                    if old_state != 'dead':
                        raise DomainError('inbox_binding_ambiguous', 'Same session has a different live or unknown native process identity')
                    generation += 1
                    rebound = True
                if not same and row and old_state != 'dead' and (
                        event not in {'startup','resume','clear','fork'} or binding.get('event') != event):
                    raise DomainError('inbox_binding_ambiguous', 'Existing owner is not confirmed dead and no native selection is verified')
            if rebound and row:
                delegation = getattr(self.app, 'operational_delegation', None)
                if delegation is not None:
                    delegation.fence_parent_runtime(uow, ctx.principal_id,
                        actor_principal_id=ctx.principal_id, reason='Native process binding renewed with a new generation')
            if not same:
                generation += 1
                delegation = getattr(self.app, 'operational_delegation', None)
                if row and delegation is not None:
                    delegation.fence_parent_runtime(uow, ctx.principal_id,
                        actor_principal_id=ctx.principal_id, reason='Parent native binding was replaced')
                sessions = uow.repository('session_registrations')
                existing = sessions.list(filters={'registration_id':registration_id,'session_id':consumer},limit=1)
                values = {'principal_id':ctx.principal_id,'registration_id':registration_id,
                    'session_id':consumer,'workspace':registration['workspace'],
                    'metadata_json':{**(existing[0]['metadata_json'] if existing else {}),'inbox_startup_claim':True}}
                if existing: sessions.update(existing[0]['id'],values,expected_revision=existing[0]['revision'])
                else: sessions.insert({'id':new_id(),**values})
                uow.connection.execute('INSERT INTO native_session_owners(agent_id,consumer_id,binding_json,generation) '
                    'VALUES(?,?,?,?) ON CONFLICT(agent_id) DO UPDATE SET consumer_id=excluded.consumer_id,'
                    'binding_json=excluded.binding_json,generation=excluded.generation,'
                    'desired_state=CASE WHEN native_session_owners.desired_state IN (\'paused\',\'stopped\') THEN native_session_owners.desired_state ELSE \'running\' END',
                    (ctx.principal_id,consumer,json.dumps(binding),generation))
            else:
                uow.connection.execute('UPDATE native_session_owners SET binding_json=?,generation=? WHERE agent_id=? AND generation=?',
                    (json.dumps(binding or json.loads(row['binding_json'])),generation,ctx.principal_id,row['generation']))
            return {'inbox_consumer_id':consumer,'inbox_generation':generation,
                    'inbox_desired_state':row['desired_state'] if row else 'running'}

    def event_visible(self, ctx, row, uow, *, email_access_cache=None):
        if row['source_type'] == 'content_sessions':
            content = getattr(self.app,'content_sessions',None)
            return bool(content and content.event_visible(ctx,row,uow))
        if row['source_type'] == 'operational_assignments':
            if ctx.delegation_assignment_id is not None:
                return False
            assignment = uow.repository('operational_assignments').get(row['source_id'])
            if (assignment is None or assignment['parent_principal_id'] != ctx.principal_id
                    or assignment['business_id'] != row['business_id']):
                return False
            try:
                self.app.authorize(ctx, self.app.registry.get('delegated_assignment_get'), (), uow,
                    namespace='global')
                return True
            except DomainError:
                return False
        if row['source_type']=='focus_cards':
            board=getattr(self.app,'attention',None)
            if board is None: return False
            try:
                selected=board.context(ctx,uow)
                self.app.authorize(selected,self.app.registry.get('focus_get'),(),uow,namespace='global')
                return not board.projection(selected,board.row(row['source_id'],uow),uow).get('unavailable')
            except DomainError:
                return False
        if row["source_type"] == "system_incidents":
            from bazarlyk.incidents import IncidentOperations
            try:
                selected=ctx.select_business(row['business_id'])
                descriptor=self.app.registry.get('system_incidents_agent_get')
                namespace='business:'+row['business_id'] if row['business_id'] else 'global'
                self.app.authorize(selected,descriptor,(),uow,namespace=namespace)
                return IncidentOperations.visible(selected,row['source_id'],uow)
            except DomainError:
                return False
        if row["source_type"] == "daily_reports":
            from bazarlyk.reports.service import event_visible
            return event_visible(self.app, ctx, row, uow)
        if row["source_type"] == "agent_messages":
            message = uow.connection.execute("SELECT * FROM agent_messages WHERE id=?", (row["source_id"],)).fetchone()
            # Deliveries follow the current recipient; read-only collapsed history never notifies.
            return bool(message and message["current_recipient_id"] == ctx.principal_id
                        and not self.messages.collapsed(message) and self.messages.visible(ctx, message))
        if row['source_type'] == 'context_inputs':
            # A Context input notice goes to the registered coordinator alone; the input must still exist.
            installation = uow.repository('installation').get('service')
            return bool(installation and installation['management_coordinator_id'] == ctx.principal_id
                        and uow.connection.execute('SELECT 1 FROM context_inputs WHERE id=?', (row['source_id'],)).fetchone())
        if self.visible(ctx, row['source_type'], row['source_id'], row['business_id'], uow,
                        email_access_cache=email_access_cache):
            return True
        if row['event_type'] != 'deleted' or row['source_type'] not in {'tasks', 'documents'}:
            return False
        if row['business_id'] is not None and not self.business_member(ctx, row['business_id'], uow):
            return False
        from .events import row_for
        if row_for(uow, row['source_type'], row['business_id'], row['source_id']):
            return False  # Never use historical metadata to bypass current access.
        try:
            selected = ctx.select_business(row['business_id'])
            if selected.business_id is None and 'read_global_context' not in selected.global_capabilities:
                return False
            operation = {'tasks': 'tasks_get', 'documents': 'context_get'}[row['source_type']]
            if row['source_type'] == 'documents':
                kind = json.loads(row['visibility_json']).get('kind')
                operation = {'business_note': 'notes_get', 'agent_note': 'notes_get', 'journal': 'notes_get', 'business_owned_note': 'notes_get',
                    'plan': 'plans_get', 'content': 'content_get', 'asset': 'assets_get', 'data_upload': 'data_uploads_get'}.get(kind, 'context_get')
            descriptor = self.app.registry.get(operation)
            if selected.business_id is not None:
                descriptor = replace(descriptor, global_capability=None)
            return self.app.policy.resolve(selected, descriptor, (), uow).mode == 'allow'
        except DomainError as error:
            if error.code in {'forbidden', 'not_found', 'operation_unavailable'}:
                return False
            raise

    def pending(self, ctx, uow, *, after=0, limit=20):
        found = []
        # Scan in bounded SQL pages: unauthorized rows never consume a visible page.
        cursor = after
        while len(found) <= limit:
            rows = uow.connection.execute('SELECT e.* FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id '
                'WHERE d.agent_id=? AND d.acknowledged_at IS NULL AND e.id>? ORDER BY e.id LIMIT 200',
                (ctx.principal_id, cursor)).fetchall()
            if not rows: break
            for row in rows:
                cursor = row['id']
                if self.event_visible(ctx, row, uow):
                    found.append(self.event_item(ctx, row, uow))
                    if len(found) > limit: break
        more = len(found) > limit
        return {'items': found[:limit], 'next_cursor': found[limit - 1]['id'] if more else None}

    def pending_owner_conversations(self, ctx, session_id, uow, *, page_size=100):
        """Whether this exact configured coordinator session has visible pending owner deliveries.

        Inspect a finite, keyset-paginated snapshot, stopping at the first
        currently visible private source. The method returns only a boolean and
        never projects a private pointer or message. Invisible or revoked
        source rows are deliberately indistinguishable from no pending delivery.
        Page memory is bounded; worst-case authorization work is proportional to
        the pending private backlog.
        """
        if type(page_size) is not int or not 1 <= page_size <= 200:
            raise DomainError('validation_error', 'Private inbox page size must be between 1 and 200')
        ctx = self.context(ctx, uow)
        private = getattr(self.app, 'private_conversations', None)
        from bazarlyk.agents.global_peers import marketing_peer
        if private is None or (private.participants(uow)[1] != ctx.principal_id
                               and not marketing_peer(uow, ctx.principal_id)):
            return False
        try:
            self.session_owner(ctx, session_id, uow)
            descriptor = self.app.registry.get('inbox_list')
            if ctx.business_id is not None:
                descriptor = replace(descriptor, global_capability=None)
            if self.app.policy.resolve(ctx, descriptor, (), uow).mode != 'allow':
                return False
        except DomainError as error:
            if error.code in {'forbidden', 'policy_denied', 'operation_unavailable',
                              'inbox_consumer_required', 'inbox_consumer_busy',
                              'inbox_session_superseded'}:
                return False
            raise
        maximum = uow.connection.execute(
            "SELECT MAX(e.id) FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id "
            "WHERE d.agent_id=? AND d.acknowledged_at IS NULL "
            "AND e.source_type='owner_conversations'", (ctx.principal_id,)).fetchone()[0]
        if maximum is None:
            return False
        cursor = 0
        while cursor < maximum:
            rows = uow.connection.execute(
                "SELECT e.* FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id "
                "WHERE d.agent_id=? AND d.acknowledged_at IS NULL "
                "AND e.source_type='owner_conversations' AND e.id>? AND e.id<=? "
                "ORDER BY e.id ASC LIMIT ?",
                (ctx.principal_id, cursor, maximum, page_size)).fetchall()
            if not rows:
                return False
            for row in rows:
                cursor = row['id']
                if self.event_visible(ctx, row, uow):
                    return True
        return False

    def event_item(self, ctx, row, uow):
        """Return the same bounded, currently authorized view used by inbox_list."""
        item = {k: row[k] for k in row.keys() if k != 'visibility_json'}
        business = uow.repository('businesses').get(row['business_id']) if row['business_id'] else None
        actor = uow.repository('principals').get(row['actor_id']) if row['actor_id'] else None
        item.update(business_slug=business['slug'] if business else None,
                    actor_name=actor['display_name'] if actor else 'System')
        if row['source_type'] == 'agent_messages':
            message = uow.connection.execute('SELECT * FROM agent_messages WHERE id=?', (row['source_id'],)).fetchone()
            if message is not None:
                item['message'] = self.messages.view(message, uow, ctx.principal_id)
        if row['source_type'] == 'daily_reports':
            from bazarlyk.reports.service import event_summary
            item['report'] = event_summary(uow, row)  # State only; the frozen evidence is read with daily_report_get.
        if row['source_type'] == 'owner_conversations':
            # Event columns and the bound source only; never a body.
            item['source'] = json.loads(row['visibility_json'] or '{}').get('source')
        if row['source_type'] == 'content_sessions':
            content = getattr(self.app,'content_sessions',None)
            if content is not None:item['content_session'] = content.event_pointer(ctx,row,uow)
        if row['source_type'] == 'operational_assignments':
            assignment = uow.repository('operational_assignments').get(row['source_id'])
            if assignment is not None and assignment['parent_principal_id'] == ctx.principal_id:
                item['assignment'] = {key: assignment[key] for key in (
                    'id', 'state', 'dispatch_reason', 'next_step', 'wake_at', 'result_json', 'updated_at')}
        return item

    def active_event_claim(self, agent_id, event_id, uow):
        row = uow.connection.execute(
            "SELECT c.assignment_id,c.execution_generation,c.lease_until,a.state,a.execution_generation current_generation,"
            "a.lease_until assignment_lease_until FROM operational_event_claims c "
            "JOIN operational_assignments a ON a.id=c.assignment_id "
            "WHERE c.event_id=? AND c.state='claimed'", (event_id,)).fetchone()
        now = timestamp(self.clock)
        return bool(row and row['state'] == 'running'
            and row['execution_generation'] == row['current_generation']
            and row['lease_until'] is not None and row['lease_until'] > now
            and row['assignment_lease_until'] is not None and row['assignment_lease_until'] > now
            and uow.connection.execute('SELECT parent_principal_id FROM operational_assignments WHERE id=?',
                (row['assignment_id'],)).fetchone()[0] == agent_id)

    def subscriptions(self, ctx, uow):
        from .events import row_for, source_summary
        from .owner_tasks import ordubashies, tasks
        result = []
        for row in uow.connection.execute('SELECT * FROM inbox_subscriptions WHERE agent_id=? ORDER BY created_at,id', (ctx.principal_id,)):
            if row['source_type'] == 'actions' or (row['source_type'] == 'tasks' and not row['include_related']) \
                    or not self.visible(ctx, row['source_type'], row['source_id'], row['business_id'], uow):
                continue
            source = row_for(uow, row['source_type'], row['business_id'], row['source_id'])
            business = uow.repository('businesses').get(row['business_id']) if row['business_id'] else None
            result.append({**dict(row), 'business_slug': business['slug'] if business else None,
                **({'managed_by': 'related_work'} if row['source_type'] == 'tasks' else {}),
                'summary': source_summary(source, row['source_type'])})
        for follow in uow.connection.execute(
                'SELECT * FROM thread_follows WHERE principal_id=? AND following=1 ORDER BY created_at,item_type,item_id', (ctx.principal_id,)):
            table = 'tasks' if follow['item_type'] == 'task' else 'actions'
            if table == 'tasks':
                identity, source = follow['item_id'], row_for(uow, 'tasks', follow['business_id'], follow['item_id'])
                summary = source_summary(source, 'tasks')
            else:
                identity = action_key(follow['business_id'], follow['item_id'])
                source = row_for(uow, 'action_logs', follow['business_id'], follow['item_id'])
                summary = one_line(source['action_text']) if source else ''
            if source is None or not self.visible(ctx, table, identity, follow['business_id'], uow):
                continue
            related_row = next((item for item in result if item['source_type'] == table and item['source_id'] == identity
                                and item['business_id'] == follow['business_id']), None)
            if related_row is not None:
                # A related-work row is the same Task follow with its removable subscription id.
                related_row.update(managed_by='thread_follow', follow_reason=follow['reason'])
                continue
            business = uow.repository('businesses').get(follow['business_id']) if follow['business_id'] else None
            result.append({'id': None, 'agent_id': ctx.principal_id, 'business_id': follow['business_id'],
                'business_slug': business['slug'] if business else None, 'source_type': table, 'source_id': identity,
                'include_related': False, 'managed_by': 'thread_follow', 'follow_reason': follow['reason'], 'summary': summary})
        manual = {(item['business_id'], item['source_type'], item['source_id']): item for item in result}
        if ctx.principal_id in ordubashies(uow):
            for raw in tasks(uow):
                row = dict(raw)
                if not self.visible(ctx, 'tasks', row['id'], row['business_id'], uow):
                    continue
                flags = {'automatic': True, 'managed_by': 'owner_task_assignment'}
                explicit = manual.get((row['business_id'], 'tasks', row['id']))
                if explicit is not None:
                    explicit.update(flags)
                    continue
                business = uow.repository('businesses').get(row['business_id']) if row['business_id'] else None
                result.append({'id': None, 'agent_id': ctx.principal_id,
                    'business_id': row['business_id'], 'business_slug': business['slug'] if business else None,
                    'source_type': 'tasks', 'source_id': row['id'], 'include_related': False,
                    **flags,
                    'summary': source_summary(row, 'tasks')})
        return result

    def read(self, operation, ctx, args, uow):
        ctx = self.context(ctx, uow)
        if operation == 'inbox_subscriptions_list': return {'items': self.subscriptions(ctx, uow)}
        return self.pending(ctx, uow, after=args.get('after', 0), limit=args.get('limit', 20))

    def validate_mutation(self, operation, ctx, args, uow):
        ctx = self.context(ctx, uow)
        if operation == 'inbox_subscribe':
            # `owner_conversations` is not in the TARGET enum above, so a private
            # conversation is refused by argument validation before this runs.
            if args.get('include_related') and args['source_type'] != 'tasks':
                raise DomainError('validation_error', 'Only Task follows can include related work')
            if not self.visible(ctx, args['source_type'], args['source_id'], ctx.business_id, uow):
                raise DomainError('not_found', 'Subscription target is unavailable')
        elif operation == 'inbox_acknowledge':
            for identity in args['event_ids']:
                row = uow.connection.execute('SELECT e.* FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id WHERE d.agent_id=? AND e.id=?',
                    (ctx.principal_id, identity)).fetchone()
                if not row or not self.event_visible(ctx, row, uow):
                    raise DomainError('not_found', 'Inbox item is unavailable')
                if self.active_event_claim(ctx.principal_id, identity, uow):
                    raise DomainError('event_owned_by_assignment',
                        'A live delegated assignment owns this event; the parent cannot acknowledge it')
        elif operation == 'inbox_unsubscribe':
            row = uow.connection.execute('SELECT agent_id FROM inbox_subscriptions WHERE id=?', (args['subscription_id'],)).fetchone()
            if row and row['agent_id'] != ctx.principal_id:
                raise DomainError('not_found', 'Subscription is unavailable')
        return ctx

    def prepare(self, operation, ctx, args, uow):
        ctx = self.validate_mutation(operation, ctx, args, uow)
        # These self-service commands carry exact arguments and use the ordinary
        # operation policy/idempotency contract; application revalidates every ID.
        builder = PlanBuilder(ctx, operation, args, 'Manage my inbox', clock=self.clock,
            namespace='business:' + ctx.business_id if ctx.business_id else 'global',
            affected_scope={'kind': 'business', 'business_id': ctx.business_id} if ctx.business_id else {'kind': 'global'})
        return builder.build()

    def apply(self, ctx, plan, uow):
        args = plain(plan.arguments)
        ctx = self.validate_mutation(plan.operation_name, ctx, args, uow)
        if plan.operation_name == 'inbox_acknowledge':
            uow.connection.executemany('UPDATE inbox_deliveries SET acknowledged_at=?,outcome=? WHERE agent_id=? AND event_id=? AND acknowledged_at IS NULL',
                [(timestamp(self.clock), args.get('outcome', ''), ctx.principal_id, identity) for identity in args['event_ids']])
            return {'acknowledged': args['event_ids']}
        if plan.operation_name == 'inbox_unsubscribe':
            uow.connection.execute('DELETE FROM inbox_subscriptions WHERE id=? AND agent_id=?', (args['subscription_id'], ctx.principal_id))
            return {'unsubscribed': args['subscription_id']}
        if args['source_type'] in THREAD_SOURCES:
            return self.follow(ctx, args, uow)
        row = uow.connection.execute('SELECT * FROM inbox_subscriptions WHERE agent_id=? AND business_id IS ? AND source_type=? AND source_id=?',
            (ctx.principal_id, ctx.business_id, args['source_type'], args['source_id'])).fetchone()
        identity = row['id'] if row else new_id()
        uow.connection.execute('INSERT INTO inbox_subscriptions(id,agent_id,business_id,source_type,source_id,include_related,created_at) VALUES(?,?,?,?,?,?,?) '
            'ON CONFLICT(id) DO UPDATE SET include_related=excluded.include_related',
            (identity, ctx.principal_id, ctx.business_id, args['source_type'], args['source_id'], int(args.get('include_related', False)), timestamp(self.clock)))
        return {'subscription_id': identity}

    def follow(self, ctx, args, uow):
        """Following a Task or an Action is the discussion's own follow state: personal, explicit and sticky."""
        from bazarlyk.domains.activity import linked_action
        if args['source_type'] == 'tasks':
            item = ThreadItem(ctx.business_id, 'task', args['source_id'])
        else:
            action = linked_action(ctx, uow, args['source_id'])
            if action is None:
                raise DomainError('not_found', 'Subscription target is unavailable')
            item = ThreadItem(action['business_id'], 'action', action['id'])
        state = set_follow(uow, item, ctx.principal_id, True)
        result = {'following': state['following'], 'source_type': args['source_type'], 'source_id': args['source_id']}
        if args['source_type'] == 'tasks' and 'include_related' in args:
            result['subscription_id'] = self.related_work(ctx, args, bool(args['include_related']), uow)
        return result

    def related_work(self, ctx, args, wanted, uow):
        """The Task follow's related-work subscription row (one per agent and Task), or None once it is turned off."""
        row = uow.connection.execute('SELECT * FROM inbox_subscriptions WHERE agent_id=? AND business_id IS ? AND source_type=? AND source_id=?',
            (ctx.principal_id, ctx.business_id, 'tasks', args['source_id'])).fetchone()
        if not wanted:
            if row:
                uow.connection.execute('DELETE FROM inbox_subscriptions WHERE id=?', (row['id'],))
            return None
        identity = row['id'] if row else new_id()
        uow.connection.execute('INSERT INTO inbox_subscriptions(id,agent_id,business_id,source_type,source_id,include_related,created_at) VALUES(?,?,?,?,?,1,?) '
            'ON CONFLICT(id) DO UPDATE SET include_related=1',
            (identity, ctx.principal_id, ctx.business_id, 'tasks', args['source_id'], timestamp(self.clock)))
        return identity
