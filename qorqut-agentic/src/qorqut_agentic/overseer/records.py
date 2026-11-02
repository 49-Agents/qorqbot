"""Immutable portable proposal storage and idempotent operations."""
from __future__ import annotations

import base64
import hashlib
import json

from ..errors import AgenticError, InvalidRequestError, NotFoundError
from ..util import check_operation_id, iso
from . import validation


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidRequestError('record must be finite JSON') from exc


def mutate(ctx, operation_id, operation, payload, fn):
    check_operation_id(operation_id)
    if operation_id.lower().startswith('run:'):
        raise InvalidRequestError('operation ID uses the reserved invocation namespace')
    fingerprint = hashlib.sha256(canonical({'operation': operation, 'payload': payload}).encode()).hexdigest()
    with ctx.store.transaction() as conn:
        old = conn.execute('SELECT fingerprint, result FROM overseer_operations WHERE operation_id=?', (operation_id,)).fetchone()
        if old:
            if old['fingerprint'] != fingerprint:
                raise AgenticError('operation ID was used for different input', code='operation_conflict')
            return json.loads(old['result'])
        result = fn(conn)
        conn.execute('INSERT INTO overseer_operations VALUES (?, ?, ?, ?)',
                     (operation_id, fingerprint, canonical(result), iso(ctx.clock())))
        return result


def get_record(ctx, table, record_id):
    validation.uuid(record_id, 'id')
    with ctx.store.reader() as conn:
        row = conn.execute(f'SELECT record FROM {table} WHERE id=?', (record_id,)).fetchone()
    if row is None:
        raise NotFoundError('record not found', details={'id': record_id})
    return json.loads(row['record'])


class Proposals:
    def __init__(self, ctx):
        self.ctx = ctx

    def record(self, record: dict, operation_id: str):
        validation.proposal(record)
        def write(conn):
            if conn.execute('SELECT 1 FROM proposed_deltas WHERE id=?', (record['id'],)).fetchone():
                raise AgenticError('proposal ID already exists', code='operation_conflict')
            conn.execute('INSERT INTO proposed_deltas VALUES (?, ?, ?, ?, ?)',
                         (record['id'], record['extraction_run_id'], canonical(record), record['created_at'], record['created_by']))
            return record
        return mutate(self.ctx, operation_id, 'proposals.record', record, write)

    def get(self, id: str):
        return get_record(self.ctx, 'proposed_deltas', id)

    def list(self, limit: int = 50, cursor: str | None = None):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise InvalidRequestError('limit must be between 1 and 200')
        after = ''
        if cursor is not None:
            try:
                after = base64.b64decode(cursor.encode('ascii'), altchars=b'-_', validate=True).decode('ascii')
                validation.uuid(after, 'cursor')
            except Exception as exc:
                raise InvalidRequestError('invalid cursor') from exc
        with self.ctx.store.reader() as conn:
            rows = conn.execute('SELECT id, record FROM proposed_deltas WHERE id > ? ORDER BY id LIMIT ?', (after, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = base64.urlsafe_b64encode(rows[-1]['id'].encode()).decode() if more else None
        return {'items': [json.loads(row['record']) for row in rows], 'next_cursor': next_cursor,
                'complete': not more, 'availability': 'available'}
