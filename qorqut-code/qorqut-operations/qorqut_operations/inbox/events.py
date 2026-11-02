"""Turn transaction-local trigger captures into immutable, addressed events.

Triggers cover SQL and repository writers alike. Finalization runs before commit; an exception rolls back the source write and
its notifications together. ``EventFinalizer`` is that one finalizer and holds no state between calls: the host builds it for
each unit of work from the selected source handlers, its item vocabulary, the captures that only feed the dashboard and a
routing adapter.

Tasks and Actions are not announced here: their one discussion writer is this package's ``discussion`` module, which writes the
thread entry, the follow state and the event with its required/optional deliveries in the applying transaction. The captures
cover every other retained item, and an edge that touches a Task or an Action is announced by the operation that wrote it.

Everything that depends on the host (Focus cards, the coordinator and active-global eligibility) is a
named call on the required ``EventRouting`` adapter, made where the flow always made it. There is no default adapter."""

import json
from typing import Protocol

from qorqut_operations.contracts import utc_now
from qorqut_operations.discussion.follows import ThreadItem, followers as follower_ids

from .related import routed

IGNORED_FIELDS = {'revision', 'status_revision', 'sequence'}


def assigned(uow, business, kind, identity):
    return {r[0] for r in uow.connection.execute(
        "SELECT to_id FROM relationships WHERE business_id IS ? AND from_type=? AND from_id=? "
        "AND to_type='agent' AND role='assigned_to'", (business, kind, identity))}


def row_for(uow, table, business, identity, sources=None):
    """The canonical record of a source key; ``sources`` are the selected extensions' handlers, none when omitted."""
    handler = sources.get(table) if sources is not None else None
    if handler is not None:
        return handler.row(uow, business, identity) if handler.row else None
    if table == 'agent_profiles':
        row = uow.connection.execute('SELECT *,agent_id AS id,home_business_id AS business_id FROM agent_profiles WHERE home_business_id IS ? AND agent_id=?', (business, identity)).fetchone()
        return dict(row) if row else None
    if table == 'principals':
        row = uow.connection.execute("SELECT p.*,m.business_id FROM principals p JOIN business_memberships m ON m.principal_id=p.id WHERE p.kind='human' AND m.business_id IS ? AND p.id=?", (business, identity)).fetchone()
        return dict(row) if row else None
    scope = 'review_business_id' if table == 'signoffs' else 'business_id'
    row = uow.connection.execute(f'SELECT * FROM {table} WHERE {scope} IS ? AND id=?', (business, identity)).fetchone()
    return dict(row) if row else None


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def source_summary(record, table):
    record = record or {}
    metadata = _json(record.get('metadata_json')) or {}
    preview = _json(record.get('preview_json')) or {}
    profile = _json(record.get('profile_json')) or {}
    return str(record.get('title') or record.get('subject') or record.get('summary') or record.get('label')
               or record.get('display_name') or profile.get('display_name') or metadata.get('title')
               or metadata.get('name') or metadata.get('filename') or preview.get('title')
               or record.get('path') or table.replace('_', ' ').rstrip('s'))[:500]


