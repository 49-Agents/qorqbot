"""Direct, scoped communication; messages never assign work or grant authority.

The generic message flow lives here: validation order, the durable ``inbox_events``/``agent_messages``/``inbox_deliveries``
writes, history pagination and the message view. Everything that depends on the host (owner-directed contacts, delegated
worker bindings and their attribution, forwarding to the owner, terminal delivery, the retirement handoff read) is a
named call on a required host adapter, made at the point the flow always made it. There is no default adapter."""

from qorqut_agentic.behavior.tool_help import text as instruction_text

import json
from dataclasses import replace
from typing import Protocol

from qorqut_operations.conversations import SUMMARY, normalize_summary
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, new_id, plain
from qorqut_operations.application.plans import PlanBuilder, schema, timestamp
from qorqut_operations.discussion.threads import RELAY, attribution, read_attribution, save_attribution

UUID = {'type': 'string', 'format': 'uuid'}
PAGE = {'after': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}


class MessageHost(Protocol):
    """What the host supplies to ``Messages``; each method runs inside the caller's unit of work."""

    def owner_directed_target(self, ctx, identity, uow) -> bool:
        """Whether ``identity`` is directed to the owner and so unavailable as a message peer."""

    def binding(self, ctx, uow):
        """The caller's current delegated assignment (an opaque row), or None when it has none; refuses a stale binding."""

    def validate_recipient(self, ctx, recipient, parent, binding, uow) -> None:
        """Refuse a recipient or reply thread outside ``binding``; a no-op when ``binding`` is None."""

    def attribution(self, ctx, parent, uow):
        """``(assignment_id, generation, binding_id, plan_revision)`` stored on the new message."""

    def history_scope(self, ctx, thread_id, uow):
        """``(extra_clauses, parameters)`` that narrow the caller's history; empty when nothing narrows it."""

    def forward_owner_message(self, uow, *, kind, source_id, sender_id, worker_id, body, accepted_at) -> None:
        """Forward an accepted message to its owner conversation in the same unit of work."""

    def retirement_backlog(self, messages, ctx, args, uow):
        """The page returned for a ``retirement_backlog`` read."""

    def validate_terminal(self, ctx, recipient, message, uow) -> None:
        """Refuse a terminal delivery that cannot be made."""

    def queue_terminal(self, ctx, row, uow) -> None:
        """Queue the terminal delivery of the stored message ``row``."""

    def terminal_projection(self, row, uow):
        """The ``terminal_delivery`` mapping of ``row``, or None when it has none."""


HOST_METHODS = ('owner_directed_target', 'binding', 'validate_recipient', 'attribution', 'history_scope', 'forward_owner_message',
                'retirement_backlog', 'validate_terminal', 'queue_terminal', 'terminal_projection')


