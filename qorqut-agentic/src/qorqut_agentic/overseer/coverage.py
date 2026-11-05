"""Immutable analysis receipts and bounded exact-source reconciliation."""
from uuid import uuid4

from ..errors import AgenticError, InvalidRequestError
from . import validation as v
from .records import canonical, get_record, mutate
from .runs import admitted_run


def _key(ref):
    v.obj(ref, 'source_ref', ('provider', 'kind', 'namespace', 'record_id', 'revision', 'content_hash'))
    for field in ('provider', 'kind', 'record_id', 'revision', 'content_hash'):
        v.string(ref[field], 'source_ref.' + field)
    ns = ref['namespace']
    v.obj(ns, 'source_ref.namespace', ('kind', 'business_id'))
    if ns['kind'] not in ('global', 'business') or (ns['kind'] == 'global' and ns['business_id'] is not None):
        v.invalid('source_ref.namespace')
    if ns['kind'] == 'business':
        v.uuid(ns['business_id'], 'source_ref.namespace.business_id')
    namespace = canonical({'kind': ns['kind'], 'business_id': ns['business_id']})
    return (ref['provider'], ref['kind'], namespace, ref['record_id'], ref['revision'], ref['content_hash'])


def _refs(value, field):
    v.array(value, field)
    if len(value) > 200:
        raise InvalidRequestError(field + ' may contain at most 200 source references')
    keys = [_key(ref) for ref in value]
    if len(set(keys)) != len(keys):
        v.invalid(field, 'must not repeat an exact source reference')
    return keys


class Coverage:
    def __init__(self, ctx):
        self.ctx = ctx

    def record(self, record: dict, operation_id: str):
        v.obj(record, 'coverage', ('schema_version', 'run_id', 'frontier', 'analyzed_sources', 'pending_sources', 'gaps', 'complete', 'next_cursor'))
        v.version(record)
        v.string(record['run_id'], 'run_id')
        frontier = record['frontier']
        v.obj(frontier, 'frontier', ('snapshot_id', 'captured_at', 'providers'))
        v.uuid(frontier['snapshot_id'], 'frontier.snapshot_id')
        v.string(frontier['captured_at'], 'frontier.captured_at')
        v.array(frontier['providers'], 'frontier.providers')
        for provider in frontier['providers']:
            v.obj(provider, 'frontier.provider', ('provider', 'anchor'))
            v.string(provider['provider'], 'frontier.provider.provider')
        analyzed = _refs(record['analyzed_sources'], 'analyzed_sources')
        pending = _refs(record['pending_sources'], 'pending_sources')
        if set(analyzed) & set(pending):
            v.invalid('pending_sources', 'must not overlap analyzed_sources')
        v.array(record['gaps'], 'gaps')
        if type(record['complete']) is not bool:
            v.invalid('complete')
        if record['next_cursor'] is not None:
            v.string(record['next_cursor'], 'next_cursor')
        if record['complete'] and (pending or record['gaps'] or record['next_cursor'] is not None):
            v.invalid('complete', 'requires no pending sources, gaps, or continuation')
        if set(record) - {'schema_version', 'run_id', 'frontier', 'analyzed_sources', 'pending_sources', 'gaps', 'complete', 'next_cursor'}:
            v.invalid('coverage', 'contains unknown fields')
        def write(conn):
            admission = admitted_run(conn, record['run_id'], 'extraction')
            if not admission:
                raise AgenticError('admitted extraction run is required', code='owner_input_required')
            saved = {**record, 'id': str(uuid4()), 'created_at': self.ctx.now(),
                     'created_by': admission['invocation']['executor']['actor_id']}
            conn.execute('INSERT INTO overseer_coverage VALUES (?, ?)', (saved['id'], canonical(saved)))
            conn.executemany('INSERT INTO overseer_coverage_sources VALUES (?, ?, ?, ?, ?, ?, ?)',
                             [(saved['id'], *key) for key in analyzed])
            return saved
        return mutate(self.ctx, operation_id, 'coverage.record', record, write)

    def get(self, id: str | None = None, source_refs: list | None = None):
        if (id is None) == (source_refs is None):
            raise InvalidRequestError('supply exactly one of id or source_refs')
        if id is not None:
            return get_record(self.ctx, 'overseer_coverage', id)
        keys = _refs(source_refs, 'source_refs')
        analyzed_sources, receipt_ids = [], set()
        with self.ctx.store.reader() as conn:
            for ref, key in zip(source_refs, keys):
                rows = conn.execute('''SELECT coverage_id FROM overseer_coverage_sources
                    WHERE provider=? AND kind=? AND namespace=? AND record_id=? AND revision=? AND content_hash=?
                    ORDER BY coverage_id LIMIT 1''', key).fetchall()
                if rows:
                    analyzed_sources.append(ref)
                    receipt_ids.update(row['coverage_id'] for row in rows)
        return {'analyzed_sources': analyzed_sources,
                'receipts': [{'type': 'coverage', 'id': item} for item in sorted(receipt_ids)]}
