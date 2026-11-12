"""The private owner conversation with Ordubashy on one Task, Signoff, Table (general or CRM) or Focus Reference.

Each source has one private channel: the installation owner and the registered Ordubashy, resolved from the stored
identity (``private_participants``), never from a display name. The channel keeps the original owner/Ordubashy source key
(a null ``peer_agent_id``) and its history. The optional agent_id/peer_role fields are not selectors: they may restate
that same Ordubashy and every other value is refused, so no request chooses a worker, a default, or another channel.

A row bound to any other ``peer_agent_id`` belongs to a retired per-worker channel. It is never readable, countable,
deliverable, notified or replayed; every projection below reads the null-peer channel alone, so such a row can neither be
disclosed nor leave an unread marker nobody can clear. The separately invoked maintenance in
``qorqut.maintenance.private_peer_cleanup`` is the only thing that removes those rows. Team notes are unchanged.
"""

from qorqut_agentic.behavior.tool_help import text as instruction_text

import json

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, new_id, plain
from qorqut_operations.application.plans import PlanBuilder, schema, timestamp, utc_clock
from qorqut_operations.conversations import SUMMARY, author, normalize_summary, participants
from qorqut_operations.discussion.threads import RELAY, attribution, read_attribution, save_attribution
from qorqut.agents.global_scope import active_business_participant
from qorqut.management.identity import installation_owner, private_participants

AUDIENCE = 'owner_ordubashy'
EVENT_SOURCE = 'owner_conversations'
EVENT_SUMMARY = 'Private message from the owner'
SOURCE = schema({'type': {'enum': ['task', 'signoff', 'reference', 'project', 'table']},
                 'id': {'type': 'string', 'minLength': 1, 'maxLength': 200},
                 'business_id': {'type': ['string', 'null']}}, ('type', 'id'))
# `agent_id` and `peer_role` only restate the one Ordubashy channel; any other value is refused (see `participants`).
FIELDS = {'agent_id': {'type': 'string', 'format': 'uuid'}, 'peer_role': {'enum': ['ordubashy']}, 'scope': {'enum': ['business', 'global']}, 'source': SOURCE}
# Every refusal that means "this coordinator cannot read that source now".
# `context_refresh_required` belongs here: one stale card must not abort the
# whole inbox page that lists it. `record_moved` is a Task that left this scope: its old-scope channel is unavailable
# there (a direct read still answers it), so a directory, inbox page or notification skips it instead of failing.
UNAVAILABLE = {'forbidden', 'not_found', 'policy_denied', 'validation_error', 'operation_unavailable',
               'management_unavailable', 'busy', 'document_unavailable', 'context_refresh_required', 'record_moved'}


def source_key(kind, business_id, identity):
    """Stable per-object key of the one owner–Ordubashy channel. A Reference keeps its card key through edits."""
    return 'reference:' + identity if kind == 'reference' else 'source:' + (business_id or 'global') + ':' + kind + ':' + identity


def conversation_row(uow, key):
    """The Ordubashy channel of one source. A row bound to another peer is retired history and never returned."""
    return uow.connection.execute('SELECT * FROM owner_conversations WHERE source_key=? AND peer_agent_id IS NULL',
                                  (key,)).fetchone()


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


