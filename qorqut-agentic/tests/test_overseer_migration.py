"""Synthetic offline migration exercises the original schema, not live state."""
import sqlite3

import pytest

from qorqut_agentic import AgenticError, AgenticLayer
from qorqut_agentic.store import DDL, Store


def old_state(tmp_path):
    root, state = tmp_path / 'root', tmp_path / 'state'
    root.mkdir()
    state.mkdir()
    (state / 'authoring.lock').touch()
    conn = sqlite3.connect(state / 'state.sqlite3')
    conn.executescript(DDL)
    conn.executemany('INSERT INTO meta VALUES (?, ?)', [
        ('schema_version', '1'), ('root_path', str(root)),
        ('instance_id', 'inst_' + 'a' * 32), ('initialized_at', 'then')])
    conn.execute('INSERT INTO inventory VALUES (?, ?, ?)', ('existing', 'hash', 7))
    conn.execute('INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                 ('run_old', 'package/workflow', 'rev', 'then', 'then', '{}', '{}', None, None, '{}', '{}'))
    conn.commit()
    conn.close()
    return root, state


def test_explicit_migration_and_replay_preserve_state(tmp_path):
    root, state = old_state(tmp_path)
    with pytest.raises(AgenticError) as exc:
        AgenticLayer.open(root, state)
    assert exc.value.code == 'migration_required'
    conn = sqlite3.connect(state / 'state.sqlite3')
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == ('1',)
    conn.close()
    result = AgenticLayer.migrate_state(root, state)
    assert result['replayed'] is False
    assert AgenticLayer.migrate_state(root, state)['replayed'] is True
    layer = AgenticLayer.open(root, state)
    assert layer.instance_id == 'inst_' + 'a' * 32
    with Store(state).reader() as conn:
        assert tuple(conn.execute('SELECT * FROM inventory').fetchone()) == ('existing', 'hash', 7)
        assert conn.execute('SELECT run_id FROM runs').fetchone()[0] == 'run_old'
        assert conn.execute('SELECT count(*) FROM overseer_migrations').fetchone()[0] == 2


def test_failed_migration_rolls_back_all_schema_changes(tmp_path, monkeypatch):
    root, state = old_state(tmp_path)
    from qorqut_agentic.overseer import schema
    install = schema.install

    def fail(conn):
        install(conn)
        raise RuntimeError('injected failure')

    monkeypatch.setattr(schema, 'install', fail)
    with pytest.raises(RuntimeError, match='injected failure'):
        AgenticLayer.migrate_state(root, state)
    with Store(state).reader() as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '1'
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name='proposed_deltas'").fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM inventory').fetchone()[0] == 1
