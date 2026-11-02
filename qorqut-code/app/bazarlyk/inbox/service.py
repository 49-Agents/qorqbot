"""The host side of the inbox: the adapter Operations' inbox service calls and the current native session owner.

Nothing here polls or leases. New work is announced by the native-chat nudge backend, which reads this service's pending view and
authorization; a read or a nudge never acknowledges anything. The generic reads, acknowledgements, follows and subscriptions are
Operations' ``InboxService``; ``HostInbox`` answers the calls it makes at fixed points (managed workers, private owner channels,
integrated sources, Focus cards, incidents, delegated assignments and the automatic owner-task follows), and this service
keeps the owner inspection and the native session ownership, which are host behavior."""
import json
from dataclasses import replace

from qorqut_operations.contracts import DomainError, Principal, new_id
from qorqut_operations.application.plans import timestamp, utc_clock
from qorqut_operations.inbox import InboxService as OperationsInboxService
from qorqut_operations.inbox.events import source_summary
from bazarlyk.agents.worker_access import require_worker_manager
from bazarlyk.extensions import NO_SOURCES
from .events import ITEM_TYPES
from .messages import Messages

SOURCES = ('tasks', 'actions', 'signoffs', 'documents')


class HostInbox:
    """What Operations' inbox service asks of the host; every method runs inside the caller's unit of work."""

    def __init__(self, inbox):
        self.inbox = inbox

    def managed_subject(self, ctx, target, uow):
        require_worker_manager(ctx, uow, target)
        return self.inbox.resolver.runtime_context(Principal(target, 'agent'), business_id=ctx.business_id)

    def business_member(self, ctx, business, uow):
        """Current business access: a human/system membership, or an active supported global's derived access."""
        from bazarlyk.agents.global_scope import active_business_participant
        return active_business_participant(uow.connection, business, ctx.principal_id)

    def private_visible(self, ctx, source_type, identity, business, uow):
        if source_type != 'owner_conversations':
            return None
        # Private rows are not an inbox item type and cannot be subscribed
        # to; only the configured Ordubashy sees a delivery of the owner
        # channel, and only while the bound source itself is still readable.
        # A retired per-worker row is never visible to anyone.
        from .private_conversations import visible_conversation
        return visible_conversation(self.inbox.app, ctx, identity, uow)

    def integration_visible(self, selected, source_type, identity, business, uow):
        return None

    def event_visible(self, ctx, row, uow):
        app = self.inbox.app
        if row['source_type'] == 'agentic_changes':
            bridge = getattr(self, 'agentic_inbox_bridge', None)
            return bridge is not None and bridge.visible_reference(ctx, row, uow) is not None
        if row['source_type'] == 'operational_assignments':
            if ctx.delegation_assignment_id is not None:
                return False
            assignment = uow.repository('operational_assignments').get(row['source_id'])
            if (assignment is None or assignment['parent_principal_id'] != ctx.principal_id
                    or assignment['business_id'] != row['business_id']):
                return False
            try:
                app.authorize(ctx, app.registry.get('delegated_assignment_get'), (), uow,
                    namespace='global')
                return True
            except DomainError:
                return False
        if row['source_type']=='focus_cards':
            board=getattr(app,'attention',None)
            if board is None: return False
            try:
                selected=board.context(ctx,uow)
                app.authorize(selected,app.registry.get('focus_get'),(),uow,namespace='global')
                return not board.projection(selected,board.row(row['source_id'],uow),uow).get('unavailable')
            except DomainError:
                return False
        if row["source_type"] == "system_incidents":
            from bazarlyk.incidents import IncidentOperations
            try:
                selected=ctx.select_business(row['business_id'])
                descriptor=app.registry.get('system_incidents_agent_get')
                namespace='business:'+row['business_id'] if row['business_id'] else 'global'
                app.authorize(selected,descriptor,(),uow,namespace=namespace)
                return IncidentOperations.visible(selected,row['source_id'],uow)
            except DomainError:
                return False
        if row['source_type'] == 'context_inputs':
            # A Context input notice goes to the registered coordinator alone; the input must still exist.
            installation = uow.repository('installation').get('service')
            return bool(installation and installation['management_coordinator_id'] == ctx.principal_id
                        and uow.connection.execute('SELECT 1 FROM context_inputs WHERE id=?', (row['source_id'],)).fetchone())
        return None

    def decorate_before_extensions(self, ctx, row, item, uow):
        if row['source_type'] == 'agentic_changes':
            bridge = getattr(self, 'agentic_inbox_bridge', None)
            reference = bridge.visible_reference(ctx, row, uow) if bridge is not None else None
            if reference is not None:
                item['agentic_change'] = reference
        if row['source_type'] == 'owner_conversations':
            # Event columns and the bound source only; never a body.
            item['source'] = json.loads(row['visibility_json'] or '{}').get('source')

    def decorate_after_extensions(self, ctx, row, item, uow):
        if row['source_type'] == 'operational_assignments':
            assignment = uow.repository('operational_assignments').get(row['source_id'])
            if assignment is not None and assignment['parent_principal_id'] == ctx.principal_id:
                item['assignment'] = {key: assignment[key] for key in (
                    'id', 'state', 'dispatch_reason', 'next_step', 'wake_at', 'result_json', 'updated_at')}

    def active_event_claim(self, agent_id, event_id, uow):
        row = uow.connection.execute(
            "SELECT c.assignment_id,c.execution_generation,c.lease_until,a.state,a.execution_generation current_generation,"
            "a.lease_until assignment_lease_until FROM operational_event_claims c "
            "JOIN operational_assignments a ON a.id=c.assignment_id "
            "WHERE c.event_id=? AND c.state='claimed'", (event_id,)).fetchone()
        now = timestamp(self.inbox.clock)
        return bool(row and row['state'] == 'running'
            and row['execution_generation'] == row['current_generation']
            and row['lease_until'] is not None and row['lease_until'] > now
            and row['assignment_lease_until'] is not None and row['assignment_lease_until'] > now
            and uow.connection.execute('SELECT parent_principal_id FROM operational_assignments WHERE id=?',
                (row['assignment_id'],)).fetchone()[0] == agent_id)

    def augment_subscriptions(self, service, ctx, result, uow):
        from .owner_tasks import ordubashies, tasks
        manual = {(item['business_id'], item['source_type'], item['source_id']): item for item in result}
        if ctx.principal_id in ordubashies(uow):
            for raw in tasks(uow):
                row = dict(raw)
                if not service.visible(ctx, 'tasks', row['id'], row['business_id'], uow):
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


class InboxService(OperationsInboxService):
    def __init__(self, application, domains, *, resolver=None, clock=utc_clock, sources=NO_SOURCES):
        # Domains absent from a narrow test composition leave the lookups unset; a read that needs one then fails as before.
        self.domains, self.resolver = domains, resolver
        super().__init__(application, items=getattr(domains, 'items', None), related=getattr(domains, 'related', None),
                         host=HostInbox(self), sources=sources, source_types=SOURCES, item_types=ITEM_TYPES,
                         message_factory=Messages, clock=clock)
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

    def pending_owner_conversations(self, ctx, session_id, uow, *, page_size=100):
        """Whether this exact configured coordinator or worker session has visible pending owner deliveries.

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
        if private is None or private.participants(uow)[1] != ctx.principal_id:
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
