"""Isolated owner conversations with Ordubashy and with individual workers.

The default preserves the original owner/Ordubashy source key. Worker threads
bind an exact peer_agent_id and use a separate key, delivery, cursor and history;
the converted Marketing agent keeps its stored keys and history under its UUID.
The optional agent_id/peer_role selector never grants access: every source and
participant is reauthorized. Team notes and private account mail are unchanged.
A worker takes part in an active business through its effective scope, never
a membership row. Business-home identities are historical: their stored channels
are history, never an executable peer.
"""

import json

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, new_id, plain
from qorqut_operations.application.plans import PlanBuilder, schema, timestamp, utc_clock
from qorqut_operations.conversations import SUMMARY, author, normalize_summary, participants
from bazarlyk.agents.global_scope import active_business_participant
from bazarlyk.management.identity import installation_owner, private_participants

from .owner_forwarding import forward_owner_message

AUDIENCE = 'owner_ordubashy'
EVENT_SOURCE = 'owner_conversations'
EVENT_SUMMARY = 'Private message from the owner'
BUSINESS_ONLY = {'email'}
SOURCE = schema({'type': {'enum': ['task', 'signoff', 'email', 'reference']},
                 'id': {'type': 'string', 'minLength': 1, 'maxLength': 200},
                 'business_id': {'type': ['string', 'null']}}, ('type', 'id'))
FIELDS = {'agent_id': {'type': 'string', 'format': 'uuid'}, 'peer_role': {'enum': ['ordubashy', 'global-worker']}, 'scope': {'enum': ['business', 'global']}, 'source': SOURCE}
# Every refusal that means "this coordinator cannot read that source now".
# `context_refresh_required` belongs here: one stale card must not abort the
# whole inbox page that lists it.
UNAVAILABLE = {'forbidden', 'not_found', 'policy_denied', 'validation_error', 'operation_unavailable',
               'management_unavailable', 'busy', 'document_unavailable', 'context_refresh_required'}
EMAIL_READERS = ('email_conversation_get', 'email_conversations_list',
                 'business_email_conversation_get', 'business_email_conversations_list')


def worker_peer(uow, identity):
    """Whether this identity is an active registered worker, the only agent with a private owner channel besides Ordubashy."""
    from bazarlyk.agents.worker_access import is_worker
    return is_worker(uow.connection, identity)


def source_key(kind, business_id, identity, peer_agent_id=None):
    """Stable per-object key. A Reference keeps its card key through edits."""
    base = 'reference:' + identity if kind == 'reference' else 'source:' + (business_id or 'global') + ':' + kind + ':' + identity
    return 'peer:' + peer_agent_id + ':' + base if peer_agent_id else base


def conversation_row(uow, key):
    return uow.connection.execute('SELECT * FROM owner_conversations WHERE source_key=?', (key,)).fetchone()


def message_count(uow, conversation_id):
    return uow.connection.execute('SELECT count(*) FROM owner_conversation_messages WHERE conversation_id=?',
                                  (conversation_id,)).fetchone()[0]


def read_cursor(uow, conversation_id, principal_id):
    row = uow.connection.execute('SELECT last_read_sequence FROM owner_conversation_reads '
        'WHERE conversation_id=? AND principal_id=?', (conversation_id, principal_id)).fetchone()
    return row['last_read_sequence'] if row else 0


def has_unread(uow, conversation_id, last_read_sequence):
    return bool(uow.connection.execute("SELECT 1 FROM owner_conversation_messages WHERE conversation_id=? "
        "AND author_role='coordinator' AND sequence>? LIMIT 1", (conversation_id, last_read_sequence)).fetchone())


EMPTY_STATE = {'private_unread': False, 'private_message_count': 0}


