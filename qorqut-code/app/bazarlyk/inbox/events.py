"""Turn transaction-local trigger captures into immutable, addressed events.

Triggers cover SQL and repository writers alike. Finalization runs before commit;
an exception rolls back the source write and its notifications together.

Tasks and Actions are not announced here: their one discussion writer is the Operations engine
(``qorqut_operations.discussion``), which writes the thread entry, the follow state and the event with its
required/optional deliveries in the applying transaction. The captures below cover every other retained item, and
an edge that touches a Task or an Action is announced by the engine operation that wrote it.
"""
import json
import logging

from bazarlyk.agents.global_scope import active_global, reaches_business
from bazarlyk.agents.roles import ORDUBASHY_ROLE
from bazarlyk.composition.capture import CAPTURED as FEED_ONLY
from bazarlyk.extensions import NO_SOURCES
from qorqut_operations.contracts import utc_now
from qorqut_operations.discussion import emit_event

from .related import routed

LOGGER = logging.getLogger(__name__)

ITEM_TYPES = {'signoffs': 'signoff',
              'email_conversations': 'material', 'documents': 'material',
              'data_tables': 'table',
              'agent_profiles': 'agent', 'context_entities': 'entity', 'principals': 'person'}
RESOURCE_TYPES = {v: k for k, v in ITEM_TYPES.items() if k != 'documents'}
IGNORED_FIELDS = {'revision', 'status_revision', 'sequence'}


def assigned(uow, business, kind, identity):
    return {r[0] for r in uow.connection.execute(
        "SELECT to_id FROM relationships WHERE business_id IS ? AND from_type=? AND from_id=? "
        "AND to_type='agent' AND role='assigned_to'", (business, kind, identity))}


def intake_coordinator(uow, business, reader):
    """The configured management coordinator when it is an active supported Ordubashy that reaches this business and may
    read its email now; otherwise None. No other role holder, the owner or a broadcast stands in."""
    installation = uow.repository('installation').get('service')
    coordinator = installation['management_coordinator_id'] if installation else None
    profile = active_global(uow.connection, coordinator)
    if (profile is None or profile['role']['id'] != ORDUBASHY_ROLE
            or not reaches_business(uow.connection, coordinator, business)):
        return None
    return coordinator if reader(coordinator, business, uow) else None


def item_type_for(table, sources=NO_SOURCES):
    """The typed reference name of a source key: a core table, or a selected extension's exact registered key."""
    return ITEM_TYPES.get(table) or sources.item_types.get(table)


def row_for(uow, table, business, identity, sources=NO_SOURCES):
    handler = sources.get(table)
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


FOCUS_FIELD_LABELS = {'display_name': 'name', 'reason': 'reason', 'requested_response': 'requested action',
                      'blocker': 'blocker flag', 'urgent': 'urgent flag', 'references': 'references',
                      'title': 'title', 'target': 'opening link'}
FOCUS_SOURCE_TABLES = {'signoff': 'signoffs', 'email': 'email_conversations'}
FOCUS_ASSIGNEE_KINDS = {'email_conversations': 'material'}


def focus_reader(uow, agent):
    """Whether the agent currently passes the Focus board's own reader gate (the active configured coordinator)."""
    from bazarlyk.management.identity import configured_ordubashy
    return bool(agent) and agent == configured_ordubashy(uow, require_codex=True)


