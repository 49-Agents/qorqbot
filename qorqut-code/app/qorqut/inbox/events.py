"""The host side of event finalization: Focus cards and recipient eligibility.

The finalizer itself is Operations' ``EventFinalizer``; ``finalize_events`` binds it to the selected source handlers, this
module's item vocabulary and ``HostEventRouting`` and is the one finalizer the writing unit runs before commit (see
``qorqut.composition.transactions``). An exception rolls back the source write and its notifications together.

Tasks and Actions are not announced here: their one discussion writer is the Operations engine
(``qorqut_operations.discussion``), which writes the thread entry, the follow state and the event with its
required/optional deliveries in the applying transaction. The captures cover every other retained item, and
an edge that touches a Task or an Action is announced by the engine operation that wrote it. The one exception is the
Focus edit of a Task's card: it announces through that same writer (``emit_event``)."""
import json

from qorqut.agents.global_scope import active_global, reaches_business
from qorqut.composition.capture import CAPTURED as FEED_ONLY
from qorqut.extensions import NO_SOURCES
from qorqut_operations.discussion import emit_event
from qorqut_operations.inbox import EventFinalizer
from qorqut_operations.inbox.events import assigned, row_for, source_summary

ITEM_TYPES = {'signoffs': 'signoff', 'documents': 'material',
              'data_tables': 'table',
              'agent_profiles': 'agent', 'context_entities': 'entity', 'principals': 'person'}


FOCUS_FIELD_LABELS = {'display_name': 'name', 'reason': 'reason', 'requested_response': 'requested action',
                      'blocker': 'blocker flag', 'urgent': 'urgent flag', 'references': 'references',
                      'title': 'title', 'target': 'opening link'}
FOCUS_SOURCE_TABLES = {'signoff': 'signoffs'}


def focus_reader(uow, agent):
    """Whether the agent currently passes the Focus board's own reader gate (the active configured coordinator)."""
    from qorqut.management.identity import configured_ordubashy
    return bool(agent) and agent == configured_ordubashy(uow)


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


class HostEventRouting:
    """The host's slices of the one event finalizer, each called at the point the finalizer always made it."""

    def begin_batch(self, uow):
        """Whether the Focus board exists in this store."""
        return bool(uow.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='focus_cards'").fetchone())

    def coalesced_creation(self, capture):
        return False

    def responsible(self, uow, capture):
        return True

    def early_targets(self, uow, capture, data):
        if capture['source_type'] == 'focus_edits':
            return focus_edit_targets(uow, data, capture['actor_id'])
        return None

    def additional_targets(self, uow, capture, data, business, actor):
        table = capture['source_type']
        if table == 'focus_notes':
            card = uow.connection.execute('SELECT * FROM focus_cards WHERE id=?', (data['card_id'],)).fetchone()
            installation = uow.repository('installation').get('service')
            recipients = {installation['management_coordinator_id']} if installation and installation['management_coordinator_id'] else set()
            record = {**dict(card), 'title': json.loads(card['reference_json'])['title']} if card else None
            return [('focus_cards', record, data['created_by'], 'note_added', recipients)] if record else []
        if table == 'signoff_notes':
            record = row_for(uow, 'signoffs', business, data['signoff_id'])
            installation = uow.repository('installation').get('service')
            recipients = {record.get('proposer_agent_id'), installation['management_coordinator_id'] if installation else None} if record else set()
            recipients.discard(None)
            return [('signoffs', record, data['created_by'], 'note_added', recipients)] if record else []
        return None

    def terminal_signoff(self, data, before, after, terminal):
        # Applying platform approval only queues execution; completion belongs
        # to the later durable external receipt. Setup failures still notify.
        if data.get('category') == 'platform_action' and after.get('state') == 'applied':
            return False
        return terminal

    def augment_direct(self, uow, capture, business, record, direct, responsible):
        return None

    def presentation(self, capture):
        return capture['source_type'] == 'focus_edits'

    def augment_recipients(self, uow, business, table, identity, change, recipients, batch):
        actor = change['actor']
        installation = uow.repository('installation').get('service')
        if (batch and installation and actor == installation['owner_principal_id'] and installation['management_coordinator_id']
                and table == 'signoffs'):
            kind = ITEM_TYPES[table]
            focused = uow.connection.execute('''SELECT 1 FROM focus_cards f
                WHERE (json_extract(source_json,'$.type')=? AND json_extract(source_json,'$.id')=? AND json_extract(source_json,'$.business_id') IS ?)
                OR (json_extract(context_json,'$.type')=? AND json_extract(context_json,'$.id')=? AND json_extract(context_json,'$.business_id') IS ?)
                OR EXISTS (SELECT 1 FROM json_each(f.references_json) r WHERE json_extract(r.value,'$.type')=?
                    AND json_extract(r.value,'$.id')=? AND json_extract(r.value,'$.business_id') IS ?) LIMIT 1''',
                (kind, identity, business) * 3).fetchone()
            if focused: recipients.add(installation['management_coordinator_id'])
        return None

    def recipient_allowed(self, uow, agent, business, table, guard):
        # Only active supported globals receive deliveries; business-home identities are historical.
        profile = active_global(uow.connection, agent)
        if profile is None: return False
        if business is None:
            allowed = bool(profile['scope'].get('global_capabilities'))
        else:
            # Derived access: every active business, no agent membership row.
            allowed = reaches_business(uow.connection, agent, business)
        return bool(allowed)

    def presentation_summary(self, record):
        return record.get('focus_summary')


def finalize_events(uow, sources=NO_SOURCES):
    EventFinalizer(sources=sources, item_types=ITEM_TYPES, feed_only=FEED_ONLY, host=HostEventRouting()).finalize(uow)
