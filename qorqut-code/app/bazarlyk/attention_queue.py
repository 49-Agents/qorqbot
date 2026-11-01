"""Owner's compact new-attention projection. Viewing never decides a Signoff."""
import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path

from bazarlyk.contracts import DomainError
from bazarlyk.management.identity import installation_owner
from bazarlyk.reply_notifications import replies
from bazarlyk.domains.viewbox import BOARD_LIMIT, attention_key


def signoff_key(row):
    return f"signoff:{row['id']}:{row['proposal_revision']}:{row['payload_hash']}"


def viewed(root, owner, ids=()):
    directory = Path(root) / '.bazarlyk' / 'dashboard-preferences'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(owner.encode()).hexdigest() + '.attention.json')
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = set(json.loads(path.read_text())) if path.exists() else set()
        if ids and not set(ids).issubset(saved):
            saved.update(ids)
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


def queue(runtime, ctx, change=None):
    with runtime.database.read() as uow:
        if ctx.principal_kind != 'human' or ctx.principal_id != installation_owner(uow):
            raise DomainError('forbidden', 'Attention queue is owner-only')
    if change is not None:
        if (not isinstance(change, dict) or set(change) != {'viewed'} or not isinstance(change['viewed'], list)
                or len(change['viewed']) > 200 or any(not isinstance(x, str) or not 1 <= len(x) <= 500 for x in change['viewed'])):
            raise DomainError('validation_error', 'Supply viewed attention IDs')
    candidates = [item for item in replies(runtime, ctx)['items'] if not item.get('nav_only')]
    with runtime.database.read() as uow:
        # Match the visible history of each active agent; listing a board does not mark it read.
        placements = uow.connection.execute("""SELECT * FROM (
            SELECT v.*, a.profile_json, ROW_NUMBER() OVER (
                PARTITION BY v.agent_id ORDER BY v.placement_sequence DESC) AS board_rank
            FROM agent_viewbox_items v JOIN agent_profiles a ON a.agent_id=v.agent_id
            JOIN principals p ON p.id=a.agent_id
            LEFT JOIN businesses b ON b.id=a.home_business_id
            WHERE a.status='active' AND p.active=1
                AND (a.home_business_id IS NULL OR (b.id IS NOT NULL AND b.archived_at IS NULL))
        ) WHERE board_rank<=?""", (BOARD_LIMIT,)).fetchall()
        for row in placements:
            profile = json.loads(row['profile_json'])
            candidates.append({'id': attention_key(row), 'kind': 'viewbox', 'agent_id': row['agent_id'],
                'agent_name': profile.get('display_name') or profile.get('name') or 'Agent',
                'viewbox_item_id': row['id'], 'title': row['title'], 'text': row['title'],
                'created_at': row['placed_at'], 'placement_sequence': row['placement_sequence'], 'unread': True})
        for item in candidates:
            if item['id'].startswith('telegram:'):
                message = uow.connection.execute('SELECT body FROM telegram_messages WHERE id=? AND sensitive=0 AND body_redacted=0', (item['id'][9:],)).fetchone()
                if message:
                    item['body'] = message['body']
            profile = uow.connection.execute('SELECT profile_json FROM agent_profiles WHERE agent_id=?', (item.get('agent_id'),)).fetchone()
            if profile:
                data = json.loads(profile[0])
                item['presentation'] = {k: data[k] for k in ('role', 'department', 'icon', 'name', 'scope') if k in data}
        _, _, signoffs = runtime.signoffs.notifications._rows(ctx, uow)
    for row in signoffs:
        candidates.append({'id': signoff_key(row), 'kind': 'signoff', 'signoff_id': row['id'],
            'title': row['title'], 'text': row['title'], 'category': row['category'],
            'business_id': row['review_business_id'], 'biz': row['business_slug'],
            'created_at': row['created_at']})
    # Only acknowledge exact versions the owner could see; a newer reply remains new.
    allowed = {item['id'] for item in candidates}
    seen = viewed(runtime.root, ctx.principal_id, [x for x in (change or {}).get('viewed', []) if x in allowed])
    groups = {}
    order = lambda item: (item['created_at'] or '', item.get('placement_sequence', 0), item['id'])
    for item in sorted(candidates, key=order):
        if item['id'] in seen or item.get('unread') is False:
            continue
        source = item.get('source', {})
        if item['kind'] == 'viewbox':
            group = ('viewbox', item['viewbox_item_id'])
        else:
            group = (item['kind'], item.get('agent_id'), bool(item.get('chat')), source.get('type'), source.get('business_id'), source.get('id')) if item['kind'] != 'signoff' else ('signoff', item['signoff_id'])
        previous = groups.get(group)
        groups[group] = {**item, 'viewed_ids': (previous['viewed_ids'] if previous else []) + [item['id']]}
    return {'items': sorted(groups.values(), key=order, reverse=True)}