def focus_edit_targets(uow, data, actor):
    """Route an owner's presentation edit to its conversation binding, without card prose."""
    card = uow.connection.execute('SELECT * FROM focus_cards WHERE id=?', (data['card_id'],)).fetchone()
    if card is None: return []
    installation = uow.repository('installation').get('service')
    coordinator = installation['management_coordinator_id'] if installation else None
    fields, until = data['fields'], data.get('deferred_until')
    types, parts = [], []
    if 'deferred_until' in fields:
        types.append('focus_snoozed' if until else 'focus_resumed')
        parts.append('snoozed Focus until ' + until if until else 'resumed Focus')
    changed = [FOCUS_FIELD_LABELS[f] for f in fields if f != 'deferred_until']
    if changed:
        types.append('focus_edited')
        parts.append('edited Focus ' + ', '.join(changed))
    binding = json.loads(card['source_json'] or 'null') or json.loads(card['context_json'] or 'null')
    recipients = {coordinator} if coordinator else set()
    if binding and binding['type'] == 'task':
        # A Task's events have one writer, the Operations discussion: the coordinator and the assignees are its
        # required recipients, exactly as for a change to the Task itself.
        task = uow.connection.execute('SELECT * FROM tasks WHERE business_id IS ? AND id=?',
                                      (binding['business_id'], binding['id'])).fetchone()
        if task is not None:
            emit_event(uow, business_id=task['business_id'], source_type='tasks', source_id=task['id'],
                       source_revision=task['revision'], actor_id=actor, event_type=','.join(types),
                       summary='Owner ' + ' and '.join(parts) + ': ' + source_summary(dict(task), 'tasks'),
                       required=recipients | assigned(uow, task['business_id'], 'task', task['id']))
            return []
    table = FOCUS_SOURCE_TABLES.get(binding['type']) if binding else None
    record = row_for(uow, table, binding['business_id'], binding['id']) if table else None
    if record:
        # The canonical source row keeps its own revision and read authorization.
        if table in FOCUS_ASSIGNEE_KINDS:
            recipients |= assigned(uow, binding['business_id'], FOCUS_ASSIGNEE_KINDS[table], binding['id'])
        if table == 'signoffs' and record.get('proposer_agent_id'):
            recipients.add(record['proposer_agent_id'])
        title = source_summary(record, table)
    else:
        table = 'focus_cards'
        reference = json.loads(card['reference_json'] or 'null') if not binding else None
        if reference and focus_reader(uow, card['created_by']): recipients.add(card['created_by'])
        record = {**dict(card), 'title': reference['title'] if reference else 'Focus card'}
        title = record['title']
    record = {**record, 'focus_summary': ('Owner ' + ' and '.join(parts) + ': ' + title)[:500]}
    return [(table, record, actor, kind, set(recipients)) for kind in types]


def targets(uow, capture, sources=NO_SOURCES):
    """Return canonical changed records, without persisting private note bodies."""
    table, action = capture['source_type'], capture['action']
    before = _json(capture['before_json']) or {}
    after = _json(capture['after_json']) or {}
    data = after or before
    if table in FEED_ONLY:
        return []  # The engine's one discussion writer announces these records; the capture only feeds the dashboard.
    if table == 'documents' and data.get('kind') == 'working_memory':
        # Own working state is not a shared Material or an inbox subscription.
        return []
    if table == 'focus_edits':
        return focus_edit_targets(uow, data, capture['actor_id'])
    business = data.get('business_id', data.get('review_business_id'))
    actor = capture['actor_id']
    if action == 'update' and all(before.get(k) == after.get(k) for k in set(before) | set(after) if k not in IGNORED_FIELDS):
        return []
    handler = sources.get(table)
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
    if table == 'relationships':
        result = []
        resource_types = {**RESOURCE_TYPES, **{kind: source for source, kind in sources.item_types.items()}}
        for edge in (before, after):
            if not edge: continue
            for prefix in ('from', 'to'):
                kind = edge[prefix + '_type']
                source_type = resource_types.get(kind)
                if source_type:
                    record = row_for(uow, source_type, business, edge[prefix + '_id'], sources)
                    if record:
                        result.append((source_type, record, actor or edge.get('created_by'), 'relationship_changed', set()))
                if kind in {'topic', 'person', 'material'} and not (source_type and record):
                    record = row_for(uow, 'documents', business, edge[prefix + '_id'])
                    if record:
                        result.append(('documents', record, actor or edge.get('created_by'), 'relationship_changed', set()))
            if edge['role'] == 'assigned_to' and edge['to_type'] == 'agent':
                source_type = resource_types.get(edge['from_type'])
                if source_type:
                    record = row_for(uow, source_type, business, edge['from_id'], sources)
                    if record:
                        result.append((source_type, record, actor or edge.get('created_by'), 'assignment_changed', {edge['to_id']}))
        return result
    if table == 'focus_notes':
        card=uow.connection.execute('SELECT * FROM focus_cards WHERE id=?',(data['card_id'],)).fetchone()
        installation=uow.repository('installation').get('service')
        recipients={installation['management_coordinator_id']} if installation and installation['management_coordinator_id'] else set()
        record={**dict(card),'title':json.loads(card['reference_json'])['title']} if card else None
        return [('focus_cards',record,data['created_by'],'note_added',recipients)] if record else []
    if table == 'signoff_notes':
        record = row_for(uow, 'signoffs', business, data['signoff_id'])
        installation=uow.repository('installation').get('service')
        recipients={record.get('proposer_agent_id'),installation['management_coordinator_id'] if installation else None} if record else set()
        recipients.discard(None)
        return [('signoffs', record, data['created_by'], 'note_added', recipients)] if record else []
    if table == 'signoff_attention':
        row = uow.connection.execute('SELECT * FROM signoffs WHERE id=?', (data['signoff_id'],)).fetchone()
        return [('signoffs', dict(row), actor, 'attention_changed', set())] if row else []
    if table == 'email_comments':
        record = row_for(uow, 'email_conversations', business, data['conversation_id'])
        installation = uow.repository('installation').get('service')
        recipients = {installation['management_coordinator_id']} if installation and installation['management_coordinator_id'] else set()
        return [('email_conversations', record, data['created_by'], 'note_added', recipients)] if record else []
    if table == 'email_messages':
        record = row_for(uow, 'email_conversations', business, data['conversation_id'])
        return [('email_conversations', record, actor, 'message_changed', set())] if record else []
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
        # Applying platform approval only queues execution; completion belongs
        # to the later durable external receipt. Setup failures still notify.
        if data.get('category') == 'platform_action' and after.get('state') == 'applied':
            terminal = False
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


