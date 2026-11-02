"""Bounded, read-only queries for the host's Overseer presentation layer."""
from __future__ import annotations

import base64
import json

from ..errors import AgenticError, InvalidRequestError, NotFoundError
from . import validation as v
from .appstate import _application
from .deltas import _read


class Views:
    def __init__(self, layer):
        self.ctx = layer._ctx

    def get_admitted_run(self, run_id):
        v.string(run_id, 'run_id')
        with self.ctx.store.reader() as conn:
            rows = conn.execute("""SELECT result FROM overseer_operations
                WHERE operation_id GLOB 'run:*' AND json_extract(result, '$.state')='linked'
                  AND json_extract(result, '$.run_id')=? LIMIT 2""", (run_id,)).fetchall()
        if len(rows) > 1:
            raise AgenticError('multiple canonical admissions for the run', code='evidence_unavailable')
        return json.loads(rows[0]['result']) if rows else None

    def has_unbound_review_input(self, delta_id, revision):
        v.uuid(delta_id, 'delta_id')
        if type(revision) is not int or revision < 1:
            raise InvalidRequestError('invalid delta revision')
        with self.ctx.store.reader() as conn:
            row = conn.execute('''SELECT 1 FROM overseer_inputs
                WHERE json_extract(record,'$.targets.delta_id')=?
                  AND json_extract(record,'$.targets.delta_revision')=?
                  AND json_extract(record,'$.purpose') IN ('review','reopen')
                  AND json_extract(record,'$.request_ref') IS NULL LIMIT 1''', (delta_id, revision)).fetchone()
        return row is not None

    def get_delta_application(self, delta_id):
        v.uuid(delta_id, 'delta_id')
        with self.ctx.store.reader() as conn:
            delta = conn.execute('SELECT decision, accepted_revision, acceptance_ref FROM deltas WHERE id=?', (delta_id,)).fetchone()
            if delta is None:
                raise NotFoundError('delta not found')
            if delta['decision'] != 'accepted':
                return None
            rows = conn.execute('''SELECT * FROM delta_applications WHERE delta_id=? AND accepted_revision=?
                AND acceptance_ref=? LIMIT 2''', (delta_id, delta['accepted_revision'], delta['acceptance_ref'])).fetchall()
        if len(rows) > 1:
            raise AgenticError('multiple applications match the current acceptance', code='evidence_unavailable')
        return _application(rows[0]) if rows else None

    @staticmethod
    def _cursor(route, filters, cursor):
        if cursor is None:
            return None
        try:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise ValueError('invalid cursor size')
            raw = base64.b64decode(cursor.encode('ascii'), altchars=b'-_', validate=True)
            value = json.loads(raw.decode('utf-8'))
            if (not isinstance(value, dict) or set(value) != {'route', 'filters', 'position'}
                    or value['route'] != route or value['filters'] != filters
                    or not isinstance(value['position'], list) or not value['position']):
                raise ValueError('invalid cursor')
            return value['position']
        except (ValueError, TypeError, UnicodeError, AttributeError) as exc:
            raise InvalidRequestError('invalid or mismatched cursor') from exc

    @staticmethod
    def _encode(route, filters, position):
        raw = json.dumps({'route': route, 'filters': filters, 'position': position},
                         sort_keys=True, separators=(',', ':')).encode()
        return base64.urlsafe_b64encode(raw).decode('ascii')

    def _page(self, route, filters, limit, cursor, query, args, position, decode):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidRequestError('limit must be between 1 and 100')
        after = self._cursor(route, filters, cursor)
        sql, params = query(after)
        with self.ctx.store.reader() as conn:
            rows = conn.execute(sql, (*args, *params, limit + 1)).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            items = [decode(conn, row) for row in rows]
        next_cursor = self._encode(route, filters, position(rows[-1])) if more else None
        return {'schema_version': 1, 'items': items, 'next_cursor': next_cursor,
                'complete': not more, 'availability': 'available', 'gaps': []}

    def list_batches(self, *, limit=20, cursor=None):
        def query(after):
            if after is not None and (len(after) != 2 or not all(isinstance(x, str) for x in after)):
                raise InvalidRequestError('invalid cursor position')
            clause = "WHERE (json_extract(record,'$.created_at'), id) < (?, ?)" if after else ''
            return f"SELECT id, record, json_extract(record,'$.created_at') AS at FROM overseer_batches {clause} ORDER BY at DESC, id DESC LIMIT ?", after or []
        return self._page('batches', {}, limit, cursor, query, (), lambda row: [row['at'], row['id']],
                          lambda conn, row: json.loads(row['record']))

    def list_deltas(self, *, decision=None, limit=20, cursor=None):
        if decision is not None and decision not in ('undecided', 'accepted', 'rejected'):
            raise InvalidRequestError('invalid decision filter')
        def query(after):
            if after is not None and (len(after) != 2 or not all(isinstance(x, str) for x in after)):
                raise InvalidRequestError('invalid cursor position')
            conditions = []
            if decision is not None: conditions.append('decision=?')
            if after: conditions.append('(created_at, id) < (?, ?)')
            where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
            return 'SELECT id, created_at FROM deltas' + where + ' ORDER BY created_at DESC, id DESC LIMIT ?', after or []
        return self._page('deltas', {'decision': decision}, limit, cursor, query,
                          (decision,) if decision is not None else (), lambda row: [row['created_at'], row['id']],
                          lambda conn, row: _read(conn, row['id']))

    def list_applications(self, *, queue=None, delta_id=None, limit=20, cursor=None):
        if queue not in (None, 'unimplemented'):
            raise InvalidRequestError('invalid queue filter')
        if delta_id is not None:
            v.uuid(delta_id, 'delta_id')
        def query(after):
            if after is not None and (len(after) != 2 or not all(isinstance(x, str) for x in after)):
                raise InvalidRequestError('invalid cursor position')
            conditions = ["merge IS NULL AND phase != 'complete'"] if queue else []
            if delta_id is not None: conditions.append('delta_id=?')
            if after: conditions.append('(created_at, id) < (?, ?)')
            where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
            return 'SELECT * FROM delta_applications' + where + ' ORDER BY created_at DESC, id DESC LIMIT ?', after or []
        return self._page('applications', {'queue': queue, 'delta_id': delta_id}, limit, cursor, query,
                          (delta_id,) if delta_id is not None else (),
                          lambda row: [row['created_at'], row['id']], lambda conn, row: _application(row))

    def _revisions(self, route, subject, table, column, *, limit, cursor):
        v.uuid(subject, 'id')
        def query(after):
            if after is not None and (len(after) != 1 or type(after[0]) is not int or after[0] < 1):
                raise InvalidRequestError('invalid cursor position')
            clause = ' AND revision < ?' if after else ''
            return f'SELECT revision, record FROM {table} WHERE {column}=?{clause} ORDER BY revision DESC LIMIT ?', after or []
        return self._page(route, {'id': subject}, limit, cursor, query, (subject,),
                          lambda row: [row['revision']], lambda conn, row: json.loads(row['record']))

    def list_delta_revisions(self, delta_id, *, limit=20, cursor=None):
        return self._revisions('delta_revisions', delta_id, 'overseer_delta_revisions', 'delta_id', limit=limit, cursor=cursor)

    def list_application_plans(self, application_id, *, limit=20, cursor=None):
        return self._revisions('application_plans', application_id, 'overseer_plan_revisions', 'application_id', limit=limit, cursor=cursor)

    def list_application_events(self, application_id, *, limit=20, cursor=None):
        v.uuid(application_id, 'application_id')
        def query(after):
            if after is not None and (len(after) != 1 or type(after[0]) is not int or after[0] < 1):
                raise InvalidRequestError('invalid cursor position')
            clause = ' AND rowid > ?' if after else ''
            return 'SELECT rowid AS ordinal, id, created_at, record FROM overseer_application_events WHERE application_id=?' + clause + ' ORDER BY rowid LIMIT ?', after or []
        return self._page('application_events', {'id': application_id}, limit, cursor, query, (application_id,),
                          lambda row: [row['ordinal']], lambda conn, row: {'id': row['id'], 'created_at': row['created_at'],
                                                                         'record': json.loads(row['record'])})

    def list_inputs(self, *, delta_id=None, application_id=None, run_id=None, limit=20, cursor=None):
        for value, name in ((delta_id, 'delta_id'), (application_id, 'application_id')):
            if value is not None: v.uuid(value, name)
        if run_id is not None: v.string(run_id, 'run_id')
        filters = {'delta_id': delta_id, 'application_id': application_id, 'run_id': run_id}
        keys = [('delta_id', '$.targets.delta_id'), ('application_id', '$.targets.application_id'), ('run_id', '$.run_id')]
        args = tuple(filters[key] for key, _ in keys if filters[key] is not None)
        def query(after):
            if after is not None and (len(after) != 1 or not isinstance(after[0], str)):
                raise InvalidRequestError('invalid cursor position')
            conditions = ["json_extract(record, '%s')=?" % path for key, path in keys if filters[key] is not None]
            if after: conditions.append('id > ?')
            where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
            return 'SELECT id, record FROM overseer_inputs' + where + ' ORDER BY id LIMIT ?', after or []
        return self._page('inputs', filters, limit, cursor, query, args, lambda row: [row['id']],
                          lambda conn, row: json.loads(row['record']))
