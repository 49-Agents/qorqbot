"""Scoped inbox reads, explicit acknowledgements and a single consumer lease."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, Principal, new_id, plain
from qorqut_operations.application.plans import PlanBuilder, schema, utc_clock, timestamp
from .events import ITEM_TYPES
from .messages import Messages

SOURCES = ('stories', 'tasks', 'signoffs', 'documents', 'email_conversations', 'outreach_events')
CONSUMER = {'type': 'string', 'format': 'uuid'}
SCOPE = {'scope': {'enum': ['business', 'global']}}
TARGET = {'source_type': {'enum': list(SOURCES)}, 'source_id': {'type': 'string', 'minLength': 1, 'maxLength': 200}}
LEASE_SECONDS = 900


class InboxService:
    def __init__(self, application, domains, *, resolver=None, clock=utc_clock):
        self.app, self.domains, self.clock = application, domains, clock
        self.resolver = resolver
        self.messages = Messages(self)

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
        ctx = self.owner_agent_context(owner, agent_id)
        with self.app.database.read() as uow:
            ctx = self.context(ctx, uow)
            batch = self.pending(ctx, uow, after=after)
            watcher = uow.connection.execute('SELECT last_check_at,lease_until FROM inbox_watchers WHERE agent_id=?', (agent_id,)).fetchone()
            return {**batch, 'subscriptions': self.subscriptions(ctx, uow),
                    'watcher': dict(watcher) if watcher else None,
                    'watcher_active': bool(watcher and watcher['lease_until'] > timestamp(self.clock)
                        and watcher['last_check_at'] > self.lease_until(seconds=-360))}

    def lease_until(self, *, seconds=LEASE_SECONDS):
        return (self.clock().astimezone(timezone.utc) + timedelta(seconds=seconds)).isoformat(
            timespec='microseconds').replace('+00:00', 'Z')

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
                'Follow an authorized item. Only Stories can include directly related items; no recursive following or time expiry.'),
            'inbox_unsubscribe': ({'subscription_id': {'type': 'string', 'format': 'uuid'}}, ('subscription_id',),
                'Remove your explicit subscription. Automatic owner-task following is independent. Already delivered updates remain pending until acknowledged.'),
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

    def visible(self, ctx, source_type, identity, business, uow):
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
        if source_type not in ITEM_TYPES: return False
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
                from bazarlyk.domains.tasks.operations import task_context
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
                self.domains.email.discussion_access(selected, uow)
                return bool(self.domains.email.conversation(uow, None, identity))
            if source_type == 'email_conversations':
                self.app.authorize(selected, self.app.registry.get(self.domains.email.read_operation(selected)), (), uow, namespace='business:'+business)
                self.domains.email.discussion_access(selected, uow)
                return bool(self.domains.email.conversation(uow, business, identity))
            if source_type == 'stories':
                descriptor = self.app.registry.get('stories_get')
                self.app.authorize(selected, descriptor, (), uow,
                    namespace='global' if business is None else 'business:'+business)
                return bool(uow.repository('stories', business_id=business).get(identity))
            self.domains.related.get(selected, uow, source_type, identity, allow_busy=True)
            return True
        except DomainError as error:
            if error.code in {'not_found', 'forbidden', 'policy_denied', 'busy', 'document_unavailable', 'operation_unavailable', 'invalid_assignee'}:
                return False
            raise

    @staticmethod
    def business_member(ctx, business, uow):
        return uow.connection.execute('SELECT 1 FROM business_memberships WHERE business_id=? AND principal_id=? AND active=1',
            (business, ctx.principal_id)).fetchone() is not None

    def startup_owned(self, agent_id, consumer, uow):
        return uow.connection.execute(
            "SELECT 1 FROM session_registrations WHERE principal_id=? AND session_id=? "
            "AND json_extract(metadata_json,'$.inbox_startup_claim')=1 LIMIT 1",
            (agent_id, consumer)).fetchone() is not None

    def current_watcher(self, ctx, consumer, uow):
        row = uow.connection.execute('SELECT * FROM inbox_watchers WHERE agent_id=?', (ctx.principal_id,)).fetchone()
        if row and row['consumer_id'] != consumer:
            raise DomainError('inbox_session_superseded', 'Another verified session owns this inbox; retire the stale helper')
        if row and row['generation'] > 0 and (
                ctx.inbox_session_id != consumer or type(ctx.inbox_generation) is not int
                or ctx.inbox_generation != row['generation']):
            raise DomainError('inbox_generation_stale', 'Caller native binding is missing or stale')
        if ctx.inbox_session_id is not None and (ctx.inbox_session_id != consumer or not row
                or type(ctx.inbox_generation) is not int or ctx.inbox_generation != row['generation']):
            raise DomainError('inbox_generation_stale', 'Caller native binding is stale')
        return row

    def lease(self, ctx, consumer, uow):
        row = self.current_watcher(ctx, consumer, uow)
        if not row or row['consumer_id'] != consumer or row['lease_until'] <= timestamp(self.clock):
            raise DomainError('inbox_consumer_required', 'Start or renew this session inbox watcher before consuming updates')
        return row

    def watch_context(self, ctx, uow):
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
            ctx = self.watch_context(ctx, uow)
            registration = uow.repository('machine_registrations').get(registration_id)
            if not registration or registration['principal_id'] != ctx.principal_id:
                raise DomainError('forbidden', 'Startup registration belongs to another agent')
            row = uow.connection.execute('SELECT * FROM inbox_watchers WHERE agent_id=?', (ctx.principal_id,)).fetchone()
            generation = row['generation'] if row else 0
            if expected_generation is not None and (type(expected_generation) is not int or expected_generation != generation):
                raise DomainError('inbox_generation_stale', 'Inbox ownership changed during verification')
            same = row and row['consumer_id'] == consumer
            rebound = False
            if same and binding is not None and generation == 0:
                generation = 1
                rebound = True
            if not same and self.startup_owned(ctx.principal_id, consumer, uow):
                raise DomainError('inbox_session_superseded', 'This session was replaced; retire its helpers')
            if binding is None:
                if not same:
                    raise DomainError('inbox_binding_required', 'Startup requires a verified native process and selection')
            else:
                if (binding.get('session_id') != consumer or binding.get('registration_id') != registration_id
                        or binding.get('agent_id') != ctx.principal_id or binding.get('workspace') != registration['workspace']):
                    raise DomainError('forbidden', 'Native binding differs from the scoped registration')
                if same and json.loads(row['binding_json']) and json.loads(row['binding_json']).get('owner') != binding.get('owner'):
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
                uow.connection.execute('INSERT INTO inbox_watchers(agent_id,consumer_id,last_check_at,lease_until,binding_json,generation) '
                    'VALUES(?,?,?,?,?,?) ON CONFLICT(agent_id) DO UPDATE SET consumer_id=excluded.consumer_id,'
                    'last_check_at=excluded.last_check_at,lease_until=excluded.lease_until,binding_json=excluded.binding_json,'
                    'generation=excluded.generation,desired_state=CASE WHEN inbox_watchers.desired_state IN (\'paused\',\'stopped\') THEN inbox_watchers.desired_state ELSE \'running\' END',
                    (ctx.principal_id,consumer,'1970-01-01T00:00:00.000000Z',self.lease_until(),json.dumps(binding or {}),generation))
            else:
                uow.connection.execute('UPDATE inbox_watchers SET lease_until=?,binding_json=?,generation=? WHERE agent_id=? AND generation=?',
                    (self.lease_until(),json.dumps(binding or json.loads(row['binding_json'])),generation,ctx.principal_id,row['generation']))
            return {'inbox_consumer_id':consumer,'inbox_generation':generation,
                    'inbox_desired_state':row['desired_state'] if row else 'running'}

    def legacy_owner(self, ctx, registration_id, consumer, uow):
        """The migration-preserved generation-0 owner of this exact registration, or the reason it is not one.

        Only the authenticated credential's registration is consulted. A fenced row, another consumer, or a
        registration this consumer was not claimed through is refused; nothing is created, renewed or upgraded here.
        """
        row = uow.connection.execute('SELECT * FROM inbox_watchers WHERE agent_id=?', (ctx.principal_id,)).fetchone()
        if not row or row['consumer_id'] != consumer:
            raise DomainError('inbox_session_superseded', 'Legacy consumer is not the current inbox owner')
        if row['generation'] != 0 or json.loads(row['binding_json']):
            raise DomainError('inbox_generation_stale', 'Inbox ownership is fenced; the legacy helper must stop')
        if not uow.connection.execute('SELECT 1 FROM session_registrations WHERE principal_id=? AND registration_id=? AND session_id=? LIMIT 1',
                (ctx.principal_id, registration_id, consumer)).fetchone():
            raise DomainError('invalid_registration', 'Legacy consumer belongs to a different registration')
        return row

    def require_legacy(self, ctx, registration_id, consumer):
        with self.app.database.read() as uow:
            self.legacy_owner(self.context(ctx, uow), registration_id, consumer, uow)

    def legacy_observation(self, ctx, registration_id, consumer, adapter):
        """Report, never create, the verified native session of the existing generation-0 owner.

        This is how the supervisor learns that a migration-preserved owner may be maintained: the owner and its
        scoped registration still match, and the same native process is verified on its execution target. It changes
        no ownership and writes nothing; an unobservable target is an error, never an assumption.
        """
        with self.app.database.read() as uow:
            ctx = self.watch_context(ctx, uow)
            state = self.legacy_owner(ctx, registration_id, consumer, uow)['desired_state']
        native = adapter.verify_legacy(ctx.principal_id, registration_id, consumer)
        return {'generation': 0, 'desired_state': state, 'session_id': consumer, 'owner': native['owner'],
                'runtime': native['runtime'], 'observation': native['observation']}

    def event_visible(self, ctx, row, uow):
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
        if self.visible(ctx, row['source_type'], row['source_id'], row['business_id'], uow):
            return True
        if row['event_type'] != 'deleted' or row['source_type'] not in {'stories', 'tasks', 'documents'}:
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
            operation = {'stories': 'stories_get', 'tasks': 'tasks_get', 'documents': 'context_get'}[row['source_type']]
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
            self.lease(ctx, session_id, uow)
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
            if not self.visible(ctx, row['source_type'], row['source_id'], row['business_id'], uow):
                continue
            source = row_for(uow, row['source_type'], row['business_id'], row['source_id'])
            business = uow.repository('businesses').get(row['business_id']) if row['business_id'] else None
            result.append({**dict(row), 'business_slug': business['slug'] if business else None,
                'summary': source_summary(source, row['source_type'])})
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
            if args.get('include_related') and args['source_type'] != 'stories':
                raise DomainError('validation_error', 'Only Story subscriptions can include related items')
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
        row = uow.connection.execute('SELECT * FROM inbox_subscriptions WHERE agent_id=? AND business_id IS ? AND source_type=? AND source_id=?',
            (ctx.principal_id, ctx.business_id, args['source_type'], args['source_id'])).fetchone()
        identity = row['id'] if row else new_id()
        uow.connection.execute('INSERT INTO inbox_subscriptions(id,agent_id,business_id,source_type,source_id,include_related,created_at) VALUES(?,?,?,?,?,?,?) '
            'ON CONFLICT(id) DO UPDATE SET include_related=excluded.include_related',
            (identity, ctx.principal_id, ctx.business_id, args['source_type'], args['source_id'], int(args.get('include_related', False)), timestamp(self.clock)))
        return {'subscription_id': identity}

    def status(self,ctx,consumer):
        from bazarlyk.installation import app_manifest
        with self.app.database.read() as uow:
            ctx=self.context(ctx,uow);row=self.current_watcher(ctx,consumer,uow)
            if not row:raise DomainError('inbox_binding_required','Verified native binding is unavailable')
            manifest=app_manifest()
            if not hasattr(self.app,'inbox_instance'):self.app.inbox_instance=new_id()
            return {'instance':self.app.inbox_instance,'release_id':manifest.get('release_id') if manifest else None,
                'generation':row['generation'],'desired_state':row['desired_state']}

    def set_desired_state(self,ctx,consumer,desired_state):
        if desired_state not in {'running','paused','stopped'}:
            raise DomainError('validation_error','Unknown desired watcher state')
        with self.app.database.transaction() as uow:
            ctx=self.watch_context(ctx,uow)
            row=self.current_watcher(ctx,consumer,uow)
            if not row:raise DomainError('inbox_binding_required','No verified native binding is available')
            uow.connection.execute('UPDATE inbox_watchers SET desired_state=? WHERE agent_id=? AND generation=?',
                (desired_state,ctx.principal_id,row['generation']))
            return {'desired_state':desired_state,'generation':row['generation']}

    def release(self, ctx, consumer):
        import uuid
        try: uuid.UUID(consumer)
        except (ValueError, TypeError, AttributeError):
            raise DomainError('validation_error', 'consumer_id must be a UUID')
        with self.app.database.transaction() as uow:
            ctx = self.context(ctx, uow)
            self.app.ensure_writes_open(uow)
            row = uow.connection.execute('SELECT * FROM inbox_watchers WHERE agent_id=?', (ctx.principal_id,)).fetchone()
            if not row or row['consumer_id'] != consumer:
                return {'released': False}
            self.current_watcher(ctx, consumer, uow)
            changed = uow.connection.execute(
                'UPDATE inbox_watchers SET lease_until=? WHERE agent_id=? AND consumer_id=? AND generation=? AND lease_until>?',
                (timestamp(self.clock), ctx.principal_id, consumer, row['generation'], timestamp(self.clock))).rowcount
            if changed:
                delegation = getattr(self.app, 'operational_delegation', None)
                if delegation is not None:
                    delegation.fence_parent_runtime(uow, ctx.principal_id,
                        actor_principal_id=ctx.principal_id, reason='Parent inbox session released')
            return {'released': bool(changed)}

    def watch(self, ctx, consumer):
        # Watchers use authenticated transport, not model calls or database access.
        import uuid
        try: uuid.UUID(consumer)
        except (ValueError, TypeError, AttributeError): raise DomainError('validation_error', 'consumer_id must be a UUID')
        with self.app.database.transaction(operation='inbox.watch.lease') as uow:
            ctx = self.watch_context(ctx, uow)
            now = timestamp(self.clock)
            owner = self.current_watcher(ctx, consumer, uow)
            if not owner:
                raise DomainError('inbox_binding_required', 'A verified native binding is required before polling')
            if owner['desired_state'] != 'running':
                raise DomainError('inbox_watching_stopped', 'Inbox watching is explicitly paused or stopped')
            until = self.lease_until()
            uow.connection.execute('INSERT INTO inbox_watchers(agent_id,consumer_id,last_check_at,lease_until) VALUES(?,?,?,?) '
                'ON CONFLICT(agent_id) DO UPDATE SET consumer_id=excluded.consumer_id,last_check_at=excluded.last_check_at,lease_until=excluded.lease_until',
                (ctx.principal_id, consumer, now, until))
        # Permission checks and inbox materialization must not hold SQLite's
        # single writer lock. Recheck ownership in this read snapshot.
        with self.app.database.read() as uow:
            ctx = self.context(ctx, uow)
            self.lease(ctx, consumer, uow)
            count, after, notifications = 0, 0, []
            while True:
                batch = self.pending(ctx, uow, after=after, limit=100)
                count += len(batch['items'])
                for item in batch['items']:
                    delivered = uow.connection.execute(
                        'SELECT notified_consumer_id FROM inbox_deliveries WHERE agent_id=? AND event_id=?',
                        (ctx.principal_id, item['id'])).fetchone()
                    if (delivered[0] != consumer and len(notifications) < 100
                            and not self.active_event_claim(ctx.principal_id, item['id'], uow)):
                        notifications.append(item['id'])
                if batch['next_cursor'] is None: break
                after = batch['next_cursor']
            return {'pending_count': count, 'last_check_at': now, 'lease_until': until,
                    'notification_event_ids': notifications, 'watcher_protocol': 3, 'generation': owner['generation']}

    def notification(self, ctx, consumer, event_ids, *, delivered=False, issue=False, notification_id=None, observed=False):
        """Revalidate just before delivery; confirming a notice never handles work."""
        if (not isinstance(event_ids, list) or not 1 <= len(event_ids) <= 100
                or any(type(i) is not int or i < 1 for i in event_ids)
                or len(set(event_ids)) != len(event_ids)):
            raise DomainError('validation_error', 'Notification requires up to 100 unique positive event IDs')
        if type(observed) is not bool or type(issue) is not bool or (issue and (notification_id or delivered)):
            raise DomainError('validation_error', 'Invalid notification issue request')
        if notification_id is not None and (not isinstance(notification_id,str) or len(notification_id)>100):
            raise DomainError('validation_error', 'Invalid notification receipt')
        unit = (self.app.database.transaction(operation='inbox.notification.confirm') if delivered or issue or observed
                else self.app.database.read())
        with unit as uow:
            ctx = self.context(ctx, uow)
            self.lease(ctx, consumer, uow)
            if delivered or issue or observed:
                self.app.ensure_writes_open(uow)
            recognized = None
            if notification_id is not None:
                receipt = uow.connection.execute('SELECT * FROM inbox_notification_receipts '
                    'WHERE id=? AND agent_id=? AND consumer_id=?',
                    (notification_id, ctx.principal_id, consumer)).fetchone()
                recognized = bool(receipt and receipt['generation'] == self.lease(ctx,consumer,uow)['generation']
                    and json.loads(receipt['event_ids_json']) == event_ids)
                if not recognized:
                    return {'event_ids': [], 'recognized': False}
            elif not issue and not delivered:
                # Rolling v2 compatibility: only cancel a notice whose exact
                # events were recorded as delivered to this same native session.
                recognized = all(uow.connection.execute('SELECT 1 FROM inbox_deliveries WHERE agent_id=? '
                    'AND event_id=? AND notified_consumer_id=? AND notified_at IS NOT NULL',
                    (ctx.principal_id,identity,consumer)).fetchone() for identity in event_ids)
            current = []
            for identity in event_ids:
                row = uow.connection.execute('SELECT e.* FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id '
                    'WHERE d.agent_id=? AND e.id=? AND d.acknowledged_at IS NULL',
                    (ctx.principal_id, identity)).fetchone()
                if row and self.event_visible(ctx, row, uow):
                    current.append(identity)
            if observed and recognized:
                # Receipt observed at the verified native prompt boundary reconciles
                # a lost terminal submission response; events remain unacknowledged.
                uow.connection.execute("UPDATE inbox_notification_receipts SET delivery_state='submitted' WHERE id=? AND delivery_state IN ('uncertain','submitted')", (notification_id,))
            if delivered or observed and recognized:
                uow.connection.executemany('UPDATE inbox_deliveries SET notified_consumer_id=?,notified_at=? '
                    'WHERE agent_id=? AND event_id=? AND acknowledged_at IS NULL',
                    [(consumer, timestamp(self.clock), ctx.principal_id, identity) for identity in current])
            result = {'event_ids': current}
            if recognized is not None:
                result['recognized'] = recognized
            if issue and current:
                owner = self.lease(ctx, consumer, uow)
                content_ids = {row[0] for row in uow.connection.execute(
                    "SELECT id FROM inbox_events WHERE source_type='content_sessions' AND id IN (" +
                    ','.join('?' for _ in current) + ')',current)}
                if content_ids:
                    # Watcher and content dispatcher share this SQLite fence.
                    # A changed event-set must never evade an in-flight or
                    # uncertain notice covering the same content commit.
                    outstanding = uow.connection.execute(
                        "SELECT id,event_ids_json,delivery_state FROM inbox_notification_receipts "
                        "WHERE agent_id=? AND consumer_id=? AND generation=? AND delivery_state IN ('queued','uncertain') "
                        "ORDER BY created_at,id",(ctx.principal_id,consumer,owner['generation'])).fetchall()
                    for notice in outstanding:
                        original = json.loads(notice['event_ids_json'])
                        if not content_ids.intersection(original):continue
                        if notice['delivery_state'] == 'uncertain':
                            return {'event_ids':[i for i in original if i in current],
                                'notification_id':notice['id'],'delivery_state':'uncertain'}
                        if set(original) <= set(current):
                            return {'event_ids':original,'notification_id':notice['id'],'delivery_state':'queued'}
                        # No transport has reserved a queued notice yet. An
                        # acknowledged/revoked member makes its set unusable.
                        uow.connection.execute("UPDATE inbox_notification_receipts SET delivery_state='cancelled' WHERE id=? AND delivery_state='queued'",(notice['id'],))
                existing = uow.connection.execute('SELECT id,delivery_state FROM inbox_notification_receipts '
                    'WHERE agent_id=? AND consumer_id=? AND generation=? AND event_ids_json=? ORDER BY created_at DESC LIMIT 1',
                    (ctx.principal_id,consumer,owner['generation'],json.dumps(current))).fetchone()
                if existing:
                    return {**result, 'notification_id':existing['id'], 'delivery_state':existing['delivery_state']}
                identity, now = new_id(), timestamp(self.clock)
                uow.connection.execute('INSERT INTO inbox_notification_receipts'
                    '(id,agent_id,consumer_id,event_ids_json,created_at,generation) VALUES(?,?,?,?,?,?)',
                    (identity,ctx.principal_id,consumer,json.dumps(current),now,owner['generation']))
                cutoff = timestamp(lambda:self.clock()-timedelta(days=7))
                uow.connection.execute("DELETE FROM inbox_notification_receipts WHERE created_at<? "
                    "AND (event_ids_json!='[]' OR delivery_state IN ('submitted','cancelled','failed'))",(cutoff,))
                result['notification_id'] = identity
            return result

    def recovery_notice(self, ctx, consumer, notification_id, backend, *, runtime, legacy=None, adapter=None):
        """One durable wake for a prepared context handoff, using the existing transport.

        Empty event IDs identify this service notice. They never acknowledge inbox
        events or establish context delivery; only the subsequent hook does that.
        """
        from uuid import UUID
        try:
            if str(UUID(notification_id)) != notification_id:raise ValueError
        except (ValueError,TypeError,AttributeError):
            raise DomainError('validation_error','Recovery notification requires its episode UUID')
        with self.app.database.transaction(operation='bootstrap.recovery.notice') as uow:
            ctx = self.watch_context(ctx,uow)
            owner = self.lease(ctx,consumer,uow)
            if owner['desired_state'] != 'running':
                raise DomainError('inbox_watching_stopped','Watching is paused or stopped')
            old = uow.connection.execute('SELECT * FROM inbox_notification_receipts WHERE id=?',(notification_id,)).fetchone()
            if old:
                if (old['agent_id'],old['consumer_id'],old['generation'],old['event_ids_json']) != (
                        ctx.principal_id,consumer,owner['generation'],'[]'):
                    raise DomainError('inbox_generation_stale','Recovery episode belongs to a different binding')
            else:
                uow.connection.execute('INSERT INTO inbox_notification_receipts '
                    '(id,agent_id,consumer_id,event_ids_json,created_at,generation) VALUES(?,?,?,?,?,?)',
                    (notification_id,ctx.principal_id,consumer,'[]',timestamp(self.clock),owner['generation']))
        return self.deliver_notice(ctx,consumer,notification_id,backend,runtime=runtime,legacy=legacy,adapter=adapter)

    def deliver_notice(self, ctx, consumer, notification_id, backend, *, runtime, legacy=None, adapter=None):
        """Durable pre-submit fencing, shared terminal transport, never acknowledge events.

        ``legacy`` is ``(registration_id, observed native owner)`` from the authenticated transport, and is honored
        only for the unfenced generation-0 owner of that registration: the same native identity is verified again
        immediately before submission. It never upgrades or replaces the owner.
        """
        from .watcher import EVENT_NUDGE
        expected = None
        with self.app.database.transaction(operation='inbox.notice.reserve') as uow:
            ctx = self.watch_context(ctx,uow)
            owner = self.lease(ctx,consumer,uow)
            if owner['desired_state'] != 'running':
                raise DomainError('inbox_watching_stopped','Watching is paused or stopped')
            row = uow.connection.execute('SELECT * FROM inbox_notification_receipts WHERE id=? AND agent_id=? AND consumer_id=?',
                (notification_id,ctx.principal_id,consumer)).fetchone()
            if not row or row['generation'] != owner['generation']:
                raise DomainError('inbox_generation_stale','Notice belongs to a stale binding')
            if row['delivery_state'] != 'queued':
                return {'state':row['delivery_state']}
            ids = json.loads(row['event_ids_json'])
            active = [identity for identity in ids if (event := uow.connection.execute(
                'SELECT e.* FROM inbox_events e JOIN inbox_deliveries d ON e.id=d.event_id WHERE d.agent_id=? AND e.id=? AND d.acknowledged_at IS NULL',
                (ctx.principal_id,identity)).fetchone()) and self.event_visible(ctx,event,uow)]
            if active != ids:
                uow.connection.execute("UPDATE inbox_notification_receipts SET delivery_state='cancelled' WHERE id=?",(notification_id,))
                return {'state':'cancelled'}
            profile = dict(uow.connection.execute('SELECT * FROM agent_profiles WHERE agent_id=?',(ctx.principal_id,)).fetchone())
            binding = json.loads(owner['binding_json'])
            if owner['generation'] == 0 and not binding and legacy is not None:
                registration_id, expected = legacy
                if not isinstance(expected, str) or not expected:
                    raise DomainError('inbox_binding_ambiguous','Legacy delivery requires the observed native identity')
                self.legacy_owner(ctx,registration_id,consumer,uow)
                binding = {'runtime':runtime,'registration_id':registration_id,'owner':expected}
            if binding.get('runtime') != runtime:
                raise DomainError('inbox_binding_ambiguous','Notice runtime differs from the native binding')
            uow.connection.execute("UPDATE inbox_notification_receipts SET delivery_state='uncertain' WHERE id=?",(notification_id,))
        text = (f"{EVENT_NUDGE} [Qorqut inbox v3: {notification_id}; events: {','.join(map(str,ids))}]" if ids else
            'The session helper has refreshed your instructions. The trusted hook delivers them with this notice. '
            'Continue the owner\'s authorized work using the hook\'s current status; no startup command or status reply is needed. '
            f'[Qorqut context recovery: {notification_id}]')
        def native_adapter():
            from bazarlyk.bootstrap.native_binding import NativeBindingAdapter
            return adapter or NativeBindingAdapter(backend.runtime)
        def checkpoint():
            with self.app.database.read() as uow:
                current = self.lease(ctx,consumer,uow)
                if current['generation'] != owner['generation']:
                    raise DomainError('inbox_generation_stale','Notice binding changed before submission')
                if current['desired_state'] != 'running':
                    raise DomainError('inbox_watching_stopped','Watching is paused or stopped')
                if expected is not None:
                    self.legacy_owner(ctx,binding['registration_id'],consumer,uow)
                    if current['desired_state'] != 'running':
                        raise DomainError('inbox_watching_stopped','Watching is paused or stopped')
                # Recheck pending events immediately before the serialized paste.
                for identity in ids:
                    event = uow.connection.execute('SELECT e.* FROM inbox_events e JOIN inbox_deliveries d ON e.id=d.event_id '
                        'WHERE d.agent_id=? AND e.id=? AND d.acknowledged_at IS NULL',(ctx.principal_id,identity)).fetchone()
                    if not event or not self.event_visible(ctx,event,uow):
                        raise DomainError('stale_notification','Notice events were handled')
            if expected is not None:
                observed = native_adapter().verify_legacy(ctx.principal_id,binding['registration_id'],consumer)
                if observed['owner'] != expected or observed['runtime'] != runtime:
                    raise DomainError('inbox_generation_stale','Native identity changed before submission')
            elif not ids:
                verified, _ = native_adapter().verify(ctx.principal_id,binding['registration_id'],consumer,previous=binding)
                if verified['owner'] != binding['owner']:
                    raise DomainError('inbox_generation_stale','Native identity changed before recovery notice')
        try:
            if runtime == 'claude-code':
                outcome = backend.service_notice(profile,text,workdir=self.resolver.paths.runtime/'inbox-paste',checkpoint=checkpoint)
                state = 'queued' if outcome.status == 'deferred' else outcome.status
                receipt = {'reason':outcome.reason, **(outcome.receipt or {})}
            else:
                checkpoint()
                if expected is None:
                    verified, _ = native_adapter().verify(ctx.principal_id,binding['registration_id'],consumer,previous=binding)
                    if verified['owner'] != binding['owner']:
                        raise DomainError('inbox_binding_ambiguous','Codex process identity changed')
                _, execution, _ = native_adapter().selected(ctx.principal_id)
                import subprocess
                if execution.target_identity is None:
                    subprocess.run(['codex','queue','--thread',consumer,'--message',text],cwd=profile['workspace'],
                        timeout=1,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                else:
                    execution.broker.queue_notice(consumer,text)
                state, receipt = 'submitted', {}
        except DomainError as error:
            state = 'cancelled' if error.code in {'stale_notification','inbox_generation_stale','inbox_session_superseded'} else 'uncertain'
            receipt = {'reason':error.code}
        except Exception:
            state, receipt = 'uncertain', {'reason':'submission_requires_reconciliation'}
        with self.app.database.transaction(operation='inbox.notice.result') as uow:
            # Even after transfer, keep the old receipt; never change the new lease.
            uow.connection.execute('UPDATE inbox_notification_receipts SET delivery_state=?,delivery_json=? WHERE id=?',
                (state,json.dumps(receipt),notification_id))
            if state == 'submitted':
                uow.connection.executemany('UPDATE inbox_deliveries SET notified_consumer_id=?,notified_at=? WHERE agent_id=? AND event_id=? AND acknowledged_at IS NULL',
                    [(consumer,timestamp(self.clock),ctx.principal_id,identity) for identity in ids])
        return {'state':state,'receipt':receipt}
