"""Owner's compact new-attention projection. Viewing never decides a Signoff."""
import fcntl
import hashlib
import json
import os
import re
import tempfile
from datetime import date, timedelta
from pathlib import Path

from qorqut_operations.contracts import DomainError, InvocationOptions
from qorqut.agents.global_scope import active_global
from qorqut.management.identity import installation_owner
from qorqut.reply_notifications import replies
from qorqut.domains.viewbox import BOARD_LIMIT, attention_key


def signoff_key(row):
    return f"signoff:{row['id']}:{row['proposal_revision']}:{row['payload_hash']}"


def reminder_key(row):
    return f"reminder:{row['reminder_id']}:{row['occurrence_id']}:{row['revision']}"


# A calendar event the owner discarded from the strip. The key leads with the event's local start date so old entries can be
# dropped without reading any provider; the rest names the connection, calendar, event and occurrence start.
CALENDAR_KEY = re.compile(r'^calendar:(\d{4}-\d{2}-\d{2}):')
CALENDAR_KEEP_DAYS = 30
CALENDAR_KEEP_MAX = 2000


def recent_calendar_key(key):
    found = CALENDAR_KEY.match(key)
    return bool(found) and found.group(1) >= (date.today() - timedelta(days=CALENDAR_KEEP_DAYS)).isoformat()


def _saved(runtime_directory, owner, suffix, ids=(), keep=None, limit=None):
    directory = Path(runtime_directory) / 'dashboard-preferences'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(owner.encode()).hexdigest() + suffix)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        stored = set(json.loads(path.read_text())) if path.exists() else set()
        saved = {item for item in stored if keep(item)} if keep else set(stored)
        saved.update(ids)
        if limit and len(saved) > limit:
            saved = set(sorted(saved)[-limit:])
        if saved != stored:
            fd, name = tempfile.mkstemp(dir=directory, prefix='.attention-')
            try:
                with os.fdopen(fd, 'w') as stream:
                    json.dump(sorted(saved), stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, path)
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try: os.fsync(fd)
                finally: os.close(fd)
            finally:
                if os.path.exists(name): os.unlink(name)
        return saved


def viewed(runtime_directory, owner, ids=()):
    return _saved(runtime_directory, owner, '.attention.json', ids)


def discarded(runtime_directory, owner, ids=()):
    """The owner's discarded calendar events, older than the retention window dropped as they are read."""
    return _saved(runtime_directory, owner, '.calendar-discarded.json', ids, keep=recent_calendar_key, limit=CALENDAR_KEEP_MAX)


def queue(runtime, ctx, change=None):
    with runtime.database.read() as uow:
        if ctx.principal_kind != 'human' or ctx.principal_id != installation_owner(uow):
            raise DomainError('forbidden', 'Attention queue is owner-only')
    if change is not None:
        if (not isinstance(change, dict) or not change or not set(change) <= {'viewed', 'discarded'}
                or any(not isinstance(ids, list) or len(ids) > 200 or any(not isinstance(x, str) or not 1 <= len(x) <= 500 for x in ids)
                       for ids in change.values())
                or not all(recent_calendar_key(x) for x in change.get('discarded', []))):
            raise DomainError('validation_error', 'Supply viewed attention IDs or recent discarded calendar keys')
    candidates = [item for item in replies(runtime, ctx)['items'] if not item.get('nav_only')]
    with runtime.database.read() as uow:
        # Match the visible history of each current supported global; listing a board does not mark it read. Boards of
        # other registrations stay readable to the owner but raise no attention.
        placements = uow.connection.execute("""SELECT * FROM (
            SELECT v.*, a.profile_json, ROW_NUMBER() OVER (
                PARTITION BY v.agent_id ORDER BY v.placement_sequence DESC) AS board_rank
            FROM agent_viewbox_items v JOIN agent_profiles a ON a.agent_id=v.agent_id
            WHERE a.status='active' AND a.home_business_id IS NULL
        ) WHERE board_rank<=?""", (BOARD_LIMIT,)).fetchall()
        current = {agent: active_global(uow.connection, agent) is not None for agent in {row['agent_id'] for row in placements}}
        for row in (row for row in placements if current[row['agent_id']]):
            profile = json.loads(row['profile_json'])
            candidates.append({'id': attention_key(row), 'kind': 'viewbox', 'agent_id': row['agent_id'],
                'agent_name': profile.get('display_name') or profile.get('name') or 'Agent',
                'viewbox_item_id': row['id'], 'title': row['title'], 'text': row['title'],
                'created_at': row['placed_at'], 'placement_sequence': row['placement_sequence'], 'unread': True})
        for item in candidates:
            profile = uow.connection.execute('SELECT profile_json FROM agent_profiles WHERE agent_id=?', (item.get('agent_id'),)).fetchone()
            if profile:
                data = json.loads(profile[0])
                item['presentation'] = {k: data[k] for k in ('role', 'icon', 'name', 'scope') if k in data}
        _, _, signoffs = runtime.signoffs.notifications._rows(ctx, uow)
        side_chats = runtime.side_chats.active(ctx, uow)
        # Reuse the reminder reader's scope and recipient checks. Each recurrence
        # and edit gets its own read marker; viewing never dismisses the reminder.
        runtime.application.authorize(ctx, runtime.application.registry.get('reminders_list'), (), uow, namespace='global')
        args = {'states': ['due'], 'all_scopes': True, 'limit': 100}
        while True:
            page = runtime.reminders.list(ctx, args, InvocationOptions(), uow)
            for row in page['reminders']:
                candidates.append({'id': reminder_key(row), 'kind': 'reminder', 'reminder_id': row['reminder_id'],
                    'title': row['text'], 'text': row['text'], 'created_at': row['updated_at']})
            if not page['next_cursor']:
                break
            args['cursor'] = page['next_cursor']
    for row in signoffs:
        candidates.append({'id': signoff_key(row), 'kind': 'signoff', 'signoff_id': row['id'],
            'title': row['title'], 'text': row['title'], 'category': row['category'],
            'business_id': row['review_business_id'], 'biz': row['business_slug'],
            'created_at': row['created_at'],
            # Something to read and acknowledge, not a decision.
            **({'to_read': True} if row.get('input_kind') == 'acknowledge' else {})})
    # Only acknowledge exact versions the owner could see; a newer reply remains new.
    allowed = {item['id'] for item in candidates}
    seen = viewed(runtime.paths.runtime, ctx.principal_id, [x for x in (change or {}).get('viewed', []) if x in allowed])
    gone = discarded(runtime.paths.runtime, ctx.principal_id, (change or {}).get('discarded', []))
    groups = {}
    order = lambda item: (item['created_at'] or '', item.get('placement_sequence', 0), item['id'])
    for item in sorted(candidates, key=order):
        if item['id'] in seen or item.get('unread') is False:
            continue
        source = item.get('source', {})
        if item['kind'] == 'viewbox':
            group = ('viewbox', item['viewbox_item_id'])
        elif item['kind'] == 'reminder':
            group = ('reminder', item['reminder_id'])
        else:
            group = (item['kind'], item.get('agent_id'), bool(item.get('chat')), source.get('type'), source.get('business_id'), source.get('id')) if item['kind'] != 'signoff' else ('signoff', item['signoff_id'])
        previous = groups.get(group)
        groups[group] = {**item, 'viewed_ids': (previous['viewed_ids'] if previous else []) + [item['id']]}
    return {'items': sorted(groups.values(), key=order, reverse=True), 'side_chats': side_chats, 'discarded': sorted(gone)}