def private_index(ctx, bindings, uow, service=None):
    """Focus counters for many card bindings in one query. Writes nothing.

    Only the installation owner ever sees an unread marker, so the cursor join
    is bound to that one principal; everybody else reads counts alone. Every
    peer channel is counted only while the exact peer/source pair would
    still pass a fresh channel read, so counters never outlive that authority
    and never fall back to the legacy Ordubashy key.
    """
    canonical = {source_key(b['type'], b.get('business_id'), b['id']): b for b in bindings}
    if not canonical:
        return {}
    owner = installation_owner(uow)
    reader = owner if owner and ctx.principal_kind == 'human' and ctx.principal_id == owner else None
    mapping = {}
    # A worker that lost its eligibility must not read the legacy channel's counts instead.
    if reader or ctx.principal_id == private_participants(uow)[1]:
        mapping.update({key: (key, None) for key in canonical})
    peers = [ctx.principal_id] if worker_peer(uow, ctx.principal_id) else []
    if reader:
        # A revoked or inactive peer's channel is unreadable, so it cannot leave
        # an unread marker the owner has no way to clear.
        peers = [r[0] for r in uow.connection.execute(
            "SELECT DISTINCT peer_agent_id FROM owner_conversations WHERE peer_agent_id IS NOT NULL")
            if worker_peer(uow, r[0])]
    for peer in peers:
        mapping.update({'peer:' + peer + ':' + key: (key, peer) for key in canonical})
    if not mapping:
        return {}
    placeholders = ','.join('?' * len(mapping))
    rows = uow.connection.execute(
        'SELECT c.source_key AS key,'
        ' (SELECT count(*) FROM owner_conversation_messages m WHERE m.conversation_id=c.id) AS total,'
        " (SELECT count(*) FROM owner_conversation_messages m WHERE m.conversation_id=c.id"
        "    AND m.author_role='coordinator' AND m.sequence>COALESCE("
        '      (SELECT r.last_read_sequence FROM owner_conversation_reads r'
        '        WHERE r.conversation_id=c.id AND r.principal_id=?),0)) AS unread'
        ' FROM owner_conversations c WHERE c.source_key IN (' + placeholders + ')',
        (reader, *mapping)).fetchall()
    result, available = {}, {}
    for row in rows:
        key, peer = mapping[row['key']]
        if peer is not None:
            if (peer, key) not in available:
                available[(peer, key)] = service is not None and service.channel_available(ctx, canonical[key], peer, uow)
            if not available[(peer, key)]:
                continue
        value = result.setdefault(key, dict(EMPTY_STATE))
        value['private_unread'] |= bool(reader) and bool(row['unread'])
        value['private_message_count'] += row['total']
    return result


def private_state(ctx, binding, uow, service=None):
    """Focus counters for one card binding. Reading them writes nothing."""
    key = source_key(binding['type'], binding.get('business_id'), binding['id'])
    return private_index(ctx, [binding], uow, service).get(key, dict(EMPTY_STATE))


def visible_conversation(app, ctx, conversation_id, uow):
    """Inbox visibility for a private event; unavailable sources disclose nothing."""
    service = getattr(app, 'private_conversations', None)
    return bool(service and service.readable(ctx, conversation_id, uow))


