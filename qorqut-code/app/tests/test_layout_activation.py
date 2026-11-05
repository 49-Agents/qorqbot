import os
import shutil
import sqlite3
import time

import pytest

from bazarlyk.agentic import installation, layout_activation, layout_intent, layout_runtime, upgrade
from bazarlyk.bootstrap import catalog_revision
from qorqut_operations.contracts import DomainError
from test_layout_intent import ATTR, legacy, git


@pytest.mark.parametrize('preserved', [True, False])
def test_public_apply_resume_and_replay(legacy, tmp_path, monkeypatch, preserved):
    paths, export = legacy
    export_copy = tmp_path / 'export'
    shutil.copytree(export, export_copy, symlinks=True)
    preferences = paths.agentic_root / 'packages/user-preferences'
    if preserved:
        (preferences / 'SKILL.md').write_text('Edited synthetic preference.\n')
    else:
        shutil.rmtree(preferences)
    (paths.agentic_root / 'packages/agentic-management/bootstrap.md').write_text('Edited synthetic guidance.\n')
    deleted_source = 'packages/imported-roles/native/skills/owner-focus/references/browser-input.md'
    (paths.agentic_root / deleted_source).unlink()
    unrelated = paths.agentic_root / 'unrelated.txt'
    unrelated.write_text('staged synthetic\n')
    git(paths, 'add', '--', 'unrelated.txt')
    unrelated.write_text('working synthetic\n')
    profile = paths.instance_root / 'config/native-profile-sentinel'
    session = paths.instance_root / 'operations/native-session-sentinel'
    profile.write_text('profile')
    session.write_text('session')
    plan = upgrade.prepare_upgrade(paths, export_copy)
    assert plan['complete'], plan['conflicts']
    deleted_target = next(x['new_path'] for x in plan['preservation']['locally_absent_files'] if x['old_path'] == deleted_source)
    db = paths.agentic_runtime / 'state.sqlite3'
    inode = db.stat().st_ino
    with sqlite3.connect(db) as conn:
        history = conn.execute('SELECT * FROM changes ORDER BY sequence').fetchall()
        cursors = conn.execute('SELECT * FROM consumers ORDER BY name').fetchall()
    if preserved:
        layout_intent.begin(paths, plan, attribution=ATTR)
        with pytest.raises(DomainError):
            upgrade.apply_upgrade(paths, plan, attribution={**ATTR, 'reason': 'different attribution'})
        with pytest.raises(DomainError, match='offline'):
            installation.open_installation(paths, plan['selected_extensions'])
        shutil.rmtree(export_copy)
    original_apply = layout_runtime.apply
    def stop_before_runtime(*args, **kwargs):
        raise OSError('stopped after Git publication')
    monkeypatch.setattr(layout_runtime, 'apply', stop_before_runtime)
    with pytest.raises(OSError, match='stopped after Git'):
        upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    monkeypatch.setattr(layout_runtime, 'apply', original_apply)
    if not preserved:
        shutil.rmtree(export_copy)
    with pytest.raises(DomainError, match='offline'):
        installation.open_installation(paths, plan['selected_extensions'])
    original_verify = layout_runtime.verify_published
    def stop_after_runtime(*args, **kwargs):
        raise OSError('stopped after runtime publication')
    monkeypatch.setattr(layout_runtime, 'verify_published', stop_after_runtime)
    with pytest.raises(OSError, match='stopped after runtime'):
        upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    monkeypatch.setattr(layout_runtime, 'verify_published', original_verify)
    with pytest.raises(DomainError, match='offline'):
        installation.open_installation(paths, plan['selected_extensions'])
    if preserved:
        original_replace = os.replace
        def stop_before_binding(src, dst, *args, **kwargs):
            if str(dst).endswith('/config/agentic-layer.json'):
                raise OSError('stopped before binding replacement')
            return original_replace(src, dst, *args, **kwargs)
        monkeypatch.setattr(layout_activation.os, 'replace', stop_before_binding)
        with pytest.raises(OSError, match='stopped before binding'):
            upgrade.apply_upgrade(paths, plan, attribution=ATTR)
        monkeypatch.setattr(layout_activation.os, 'replace', original_replace)
        with pytest.raises(DomainError, match='offline'):
            installation.open_installation(paths, plan['selected_extensions'])
    original = layout_activation.layout_files._journal_write
    def interrupt(path, journal):
        if path.name.startswith('activation-layout-') and journal['phase'] == 'complete':
            raise OSError('interrupted completion')
        return original(path, journal)
    monkeypatch.setattr(layout_activation.layout_files, '_journal_write', interrupt)
    with pytest.raises(OSError, match='interrupted completion'):
        upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    with pytest.raises(DomainError, match='offline'):
        installation.open_installation(paths, plan['selected_extensions'])
    with pytest.raises(DomainError):
        catalog_revision.observe(paths.instance_root, 15)
    monkeypatch.setattr(layout_activation.layout_files, '_journal_write', original)
    binding_path = paths.instance_root / 'config/agentic-layer.json'
    candidate_raw = binding_path.read_bytes()
    binding_path.write_bytes(b'{}\n')
    with pytest.raises(DomainError):
        upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    assert binding_path.read_bytes() == b'{}\n'
    binding_path.write_bytes(candidate_raw)
    result = upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    assert result['activation'] and result['worked'] and db.stat().st_ino == inode
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT * FROM consumers ORDER BY name').fetchall() == cursors
        assert conn.execute('SELECT * FROM changes WHERE sequence < ? ORDER BY sequence', (result['sequence'],)).fetchall() == history
        event = conn.execute('SELECT actor_id, actor_kind, reason FROM changes WHERE change_id=?', (result['change_id'],)).fetchall()
        assert event == [(ATTR['actor_id'], ATTR['actor_kind'], ATTR['reason'])]
    layer, _ = installation.open_installation(paths, plan['selected_extensions'])
    assert (paths.agentic_root / 'packages').is_dir()
    assert (paths.agentic_root / 'operations/agentic-management/bootstrap.md').read_text() == 'Edited synthetic guidance.\n'
    assert not (paths.agentic_root / deleted_target).exists()
    assert git(paths, 'show', ':unrelated.txt') == b'staged synthetic\n'
    assert unrelated.read_text() == 'working synthetic\n'
    assert profile.read_text() == 'profile' and session.read_text() == 'session'
    binding, _ = upgrade.read_binding(paths)
    assert binding['preferences_seed']['kind'] == ('preserved_existing' if preserved else 'layout_verified_export_seed')
    assert layout_runtime.read_receipt(paths, plan)['change_id'] == result['change_id']
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE consumers SET updated_at='later' WHERE name=(SELECT name FROM consumers LIMIT 1)")
    layer.authoring.apply(ATTR, [{'op': 'create', 'path': 'plugins/custom-tools/new.txt', 'content': 'later plugin'}])
    layer.authoring.apply(ATTR, [{'op': 'create', 'path': 'user-preferences/later.txt', 'content': 'later preference'}])
    from qorqut_agentic.errors import AgenticError
    with pytest.raises(AgenticError):
        layer.authoring.apply(ATTR, [{'op': 'create', 'path': 'operations/agentic-management/forbidden.txt', 'content': 'no'}])
    later_commit = layer.history.commit('later synthetic changes', paths=['plugins/custom-tools/new.txt', 'user-preferences/later.txt'])['commit']
    replay = upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    assert not replay['worked'] and replay['replayed'] and replay['commit'] == result['commit']
    installation.open_installation(paths, plan['selected_extensions'])
    # Each observation must independently verify the completed installation.
    with catalog_revision.open_observer(paths.instance_root, time.monotonic() + 60) as observer:
        revision = observer.observe()
        activation = upgrade.history_directory(paths) / ('activation-layout-' + plan['plan_digest'] + '.json')
        saved_activation = activation.read_bytes()
        activation_mode = activation.stat().st_mode & 0o777
        activation.unlink()
        with pytest.raises(DomainError):
            observer.observe()
        assert not activation.exists()
        activation.write_bytes(saved_activation)
        activation.chmod(activation_mode)
        assert observer.observe() == revision
        runtime_receipt = upgrade.history_directory(paths) / ('runtime-layout-' + plan['plan_digest'] + '.json')
        saved_runtime = runtime_receipt.read_bytes()
        runtime_receipt.write_bytes(b'{}\n')
        with pytest.raises(DomainError):
            observer.observe()
        assert runtime_receipt.read_bytes() == b'{}\n'
        runtime_receipt.write_bytes(saved_runtime)
        assert observer.observe() == revision
        intent_path = upgrade.history_directory(paths) / ('layout-' + plan['plan_digest'] + '.json')
        saved_intent = intent_path.read_bytes()
        intent_path.write_bytes(b'{}\n')
        with pytest.raises(DomainError):
            observer.observe()
        assert intent_path.read_bytes() == b'{}\n'
        intent_path.write_bytes(saved_intent)
        assert observer.observe() == revision
        binding_path = paths.instance_root / 'config/agentic-layer.json'
        saved_binding = binding_path.read_bytes()
        binding_path.write_bytes(b'{}\n')
        # A scheduling hint is deliberately insufficient to deliver instructions.
        assert observer.hint() == revision
        with pytest.raises(DomainError):
            observer.observe()
        assert binding_path.read_bytes() == b'{}\n'
        binding_path.write_bytes(saved_binding)
        assert observer.observe() == revision
        key = 'layout_upgrade:' + plan['plan_digest']
        with sqlite3.connect(db) as conn:
            saved_db_receipt = conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0]
            conn.execute('UPDATE meta SET value=? WHERE key=?', ('{}', key))
        with pytest.raises(DomainError):
            observer.observe()
        with sqlite3.connect(db) as conn:
            assert conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0] == '{}'
            conn.execute('UPDATE meta SET value=? WHERE key=?', (saved_db_receipt, key))
        assert observer.observe() == revision
    assert observer.process.poll() is not None
    assert git(paths, 'rev-parse', 'HEAD').decode().strip() == later_commit
    assert (paths.agentic_root / 'plugins/custom-tools/new.txt').read_text() == 'later plugin'
    runtime_path = upgrade.history_directory(paths) / ('runtime-layout-' + plan['plan_digest'] + '.json')
    original_runtime = runtime_path.read_bytes()
    runtime_path.write_bytes(b'{}\n')
    with pytest.raises(DomainError):
        installation.open_installation(paths, plan['selected_extensions'])
    with pytest.raises(DomainError):
        upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    runtime_path.write_bytes(original_runtime)
    installation.open_installation(paths, plan['selected_extensions'])
    binding_path = paths.instance_root / 'config/agentic-layer.json'
    original_binding = binding_path.read_bytes()
    binding_path.write_bytes(b'{}\n')
    with pytest.raises(DomainError):
        installation.open_installation(paths, plan['selected_extensions'])
    binding_path.write_bytes(original_binding)
    installation.open_installation(paths, plan['selected_extensions'])
    key = 'layout_upgrade:' + plan['plan_digest']
    with sqlite3.connect(db) as conn:
        prior_receipt = conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0]
        conn.execute('UPDATE meta SET value=? WHERE key=?', ('{}', key))
    with pytest.raises(DomainError):
        installation.open_installation(paths, plan['selected_extensions'])
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE meta SET value=? WHERE key=?', (prior_receipt, key))
    installation.open_installation(paths, plan['selected_extensions'])
    activation = upgrade.history_directory(paths) / ('activation-layout-' + plan['plan_digest'] + '.json')
    saved = activation.read_bytes()
    activation.unlink()
    with pytest.raises(DomainError, match='offline'):
        installation.open_installation(paths, plan['selected_extensions'])
    activation.write_bytes(saved)


def test_capability_refusal_before_effects(legacy, monkeypatch):
    paths, export = legacy
    plan = upgrade.prepare_upgrade(paths, export)
    before = upgrade.read_binding_bytes(paths)
    monkeypatch.setattr(layout_activation, '_probe', lambda parent: (_ for _ in ()).throw(OSError('unsupported')))
    with pytest.raises(DomainError, match='Filesystem'):
        upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    assert upgrade.read_binding_bytes(paths) == before
    assert not layout_intent.retained_intents(paths)
