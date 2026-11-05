import os
import shutil
import sqlite3

import pytest

from bazarlyk.agentic import layout_files, layout_git, layout_intent, layout_runtime, upgrade
from qorqut_operations.contracts import DomainError
from test_layout_intent import ATTR, legacy


def test_synthetic_runtime_commit_and_recovery(legacy, monkeypatch, tmp_path):
    paths, export = legacy
    local_export = tmp_path / 'export-copy'
    shutil.copytree(export, local_export, symlinks=True)
    profile = paths.instance_root / 'config/native-profile-sentinel'
    session = paths.instance_root / 'operations/native-session-sentinel'
    profile.write_text('synthetic profile')
    session.write_text('synthetic session')
    plan = upgrade.prepare_upgrade(paths, local_export)
    assert plan['complete'], plan['conflicts']
    binding = upgrade.read_binding_bytes(paths)
    dbpath = paths.agentic_runtime / 'state.sqlite3'
    inode = dbpath.stat().st_ino
    with sqlite3.connect(dbpath) as db:
        cursor = db.execute('SELECT * FROM consumers ORDER BY name').fetchall()
        historical = db.execute('SELECT * FROM changes ORDER BY sequence').fetchall()
    layout_intent.begin(paths, plan, attribution=ATTR)
    layout_files.relocate(paths, plan, attribution=ATTR)
    layout_git.publish(paths, plan, attribution=ATTR)
    shutil.rmtree(local_export)
    original_write = layout_runtime._write
    def interrupt(path, journal):
        if journal['phase'] == 'runtime_published':
            raise OSError('interrupted after commit')
        return original_write(path, journal)
    monkeypatch.setattr(layout_runtime, '_write', interrupt)
    with pytest.raises(OSError, match='interrupted'):
        layout_runtime.apply(paths, plan, attribution=ATTR)
    monkeypatch.setattr(layout_runtime, '_write', original_write)
    # The database committed while the private journal still says prepared.
    # Each change must refuse sealing; restoring the exact logical state permits it.
    with sqlite3.connect(dbpath) as db:
        cursor_row = db.execute('SELECT name,updated_at FROM consumers ORDER BY name LIMIT 1').fetchone()
        history_row = db.execute('SELECT change_id,reason FROM changes ORDER BY sequence LIMIT 1').fetchone()
        inventory_row = db.execute('SELECT path,size FROM inventory ORDER BY path LIMIT 1').fetchone()
    for query, restore, item in [
        ('UPDATE consumers SET updated_at=? WHERE name=?', cursor_row[1], cursor_row[0]),
        ('UPDATE changes SET reason=? WHERE change_id=?', history_row[1], history_row[0]),
        ('UPDATE inventory SET size=? WHERE path=?', inventory_row[1], inventory_row[0]),
    ]:
        altered = restore + 1 if isinstance(restore, int) else 'changed synthetic evidence'
        with sqlite3.connect(dbpath) as db:
            db.execute(query, (altered, item))
        with pytest.raises(DomainError):
            layout_runtime.apply(paths, plan, attribution=ATTR)
        with sqlite3.connect(dbpath) as db:
            db.execute(query, (restore, item))
    result = layout_runtime.apply(paths, plan, attribution=ATTR)
    assert result['worked'] and result['state_receipt']['change_id']
    assert not layout_runtime.apply(paths, plan, attribution=ATTR)['worked']
    assert layout_runtime.read_receipt(paths, plan) == result['state_receipt']
    assert layout_runtime.verify_published(paths, plan, attribution=ATTR)['state_receipt'] == result['state_receipt']
    assert upgrade.read_binding_bytes(paths) == binding
    assert dbpath.stat().st_ino == inode
    assert profile.read_text() == 'synthetic profile'
    assert session.read_text() == 'synthetic session'
    with sqlite3.connect(dbpath) as db:
        assert db.execute('SELECT * FROM consumers ORDER BY name').fetchall() == cursor
        assert db.execute('SELECT * FROM changes WHERE sequence < ? ORDER BY sequence', (result['state_receipt']['sequence'],)).fetchall() == historical
        event = db.execute('SELECT actor_id,actor_kind,reason,details FROM changes WHERE change_id=?', (result['state_receipt']['change_id'],)).fetchone()
        assert event[:3] == (ATTR['actor_id'], ATTR['actor_kind'], ATTR['reason'])
        assert plan['plan_digest'] in event[3]
        assert db.execute('SELECT count(*) FROM change_files WHERE change_id=? AND commit_id != ?', (result['state_receipt']['change_id'], result['commit'])).fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM changes WHERE change_id=?', (result['state_receipt']['change_id'],)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM meta WHERE key LIKE 'layout_upgrade:%'").fetchone()[0] == 1

    with sqlite3.connect(dbpath) as db:
        db.execute("UPDATE consumers SET updated_at='changed' WHERE name=(SELECT name FROM consumers LIMIT 1)")
    with pytest.raises(DomainError):
        layout_runtime.apply(paths, plan, attribution=ATTR)
    with pytest.raises(DomainError):
        layout_runtime.verify_published(paths, plan, attribution=ATTR)
    assert layout_runtime.read_receipt(paths, plan) == result['state_receipt']


