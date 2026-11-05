"""Caller-owned runtime reconciliation transaction regression tests."""
import sqlite3

import pytest

from qorqut_agentic.changes import Changes, normalize_attribution
from qorqut_agentic.core import LayerContext
from qorqut_agentic.store import Store
from tests.test_overseer_migration import old_state

ATTR = normalize_attribution({'actor_id': 'synthetic', 'actor_kind': 'human', 'reason': 'upgrade'}, trust='host_installer')


@pytest.mark.parametrize('version', ['1', '2', '3'])
@pytest.mark.parametrize('failure', ['schema', 'scan', 'link'])
def test_caller_transaction_rolls_back_all_stages(tmp_path, failure, version):
    root, state = old_state(tmp_path)
    if version != '1':
        from qorqut_agentic.overseer import schema
        with sqlite3.connect(state / 'state.sqlite3') as db:
            schema.install(db)
            if version == '3':
                schema.install_v3(db)
            db.execute("UPDATE meta SET value=? WHERE key='schema_version'", (version,))
    (root / 'plugins' / 'custom').mkdir(parents=True)
    (root / 'plugins' / 'custom' / 'note.txt').write_text('synthetic')
    store = Store(state)
    ctx = LayerContext(root=root, store=store, git_executable=None, runtimes={}, clock=None, attribution_source='host_installer')
    changes = Changes(ctx)
    with store.reader() as conn:
        before = list(conn.iterdump())
    with pytest.raises(RuntimeError):
        with store.lock(exclusive=True), store.transaction() as conn:
            if version != '3':
                store._migrate_to_current(conn, str(root), 'fixed-time')
            else:
                from qorqut_agentic.store import _verify_layout
                _verify_layout(conn, version)
            if failure == 'schema':
                raise RuntimeError('injected')
            result = changes._scan_connection(conn, ATTR, change_id='chg_' + 'a' * 32, created_at='fixed-time', details={'layout_plan_digest': 'a' * 64})
            if failure == 'scan':
                raise RuntimeError('injected')
            event = result['change']
            changes._link_commit_connection(conn, 'a' * 40, {f['path']: f['after_sha256'] for f in event['files']}, change_id=event['change_id'])
            raise RuntimeError('injected')
    with store.reader() as conn:
        assert list(conn.iterdump()) == before
        assert conn.execute('SELECT count(*) FROM changes').fetchone()[0] == 0