def finalize_events(uow, sources=NO_SOURCES):
    captures = uow.connection.execute('SELECT * FROM inbox_changes ORDER BY id').fetchall()
    if not captures: return
    attention_enabled=bool(uow.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='focus_cards'").fetchone())
    grouped = {}
    noted_signoffs={json.loads(c['after_json'])['signoff_id'] for c in captures if c['source_type']=='signoff_notes' and c['after_json']}
    created = {(c['source_type'], (_json(c['after_json']) or {}).get('business_id'),
                (_json(c['after_json']) or {}).get('id')) for c in captures
               if c['action'] == 'insert' and c['source_type'] == 'email_conversations'}
    for capture in captures:
        responsible = True
        if capture['source_type'] == 'email_messages':
            # Mailbox synchronization also updates read flags and outgoing mail.
            # Those remain subscription events, but do not summon the assignee.
            data = _json(capture['after_json']) or {}
            responsible = capture['action'] == 'insert' and not data.get('deleted')
            if responsible:
                from bazarlyk.domains.email.attribution import outgoing
                from bazarlyk.domains.email.purposes import NO_PURPOSES
                message = uow.repository('email_messages', business_id=data['business_id']).get(data['id'])
                # Baseline rows mark mail that predates the first sync; they summon no one.
                responsible = bool(message and message.get('origin') != 'baseline' and not outgoing(message, uow, getattr(uow.database, 'mailbox_purposes', NO_PURPOSES)))
        for table, record, actor, event_type, direct in targets(uow, capture, sources):
            business = record.get('business_id', record.get('review_business_id'))
            if capture['source_type']=='email_messages' and responsible:
                reader=getattr(uow.database,'email_reader',None)
                if reader:
                    # The configured coordinator is told of every new incoming business message, whoever else is assigned;
                    # assignees keep their own delivery below. The assignment edges are never rewritten.
                    coordinator=intake_coordinator(uow,business,reader)
                    if coordinator:
                        direct.add(coordinator)
                    elif not any(reader(agent,business,uow) for agent in assigned(uow,business,'material',record['id'])):
                        LOGGER.warning('Incoming project email has no eligible intake recipient: business=%s conversation=%s',
                                       business, record['id'])
            edge = _json(capture['after_json']) or _json(capture['before_json']) or {}
            assignment = capture['source_type'] == 'relationships' and edge.get('role') == 'assigned_to'
            if assignment and (table, business, record['id']) in created:
                continue  # The creation event already routes to final assignees.
            key = (business, table, record['id'], 'assignment' if assignment else capture['id'])
            if table == 'signoffs' and record['id'] in noted_signoffs and capture['source_type'] != 'focus_edits':
                key = (business,table,record['id'],'signoff_note_change')
            if table == 'data_tables':
                key = (business, table, record['id'], 'table_change')
            if key not in grouped:
                grouped[key] = {'record': record, 'actor': actor, 'types': [], 'direct': set(), 'responsible': responsible,
                                'presentation': capture['source_type'] == 'focus_edits'}
            group = grouped[key]
            group['record'] = record
            group['actor'] = actor or group['actor']
            if event_type not in group['types']: group['types'].append(event_type)
            group['direct'].update(direct)
    now = utc_now()
    for (business, table, identity, _capture_id), change in grouped.items():
        record, actor = change['record'], change['actor']
        recipients = change['direct']
        installation=uow.repository('installation').get('service')
        if (attention_enabled and installation and actor==installation['owner_principal_id'] and installation['management_coordinator_id']
                and table == 'signoffs'):
            kind=ITEM_TYPES[table]
            focused=uow.connection.execute('''SELECT 1 FROM focus_cards f
                WHERE (json_extract(source_json,'$.type')=? AND json_extract(source_json,'$.id')=? AND json_extract(source_json,'$.business_id') IS ?)
                OR (json_extract(context_json,'$.type')=? AND json_extract(context_json,'$.id')=? AND json_extract(context_json,'$.business_id') IS ?)
                OR EXISTS (SELECT 1 FROM json_each(f.references_json) r WHERE json_extract(r.value,'$.type')=?
                    AND json_extract(r.value,'$.id')=? AND json_extract(r.value,'$.business_id') IS ?) LIMIT 1''',
                (kind,identity,business)*3).fetchone()
            if focused: recipients.add(installation['management_coordinator_id'])
        if change['responsible'] and table == 'email_conversations':
            recipients.update(assigned(uow, business, ITEM_TYPES[table], identity))
        email_link=None
        if table=='signoffs' and record.get('category')=='input_request':
            email_link=uow.connection.execute('SELECT * FROM email_decisions WHERE signoff_id=?',(identity,)).fetchone()
            if email_link:
                if installation and installation['management_coordinator_id']:recipients.add(installation['management_coordinator_id'])
                if 'response_submitted' in change['types']:
                    recipients.update(assigned(uow,business,'material',email_link['conversation_id']))
        if table == 'signoffs' and {'response_submitted','action_outcome'} & set(change['types']):
            recipients.update(r[0] for r in uow.connection.execute(
                "SELECT DISTINCT a.to_id FROM thread_messages n JOIN json_each(n.refs_json) j "
                "JOIN relationships a ON a.business_id IS n.business_id AND a.from_type='task' "
                "AND a.from_id=n.item_id AND a.role='assigned_to' AND a.to_type='agent' "
                "WHERE n.business_id IS ? AND n.item_type='task' AND n.kind='comment' AND n.refs_json<>'[]' "
                "AND json_extract(j.value,'$.type')='signoff' AND json_extract(j.value,'$.id')=?", (business,identity)))
        item_type = item_type_for(table, sources)
        if table == 'documents':
            item_type = {'context_topic': 'topic', 'context_person': 'person'}.get(record.get('kind'), 'material')
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
            # Only active supported globals receive deliveries; business-home identities are historical.
            profile = active_global(uow.connection, agent)
            if profile is None: continue
            if business is None:
                allowed = bool(profile['scope'].get('global_capabilities'))
            else:
                # Derived access: every active business, no agent membership row.
                allowed = reaches_business(uow.connection, agent, business)
            reader=getattr(uow.database,'email_reader',None)
            if allowed and reader and (table=='email_conversations' or email_link):
                allowed=reader(agent,business,uow)
            if allowed: active.add(agent)
        if not active: continue
        title = record.get('focus_summary') or source_summary(record, table)
        visibility = {k: record[k] for k in ('kind', 'owning_agent_id', 'path') if k in record}
        # The decision already communicates a combined closing message; one
        # existing response event remains sufficient for consumers.
        event_types=change['types']
        if table=='signoffs' and 'response_submitted' in event_types:
            event_types=[kind for kind in event_types if kind!='note_added']
        cursor = uow.connection.execute(
            'INSERT INTO inbox_events(business_id,source_type,source_id,source_revision,actor_id,event_type,summary,visibility_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
            (business, table, identity, record.get('revision', record.get('status_revision')), actor,
             ','.join(event_types), str(title)[:500], json.dumps(visibility), now))
        uow.connection.executemany('INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)', [(agent, cursor.lastrowid) for agent in sorted(active)])
    uow.connection.execute('DELETE FROM inbox_changes')
