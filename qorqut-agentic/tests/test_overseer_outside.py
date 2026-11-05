import copy
import sqlite3
from uuid import uuid4

import pytest

from qorqut_agentic import AgenticError, AgenticLayer
from qorqut_agentic.cli import dispatch
from qorqut_agentic.overseer.views import Views
from qorqut_agentic.store import Store
from .test_overseer_state import proposal


def observation():
    return {'schema_version': 1, 'id': str(uuid4()), 'summary': 'A problem',
            'sources': proposal()['sources'], 'suggested_improvement': None,
            'created_at': 'now', 'created_by': 'agent'}


def test_outside_record_cli_pagination_and_separation(layer):
    records = [observation() for _ in range(3)]
    for i, value in enumerate(records):
        args = {'record': value, 'operation_id': f'op{i}'}
        assert dispatch(layer, {'operation': 'overseer.outside.record', 'arguments': args}) == value
        assert layer.overseer.outside.record(value, f'op{i}') == value
    first = layer.overseer.outside.list(limit=1)
    second = layer.overseer.outside.list(limit=2, cursor=first['next_cursor'])
    assert sorted(v['id'] for v in first['items'] + second['items']) == sorted(v['id'] for v in records)
    assert second['complete']
    assert layer.overseer.outside.get(records[0]['id']) == records[0]
    assert layer.overseer.proposals.list()['items'] == []
    assert layer.overseer.deltas.list()['items'] == []
    assert Views(layer).list_deltas()['items'] == []
    with layer._ctx.store.reader() as conn:
        for table in ('overseer_memberships', 'overseer_batches', 'delta_applications', 'overseer_inputs'):
            assert conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    changed = {**records[0], 'summary': 'different'}
    for op in ('op0', 'new'):
        with pytest.raises(AgenticError): layer.overseer.outside.record(changed, op)
    with pytest.raises(AgenticError): layer.overseer.outside.list(cursor='broken')


@pytest.mark.parametrize('mutation', [
    lambda r: r.update(extra=True), lambda r: r.update(summary=' '),
    lambda r: r.update(summary='x'*8001), lambda r: r.update(sources=[]),
    lambda r: r.update(sources=r['sources']*101),
    lambda r: r['sources'][0].update(role='contrary'),
    lambda r: r['sources'][0]['ref']['provenance'].update(overseer_origin=True),
    lambda r: r['sources'][0]['ref'].update(body='private'),
    lambda r: r['sources'][0]['ref'].pop('revision'),
])
def test_invalid_records_rejected(layer, mutation):
    value = observation(); mutation(value)
    with pytest.raises(AgenticError): layer.overseer.outside.record(value, 'bad')
    assert layer.overseer.outside.list()['items'] == []


def version2(layer):
    with layer._ctx.store.transaction() as conn:
        conn.execute('DROP TABLE outside_deltas')
        conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")


def test_migrate_v2_preserves_and_replays(layer):
    item = proposal(); layer.overseer.proposals.record(item, 'old')
    identity = layer.instance_id
    version2(layer)
    with pytest.raises(AgenticError) as exc: AgenticLayer.open(layer.root, layer.state)
    assert exc.value.code == 'migration_required'
    assert AgenticLayer.migrate_state(layer.root, layer.state)['replayed'] is False
    assert AgenticLayer.migrate_state(layer.root, layer.state)['replayed'] is True
    opened = AgenticLayer.open(layer.root, layer.state)
    assert opened.instance_id == identity and opened.overseer.proposals.get(item['id']) == item


def test_migration_rolls_back_and_refuses_unknown(layer, monkeypatch):
    version2(layer)
    from qorqut_agentic.overseer import schema
    install = schema.install_v3
    def fail(conn):
        install(conn)
        raise RuntimeError('injected')
    monkeypatch.setattr(schema, 'install_v3', fail)
    with pytest.raises(RuntimeError): AgenticLayer.migrate_state(layer.root, layer.state)
    with Store(layer.state).reader() as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '2'
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='outside_deltas'").fetchone()
    monkeypatch.setattr(schema, 'install_v3', install)
    with Store(layer.state).transaction() as conn: conn.execute('DROP TABLE overseer_batches')
    with pytest.raises(AgenticError): AgenticLayer.migrate_state(layer.root, layer.state)
    with Store(layer.state).reader() as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '2'


def test_migration_refuses_unfinished_and_unknown_versions(layer):
    version2(layer)
    with Store(layer.state).transaction() as conn:
        conn.execute("INSERT INTO pending_changes VALUES ('pending','now','edit','{}','[]','[]','{}')")
    with pytest.raises(AgenticError): AgenticLayer.migrate_state(layer.root, layer.state)
    with Store(layer.state).transaction() as conn:
        conn.execute('DELETE FROM pending_changes')
        conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    with pytest.raises(AgenticError): AgenticLayer.migrate_state(layer.root, layer.state)
    with Store(layer.state).reader() as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '99'


def test_partial_current_schema_cannot_open(layer):
    with Store(layer.state).transaction() as conn: conn.execute('DROP TABLE outside_deltas')
    with pytest.raises(AgenticError): AgenticLayer.open(layer.root, layer.state)


def test_unfinished_publication_refuses_migration(layer):
    version2(layer)
    conn = sqlite3.connect(layer.state / 'state.sqlite3')
    conn.execute("INSERT INTO overseer_publications VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 ('app', 'op', 'change', 'base', None, None, None, None, None, '[]', 'publishing', '[]'))
    conn.commit(); conn.close()
    with pytest.raises(AgenticError): AgenticLayer.migrate_state(layer.root, layer.state)
    with Store(layer.state).reader() as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '2'
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='outside_deltas'").fetchone()
