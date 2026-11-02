"""Immutable revisions and explicit current proposal membership."""
import base64
import json

from ..errors import AgenticError, InvalidRequestError, NotFoundError
from . import validation as v
from .records import canonical, mutate


def _error(code, message, **details):
    raise AgenticError(message, code=code, details=details)


def _delta(conn, delta_id):
    row = conn.execute('SELECT * FROM deltas WHERE id=?', (delta_id,)).fetchone()
    if row is None:
        raise NotFoundError('delta not found', details={'id': delta_id})
    return row


def _revision(conn, delta_id, revision):
    row = conn.execute('SELECT record FROM overseer_delta_revisions WHERE delta_id=? AND revision=?',
                       (delta_id, revision)).fetchone()
    if row is None:
        raise NotFoundError('delta revision not found')
    return json.loads(row['record'])


def _read(conn, delta_id, revision=None):
    row = _delta(conn, delta_id)
    revision = row['current_revision'] if revision is None else revision
    return {'schema_version': 1, 'id': delta_id, 'current_revision': row['current_revision'],
            'decision': row['decision'], 'accepted_revision': row['accepted_revision'],
            'acceptance_ref': json.loads(row['acceptance_ref']) if row['acceptance_ref'] else None,
            'created_at': row['created_at'], 'updated_at': row['updated_at'],
            'revision': _revision(conn, delta_id, revision)}


def _validate_members(conn, record):
    proposals = []
    for pid in record['proposal_ids']:
        row = conn.execute('SELECT record FROM proposed_deltas WHERE id=?', (pid,)).fetchone()
        if row is None:
            raise NotFoundError('proposal not found', details={'id': pid})
        assigned = conn.execute('''SELECT m.delta_id FROM overseer_memberships m
             JOIN deltas d ON d.id=m.delta_id AND d.current_revision=m.revision
             WHERE m.proposal_id=? AND m.delta_id != ?''', (pid, record['delta_id'])).fetchone()
        if assigned:
            _error('invalid_transition', 'proposal already belongs to another delta', proposal_id=pid, delta_id=assigned['delta_id'])
        proposals.append(json.loads(row['record']))
    events, episodes = set(), set()
    for proposal in proposals:
        for link in proposal['sources']:
            if link['role'] == 'supporting':
                ref = link['ref']
                if ref['origin_event_id'] is not None:
                    events.add(ref['origin_event_id'])
                if ref['episode_id'] is not None:
                    episodes.add(ref['episode_id'])
    summary = record['support_summary']
    if (summary['proposal_count'] != len(proposals) or summary['event_count'] != len(events)
        or summary['episode_count'] != len(episodes) or set(summary['event_refs']) != events
        or set(summary['episode_refs']) != episodes or len(summary['event_refs']) != len(events)
        or len(summary['episode_refs']) != len(episodes)):
        v.invalid('support_summary', 'must match distinct supporting event and episode references')


def _insert_revision(conn, record):
    conn.execute('INSERT INTO overseer_delta_revisions VALUES (?, ?, ?)',
                 (record['delta_id'], record['revision'], canonical(record)))
    conn.executemany('INSERT INTO overseer_memberships VALUES (?, ?, ?)',
                     [(record['delta_id'], record['revision'], pid) for pid in record['proposal_ids']])


