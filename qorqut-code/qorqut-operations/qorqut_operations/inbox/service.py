"""Scoped inbox reads, explicit acknowledgements, follows and subscriptions.

Nothing here polls or leases. New work is announced by the native-chat nudge backend, which reads this service's pending view and
authorization; a read or a nudge never acknowledges anything.

The generic algorithms live here: the inbox operations and their schemas, the current-context and subject checks, the visibility
of a subscription and of a delivered event, the pending page and its cursor, the exact acknowledgement rows, the sticky Task and
Action follows and the related-work subscription. Everything that depends on the host (managed workers, private owner channels,
integrated sources, Focus cards, incidents, delegated assignments and the automatic owner-task follows) is a named call on a
required host adapter, made at the point the flow always made it. There is no default adapter, and the selected extensions'
source handlers are an explicit required argument: this module neither imports nor builds a registry of its own."""

import json
from dataclasses import replace
from typing import Protocol

from qorqut_operations.actions import action_key, one_line
from qorqut_operations.actions.writeback import linked_action
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import UUID, PlanBuilder, schema, timestamp, utc_clock
from qorqut_operations.contracts import DomainError, new_id, plain
from qorqut_operations.discussion import ThreadItem, set_follow
from qorqut_operations.tasks.scope import task_context

from .events import row_for, source_summary

# Tasks and Actions are followed through the discussion's follow state (a personal, sticky preference), not through
# a subscription row; every other source keeps its subscription.
THREAD_SOURCES = {'tasks': 'task', 'actions': 'action'}
CONSUMER = {'type': 'string', 'format': 'uuid'}
SCOPE = {'scope': {'enum': ['business', 'global']}}
SOURCE_ID = {'type': 'string', 'minLength': 1, 'maxLength': 200}
# The installation owner or Ordubashy may manage a global worker's subscriptions; omitted, the caller's own.
WORKER = {'agent_id': UUID}


def visible_types(item_types):
    """The source keys whose items an agent may follow or receive, each with its typed reference name."""
    return {**item_types, **THREAD_SOURCES}


class InboxHost(Protocol):
    """What the host supplies to ``InboxService``; each method runs inside the caller's unit of work."""

    def managed_subject(self, ctx, target, uow):
        """The runtime context of the global worker ``target`` that ``ctx`` may manage; refuses a caller who may not."""

    def business_member(self, ctx, business, uow) -> bool:
        """Whether ``ctx`` currently has access to ``business`` (a membership, or an active global's derived access)."""

    def private_visible(self, ctx, source_type, identity, business, uow):
        """Whether the private ``source_type`` item is visible, or None when the host has no such private source."""

    def integration_visible(self, selected, source_type, identity, business, uow):
        """Whether an item of an integrated source is visible (inside the caller's denial handling), or None when it is not one."""

    def event_visible(self, ctx, row, uow):
        """Whether a delivered event of a host-owned source is visible, or None when the host does not own its source."""

    def decorate_before_extensions(self, ctx, row, item, uow) -> None:
        """Add the host's pointer fields to the event ``item`` before the selected extensions decorate it."""

    def decorate_after_extensions(self, ctx, row, item, uow) -> None:
        """Add the host's pointer fields to the event ``item`` after the selected extensions decorated it."""

    def active_event_claim(self, agent_id, event_id, uow) -> bool:
        """Whether a live delegated assignment of ``agent_id`` currently owns ``event_id``."""

    def augment_subscriptions(self, service, ctx, result, uow):
        """``result`` (the listed subscriptions) with the host's automatic follows merged in."""


HOST_METHODS = ('managed_subject', 'business_member', 'private_visible', 'integration_visible', 'event_visible',
                'decorate_before_extensions', 'decorate_after_extensions', 'active_event_claim', 'augment_subscriptions')