def test_logical_digest_tracks_rows_and_ignores_receipt(tmp_path):
    db = sqlite3.connect(tmp_path / 'synthetic.sqlite3')
    db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)')
    db.execute('CREATE TABLE consumers(name TEXT, cursor INTEGER)')
    db.execute("INSERT INTO consumers VALUES ('test', 1)")
    before = layout_runtime._digest_database(db, 'layout_upgrade:abc')
    db.execute("INSERT INTO meta VALUES ('layout_upgrade:abc', 'receipt')")
    assert layout_runtime._digest_database(db, 'layout_upgrade:abc') == before
    db.execute("UPDATE consumers SET cursor=2 WHERE name='test'")
    assert layout_runtime._digest_database(db, 'layout_upgrade:abc') != before
    db.close()


@pytest.mark.parametrize('blocked', ['unsupported', 'unfinished'])
def test_runtime_refuses_invalid_preexisting_work_before_writes(tmp_path, monkeypatch, blocked):
    from types import SimpleNamespace
    from qorqut_agentic.store import DDL
    root, state = tmp_path / 'root', tmp_path / 'state'
    root.mkdir(); state.mkdir()
    (state / 'authoring.lock').touch()
    dbpath = state / 'state.sqlite3'
    version = '4' if blocked == 'unsupported' else '1'
    with sqlite3.connect(dbpath) as db:
        db.executescript(DDL)
        db.executemany('INSERT INTO meta VALUES (?,?)', [('schema_version', version), ('root_path', str(root)),
                       ('instance_id', 'inst_' + 'a' * 32), ('initialized_at', 'then')])
        if blocked == 'unfinished':
            db.execute("INSERT INTO pending_changes VALUES ('chg_pending','then','edit','{}','[]','[]','{}')")
    paths = SimpleNamespace(agentic_runtime=state, agentic_root=root)
    plan = {'plan_digest': 'a' * 64, 'state': {'schema_version': version},
            'binding': {'instance_id': 'inst_' + 'a' * 32, 'root': str(root)}}
    intent, relocation, git = {'receipt_sha256': 'i'}, {'receipt_sha256': 'r'}, {'receipt_sha256': 'g', 'commit': 'c'}
    candidate = {'instance_id': plan['binding']['instance_id']}
    journal_path = tmp_path / 'runtime.json'
    evidence = (tmp_path, journal_path, intent, relocation, git, candidate, 'b' * 64, {})
    monkeypatch.setattr(layout_runtime, '_evidence', lambda *a, **kw: evidence)
    monkeypatch.setattr(layout_runtime, 'inspect_runtime', lambda *a: plan['state'])
    with sqlite3.connect(dbpath) as db:
        original = list(db.iterdump())
    with pytest.raises(DomainError):
        layout_runtime.apply(paths, plan, attribution=ATTR)
    with sqlite3.connect(dbpath) as db:
        assert list(db.iterdump()) == original
        assert not db.execute("SELECT 1 FROM meta WHERE key LIKE 'layout_upgrade:%'").fetchone()


def test_current_schema_no_net_event_and_conflicting_receipt_refuses(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from qorqut_agentic.store import Store
    root, state = tmp_path / 'root', tmp_path / 'state'
    root.mkdir()
    Store(state).create(str(root), 'then', 'inst_' + 'a' * 32)
    paths = SimpleNamespace(agentic_runtime=state, agentic_root=root)
    plan = {'plan_digest': 'a' * 64, 'state': {'schema_version': '3'},
            'binding': {'instance_id': 'inst_' + 'a' * 32, 'root': str(root)}}
    intent, relocation, gj = {'receipt_sha256': 'i'}, {'receipt_sha256': 'r'}, {'receipt_sha256': 'g', 'commit': 'c'}
    evidence = (tmp_path, tmp_path / 'runtime.json', intent, relocation, gj, {'instance_id': plan['binding']['instance_id']}, 'b' * 64, {})
    monkeypatch.setattr(layout_runtime, '_evidence', lambda *a, **kw: evidence)
    monkeypatch.setattr(layout_runtime, 'inspect_runtime', lambda *a: plan['state'])
    result = layout_runtime.apply(paths, plan, attribution=ATTR)
    assert result['state_receipt']['change_id'] is None
    assert not layout_runtime.apply(paths, plan, attribution=ATTR)['worked']
    with sqlite3.connect(state / 'state.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM changes').fetchone()[0] == 0
        db.execute("UPDATE meta SET value='{}' WHERE key LIKE 'layout_upgrade:%'")
    with pytest.raises(DomainError):
        layout_runtime.apply(paths, plan, attribution=ATTR)