def private_index(ctx, bindings, uow):
    """Focus counters for many card bindings in one query. Writes nothing.

    Only the installation owner ever sees an unread marker, so the cursor join is bound to that one principal; the
    registered Ordubashy reads counts alone and every other caller reads nothing. Only the null-peer Ordubashy channel is
    counted: a retired per-worker row never contributes a count or an unread marker.
    """
    canonical = {source_key(b['type'], b.get('business_id'), b['id']) for b in bindings}
    if not canonical:
        return {}
    owner = installation_owner(uow)
    reader = owner if owner and ctx.principal_kind == 'human' and ctx.principal_id == owner else None
    coordinator = private_participants(uow)[1]
    if not reader and not (coordinator and ctx.principal_kind == 'agent' and ctx.principal_id == coordinator):
        return {}
    placeholders = ','.join('?' * len(canonical))
    rows = uow.connection.execute(
        'SELECT c.source_key AS key,'
        ' (SELECT count(*) FROM owner_conversation_messages m WHERE m.conversation_id=c.id) AS total,'
        " (SELECT count(*) FROM owner_conversation_messages m WHERE m.conversation_id=c.id"
        "    AND m.author_role='coordinator' AND m.sequence>COALESCE("
        '      (SELECT r.last_read_sequence FROM owner_conversation_reads r'
        '        WHERE r.conversation_id=c.id AND r.principal_id=?),0)) AS unread'
        ' FROM owner_conversations c WHERE c.peer_agent_id IS NULL AND c.source_key IN (' + placeholders + ')',
        (reader, *canonical)).fetchall()
    return {row['key']: {'private_unread': bool(reader) and bool(row['unread']), 'private_message_count': row['total']}
            for row in rows}