class InboxService:
    """``items`` and ``related`` are the host's typed item lookup and related-record lookup; ``sources`` the selected extensions'
    source handlers; ``source_types`` the host's exact ordered core source keys and ``item_types`` its exact ordered
    ``{source key: typed reference}`` map; ``message_factory(service)`` builds the service's direct-message flow."""

    def __init__(self, application, *, items, related, host, sources, source_types, item_types, message_factory, clock=utc_clock):
        missing = [name for name in HOST_METHODS if not callable(getattr(host, name, None))]
        if missing:
            raise TypeError('InboxService needs a host adapter with: ' + ', '.join(missing))
        if not callable(message_factory):
            raise TypeError('InboxService needs a message factory')
        self.app, self.items, self.related, self.clock = application, items, related, clock
        self.host = host
        # The selected extensions' exact-key source handlers; a source without one is unavailable, never visible.
        self.sources = sources
        self.item_types = item_types
        self.subscribable = (*source_types, *sources.subscribable)
        self.visible_types = visible_types(item_types)
        self.messages = message_factory(self)

    def register(self):
        common = dict(supports_signoff=False, scope_kinds=frozenset({'business', 'global'}), global_capability='read_global_context',
            resolve_namespace=lambda ctx, args: 'global' if ctx.business_id is None else 'business:' + ctx.business_id)
        TARGET = {'source_type': {'enum': list(self.subscribable)}, 'source_id': SOURCE_ID}
        operations = {
            'inbox_list': ({'consumer_id': CONSUMER, 'after': {'type': 'integer', 'minimum': 0},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}, (),
                'Read a batch of your pending updates. For a fresh reconciliation omit after (or use 0); use next_cursor as after only to continue that bounded pass. A notification watermark does not prove earlier events were handled. Reading never acknowledges. Your authenticated agent identity selects the inbox; consumer_id is accepted for older clients but is unnecessary.'),
            'inbox_subscriptions_list': (WORKER, (), 'List authorized subscriptions, including automatic owner-task follows for Ordubashy. An ID identifies a removable manual follow; id=null means automatic-only. Removing a manual follow never disables automatic following. The installation owner or Ordubashy may pass agent_id to list a global worker\'s subscriptions, authorized as that worker.'),
            'inbox_acknowledge': ({'consumer_id': CONSUMER, 'event_ids': {'type': 'array', 'minItems': 1, 'maxItems': 100,
                'uniqueItems': True, 'items': {'type': 'integer', 'minimum': 1}}, 'outcome': {'type': 'string', 'maxLength': 2000}},
                ('event_ids',), 'Acknowledge exact handled events; a read or a nudge never clears them.'),
            'inbox_subscribe': ({**TARGET, **WORKER, 'include_related': {'type': 'boolean'}}, ('source_type', 'source_id'),
                'Follow an authorized item. A Task or Action follow is the same preference as tasks_follow/actions_follow and is removed the same way. Only a Task follow can include its directly related work (its child Tasks and the items it is linked to, one hop); that routing lasts while the Task is followed, with no recursive following or time expiry. Remove it with inbox_unsubscribe. The installation owner or Ordubashy may pass agent_id to follow for a global worker; the item must then be readable by that worker.'),
            'inbox_unsubscribe': ({**WORKER, 'subscription_id': {'type': 'string', 'format': 'uuid'},
                'source_type': {'enum': list(THREAD_SOURCES)}, 'source_id': SOURCE_ID}, (),
                'Remove an explicit subscription by subscription_id, or a Task or Action follow by source_type and source_id (the same preference as tasks_follow/actions_follow); automatic owner-task following is independent. The installation owner or Ordubashy may pass agent_id to remove a global worker\'s. Already delivered updates remain pending until acknowledged.'),
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

    def subject(self, ctx, args, uow):
        """The agent whose subscriptions an operation reads or changes: the caller, or the global worker the installation owner or
        Ordubashy names. Sources are then authorized as that worker, never with the manager's own access."""
        target = args.get('agent_id')
        if target is None or (ctx.principal_kind == 'agent' and target == ctx.principal_id):
            return self.context(ctx, uow)
        return self.context(self.host.managed_subject(ctx, target, uow), uow)

    def business_member(self, ctx, business, uow):
        """Current business access, decided by the host at every call."""
        return self.host.business_member(ctx, business, uow)

    def visible(self, ctx, source_type, identity, business, uow):
        """Use current domain authorization; unavailable objects disclose no summary."""
        private = self.host.private_visible(ctx, source_type, identity, business, uow)
        if private is not None:
            return private
        handler = self.sources.get(source_type)
        if handler is not None:
            return bool(handler.subscription_visible and handler.subscription_visible(self, ctx, identity, business, uow))
        if source_type not in self.visible_types: return False
        try:
            selected = ctx.select_business(business)
            if business is not None and not self.host.business_member(ctx, business, uow):
                return False
            if business is None and 'read_global_context' not in selected.global_capabilities:
                return False
            if business is None and source_type == 'tasks':
                selected = task_context(replace(selected, business_id=None), {'scope': 'global'}, uow)
                descriptor = self.app.registry.get('tasks_get')
                self.app.authorize(selected, descriptor, (), uow, namespace='global')
                return bool(uow.repository('tasks', business_id=None).get(identity))
            if source_type in {'principals', 'agent_profiles', 'context_entities'}:
                self.items.resolve(selected,
                    {'type': self.item_types[source_type], 'id': identity}, uow, allow_busy=True)
                return True
            integrated = self.host.integration_visible(selected, source_type, identity, business, uow)
            if integrated is not None:
                return integrated
            self.related.get(selected, uow, 'action_logs' if source_type == 'actions' else source_type, identity, allow_busy=True)
            return True
        except DomainError as error:
            if error.code in {'not_found', 'forbidden', 'policy_denied', 'busy', 'document_unavailable', 'operation_unavailable', 'invalid_assignee'}:
                return False
            raise

    def event_visible(self, ctx, row, uow):
        handler = self.sources.get(row['source_type'])
        if handler is not None and handler.event_visible is not None:
            return bool(handler.event_visible(self, ctx, row, uow))
        owned = self.host.event_visible(ctx, row, uow)
        if owned is not None:
            return owned
        if row["source_type"] == "agent_messages":
            message = uow.connection.execute("SELECT * FROM agent_messages WHERE id=?", (row["source_id"],)).fetchone()
            # Deliveries follow the current recipient; read-only collapsed history never notifies.
            return bool(message and message["current_recipient_id"] == ctx.principal_id
                        and not self.messages.collapsed(message) and self.messages.visible(ctx, message))
        if self.visible(ctx, row['source_type'], row['source_id'], row['business_id'], uow):
            return True
        if row['event_type'] != 'deleted' or row['source_type'] not in {'tasks', 'documents'}:
            return False
        if row['business_id'] is not None and not self.host.business_member(ctx, row['business_id'], uow):
            return False
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
        self.host.decorate_before_extensions(ctx, row, item, uow)
        handler = self.sources.get(row['source_type'])
        if handler is not None and handler.pointer is not None:
            field, decorate = handler.pointer
            value = decorate(ctx, row, uow)
            if value is not None:
                item[field] = value
        self.host.decorate_after_extensions(ctx, row, item, uow)
        return item

    def subscriptions(self, ctx, uow):
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
        return self.host.augment_subscriptions(self, ctx, result, uow)

    def read(self, operation, ctx, args, uow):
        ctx = self.subject(ctx, args, uow)
        if operation == 'inbox_subscriptions_list': return {'items': self.subscriptions(ctx, uow)}
        return self.pending(ctx, uow, after=args.get('after', 0), limit=args.get('limit', 20))

    def validate_mutation(self, operation, ctx, args, uow):
        ctx = self.subject(ctx, args, uow)
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
                if self.host.active_event_claim(ctx.principal_id, identity, uow):
                    raise DomainError('event_owned_by_assignment',
                        'A live delegated assignment owns this event; the parent cannot acknowledge it')
        elif operation == 'inbox_unsubscribe':
            if ('subscription_id' in args) == ('source_type' in args) or ('source_type' in args) != ('source_id' in args):
                raise DomainError('validation_error', 'Name a subscription_id, or a Task or Action source_type with its source_id')
            if 'source_type' in args:
                if not self.visible(ctx, args['source_type'], args['source_id'], ctx.business_id, uow):
                    raise DomainError('not_found', 'Subscription target is unavailable')
            else:
                row = uow.connection.execute('SELECT agent_id FROM inbox_subscriptions WHERE id=?', (args['subscription_id'],)).fetchone()
                if row and row['agent_id'] != ctx.principal_id:
                    raise DomainError('not_found', 'Subscription is unavailable')
        return ctx

    def prepare(self, operation, ctx, args, uow):
        self.validate_mutation(operation, ctx, args, uow)
        # These commands carry exact arguments and use the ordinary operation policy/idempotency contract; the plan belongs
        # to the caller even when a manager names a worker, and application revalidates every ID.
        ctx = self.app.current_context(ctx, uow)
        builder = PlanBuilder(ctx, operation, args, 'Manage inbox', clock=self.clock,
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
            if 'source_type' in args:
                return self.unfollow(ctx, args, uow)
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

    def thread_item(self, ctx, args, uow):
        """The Task or Action discussion a follow names, as this agent's workspace sees it."""
        if args['source_type'] == 'tasks':
            return ThreadItem(ctx.business_id, 'task', args['source_id'])
        action = linked_action(ctx, uow, args['source_id'])
        if action is None:
            raise DomainError('not_found', 'Subscription target is unavailable')
        return ThreadItem(action['business_id'], 'action', action['id'])

    def follow(self, ctx, args, uow):
        """Following a Task or an Action is the discussion's own follow state: personal, explicit and sticky."""
        state = set_follow(uow, self.thread_item(ctx, args, uow), ctx.principal_id, True)
        result = {'following': state['following'], 'source_type': args['source_type'], 'source_id': args['source_id']}
        if args['source_type'] == 'tasks' and 'include_related' in args:
            result['subscription_id'] = self.related_work(ctx, args, bool(args['include_related']), uow)
        return result

    def unfollow(self, ctx, args, uow):
        """The same sticky unfollow as tasks_follow/actions_follow, applied to this agent's own follow."""
        state = set_follow(uow, self.thread_item(ctx, args, uow), ctx.principal_id, False)  # Related-work routing follows the Task follow.
        return {'following': state['following'], 'source_type': args['source_type'], 'source_id': args['source_id']}

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