class Messages:
    def __init__(self, inbox, *, host):
        missing = [name for name in HOST_METHODS if not callable(getattr(host, name, None))]
        if missing:
            raise TypeError('Messages need a host adapter with: ' + ', '.join(missing))
        self.inbox, self.app, self.host = inbox, inbox.app, host

    def register(self, common):
        self.app.registry.register(OperationDescriptor('agents_message_send', schema({
            'scope': {'enum': ['business', 'global']}, 'to_agent_id': UUID, 'reply_to': UUID, 'summary': SUMMARY,
            'delivery': {'enum': ['inbox', 'terminal'], 'description': instruction_text('messages-default-inbox-terminal-targeted-retired-project-dev-agents')},
            'message': {'type': 'string', 'minLength': 1, 'maxLength': 16000}, 'relay': RELAY}, ('message',)),
            **common, description=instruction_text('messages-send-a-durable-internal-message-to-a-scoped'),
            prepare=self.prepare, apply=self.apply))
        self.app.registry.register(OperationDescriptor('agents_messages_list', schema({
            'scope': {'enum': ['business', 'global']}, 'thread_id': UUID, 'counterpart_id': UUID,
            'retirement_backlog': {'type': 'boolean'}, **PAGE}),
            **common, effect_kind='read', description=instruction_text('messages-read-your-sent-and-received-message-history-in'), read=self.history))
        self.app.registry.register(OperationDescriptor('agents_message_recipients', schema({
            'scope': {'enum': ['business', 'global']}, 'all_businesses': {'type': 'boolean'},
            'role_id': {'type': 'string', 'minLength': 1, 'maxLength': 100}}), **common, effect_kind='read',
            description=instruction_text('messages-find-active-agents-in-the-selected-scope-or'), read=self.recipients))

    @staticmethod
    def scope_visible(ctx, business):
        return business in ctx.allowed_business_ids if business else 'read_global_context' in ctx.global_capabilities

    def visible(self, ctx, row):
        return ctx.principal_id in (row['current_sender_id'], row['current_recipient_id']) and self.scope_visible(ctx, row['business_id'])

    @staticmethod
    def collapsed(row):
        """Two original participants now one principal: read-only history, never a reply or delivery.
        Self-reminders (one original participant) stay ordinary messages."""
        return row['current_sender_id'] == row['current_recipient_id'] and row['sender_id'] != row['recipient_id']

    @staticmethod
    def counterpart(row, principal_id):
        pair = (row['current_sender_id'], row['current_recipient_id'])
        if pair[0] == pair[1] or principal_id not in pair:
            return None
        return pair[1] if pair[0] == principal_id else pair[0]

    def peer(self, ctx, identity, uow, *, allow_owner=False):
        if self.host.owner_directed_target(ctx, identity, uow):
            raise DomainError('not_found', 'Recipient is owner-directed and unavailable for agent messages, including replies')
        try:
            peer = self.app.policy.context_for(identity, business_id=ctx.business_id, review_business_id=None, uow=uow)
            if not self.scope_visible(peer, ctx.business_id):
                raise DomainError('not_found', 'Recipient is unavailable in this scope')
            if peer.principal_kind == 'agent':
                return peer
            if allow_owner and peer.principal_kind == 'human':
                installation = uow.repository('installation').get('service')
                member = uow.repository('business_memberships', business_id=ctx.business_id).get(identity) if ctx.business_id else None
                if installation and installation['owner_principal_id'] == identity or member and member['is_owner'] and member['active']:
                    return peer
        except DomainError:
            pass
        raise DomainError('not_found', 'Recipient is unavailable in this scope')

    def validate(self, ctx, args, uow):
        ctx = self.app.current_context(ctx, uow)
        if ctx.principal_kind not in {'agent', 'human'}:
            raise DomainError('forbidden', 'Messages require an agent or owner identity')
        normalize_summary(args.get('summary'))
        if not args['message'].strip():
            raise DomainError('validation_error', 'Message cannot be blank')
        binding = self.host.binding(ctx, uow)
        parent = None
        if args.get('reply_to'):
            parent = uow.connection.execute('SELECT * FROM agent_messages WHERE id=?', (args['reply_to'],)).fetchone()
            if not parent or parent['business_id'] != ctx.business_id or not self.visible(ctx, parent):
                raise DomainError('not_found', 'Reply target is unavailable')
            if self.collapsed(parent):
                raise DomainError('invalid_transition', 'This message is read-only history; send a new message instead')
            recipient = self.counterpart(parent, ctx.principal_id)
            if args.get('to_agent_id', recipient) != recipient:
                raise DomainError('validation_error', 'A reply must keep the current participants')
        else:
            recipient = args.get('to_agent_id')
        if not recipient or recipient == ctx.principal_id:
            raise DomainError('validation_error', 'Choose another agent or provide a reply target')
        self.host.validate_recipient(ctx, recipient, parent, binding, uow)
        peer = self.peer(ctx, recipient, uow, allow_owner=bool(parent))
        if args.get('delivery') == 'terminal':
            self.host.validate_terminal(ctx, recipient, args['message'], uow)
        return ctx, peer, parent

    def prepare(self, ctx, args, options, uow):
        ctx, peer, parent = self.validate(ctx, args, uow)
        attribution(ctx, uow, args.get('relay'))
        return PlanBuilder(ctx, 'agents_message_send', args, 'Send internal message', clock=self.inbox.clock,
            namespace='business:' + ctx.business_id if ctx.business_id else 'global',
            affected_scope={'kind': 'business', 'business_id': ctx.business_id} if ctx.business_id else {'kind': 'global'}).build()

    def apply(self, ctx, plan, uow):
        args = plain(plan.arguments)
        ctx, peer, parent = self.validate(ctx, args, uow)
        identity, now = new_id(), timestamp(self.inbox.clock)
        save_attribution(ctx, uow, 'agent_message', identity, args.get('relay'))
        event_id = uow.connection.execute(
            'INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) VALUES(?,?,?,?,?,?,?)',
            (ctx.business_id, 'agent_messages', identity, ctx.principal_id, 'direct_message', args['message'][:160], now)).lastrowid
        assignment_id, generation, binding_id, plan_revision = self.host.attribution(ctx, parent, uow)
        uow.connection.execute('INSERT INTO agent_messages(id,event_id,business_id,sender_id,recipient_id,reply_to,thread_id,body,created_at,summary,delegation_assignment_id,delegation_generation,delegation_binding_id,delegation_plan_revision,current_sender_id,current_recipient_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (identity, event_id, ctx.business_id, ctx.principal_id, peer.principal_id, args.get('reply_to'), parent['thread_id'] if parent else identity, args['message'], now, normalize_summary(args.get('summary')), assignment_id, generation, binding_id, plan_revision, ctx.principal_id, peer.principal_id))
        if peer.principal_kind == 'agent':
            uow.connection.execute('INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)', (peer.principal_id, event_id))
            self.host.forward_owner_message(uow, kind='agent_message', source_id=identity, sender_id=ctx.principal_id,
                                            worker_id=peer.principal_id, body=args['message'], accepted_at=now)
        row = uow.connection.execute('SELECT * FROM agent_messages WHERE id=?', (identity,)).fetchone()
        if args.get('delivery') == 'terminal':
            self.host.queue_terminal(ctx, row, uow)
        return {'message': self.view(row, uow, ctx.principal_id)}

    def view(self, row, uow, viewer=None):
        """Original names attribute authorship; current participants route replies and delivery."""
        result = dict(row)
        result.update(read_attribution(uow, 'agent_message', row['id'], row['sender_id']))
        from qorqut_operations.conversations import author
        result.update(author({'created_by': row['sender_id'], 'author_id': result['author_id'],
                              'recorder_id': row['sender_id']}, uow))
        for field in ('delegation_assignment_id', 'delegation_generation',
                      'delegation_binding_id', 'delegation_plan_revision'):
            result.pop(field, None)
        if row['delegation_assignment_id'] is not None:
            result['delegation_attribution'] = {
                'assignment_id': row['delegation_assignment_id'],
                'generation': row['delegation_generation'],
                'binding_id': row['delegation_binding_id'],
                'plan_revision': row['delegation_plan_revision'],
                'worker_authored': row['delegation_binding_id'] is not None,
            }
        for side in ('sender', 'recipient'):
            principal = uow.repository('principals').get(row[side + '_id'])
            result[side + '_name'] = principal['display_name'] if principal else 'Unavailable'
            result[side + '_kind'] = principal['kind'] if principal else None
            current = row['current_' + side + '_id']
            principal = principal if current == row[side + '_id'] else uow.repository('principals').get(current)
            result['current_' + side + '_name'] = principal['display_name'] if principal else 'Unavailable'
        result['counterpart_id'] = self.counterpart(row, viewer)
        delivery = uow.connection.execute('SELECT acknowledged_at,outcome FROM inbox_deliveries WHERE agent_id=? AND event_id=?',
            (row['current_recipient_id'], row['event_id'])).fetchone()
        result.update(acknowledged_at=delivery['acknowledged_at'] if delivery else None, outcome=delivery['outcome'] if delivery else None)
        terminal = self.host.terminal_projection(row, uow)
        if terminal is not None:
            result['terminal_delivery'] = terminal
        return result

    def history(self, ctx, args, options, uow):
        ctx = self.app.current_context(ctx, uow)
        if args.get('retirement_backlog'):
            return self.host.retirement_backlog(self, ctx, args, uow)
        limit, me, thread_id = args.get('limit', 20), ctx.principal_id, args.get('thread_id')
        clauses = ['business_id IS ?', '(current_sender_id=? OR current_recipient_id=?)', 'event_id>?']
        params = [ctx.business_id, me, me, args.get('after', 0)]
        extra_clauses, extra_params = self.host.history_scope(ctx, thread_id, uow)
        clauses += extra_clauses
        params += extra_params
        if thread_id:
            clauses.append('thread_id=?'); params.append(thread_id)
        if args.get('counterpart_id'):
            clauses.append('((current_sender_id=? AND current_recipient_id=?) OR (current_recipient_id=? AND current_sender_id=?))')
            params += [me, args['counterpart_id']] * 2
        rows = uow.connection.execute('SELECT * FROM agent_messages WHERE ' + ' AND '.join(clauses) + ' ORDER BY event_id LIMIT ?',
            (*params, limit + 1)).fetchall()
        return {'items': [self.view(row, uow, me) for row in rows[:limit]], 'next_cursor': rows[limit - 1]['event_id'] if len(rows) > limit else None}

    def recipients(self, ctx, args, options, uow):
        ctx = self.app.current_context(ctx, uow)
        result = []
        targets = sorted(ctx.allowed_business_ids) if args.get('all_businesses') else [ctx.business_id]
        businesses = []
        for business_id in targets:
            selected = replace(ctx, business_id=business_id)
            business = uow.repository('businesses').get(business_id) if business_id else None
            if business and business['archived_at'] is not None:
                continue
            slug = business['slug'] if business else None
            businesses.append({'business_id': business_id, 'business_slug': slug})
            for row in uow.connection.execute("SELECT agent_id,name,profile_json FROM agent_profiles WHERE status='active' AND agent_id!=? ORDER BY name,agent_id", (ctx.principal_id,)):
                role = json.loads(row['profile_json']).get('role', {}).get('id')
                if args.get('role_id') and role != args['role_id']:
                    continue
                try:
                    peer = self.peer(selected, row['agent_id'], uow)
                except DomainError:
                    continue
                result.append({'agent_id': peer.principal_id, 'name': row['name'], 'display_name': peer.principal.display_name,
                    'role_id': role, 'business_id': business_id, 'business_slug': slug})
        return {'items': result, 'businesses': businesses}