def private_state(ctx, binding, uow):
    """Focus counters for one card binding. Reading them writes nothing."""
    key = source_key(binding['type'], binding.get('business_id'), binding['id'])
    return private_index(ctx, [binding], uow).get(key, dict(EMPTY_STATE))


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
            description=instruction_text('private-conversations-show-the-owner-s-one-private-channel')))
        registry.register(OperationDescriptor('owner_conversation_get', schema({**FIELDS,
            'before': {'type': 'integer', 'minimum': 1}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}, ('source',)),
            **common, effect_kind='read', read=lambda c, a, o, u: self.read(c, a, u),
            description=instruction_text('private-conversations-read-the-private-owner-ordubashy-conversation-on')))
        registry.register(OperationDescriptor('owner_conversation_send', schema({**FIELDS,
            'text': {'type': 'string', 'minLength': 1, 'maxLength': 20000}, 'summary': SUMMARY, 'relay': RELAY,
            'expected_conversation_revision': {'type': 'integer', 'minimum': 1}}, ('source', 'text')),
            **common, prepare=self.prepare, apply=self.apply, replay_validator=self.validate_replay,
            description=instruction_text('private-conversations-send-one-private-message-to-the-owner')))
        registry.register(OperationDescriptor('owner_conversation_mark_read', schema({**FIELDS,
            'sequence': {'type': 'integer', 'minimum': 0}}, ('source', 'sequence')),
            supports_signoff=False, scope_kinds=frozenset({'business', 'global'}),
            global_capability='read_global_context', owner_only=True,
            exposure=frozenset({'dashboard', 'worker'}),
            resolve_namespace=common['resolve_namespace'], prepare=self.prepare_mark, apply=self.apply_mark,
            replay_validator=self.validate_replay,
            description=instruction_text('private-conversations-record-the-owner-s-private-read-position')))
        from .thread_views import directory, related
        registry.register(OperationDescriptor('owner_conversations_list', schema({
            'scope': {'const': 'global'}, 'before': {'type': 'integer', 'minimum': 1},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}),
            supports_signoff=False, scope_kinds=frozenset({'global'}), global_capability='read_global_context',
            resolve_namespace=lambda c, a: 'global', owner_only=True, exposure=frozenset({'dashboard', 'worker'}),
            effect_kind='read', read=lambda c, a, o, u: directory(self, c, a, u),
            description=instruction_text('private-conversations-list-readable-owner-ordubashy-threads-by-their')))
        registry.register(OperationDescriptor('owner_conversation_related', schema({**FIELDS,
            'offset': {'type': 'integer', 'minimum': 0},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
            'notes_audience': {'enum': ['owner', 'all']}}, ('source',)),
            **common, owner_only=True, exposure=frozenset({'dashboard', 'worker'}), effect_kind='read',
            read=lambda c, a, o, u: related(self, c, a, u),
            description=instruction_text('private-conversations-read-related-records-for-visual-thread-context')))
        self.app.private_conversations = self
        return self

    # ----- identity and canonical binding -------------------------------------

    def participants(self, uow, ctx=None, args=None):
        """The exact (owner, Ordubashy) pair. A request may restate Ordubashy and nothing else.

        Neither the caller nor a default chooses the peer: an explicit agent_id other than the registered Ordubashy is
        refused, and so is any other caller than the owner or Ordubashy (see `role`)."""
        owner, coordinator = private_participants(uow)
        args = args or {}
        selected = args.get('agent_id')
        if (selected is not None and selected != coordinator) or args.get('peer_role', 'ordubashy') != 'ordubashy':
            raise DomainError('forbidden', 'The only private owner channel is with Ordubashy')
        return owner, coordinator

    def role(self, ctx, uow, args=None):
        owner, coordinator = self.participants(uow, ctx, args)
        if owner and ctx.principal_kind == 'human' and ctx.principal_id == owner:
            return 'owner'
        if coordinator and ctx.principal_kind == 'agent' and ctx.principal_id == coordinator:
            return 'coordinator'
        raise DomainError('forbidden', 'Private conversations belong to the owner and Ordubashy')

    def key(self, source):
        return source_key(source['type'], source['business_id'], source['id'])

    def effective_business(self, ctx, source):
        """Business-scoped calls bind their own business; Global may name one."""
        kind, named = source['type'], source.get('business_id')
        if ctx.business_id is None:
            business = named
        else:
            if 'business_id' in source and named != ctx.business_id:
                raise DomainError('validation_error', 'Source project must match the selected project')
            business = ctx.business_id
        if kind == 'reference' and business is not None:
            raise DomainError('validation_error', 'Focus References are Global; do not name a project')
        if kind == 'project' and (named is None or named != source['id']):
            # The canonical Project reference names the actual Project UUID as its own scope; a missing/null scope is
            # never inferred from the route.
            raise DomainError('validation_error', 'A project source must name the project as its own scope')
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

    def source_check(self, selected, kind, identity, uow):
        """Authorize the caller's canonical read of the bound object."""
        if kind == 'task':
            from qorqut_operations.tasks.scope import task_context
            scoped = task_context(selected, {'scope': 'global'} if selected.business_id is None else {}, uow)
            self.app.authorize(scoped, self.app.registry.get('tasks_get'), (), uow, namespace=self.namespace(scoped))
            if not uow.repository('tasks', business_id=scoped.business_id).get(identity):
                # Like every other Task read, a Task that moved answers record_moved for a caller who may read it where it is now.
                raise self.domains.tasks.missing(scoped, uow, identity)
            return
        if kind == 'table':
            # A general and a CRM Table are one source kind; the private channel never reads or posts to the team thread.
            from qorqut_operations.tables.operations import table_context
            scoped = table_context(selected, {'scope': 'global'} if selected.business_id is None else {}, uow)
            self.app.authorize(scoped, self.app.registry.get('tables_get'), (), uow, namespace=self.namespace(scoped))
            if not uow.repository('data_tables', business_id=scoped.business_id).get(identity):
                raise DomainError('not_found', 'Table does not exist in this scope')
            return
        if kind == 'project':
            if identity != selected.business_id:
                raise DomainError('validation_error', 'A project source must name the project as its own scope')
            from qorqut.domains.projects import access as project_access
            project_access.guard(selected, self.app, uow, project_id=identity, operation='project_record_get')
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
        if kind != 'reference':
            raise DomainError('validation_error', 'This conversation source is no longer available')
        board = getattr(self.app, 'attention', None)
        if board is None:
            raise DomainError('operation_unavailable', 'Owner attention is not installed')
        self.app.authorize(selected, self.app.registry.get('focus_get'), (), uow, namespace='global')
        row = uow.connection.execute('SELECT source_json,context_json FROM focus_cards WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise DomainError('not_found', 'Focus card does not exist')
        if row['source_json'] is not None or row['context_json'] is not None:
            raise DomainError('validation_error', 'This card is bound to a source; discuss it under that source')

    def resolve(self, ctx, args, uow):
        """One place for the pair ACL, the effective scope and the source read."""
        ctx = self.app.current_context(ctx, uow)
        role = self.role(ctx, uow, args)
        source = plain(args['source'])
        business = self.effective_business(ctx, source)
        selected = self.select(ctx, business, uow)
        self.source_check(selected, source['type'], source['id'], uow)
        return role, {'type': source['type'], 'id': source['id'], 'business_id': business}

    def readable(self, ctx, conversation_id, uow):
        """True when Ordubashy can still read this Ordubashy-channel conversation; a retired peer's is never readable."""
        row = uow.connection.execute('SELECT * FROM owner_conversations WHERE id=? AND peer_agent_id IS NULL',
                                     (conversation_id,)).fetchone()
        if row is None:
            return False
        _, coordinator = private_participants(uow)
        if not coordinator or ctx.principal_kind != 'agent' or ctx.principal_id != coordinator:
            return False
        try:
            selected = self.select(self.app.current_context(ctx, uow), row['business_id'], uow)
            # Inbox visibility must not promise a read that the effective
            # private-read policy denies. Keep the event's canonical source
            # authorization below as a separate requirement.
            self.app.authorize(selected, self.app.registry.get('owner_conversation_get'), (), uow,
                               namespace=self.namespace(selected))
            self.source_check(selected, row['source_type'], row['source_id'], uow)
            return True
        except DomainError as error:
            if error.code in UNAVAILABLE:
                return False
            raise

    # ----- operations ---------------------------------------------------------

    def view(self, row, source, uow):
        return {'id': row['id'] if row else None, 'source': source, 'audience': AUDIENCE, 'agent_id': None,
                'revision': row['revision'] if row else 0,
                'latest_sequence': row['latest_sequence'] if row else 0,
                'message_count': message_count(uow, row['id']) if row else 0}

    def channel_available(self, ctx, source, uow):
        """True while this caller could read the Ordubashy channel for the source right now."""
        try:
            self.resolve(ctx, {'source': source}, uow)
        except DomainError as error:
            if error.code in UNAVAILABLE:
                return False
            raise
        return True

    def peers(self, ctx, args, uow):
        """The owner's one private channel for the source: Ordubashy's, while it can be read and the source is visible."""
        owner, coordinator = self.participants(uow, ctx, args)
        if ctx.principal_kind != 'human' or ctx.principal_id != owner:
            raise DomainError('forbidden', 'Only the owner can list the private channel')
        source = dict(args['source'])
        business = self.effective_business(ctx, source)
        source['business_id'] = business
        selected = self.select(ctx, business, uow)
        self.source_check(selected, source['type'], source['id'], uow)
        if not coordinator or not self.channel_available(ctx, source, uow):
            return {'peers': []}
        row = uow.connection.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=? AND status='active'",
                                     (coordinator,)).fetchone()
        if row is None:
            return {'peers': []}
        profile = json.loads(row['profile_json'])
        stored = conversation_row(uow, source_key(source['type'], business, source['id']))
        return {'peers': [{'agent_id': coordinator, 'name': profile.get('display_name') or profile.get('name') or coordinator,
                           'role': profile['role'], 'channel': 'private',
                           'unread': bool(stored and has_unread(uow, stored['id'], read_cursor(uow, stored['id'], owner)))}]}

    def read(self, ctx, args, uow):
        role, source = self.resolve(ctx, args, uow)
        row = conversation_row(uow, self.key(source))
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
                message.update(read_attribution(uow, 'owner_conversation', message['id'], message['created_by']))
                author(message, uow)
        owner, coordinator = self.participants(uow, ctx, args)
        state = None
        if role == 'owner':
            cursor = read_cursor(uow, row['id'], owner) if row is not None else 0
            state = {'last_read_sequence': cursor,
                     'unread': has_unread(uow, row['id'], cursor) if row is not None else False}
        return {'conversation': self.view(row, source, uow), 'messages': messages,
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
        attribution(ctx, uow, args.get('relay'))
        _, coordinator = self.participants(uow, ctx, args)
        return self.plan(ctx, 'owner_conversation_send', args, 'Private message', coordinator)

    def validate_peer(self, ctx, args, plan, uow):
        """The Ordubashy a request was prepared for must still be the registered one when it applies."""
        _, coordinator = self.participants(uow, ctx, args)
        if coordinator != plan.affected_scope.get('conversation_peer_id'):
            raise DomainError('revision_conflict', 'Conversation peer changed; read the channel again')

    def validate_replay(self, ctx, args, saved, uow):
        """Re-authorize a saved private receipt and confirm it still belongs to the Ordubashy channel.

        The same pair and canonical-source checks as a fresh read run first, so a revoked capability, a denied source or a
        request that selected any peer but Ordubashy (a retired per-worker channel included) never discloses the saved
        private message. A receipt bound to another peer or conversation is refused rather than retargeted.
        """
        _, source = self.resolve(ctx, args, uow)
        binding = (saved.data or {}).get('conversation')
        if binding is None:
            return                      # Only legacy owner–Ordubashy receipts predate the binding.
        row = conversation_row(uow, self.key(source))
        if binding.get('agent_id') is not None or (binding.get('id') is not None and (row is None or row['id'] != binding['id'])):
            raise DomainError('revision_conflict', 'Conversation peer changed; read the channel again')

    def apply(self, ctx, plan, uow):
        args = plain(plan.arguments)
        role, source = self.resolve(ctx, args, uow)
        self.validate_peer(ctx, args, plan, uow)
        key = self.key(source)
        now = timestamp(self.clock)
        # Concurrent first sends converge on the one row this key already owns.
        uow.connection.execute('INSERT OR IGNORE INTO owner_conversations'
            '(id,source_key,business_id,source_type,source_id,audience,revision,latest_sequence,created_at,updated_at,peer_agent_id)'
            ' VALUES(?,?,?,?,?,?,1,0,?,?,NULL)',
            (new_id(), key, source['business_id'], source['type'], source['id'], AUDIENCE, now, now))
        row = conversation_row(uow, key)
        if row is None:
            raise DomainError('revision_conflict', 'Private conversation is bound to another channel; read it again')
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
        save_attribution(ctx, uow, 'owner_conversation', message['id'], args.get('relay'))
        message.update(read_attribution(uow, 'owner_conversation', message['id'], ctx.principal_id))
        uow.connection.execute('UPDATE owner_conversations SET revision=revision+1,latest_sequence=?,updated_at=? '
                               'WHERE id=?', (sequence, now, row['id']))
        _, coordinator = private_participants(uow)
        if coordinator:
            from qorqut.agents.attention_delivery import record_attention_activity
            record_attention_activity(uow, coordinator, 'owner_message' if role == 'owner' else 'agent_activity',
                                      now, 'owner-conversation:' + message['id'],
                                      attention=self.app.ordubashy_attention)
        if role == 'owner':
            self.deliver(ctx, row, source, sequence, now, uow)
        message.pop('conversation_id')
        return {'message': author({**message, 'sequence': sequence}, uow),
                'conversation': {'id': row['id'], 'agent_id': None, 'revision': row['revision'] + 1,
                                 'latest_sequence': sequence, 'message_count': message_count(uow, row['id'])}}

    def deliver(self, ctx, row, source, sequence, now, uow):
        """One delivery to the coordinator. No body, summary or source title."""
        _, coordinator = private_participants(uow)
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
        _, coordinator = self.participants(uow, ctx, args)
        return self.plan(ctx, 'owner_conversation_mark_read', args, 'Private read position', coordinator)

    def apply_mark(self, ctx, plan, uow):
        args = plain(plan.arguments)
        role, source = self.resolve(ctx, args, uow)
        self.validate_peer(ctx, args, plan, uow)
        if role != 'owner':
            raise DomainError('forbidden', 'Only the installation owner records the private read position')
        row = conversation_row(uow, self.key(source))
        # The receipt binds the exact channel it marked, so a replay can be checked against the current selection.
        binding = {'id': row['id'] if row else None, 'agent_id': None}
        if row is None:
            return {'read_state': {'last_read_sequence': 0, 'unread': False,
                                   'any_unread': private_state(ctx, source, uow)['private_unread']}, 'conversation': binding}
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
                               'any_unread': private_state(ctx, source, uow)['private_unread']}, 'conversation': binding}
