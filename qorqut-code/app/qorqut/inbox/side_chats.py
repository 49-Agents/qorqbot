"""Owner-created Signoff shells using the ordinary private conversation and inbox.

Pending means open; cancelled means closed. Neither replies nor agent decisions can
close a side chat. The first message and its inbox delivery commit with the shell.
"""
from dataclasses import replace
import json

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PlanBuilder, schema
from qorqut_operations.contracts import DomainError, InvocationOptions, new_id, plain
from .private_conversations import UNAVAILABLE, has_unread, read_cursor


class SideChats:
    def __init__(self, runtime):
        self.app = runtime.application
        self.signoffs = runtime.signoffs
        self.conversations = runtime.private_conversations

    def register(self):
        common = dict(supports_signoff=False, owner_only=True, exposure=frozenset({'dashboard', 'worker'}),
            scope_kinds=frozenset({'business', 'global'}), global_capability='read_global_context',
            resolve_namespace=lambda c, a: 'global' if a.get('scope') == 'global' else 'business:' + (c.business_id or ''))
        for name, fields, required, description in (
            ('side_chats_create', {'text': {'type': 'string', 'minLength': 1, 'maxLength': 20000, 'pattern': r'\S'},
                'title': {'type': 'string', 'maxLength': 200}}, ('text',),
                'Open a side chat with the owner’s first private message to Ordubashy. A title is optional.'),
            ('side_chats_rename', {'signoff_id': {'type': 'string', 'format': 'uuid'},
                'title': {'type': 'string', 'minLength': 1, 'maxLength': 200, 'pattern': r'\S'}}, ('signoff_id', 'title'),
                'Owner only: rename a side chat while retaining its conversation history.'),
            ('side_chats_close', {'signoff_id': {'type': 'string', 'format': 'uuid'}}, ('signoff_id',),
                'Owner only: close a side chat while retaining its conversation history.')):
            self.app.registry.register(OperationDescriptor(name, schema({
                'scope': {'enum': ['business', 'global']}, **fields}, required), **common,
                description=description, prepare=lambda c, a, o, u, n=name: self.prepare(n, c, a, u),
                apply=self.apply, replay_validator=self.replay))
        return self

    def owner(self, ctx, uow):
        ctx = self.signoffs._context(ctx, uow, owner=True)
        if self.conversations.role(ctx, uow) != 'owner':
            raise DomainError('forbidden', 'Only the installation owner may create, rename or close side chats')
        return ctx

    def target(self, ctx, identity, uow):
        row = self.signoffs._row(ctx, identity, uow)
        if row['category'] != 'side_chat':
            raise DomainError('invalid_transition', 'Only side chats may be changed here')
        return row

    def prepare(self, name, ctx, args, uow):
        if args.get('scope') == 'global':
            ctx = replace(ctx, business_id=None, review_business_id=None)
        ctx = self.owner(ctx, uow)
        _, peer = self.conversations.participants(uow)
        scope = {'kind': 'business', 'business_id': ctx.business_id} if ctx.business_id else {'kind': 'global'}
        builder = PlanBuilder(ctx, name, args, 'Side chat', affected_scope={**scope, 'conversation_peer_id': peer})
        if name == 'side_chats_create':
            if not args['text'].strip():
                raise DomainError('validation_error', 'Write a message to start the side chat')
            builder.record('signoffs', new_id(), None, {'title': args.get('title', '').strip() or 'Side chat'})
        else:
            row = self.target(ctx, args['signoff_id'], uow)
            if name == 'side_chats_rename':
                title = args['title'].strip()
                if not title:
                    raise DomainError('validation_error', 'Enter a side chat name')
                values = {'display_title': title}
            else:
                values = {'state': 'cancelled'}
            builder.record('signoffs', row['id'], row, values, business_id=row['review_business_id'])
        return builder.build()

    def apply(self, ctx, plan, uow):
        change = plan.changes[0]
        ctx = self.owner(replace(ctx, business_id=change.resource.business_id,
                                 review_business_id=change.resource.business_id), uow)
        identity = change.resource.resource_id
        if plan.operation_name in {'side_chats_rename', 'side_chats_close'}:
            row = self.target(ctx, identity, uow)
            values = plain(change.values)
            if any(row[key] != value for key, value in values.items()):
                row = self.signoffs._update(row, values, uow)
        else:
            self.conversations.validate_peer(ctx, {}, plan, uow)
            # Shared Signoff metadata contains no copy of the private first message.
            binding = plain(plan.policy_binding)
            if ctx.business_id is None:
                # Owner-only writes bypass the policy resolver, but this inert shell
                # still uses the descriptor's checked capability for Signoff visibility.
                binding['operations'][plan.operation_name]['binding']['global_capability'] = 'read_global_context'
            stored = replace(plan, arguments={'title': change.values['title']}, policy_binding=binding)
            row = uow.repository('signoffs', business_id=ctx.business_id).insert({
                'id': identity, 'category': 'side_chat',
                'review_scope_kind': 'business' if ctx.business_id else 'global',
                'proposer_principal_id': ctx.principal_id, 'proposer_agent_id': None,
                'initiated_by_principal_id': ctx.principal_id,
                'operation_name': plan.operation_name, 'operation_version': 1, 'proposal_revision': 1,
                'payload_hash': stored.payload_hash, 'change_set': stored.canonical,
                'preview_json': {'title': change.values['title']}, 'expected_targets_json': [],
                'policy_binding_json': binding, 'affected_scope_json': plain(plan.affected_scope),
                'state': 'pending', 'updated_sequence': uow.next_sequence('signoffs'), 'created_at': self.signoffs.clock()})
            args = {'source': {'type': 'signoff', 'id': identity, 'business_id': ctx.business_id},
                    'text': plan.arguments['text']}
            self.app.authorize(ctx, self.app.registry.get('owner_conversation_send'), (), uow,
                               namespace=plan.target_namespace)
            message = self.conversations.prepare(ctx, args, InvocationOptions(), uow)
            self.conversations.apply(ctx, message, uow)
        return {'signoff': self.signoffs._dto(row, uow)}

    def replay(self, ctx, args, saved, uow):
        ctx = self.owner(ctx, uow)
        row = self.target(ctx, saved.data['signoff']['id'], uow)
        self.conversations.resolve(ctx, {'source': {'type': 'signoff', 'id': row['id'],
                                    'business_id': row['review_business_id']}}, uow)

    def active(self, ctx, uow):
        ctx = self.owner(ctx, uow)
        clause, params = self.signoffs._visibility(ctx, uow)
        rows = uow.connection.execute(f"""SELECT s.id,s.review_business_id,s.preview_json,s.display_title,b.slug AS business_slug,
            c.id AS conversation_id,c.latest_sequence,c.updated_at,m.body
            FROM signoffs s JOIN owner_conversations c ON c.source_type='signoff'
                AND c.source_id=s.id AND c.business_id IS s.review_business_id AND c.peer_agent_id IS NULL
            JOIN owner_conversation_messages m ON m.conversation_id=c.id AND m.sequence=c.latest_sequence
            LEFT JOIN businesses b ON b.id=s.review_business_id
            WHERE {clause} AND s.category='side_chat' AND s.state='pending'
            ORDER BY c.latest_sequence DESC""", params).fetchall()
        items = []
        for row in rows:
            source = {'type': 'signoff', 'id': row['id'], 'business_id': row['review_business_id']}
            try:
                self.conversations.resolve(ctx, {'source': source}, uow)
            except DomainError as error:
                if error.code in UNAVAILABLE:
                    continue
                raise
            items.append({'id': row['id'], 'source': source, 'title': row['display_title'] or json.loads(row['preview_json'])['title'],
                'business_slug': row['business_slug'],
                'preview': row['body'][:180], 'updated_at': row['updated_at'], 'latest_sequence': row['latest_sequence'],
                'unread': has_unread(uow, row['conversation_id'], read_cursor(uow, row['conversation_id'], ctx.principal_id))})
        return items