class EventRouting(Protocol):
    """What the host supplies to ``EventFinalizer``; each method runs inside the finalizing unit of work."""

    def begin_batch(self, uow):
        """Read the batch-wide facts once, right after the captures are read; the result is passed back opaquely."""

    def coalesced_creation(self, capture) -> bool:
        """Whether ``capture`` is the insert of a record whose later assignment edges its creation event already covers."""

    def responsible(self, uow, capture) -> bool:
        """Whether the change summons the record's assignees; True when the capture is not one that can decline to."""

    def early_targets(self, uow, capture, data):
        """The targets of a capture the host alone routes (an empty list is handled), or None to continue."""

    def additional_targets(self, uow, capture, data, business, actor):
        """The targets of a host-routed capture after the generic ones, or None when the host has none for it."""

    def terminal_signoff(self, data, before, after, terminal) -> bool:
        """``terminal`` unchanged, or False for a Signoff state that only queues its execution."""

    def augment_direct(self, uow, capture, business, record, direct, responsible) -> None:
        """Add to the target's direct recipients ``direct`` (a set) in place."""

    def presentation(self, capture) -> bool:
        """Whether the capture is a presentation change: it keeps its own group and never joins a note group."""

    def augment_recipients(self, uow, business, table, identity, change, recipients, batch):
        """Add the host's recipients of a group to ``recipients`` in place; the result is passed to ``recipient_allowed``."""

    def recipient_allowed(self, uow, agent, business, table, guard) -> bool:
        """Whether the host delivers an event of ``table`` in ``business`` to ``agent`` now."""

    def presentation_summary(self, record):
        """The event summary the host composes for ``record``, or None for the generic one."""


ROUTING_METHODS = ('begin_batch', 'coalesced_creation', 'responsible', 'early_targets', 'additional_targets', 'terminal_signoff',
                   'augment_direct', 'presentation', 'augment_recipients', 'recipient_allowed', 'presentation_summary')