class Deltas:
    def __init__(self, ctx):
        self.ctx = ctx

    def create(self, record: dict, operation_id: str):
        v.revision(record)
        if record['revision'] != 1:
            v.invalid('revision', 'must be 1 for creation')
        if not record['proposal_ids']:
            v.invalid('proposal_ids', 'must contain at least one proposal on creation')
        def write(conn):
            if conn.execute('SELECT 1 FROM deltas WHERE id=?', (record['delta_id'],)).fetchone():
                _error('operation_conflict', 'delta ID already exists')
            _validate_members(conn, record)
            conn.execute('INSERT INTO deltas VALUES (?, 1, ?, NULL, NULL, ?, ?)',
                         (record['delta_id'], 'undecided', record['created_at'], record['created_at']))
            _insert_revision(conn, record)
            return _read(conn, record['delta_id'])
        return mutate(self.ctx, operation_id, 'deltas.create', record, write)

    def revise(self, record: dict, expected_revision: int, operation_id: str):
        v.revision(record)
        if type(expected_revision) is not int or expected_revision < 1:
            v.invalid('expected_revision')
        def write(conn):
            row = _delta(conn, record['delta_id'])
            if row['current_revision'] != expected_revision or record['revision'] != expected_revision + 1:
                _error('stale_revision', 'delta revision changed', current_revision=row['current_revision'])
            previous = _revision(conn, record['delta_id'], expected_revision)
            if row['decision'] in ('rejected', 'accepted') and not set(previous['proposal_ids']).issubset(record['proposal_ids']):
                _error('invalid_transition', 'decided-delta membership cannot be detached')
            if row['decision'] == 'rejected':
                if any(record[k] != previous[k] for k in ('summary', 'change_direction', 'scope', 'target_refs')):
                    _error('invalid_transition', 'rejected meaning cannot change before reopening')
            _validate_members(conn, record)
            _insert_revision(conn, record)
            conn.execute('UPDATE deltas SET current_revision=?, updated_at=? WHERE id=?',
                         (record['revision'], record['created_at'], record['delta_id']))
            return _read(conn, record['delta_id'])
        return mutate(self.ctx, operation_id, 'deltas.revise', {'record': record, 'expected_revision': expected_revision}, write)

    def get(self, id: str, revision: int | None = None, history_limit: int = 50, history_cursor: str | None = None):
        v.uuid(id, 'id')
        if revision is not None and (type(revision) is not int or revision < 1):
            v.invalid('revision')
        if type(history_limit) is not int or not 1 <= history_limit <= 200:
            raise InvalidRequestError('history_limit must be between 1 and 200')
        after = 0
        if history_cursor is not None:
            try:
                decoded = base64.b64decode(history_cursor.encode('ascii'), altchars=b'-_', validate=True).decode('ascii')
                cursor_id, cursor_row = decoded.split(':', 1)
                if cursor_id != id or not cursor_row.isdigit() or int(cursor_row) < 1:
                    raise ValueError('bad cursor')
                after = int(cursor_row)
            except Exception as exc:
                raise InvalidRequestError('invalid history cursor') from exc
        with self.ctx.store.reader() as conn:
            result = _read(conn, id, revision)
            rows = conn.execute('''SELECT r.rowid AS ordinal,r.binding_id,r.digest,r.record,r.result
                FROM overseer_owner_responses r JOIN overseer_inputs i ON i.id=r.binding_id
                WHERE r.rowid > ? AND json_extract(i.record, '$.targets.delta_id')=?
                ORDER BY r.rowid LIMIT ?''', (after, id, history_limit + 1)).fetchall()
            more = len(rows) > history_limit
            rows = rows[:history_limit]
            result['decision_history'] = [{'binding_id': row['binding_id'], 'canonical_digest': row['digest'],
                                           'response': json.loads(row['record']), 'result': json.loads(row['result'])}
                                          for row in rows]
            result['history_next_cursor'] = base64.urlsafe_b64encode(f"{id}:{rows[-1]['ordinal']}".encode()).decode() if more else None
            result['history_complete'] = not more
            return result

    def list(self, limit: int = 50, cursor: str | None = None):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise InvalidRequestError('limit must be between 1 and 200')
        after = ''
        if cursor is not None:
            try:
                after = base64.b64decode(cursor.encode('ascii'), altchars=b'-_', validate=True).decode('ascii')
                v.uuid(after, 'cursor')
            except Exception as exc:
                raise InvalidRequestError('invalid cursor') from exc
        with self.ctx.store.reader() as conn:
            rows = conn.execute('SELECT id FROM deltas WHERE id > ? ORDER BY id LIMIT ?', (after, limit + 1)).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            items = [_read(conn, row['id']) for row in rows]
        next_cursor = base64.urlsafe_b64encode(rows[-1]['id'].encode()).decode() if more else None
        return {'items': items, 'next_cursor': next_cursor, 'complete': not more, 'availability': 'available'}