class PrivateConversations:
    def __init__(self, inbox, *, clock=None):
        self.inbox = inbox
        self.app = inbox.app
        self.domains = inbox.domains
        self.clock = clock or getattr(inbox, 'clock', utc_clock)

    def register(self, registry=None):
        registry = registry or self.app.registry
        common = dict(supports_signoff=False, scope_kinds=frozenset({'business', 'global'}),
            global_capability='read_global_context',
            resolve_namespace=lambda c, a: 'global' if a.get('scope') == 'global' else 'business:' + (c.business_id or ''))
        registry.register(OperationDescriptor('owner_conversation_peers', schema(FIELDS, ('source',)),
            **common, owner_only=True, exposure=frozenset({'dashboard', 'worker'}), effect_kind='read',
            read=lambda c, a, o, u: self.peers(c, a, u),
            description='List available private Task peers and unread state for the owner. Does not mark messages read.'))
        registry.register(OperationDescriptor('owner_conversation_get', schema({**FIELDS,
            'before': {'type': 'integer', 'minimum': 1}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}, ('source',)),
            **common, effect_kind='read', read=lambda c, a, o, u: self.read(c, a, u),
            description='Read your private owner conversation on one object. A worker defaults to its own isolated channel. The owner selects a worker by agent_id (peer_role global-worker requires it); omission preserves the Ordubashy channel.'))
        registry.register(OperationDescriptor('owner_conversation_send', schema({**FIELDS,
            'text': {'type': 'string', 'minLength': 1, 'maxLength': 20000}, 'summary': SUMMARY,
            'expected_conversation_revision': {'type': 'integer', 'minimum': 1}}, ('source', 'text')),
            **common, prepare=self.prepare, apply=self.apply, replay_validator=self.validate_replay,
            description='Send one private message to the selected owner–peer channel on this object. It never posts to the team discussion and never changes the source.'))
        registry.register(OperationDescriptor('owner_conversation_mark_read', schema({**FIELDS,
            'sequence': {'type': 'integer', 'minimum': 0}}, ('source', 'sequence')),
            supports_signoff=False, scope_kinds=frozenset({'business', 'global'}),
            global_capability='read_global_context', owner_only=True,
            exposure=frozenset({'dashboard', 'worker'}),
            resolve_namespace=common['resolve_namespace'], prepare=self.prepare_mark, apply=self.apply_mark,
            replay_validator=self.validate_replay,
            description='Record the owner’s private read position up to a sequence the server already returned.'))
        self.app.private_conversations = self
        return self

    # ----- identity and canonical binding -------------------------------------

    def participants(self, uow, ctx=None, args=None):
        owner, coordinator = private_participants(uow)
        args = args or {}
        selected = args.get('agent_id')
        agent = ctx is not None and ctx.principal_kind == 'agent'
        if selected is None and agent and worker_peer(uow, ctx.principal_id):
            selected = ctx.principal_id
        if args.get('peer_role') == 'global-worker' and not selected:
            raise DomainError('validation_error', 'Select one active worker by agent_id')
        selected = selected or coordinator
        # The retained peer_role must agree with the selected UUID; nothing falls back to another channel.
        if args.get('peer_role') == 'ordubashy' and selected != coordinator:
            raise DomainError('forbidden', 'Selected peer does not match this channel')
        if args.get('peer_role') == 'global-worker' and selected == coordinator:
            raise DomainError('forbidden', 'Selected peer does not match this channel')
        if selected != coordinator and not worker_peer(uow, selected):
            raise DomainError('forbidden', 'Select an active worker for this source')
        if agent and ctx.principal_id != selected:
            raise DomainError('forbidden', 'A peer may access only its own private owner conversation')
        return owner, selected

    def role(self, ctx, uow, args=None):
        owner, peer = self.participants(uow, ctx, args)
        if owner and ctx.principal_kind == 'human' and ctx.principal_id == owner:
            return 'owner'
        if peer and ctx.principal_kind == 'agent' and ctx.principal_id == peer:
            return 'coordinator'
        raise DomainError('forbidden', 'Private conversations belong to the owner and the selected agent')

    def peer_binding(self, ctx, args, uow):
        _, peer = self.participants(uow, ctx, args)
        return peer if peer != private_participants(uow)[1] else None

    def key(self, ctx, args, source, uow):
        return source_key(source['type'], source['business_id'], source['id'], self.peer_binding(ctx, args, uow))

    def effective_business(self, ctx, source):
        """Business-scoped calls bind their own business; Global may name one."""
        kind, named = source['type'], source.get('business_id')
        if ctx.business_id is None:
            business = named
        else:
            if 'business_id' in source and named != ctx.business_id:
                raise DomainError('validation_error', 'Source project must match the selected project')
            business = ctx.business_id
        if kind in BUSINESS_ONLY and business is None:
            raise DomainError('validation_error', 'Email threads belong to one project')
        if kind == 'reference' and business is not None:
            raise DomainError('validation_error', 'Focus References are Global; do not name a project')
        return business

    def select(self, ctx, business_id, uow):
        from dataclasses import replace
        selected = ctx.select_business(business_id) if business_id is not None else replace(ctx, business_id=None)
        selected = replace(selected, review_business_id=business_id)
        if business_id is not None and not self.business_participant(ctx, business_id, uow):
            raise DomainError('forbidden', 'Private conversation source is outside an active project membership')
        return self.app.current_context(selected, uow)

    @staticmethod
    def business_participant(ctx, business_id, uow):
        """Read now, in this unit of work: an agent through the effective scope of an active supported global (no
        membership row, no business home, an active business); a person or system principal through its active
        membership of the active business. Reach alone never selects a peer channel."""
        if ctx.principal_kind == 'agent':
            return active_business_participant(uow.connection, business_id, ctx.principal_id)
        return bool(uow.connection.execute(
            'SELECT 1 FROM business_memberships m JOIN businesses b ON b.id=m.business_id '
            'WHERE m.principal_id=? AND m.business_id=? AND m.active=1 AND b.archived_at IS NULL',
            (ctx.principal_id, business_id)).fetchone())

    def namespace(self, ctx):
        return 'business:' + ctx.business_id if ctx.business_id else 'global'

    def source_check(self, selected, kind, identity, uow, role='owner'):
        """Authorize the caller's canonical read of the bound object."""
        if kind == 'task':
            from qorqut_operations.tasks.scope import task_context
            scoped = task_context(selected, {'scope': 'global'} if selected.business_id is None else {}, uow)
            self.app.authorize(scoped, self.app.registry.get('tasks_get'), (), uow, namespace=self.namespace(scoped))
            if not uow.repository('tasks', business_id=scoped.business_id).get(identity):
                raise DomainError('not_found', 'Task does not exist in this scope')
            return
        if kind == 'signoff':
            service = getattr(self.app, 'signoffs', None)
            if service is None:
                raise DomainError('operation_unavailable', 'Signoff review is not installed')
            scoped = service._context(selected, uow)
            self.app.authorize(scoped, self.app.registry.get('signoffs_get'), (), uow, namespace=self.namespace(scoped))
            row = service._row(scoped, identity, uow)
            if row['review_business_id'] != scoped.business_id:
                raise DomainError('not_found', 'Select this signoff’s owning review scope')
            return
        if kind == 'email':
            email = getattr(self.domains, 'email', None)
            if email is None:
                raise DomainError('operation_unavailable', 'Email is not installed')
            self.app.authorize(selected, self.app.registry.get(email.read_operation(selected)), (), uow,
                               namespace=self.namespace(selected))
            if role == 'coordinator':
                self.coordinator_email_access(email, selected, uow)
            else:
                email.discussion_access(selected, uow)
            email.conversation(uow, selected.business_id, identity)
            return
        board = getattr(self.app, 'attention', None)
        if board is None:
            raise DomainError('operation_unavailable', 'Owner attention is not installed')
        self.app.authorize(selected, self.app.registry.get('focus_get'), (), uow, namespace='global')
        row = uow.connection.execute('SELECT source_json,context_json FROM focus_cards WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise DomainError('not_found', 'Focus card does not exist')
        if row['source_json'] is not None or row['context_json'] is not None:
            raise DomainError('validation_error', 'This card is bound to a source; discuss it under that source')

    def coordinator_email_access(self, email, selected, uow):
        """The private coordinator's email read, without the Codex-only gate.

        `EmailOperations.discussion_access` resolves the management coordinator,
        which is registered Codex-only, so the private channel cannot borrow it
        without losing its runtime independence. The same active-membership,
        registered-scope and explicit-deny conditions are checked here instead;
        the shared email authority itself is untouched for its other callers.
        """
        resolver = getattr(email, 'resolver', None)
        if resolver is None:
            raise DomainError('operation_unavailable', 'Email is not installed')
        profile, _ = resolver.profile(selected.principal, connection=uow.connection)
        if not (profile.data['status'] == 'active' and selected.business_id in profile.scope.businesses
                and selected.business_id in selected.allowed_business_ids):
            raise DomainError('forbidden', 'Email is outside this active project membership')
        overrides = profile.data.get('tool_policy', {}).get('overrides', {})
        if any(overrides.get(name) == 'deny' for name in EMAIL_READERS):
            raise DomainError('forbidden', 'Email read access was explicitly denied')

    def resolve(self, ctx, args, uow):
        """One place for the pair ACL, the effective scope and the source read."""
        ctx = self.app.current_context(ctx, uow)
        role = self.role(ctx, uow, args)
        source = plain(args['source'])
        business = self.effective_business(ctx, source)
        selected = self.select(ctx, business, uow)
        self.source_check(selected, source['type'], source['id'], uow, role)
        binding = self.peer_binding(ctx, args, uow)
        if binding:
            peer = self.app.policy.context_for(binding, business_id=business, review_business_id=business, uow=uow)
            self.app.authorize(peer, self.app.registry.get('owner_conversation_get'), (), uow, namespace=self.namespace(peer))
            peer = self.select(peer, business, uow)
            self.source_check(peer, source['type'], source['id'], uow, 'coordinator')
        return role, {'type': source['type'], 'id': source['id'], 'business_id': business}

    def readable(self, ctx, conversation_id, uow):
        """True when the exact selected agent can still read this conversation."""
        row = uow.connection.execute('SELECT * FROM owner_conversations WHERE id=?', (conversation_id,)).fetchone()
        if row is None:
            return False
        _, coordinator = private_participants(uow)
        coordinator = row['peer_agent_id'] or coordinator
        if row['peer_agent_id'] and not worker_peer(uow, coordinator):
            return False
        if not coordinator or ctx.principal_kind != 'agent' or ctx.principal_id != coordinator:
            return False
        try:
            selected = self.select(self.app.current_context(ctx, uow), row['business_id'], uow)
            # Inbox visibility must not promise a read that the effective
            # private-read policy denies. Keep the event's canonical source
            # authorization below as a separate requirement.
            self.app.authorize(selected, self.app.registry.get('owner_conversation_get'), (), uow,
                               namespace=self.namespace(selected))
            self.source_check(selected, row['source_type'], row['source_id'], uow, 'coordinator')
            return True
        except DomainError as error:
            if error.code in UNAVAILABLE:
                return False
            raise

    # ----- operations ---------------------------------------------------------

    def view(self, row, source, uow, peer=None):
        bound = row['peer_agent_id'] if row is not None else peer
        audience = 'owner_agent' if bound else AUDIENCE
        return {'id': row['id'] if row else None, 'source': source, 'audience': audience,
                'agent_id': row['peer_agent_id'] if row is not None else peer,
                'revision': row['revision'] if row else 0,
                'latest_sequence': row['latest_sequence'] if row else 0,
                'message_count': message_count(uow, row['id']) if row else 0}

    def channel_available(self, ctx, source, peer, uow):
        """True while this caller could read the exact peer channel for the source right now."""
        try:
            self.resolve(ctx, {'source': source, 'agent_id': peer}, uow)
        except DomainError as error:
            if error.code in UNAVAILABLE:
                return False
            raise
        return True

    def peers(self, ctx, args, uow):
        owner, coordinator = private_participants(uow)
        if ctx.principal_kind != 'human' or ctx.principal_id != owner:
            raise DomainError('forbidden', 'Only the owner can list private peers')
        source = dict(args['source'])
        business = self.effective_business(ctx, source)
        source['business_id'] = business
        selected = self.select(ctx, business, uow)
        self.source_check(selected, source['type'], source['id'], uow)
        result = []
        for row in uow.connection.execute("SELECT agent_id,profile_json FROM agent_profiles WHERE status='active'"):
            identity = row['agent_id']
            if identity != coordinator and not worker_peer(uow, identity):
                continue
            if not self.channel_available(ctx, source, identity, uow):
                continue
            profile = json.loads(row['profile_json'])
            stored = conversation_row(uow, source_key(source['type'], business, source['id'], None if identity == coordinator else identity))
            result.append({'agent_id': identity, 'name': profile.get('display_name') or profile.get('name') or identity,
                'role': profile['role'], 'channel': 'private' if identity == coordinator else 'worker',
                'unread': bool(stored and has_unread(uow,stored['id'],read_cursor(uow,stored['id'],owner)))})
        return {'peers': result}

    def read(self, ctx, args, uow):
        role, source = self.resolve(ctx, args, uow)
        row = conversation_row(uow, self.key(ctx, args, source, uow))
        limit, messages, more = args.get('limit', 20), [], False
        if row is not None:
            page = [dict(item) for item in uow.connection.execute(
                'SELECT * FROM owner_conversation_messages WHERE conversation_id=? AND sequence<? '
                'ORDER BY sequence DESC LIMIT ?',
                (row['id'], args.get('before', 9223372036854775807), limit + 1))]
            more = len(page) > limit
            messages = list(reversed(page[:limit]))
            for message in messages:
                message.pop('conversation_id', None)
                author(message, uow)
        owner, coordinator = self.participants(uow, ctx, args)
        state = None
        if role == 'owner':
            cursor = read_cursor(uow, row['id'], owner) if row is not None else 0
            state = {'last_read_sequence': cursor,
                     'unread': has_unread(uow, row['id'], cursor) if row is not None else False}
        return {'conversation': self.view(row, source, uow, self.peer_binding(ctx, args, uow)), 'messages': messages,
                'next_before': messages[0]['sequence'] if more and messages else None,
                'participants': participants(uow, {owner, coordinator}), 'read_state': state}

    def plan(self, ctx, operation, args, title, peer=None):
        return PlanBuilder(ctx, operation, args, title, namespace=self.namespace(ctx),
            affected_scope={**({'kind': 'business', 'business_id': ctx.business_id} if ctx.business_id
                else {'kind': 'global'}), 'conversation_peer_id': peer}).build()

    def prepare(self, ctx, args, options, uow):
        self.resolve(ctx, args, uow)
        if not isinstance(args['text'], str) or not args['text'].strip():
            raise DomainError('validation_error', 'A private message must contain 1–20000 characters and cannot be blank')
        normalize_summary(args.get('summary'))
        _, peer = self.participants(uow, ctx, args)
        return self.plan(ctx, 'owner_conversation_send', args, 'Private message', peer)

    def validate_peer(self, ctx, args, plan, uow):
        _, peer = self.participants(uow, ctx, args)
        if peer != plan.affected_scope.get('conversation_peer_id'):
            raise DomainError('revision_conflict', 'Conversation peer changed; read the channel again')

    def validate_replay(self, ctx, args, saved, uow):
        """Re-authorize a saved private receipt and confirm it still belongs to the exact selected channel.

        The same pair, peer and canonical-source checks as a fresh read run first, so a revoked capability or a
        denied source never discloses the saved private message. A role-only selector that now resolves to a
        different Marketing identity, or a receipt that predates the peer binding, is refused rather than retargeted.
        """
        _, source = self.resolve(ctx, args, uow)
        peer = self.peer_binding(ctx, args, uow)
        binding = (saved.data or {}).get('conversation')
        if binding is None:
            # Only legacy owner-Ordubashy receipts predate the binding.
            if peer is not None:
                raise DomainError('revision_conflict', 'Conversation peer changed; read the channel again')
            return
        row = conversation_row(uow, self.key(ctx, args, source, uow))
        if binding.get('agent_id') != peer or (binding.get('id') is not None and (row is None or row['id'] != binding['id'])):
            raise DomainError('revision_conflict', 'Conversation peer changed; read the channel again')

    def apply(self, ctx, plan, uow):
        args = plain(plan.arguments)
        role, source = self.resolve(ctx, args, uow)
        self.validate_peer(ctx, args, plan, uow)
        key = self.key(ctx, args, source, uow)
        peer = self.peer_binding(ctx, args, uow)
        now = timestamp(self.clock)
        # Concurrent first sends converge on the one row this key already owns.
        uow.connection.execute('INSERT OR IGNORE INTO owner_conversations'
            '(id,source_key,business_id,source_type,source_id,audience,revision,latest_sequence,created_at,updated_at,peer_agent_id)'
            ' VALUES(?,?,?,?,?,?,1,0,?,?,?)',
            (new_id(), key, source['business_id'], source['type'], source['id'], AUDIENCE, now, now, peer))
        row = conversation_row(uow, key)
        expected = args.get('expected_conversation_revision')
        if expected is not None and expected != row['revision']:
            raise DomainError('revision_conflict', 'Private conversation changed; read it again',
                              current_revision=row['revision'])
        message = {'id': new_id(), 'conversation_id': row['id'], 'created_by': ctx.principal_id,
                   'author_role': role, 'body': args['text'],
                   'summary': normalize_summary(args.get('summary')), 'created_at': now}
        columns = ('id', 'conversation_id', 'created_by', 'author_role', 'body', 'summary', 'created_at')
        cursor = uow.connection.execute('INSERT INTO owner_conversation_messages'
            '(' + ','.join(columns) + ') VALUES(' + ','.join('?' * len(columns)) + ')',
            tuple(message[name] for name in columns))
        sequence = cursor.lastrowid
        uow.connection.execute('UPDATE owner_conversations SET revision=revision+1,latest_sequence=?,updated_at=? '
                               'WHERE id=?', (sequence, now, row['id']))
        if role == 'owner':
            self.deliver(ctx, row, source, sequence, now, uow)
            if peer:
                forward_owner_message(uow, kind='private_conversation', source_id=message['id'], sender_id=ctx.principal_id,
                                      worker_id=peer, body=args['text'], accepted_at=now)
        message.pop('conversation_id')
        return {'message': author({**message, 'sequence': sequence}, uow),
                'conversation': {'id': row['id'], 'agent_id': peer, 'revision': row['revision'] + 1,
                                 'latest_sequence': sequence, 'message_count': message_count(uow, row['id'])}}

    def deliver(self, ctx, row, source, sequence, now, uow):
        """One delivery to the coordinator. No body, summary or source title."""
        _, coordinator = private_participants(uow)
        coordinator = row['peer_agent_id'] or coordinator
        if not coordinator:
            return
        event = uow.connection.execute('INSERT INTO inbox_events'
            '(business_id,source_type,source_id,source_revision,actor_id,event_type,summary,visibility_json,created_at)'
            ' VALUES(?,?,?,?,?,?,?,?,?)',
            (source['business_id'], EVENT_SOURCE, row['id'], row['revision'] + 1, ctx.principal_id,
             'private_message', EVENT_SUMMARY, json.dumps({'source': source}), now)).lastrowid
        uow.connection.execute('INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)', (coordinator, event))

    def prepare_mark(self, ctx, args, options, uow):
        role, _ = self.resolve(ctx, args, uow)
        if role != 'owner':
            raise DomainError('forbidden', 'Only the installation owner records the private read position')
        _, peer = self.participants(uow, ctx, args)
        return self.plan(ctx, 'owner_conversation_mark_read', args, 'Private read position', peer)

    def apply_mark(self, ctx, plan, uow):
        args = plain(plan.arguments)
        role, source = self.resolve(ctx, args, uow)
        self.validate_peer(ctx, args, plan, uow)
        if role != 'owner':
            raise DomainError('forbidden', 'Only the installation owner records the private read position')
        row = conversation_row(uow, self.key(ctx, args, source, uow))
        peer = self.peer_binding(ctx, args, uow)
        # The receipt binds the exact channel it marked, so a replay can be checked against the current selection.
        binding = {'id': row['id'] if row else None, 'agent_id': peer}
        if row is None:
            return {'read_state': {'last_read_sequence': 0, 'unread': False,
                                   'any_unread': private_state(ctx, source, uow, self)['private_unread']}, 'conversation': binding}
        if args['sequence'] > row['latest_sequence']:
            raise DomainError('validation_error', 'Acknowledge only a sequence this conversation has reached')
        existing = uow.connection.execute('SELECT last_read_sequence FROM owner_conversation_reads '
            'WHERE conversation_id=? AND principal_id=?', (row['id'], ctx.principal_id)).fetchone()
        value = max(existing['last_read_sequence'] if existing else 0, args['sequence'])
        if existing is None:
            if value > 0:
                uow.connection.execute('INSERT INTO owner_conversation_reads'
                    '(conversation_id,principal_id,last_read_sequence,updated_at) VALUES(?,?,?,?)',
                    (row['id'], ctx.principal_id, value, timestamp(self.clock)))
        elif value != existing['last_read_sequence']:
            uow.connection.execute('UPDATE owner_conversation_reads SET last_read_sequence=?,updated_at=? '
                'WHERE conversation_id=? AND principal_id=?',
                (value, timestamp(self.clock), row['id'], ctx.principal_id))
        return {'read_state': {'last_read_sequence': value, 'unread': has_unread(uow, row['id'], value),
                               'any_unread': private_state(ctx, source, uow, self)['private_unread']}, 'conversation': binding}