class EventFinalizer:
    """``sources`` are the selected extensions' source handlers, ``item_types`` the host's exact ordered ``{source: typed
    reference}`` map, ``feed_only`` the sources whose one writer is the discussion engine, and ``host`` the routing adapter."""

    def __init__(self, *, sources, item_types, feed_only, host):
        missing = [name for name in ROUTING_METHODS if not callable(getattr(host, name, None))]
        if missing:
            raise TypeError('EventFinalizer needs a host routing adapter with: ' + ', '.join(missing))
        self.sources, self.item_types, self.feed_only, self.host = sources, item_types, feed_only, host
        # The typed reference names resolve back to a source key; documents are typed by their kind instead.
        self.resource_types = {v: k for k, v in item_types.items() if k != 'documents'}

    def item_type_for(self, table):
        """The typed reference name of a source key: a core table, or a selected extension's exact registered key."""
        return self.item_types.get(table) or self.sources.item_types.get(table)

    def targets(self, uow, capture):
        """Return canonical changed records, without persisting private note bodies."""
        table, action = capture['source_type'], capture['action']
        before = _json(capture['before_json']) or {}
        after = _json(capture['after_json']) or {}
        data = after or before
        if table in self.feed_only:
            return []  # The engine's one discussion writer announces these records; the capture only feeds the dashboard.
        if table == 'documents' and data.get('kind') == 'working_memory':
            # Own working state is not a shared Material or an inbox subscription.
            return []
        early = self.host.early_targets(uow, capture, data)
        if early is not None:
            return early
        business = data.get('business_id', data.get('review_business_id'))
        actor = capture['actor_id']
        if action == 'update' and all(before.get(k) == after.get(k) for k in set(before) | set(after) if k not in IGNORED_FIELDS):
            return []
        handler = self.sources.get(table)
        if handler is not None and handler.targets is not None:
            return handler.targets(uow, data, business, actor)
        if table == 'agent_profiles':
            data = {**data, 'id': data['agent_id'], 'business_id': data['home_business_id']}
        if table == 'principals':
            if data['kind'] != 'human': return []
            return [('principals', {**data, 'business_id': row[0]}, actor, 'updated', set())
                    for row in uow.connection.execute('SELECT business_id FROM business_memberships WHERE principal_id=?', (data['id'],))]
        if table == 'business_memberships':
            record = row_for(uow, 'principals', business, data['principal_id'])
            return [('principals', record, actor, 'membership_changed', set())] if record else []
        if table == 'data_rows':
            record = row_for(uow, 'data_tables', business, data['table_id'])
            return [('data_tables', record, actor, 'rows_changed', set())] if record else []
        if table == 'crm_entries':
            # A CRM entry is the CRM table's row: its membership, stage and local cells are announced as one data change.
            record = row_for(uow, 'data_tables', business, data['table_id'])
            return [('data_tables', record, actor, 'rows_changed', set())] if record else []
        if table == 'relationships':
            result = []
            resource_types = {**self.resource_types, **{kind: source for source, kind in self.sources.item_types.items()}}
            for edge in (before, after):
                if not edge: continue
                for prefix in ('from', 'to'):
                    kind = edge[prefix + '_type']
                    source_type = resource_types.get(kind)
                    if source_type:
                        record = row_for(uow, source_type, business, edge[prefix + '_id'], self.sources)
                        if record:
                            result.append((source_type, record, actor or edge.get('created_by'), 'relationship_changed', set()))
                    if kind in {'topic', 'person', 'material'} and not (source_type and record):
                        record = row_for(uow, 'documents', business, edge[prefix + '_id'])
                        if record:
                            result.append(('documents', record, actor or edge.get('created_by'), 'relationship_changed', set()))
                if edge['role'] == 'assigned_to' and edge['to_type'] == 'agent':
                    source_type = resource_types.get(edge['from_type'])
                    if source_type:
                        record = row_for(uow, source_type, business, edge['from_id'], self.sources)
                        if record:
                            result.append((source_type, record, actor or edge.get('created_by'), 'assignment_changed', {edge['to_id']}))
            return result
        extra = self.host.additional_targets(uow, capture, data, business, actor)
        if extra is not None:
            return extra
        if table == 'signoff_attention':
            row = uow.connection.execute('SELECT * FROM signoffs WHERE id=?', (data['signoff_id'],)).fetchone()
            return [('signoffs', dict(row), actor, 'attention_changed', set())] if row else []
        if table == 'documents' and after.get('availability') not in (None, 'ready'):
            return []  # File effects are announced only after durable finalization.
        if (table == 'documents' and before.get('availability') not in (None, 'ready')
                and (business, data['id']) not in uow.completed_file_documents):
            return []  # Releasing an unchanged failed receipt is not a content edit.
        recipients = set()
        event_type = {'insert': 'created', 'update': 'updated', 'delete': 'deleted'}[action]
        if table == 'signoffs':
            decision_changed = after.get('decision_json') != before.get('decision_json') and after.get('decision_json') is not None
            terminal = after.get('state') in {'applied', 'failed', 'conflicted'} and before.get('state') != after.get('state')
            terminal = self.host.terminal_signoff(data, before, after, terminal)
            execution = _json(after.get('execution_json')) or {}
            previous = _json(before.get('execution_json')) or {}
            terminal_execution = execution.get('state') in {'completed', 'failed', 'canceled', 'reconciliation_required'} and execution.get('state') != previous.get('state')
            if decision_changed or terminal or terminal_execution:
                if data.get('proposer_agent_id'): recipients.add(data['proposer_agent_id'])
                event_type = 'response_submitted' if decision_changed else 'action_outcome'
                if decision_changed:
                    actor = (_json(after['decision_json']) or {}).get('by')
                else:
                    # The worker executes under the proposer's authority, but its
                    # completion is a system result the proposer must receive.
                    actor = None
        return [(table, data, actor, event_type, recipients)]

    def finalize(self, uow):
        captures = uow.connection.execute('SELECT * FROM inbox_changes ORDER BY id').fetchall()
        if not captures: return
        batch = self.host.begin_batch(uow)
        grouped = {}
        noted_signoffs = {json.loads(c['after_json'])['signoff_id'] for c in captures if c['source_type'] == 'signoff_notes' and c['after_json']}
        created = {(c['source_type'], (_json(c['after_json']) or {}).get('business_id'),
                    (_json(c['after_json']) or {}).get('id')) for c in captures
                   if self.host.coalesced_creation(c)}
        for capture in captures:
            responsible = self.host.responsible(uow, capture)
            for table, record, actor, event_type, direct in self.targets(uow, capture):
                business = record.get('business_id', record.get('review_business_id'))
                self.host.augment_direct(uow, capture, business, record, direct, responsible)
                edge = _json(capture['after_json']) or _json(capture['before_json']) or {}
                assignment = capture['source_type'] == 'relationships' and edge.get('role') == 'assigned_to'
                if assignment and (table, business, record['id']) in created:
                    continue  # The creation event already routes to final assignees.
                key = (business, table, record['id'], 'assignment' if assignment else capture['id'])
                if table == 'signoffs' and record['id'] in noted_signoffs and not self.host.presentation(capture):
                    key = (business, table, record['id'], 'signoff_note_change')
                if table == 'data_tables':
                    key = (business, table, record['id'], 'table_change')
                if key not in grouped:
                    grouped[key] = {'record': record, 'actor': actor, 'types': [], 'direct': set(), 'responsible': responsible,
                                    'presentation': self.host.presentation(capture)}
                group = grouped[key]
                group['record'] = record
                group['actor'] = actor or group['actor']
                if event_type not in group['types']: group['types'].append(event_type)
                group['direct'].update(direct)
        now = utc_now()
        for (business, table, identity, _capture_id), change in grouped.items():
            record, actor = change['record'], change['actor']
            recipients = change['direct']
            guard = self.host.augment_recipients(uow, business, table, identity, change, recipients, batch)
            if table == 'signoffs' and {'response_submitted', 'action_outcome'} & set(change['types']):
                recipients.update(r[0] for r in uow.connection.execute(
                    "SELECT DISTINCT a.to_id FROM thread_messages n JOIN json_each(n.refs_json) j "
                    "JOIN relationships a ON a.business_id IS n.business_id AND a.from_type='task' "
                    "AND a.from_id=n.item_id AND a.role='assigned_to' AND a.to_type='agent' "
                    "WHERE n.business_id IS ? AND n.item_type='task' AND n.kind='comment' AND n.refs_json<>'[]' "
                    "AND json_extract(j.value,'$.type')='signoff' AND json_extract(j.value,'$.id')=?", (business, identity)))
            item_type = self.item_type_for(table)
            if table == 'documents':
                item_type = {'context_topic': 'topic', 'context_person': 'person'}.get(record.get('kind'), 'material')
            if table == 'data_tables':
                # A Table's followers hear of its data changes as they hear of its comments (current access is rechecked on read).
                recipients.update(follower_ids(uow, ThreadItem(business, 'table', identity)))
            subscriptions = uow.connection.execute('SELECT * FROM inbox_subscriptions WHERE business_id IS ?', (business,)).fetchall()
            for sub in subscriptions:
                matched = sub['source_type'] == table and sub['source_id'] == identity
                if not matched and sub['source_type'] == 'tasks' and sub['include_related']:
                    matched = routed(uow, sub, item_type, identity)
                if matched: recipients.add(sub['agent_id'])
            # No self-generated wakeups. Current authorization is rechecked on reads.
            recipients.discard(actor)
            active = set()
            for agent in recipients:
                if self.host.recipient_allowed(uow, agent, business, table, guard): active.add(agent)
            if not active: continue
            title = self.host.presentation_summary(record) or source_summary(record, table)
            visibility = {k: record[k] for k in ('kind', 'owning_agent_id', 'path') if k in record}
            # The decision already communicates a combined closing message; one
            # existing response event remains sufficient for consumers.
            event_types = change['types']
            if table == 'signoffs' and 'response_submitted' in event_types:
                event_types = [kind for kind in event_types if kind != 'note_added']
            cursor = uow.connection.execute(
                'INSERT INTO inbox_events(business_id,source_type,source_id,source_revision,actor_id,event_type,summary,visibility_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (business, table, identity, record.get('revision', record.get('status_revision')), actor,
                 ','.join(event_types), str(title)[:500], json.dumps(visibility), now))
            uow.connection.executemany('INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)', [(agent, cursor.lastrowid) for agent in sorted(active)])
        uow.connection.execute('DELETE FROM inbox_changes')
