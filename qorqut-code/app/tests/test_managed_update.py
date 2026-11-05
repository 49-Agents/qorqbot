import pytest
from qorqut_operations.contracts import DomainError
from bazarlyk.agentic import upgrade
from bazarlyk.agentic.installation import open_installation
from test_agentic_managed_plan import installed, refresh_export, snapshot_instance

ATTR = {'actor_id': 'synthetic-admin', 'actor_kind': 'human', 'reason': 'Offline managed update'}


def test_noop_and_fresh_application(installed):
    paths, source = installed
    plan = upgrade.prepare_upgrade(paths, source)
    before = snapshot_instance(paths)
    receipt = upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    assert receipt['no_change'] and receipt['upgrade_commit'] == plan['git']['head']
    assert snapshot_instance(paths) == before
    from test_agentic_managed_plan import scan_and_commit
    preference = paths.agentic_root / 'user-preferences/SKILL.md'
    preference.write_text('Synthetic customized preference.\n')
    custom = paths.agentic_root / 'plugins/custom-extra'
    custom.mkdir(parents=True)
    (custom / 'agentic.yaml').write_text('schema_version: 1\nid: custom-extra\nname: Custom\ndescription: Custom\nentries: []\n')
    (custom / 'owner.txt').write_text('Synthetic custom plugin content.\n')
    scan_and_commit(paths)
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(paths.agentic_runtime / 'state.sqlite3')) as db:
        db.execute("INSERT INTO consumers(name,cursor,updated_at) VALUES ('synthetic-sentinel',0,'fixed')")
        db.execute("INSERT INTO meta(key,value) VALUES ('synthetic-sentinel','fixed')")
        db.commit()
        old_events = db.execute('SELECT * FROM changes ORDER BY sequence').fetchall()
        old_links = db.execute('SELECT * FROM change_files ORDER BY rowid').fetchall()
    desired = source / 'qorqut-agentic/src/qorqut_agentic/resources/defaults/overseer/overseer/instructions/review.md'
    desired.write_bytes(desired.read_bytes() + b'\nSynthetic supplier update.\n')
    refresh_export(source)
    plan = upgrade.prepare_upgrade(paths, source)
    assert plan['complete'], plan['conflicts']
    result = upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    assert not result['no_change'] and result['change_id'] and result['upgrade_commit'] != plan['git']['head']
    assert (paths.agentic_root / 'overseer/overseer/instructions/review.md').read_bytes() == desired.read_bytes()
    assert preference.read_text() == 'Synthetic customized preference.\n'
    assert (custom / 'owner.txt').read_text() == 'Synthetic custom plugin content.\n'
    with closing(sqlite3.connect(paths.agentic_runtime / 'state.sqlite3')) as db:
        assert db.execute('SELECT * FROM changes WHERE sequence < ? ORDER BY sequence', (result['sequence'],)).fetchall() == old_events
        assert db.execute('SELECT * FROM change_files WHERE change_id != ? ORDER BY rowid', (result['change_id'],)).fetchall() == old_links
        assert db.execute("SELECT cursor,updated_at FROM consumers WHERE name='synthetic-sentinel'").fetchone() == (0, 'fixed')
        assert db.execute("SELECT value FROM meta WHERE key='synthetic-sentinel'").fetchone() == ('fixed',)
    layer, _ = open_installation(paths, tuple(plan['selected_extensions']))
    from qorqut_agentic.errors import AgenticError
    with pytest.raises(AgenticError) as error:
        layer.authoring.apply(ATTR, [{'op': 'create', 'path': 'operations/agentic-management/new.md', 'content': 'no'}])
    assert error.value.code == 'protected_agentic_area'
    before = snapshot_instance(paths)
    replay = upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    assert replay['replayed'] and not replay['worked']
    assert snapshot_instance(paths) == before


@pytest.mark.parametrize('stage', ['file', 'ref_published', 'feed_published', 'binding_published'])
def test_interrupted_exact_resume(installed, monkeypatch, stage):
    import shutil
    from bazarlyk.agentic import managed_update
    paths, source = installed
    desired = source / 'qorqut-agentic/src/qorqut_agentic/resources/defaults/overseer/overseer/instructions/review.md'
    desired.write_bytes(desired.read_bytes() + b'\nSynthetic changed guidance.\n')
    refresh_export(source)
    plan = upgrade.prepare_upgrade(paths, source)
    seen = []
    def fail(point, **kwargs):
        if point == stage and not seen:
            seen.append(point)
            raise RuntimeError('synthetic interruption')
    monkeypatch.setattr(managed_update, '_fault_hook', fail)
    with pytest.raises(RuntimeError, match='synthetic interruption'):
        upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    before = snapshot_instance(paths)
    with pytest.raises(DomainError):
        open_installation(paths, tuple(plan['selected_extensions']))
    assert snapshot_instance(paths) == before
    monkeypatch.setattr(managed_update, '_fault_hook', lambda *a, **kw: None)
    shutil.rmtree(source)
    result = upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    assert result['recovered_partial_attempt'] and result['change_id']
    open_installation(paths, tuple(plan['selected_extensions']))
    before = snapshot_instance(paths)
    replay = upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    assert replay['replayed'] and not replay['worked']
    assert snapshot_instance(paths) == before


@pytest.mark.parametrize('foreign', ['file', 'git', 'index', 'ref', 'runtime', 'attribution', 'source', 'provision_mode'])
def test_foreign_changes_after_interruption_refuse(installed, monkeypatch, foreign):
    import sqlite3
    from bazarlyk.agentic import managed_update
    paths, source = installed
    desired = source / 'qorqut-agentic/src/qorqut_agentic/resources/defaults/overseer/overseer/instructions/review.md'
    desired.write_bytes(desired.read_bytes() + b'\nSynthetic changed guidance.\n')
    refresh_export(source)
    plan = upgrade.prepare_upgrade(paths, source)
    def fail(point, **kwargs):
        if point == 'file':
            raise RuntimeError('synthetic interruption')
    monkeypatch.setattr(managed_update, '_fault_hook', fail)
    with pytest.raises(RuntimeError):
        upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    monkeypatch.setattr(managed_update, '_fault_hook', lambda *a, **kw: None)
    if foreign == 'file':
        (paths.agentic_root / plan['changes'][0]['path']).write_bytes(b'foreign synthetic edit\n')
    elif foreign == 'git':
        (paths.agentic_root / 'foreign.txt').write_text('foreign synthetic git work\n')
    elif foreign == 'index':
        import subprocess
        subprocess.run(['git', '-C', str(paths.agentic_root), 'add', '--', plan['changes'][0]['path']], check=True)
    elif foreign == 'ref':
        import subprocess
        subprocess.run(['git', '-C', str(paths.agentic_root), 'branch', 'synthetic-other'], check=True)
        subprocess.run(['git', '-C', str(paths.agentic_root), 'symbolic-ref', 'HEAD', 'refs/heads/synthetic-other'], check=True)
    elif foreign == 'runtime':
        conn = sqlite3.connect(paths.agentic_runtime / 'state.sqlite3')
        try:
            conn.execute("INSERT INTO meta(key,value) VALUES ('foreign_synthetic','unchanged')")
            conn.commit()
        finally:
            conn.close()
    elif foreign == 'source':
        desired.write_bytes(desired.read_bytes() + b'\nForeign export edit.\n')
    elif foreign == 'provision_mode':
        (paths.operations / '.agentic-worktrees').chmod(0o500)
    attr = {**ATTR, 'reason': 'Changed attribution'} if foreign == 'attribution' else ATTR
    before = snapshot_instance(paths)
    with pytest.raises(DomainError):
        upgrade.apply_upgrade(paths, plan, attribution=attr)
    assert snapshot_instance(paths) == before
    if foreign == 'provision_mode':
        assert (paths.operations / '.agentic-worktrees').stat().st_mode & 0o777 == 0o500


def test_foreign_edit_at_check_before_effect(installed, monkeypatch):
    from bazarlyk.agentic import managed_update
    paths, source = installed
    desired = source / 'qorqut-agentic/src/qorqut_agentic/resources/defaults/overseer/overseer/instructions/review.md'
    desired.write_bytes(desired.read_bytes() + b'\nSynthetic changed guidance.\n')
    refresh_export(source)
    plan = upgrade.prepare_upgrade(paths, source)
    affected = []
    def change(point, **kwargs):
        if point == 'before_file' and not affected:
            path = paths.agentic_root / kwargs['path']
            path.write_bytes(b'foreign synthetic edit\n')
            affected.append(path)
    monkeypatch.setattr(managed_update, '_fault_hook', change)
    with pytest.raises(DomainError):
        upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    assert affected and affected[0].read_bytes() == b'foreign synthetic edit\n'


from test_layout_intent import legacy


def test_legacy_two_updates_and_historical_replay(legacy, tmp_path, monkeypatch):
    import shutil
    from agentic_installation_support import snapshot
    from test_layout_intent import ATTR as LAYOUT_ATTR
    from test_agentic_managed_plan import scan_and_commit
    paths, old_source = legacy
    (paths.agentic_root / 'packages/user-preferences/SKILL.md').write_text('Synthetic legacy preference.\n')
    layout = upgrade.prepare_upgrade(paths, old_source)
    assert layout['complete'], layout['conflicts']
    upgrade.apply_upgrade(paths, layout, attribution=LAYOUT_ATTR)
    migrated, _ = upgrade.read_binding(paths)
    source = snapshot(tmp_path / 'current-source', optional=migrated['selected_extensions'])
    bootstrap = source / 'qorqut-agentic/src/qorqut_agentic/resources/defaults/operations/agentic-management/bootstrap.md'
    (paths.agentic_root / 'operations/agentic-management/bootstrap.md').write_bytes(bootstrap.read_bytes())
    scan_and_commit(paths)
    first = upgrade.prepare_upgrade(paths, source)
    assert first['complete'], first['conflicts']
    from bazarlyk.agentic import managed_update
    def fail(stage, **kwargs):
        if stage == 'file':
            raise RuntimeError('synthetic legacy interruption')
    monkeypatch.setattr(managed_update, '_fault_hook', fail)
    with pytest.raises(RuntimeError):
        upgrade.apply_upgrade(paths, first, attribution=ATTR)
    before = snapshot_instance(paths)
    with pytest.raises(DomainError):
        open_installation(paths, tuple(migrated['selected_extensions']))
    assert snapshot_instance(paths) == before
    monkeypatch.setattr(managed_update, '_fault_hook', lambda *a, **kw: None)
    first_result = upgrade.apply_upgrade(paths, first, attribution=ATTR)
    assert first_result['recovered_partial_attempt']
    first_binding, _ = upgrade.read_binding(paths)
    assert first_binding['preferences_seed'] == migrated['preferences_seed']
    assert any(s['id'] == 'custom-synthetic' and not s['enabled'] for s in first_binding['inventory']['sources'])
    guidance = source / 'qorqut-agentic/src/qorqut_agentic/resources/defaults/overseer/overseer/instructions/review.md'
    guidance.write_bytes(guidance.read_bytes() + b'\nSecond synthetic update.\n')
    refresh_export(source)
    second = upgrade.prepare_upgrade(paths, source)
    assert second['complete'], second['conflicts']
    second_result = upgrade.apply_upgrade(paths, second, attribution=ATTR)
    assert second_result['upgrade_commit'] != first_result['upgrade_commit']
    open_installation(paths, tuple(migrated['selected_extensions']))
    (paths.agentic_root / 'user-preferences/SKILL.md').write_text('Later synthetic owner change.\n')
    scan_and_commit(paths)
    shutil.rmtree(source)
    before = snapshot_instance(paths)
    replay = upgrade.apply_upgrade(paths, first, attribution=ATTR)
    assert replay['replayed'] and replay['upgrade_commit'] == first_result['upgrade_commit']
    assert snapshot_instance(paths) == before
    open_installation(paths, tuple(migrated['selected_extensions']))
    snapshot = __import__('pathlib').Path(first_result['prior_binding']['snapshot'])
    original = snapshot.read_bytes()
    try:
        snapshot.unlink()
        with pytest.raises(DomainError):
            open_installation(paths, tuple(migrated['selected_extensions']))
        with pytest.raises(DomainError):
            upgrade.apply_upgrade(paths, first, attribution=ATTR)
    finally:
        snapshot.write_bytes(original)
        snapshot.chmod(0o600)
    open_installation(paths, tuple(migrated['selected_extensions']))


def test_binding_only_activation_has_no_commit_or_event(installed):
    import base64
    import json
    import subprocess
    from test_agentic_managed_plan import scan_and_commit
    paths, source = installed
    manifest = source / 'SOURCE-MANIFEST.json'
    value = json.loads(manifest.read_text())
    value['components'][0]['commit'] = 'c' * 40
    manifest.write_text(json.dumps(value))
    initial = upgrade.prepare_upgrade(paths, source)
    assert initial['changes'] and all(c['path'].endswith('agentic.yaml') for c in initial['changes'])
    for change in initial['changes']:
        (paths.agentic_root / change['path']).write_bytes(base64.b64decode(change['content_base64']))
    scan_and_commit(paths)
    plan = upgrade.prepare_upgrade(paths, source)
    assert plan['complete'] and not plan['no_change'] and not plan['changes']
    head = subprocess.check_output(['git', '-C', str(paths.agentic_root), 'rev-parse', 'HEAD']).decode().strip()
    db = (paths.agentic_runtime / 'state.sqlite3').read_bytes()
    result = upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    assert result['upgrade_commit'] == head and result['change_id'] is None and result['sequence'] is None
    assert subprocess.check_output(['git', '-C', str(paths.agentic_root), 'rev-parse', 'HEAD']).decode().strip() == head
    assert (paths.agentic_runtime / 'state.sqlite3').read_bytes() == db
    open_installation(paths, tuple(plan['selected_extensions']))


def test_completed_proof_tampering_refuses_open_and_replay(installed):
    paths, source = installed
    desired = source / 'qorqut-agentic/src/qorqut_agentic/resources/defaults/overseer/overseer/instructions/review.md'
    desired.write_bytes(desired.read_bytes() + b'\nSynthetic changed guidance.\n')
    refresh_export(source)
    plan = upgrade.prepare_upgrade(paths, source)
    result = upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    targets = [__import__('pathlib').Path(result['prior_binding']['snapshot']),
               __import__('pathlib').Path(result['proof']['journal']['path']),
               __import__('pathlib').Path(result['proof']['intent']['path'])]
    for evidence in targets:
        original = evidence.read_bytes()
        try:
            evidence.write_bytes(original + b' ')
            before = snapshot_instance(paths)
            with pytest.raises(DomainError):
                open_installation(paths, tuple(plan['selected_extensions']))
            with pytest.raises(DomainError):
                upgrade.apply_upgrade(paths, plan, attribution=ATTR)
            assert snapshot_instance(paths) == before
        finally:
            evidence.write_bytes(original)
    import json
    from bazarlyk.agentic.support import canonical
    binding = paths.instance_root / 'config/agentic-layer.json'
    original = binding.read_bytes()
    try:
        value = json.loads(original)
        value.pop('managed_upgrade')
        binding.write_bytes(canonical(value) + b'\n')
        with pytest.raises(DomainError):
            open_installation(paths, tuple(plan['selected_extensions']))
        value = json.loads(original)
        value['managed_upgrade']['plan_digest'] = []
        binding.write_bytes(canonical(value) + b'\n')
        with pytest.raises(DomainError):
            open_installation(paths, tuple(plan['selected_extensions']))
    finally:
        binding.write_bytes(original)
    import sqlite3
    db = sqlite3.connect(paths.agentic_runtime / 'state.sqlite3')
    try:
        db.execute("UPDATE meta SET value='synthetic-foreign' WHERE key='instance_id'")
        db.commit()
    finally:
        db.close()
    try:
        with pytest.raises(DomainError):
            open_installation(paths, tuple(plan['selected_extensions']))
        with pytest.raises(DomainError):
            upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    finally:
        db = sqlite3.connect(paths.agentic_runtime / 'state.sqlite3')
        try:
            db.execute("UPDATE meta SET value=? WHERE key='instance_id'", (plan['binding']['instance_id'],))
            db.commit()
        finally:
            db.close()
    open_installation(paths, tuple(plan['selected_extensions']))


@pytest.mark.parametrize('case', ['after_mkdir', 'foreign_creation', 'recorded', 'replaced', 'mode'])
def test_provisioning_interruption_identity(installed, monkeypatch, case):
    from pathlib import Path
    from bazarlyk.agentic import managed_update
    paths, source = installed
    directory = paths.operations / '.agentic-worktrees'
    directory.rmdir()
    plan = upgrade.prepare_upgrade(paths, source)
    assert plan['complete'] and not plan['no_change'] and not plan['changes']
    stop = {'after_mkdir': 'provision_mkdir', 'foreign_creation': 'before_provision_mkdir',
            'recorded': 'provision_recorded', 'replaced': 'provision_recorded', 'mode': 'provision_recorded'}[case]
    def interrupt(point, **kwargs):
        if point == stop and kwargs.get('name') == 'application_worktrees':
            raise RuntimeError('synthetic provision interruption')
    monkeypatch.setattr(managed_update, '_fault_hook', interrupt)
    with pytest.raises(RuntimeError, match='synthetic provision interruption'):
        upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    monkeypatch.setattr(managed_update, '_fault_hook', lambda *a, **kw: None)
    if case == 'foreign_creation':
        directory.mkdir(mode=0o700)
    if case == 'replaced':
        directory.rename(directory.with_name('.held-synthetic-worktrees'))
        directory.mkdir(mode=0o700)
    if case == 'mode':
        directory.chmod(0o500)
    if case == 'recorded':
        result = upgrade.apply_upgrade(paths, plan, attribution=ATTR)
        assert result['recovered_partial_attempt'] and result['change_id'] is None
        open_installation(paths, tuple(plan['selected_extensions']))
    else:
        before = snapshot_instance(paths)
        with pytest.raises(DomainError, match='Ambiguous|identity changed'):
            upgrade.apply_upgrade(paths, plan, attribution=ATTR)
        assert directory.is_dir() and not list(directory.iterdir())
        if case == 'mode':
            assert directory.stat().st_mode & 0o777 == 0o500
        assert snapshot_instance(paths) == before


@pytest.mark.parametrize('change', ['tag', 'config', 'filter'])
def test_intent_only_resume_allows_safe_unrelated_git_edits(installed, monkeypatch, change):
    import subprocess
    from bazarlyk.agentic import managed_update
    paths, source = installed
    desired = source / 'qorqut-agentic/src/qorqut_agentic/resources/defaults/overseer/overseer/instructions/review.md'
    desired.write_bytes(desired.read_bytes() + b'\nSynthetic changed guidance.\n')
    refresh_export(source)
    plan = upgrade.prepare_upgrade(paths, source)
    def interrupt(point, **kwargs):
        if point == 'intent_published':
            raise RuntimeError('synthetic intent interruption')
    monkeypatch.setattr(managed_update, '_fault_hook', interrupt)
    with pytest.raises(RuntimeError):
        upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    monkeypatch.setattr(managed_update, '_fault_hook', lambda *a, **kw: None)
    before_open = snapshot_instance(paths)
    with pytest.raises(DomainError):
        open_installation(paths, tuple(plan['selected_extensions']))
    assert snapshot_instance(paths) == before_open
    git = ['git', '-C', str(paths.agentic_root)]
    if change == 'tag':
        subprocess.run([*git, 'tag', 'synthetic-other'], check=True)
    elif change == 'config':
        subprocess.run([*git, 'config', '--local', 'user.name', 'Synthetic Other'], check=True)
    else:
        subprocess.run([*git, 'config', '--local', 'filter.synthetic.clean', 'false'], check=True)
    config = (paths.agentic_root / '.git/config').read_bytes()
    refs = subprocess.check_output([*git, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/tags'])
    if change == 'filter':
        before = snapshot_instance(paths)
        with pytest.raises(DomainError):
            upgrade.apply_upgrade(paths, plan, attribution=ATTR)
        assert snapshot_instance(paths) == before
        return
    result = upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    assert result['recovered_partial_attempt']
    assert (paths.agentic_root / '.git/config').read_bytes() == config
    assert subprocess.check_output([*git, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/tags']) == refs
    text = subprocess.check_output([*git, 'cat-file', 'commit', result['upgrade_commit']]).decode()
    assert 'author Qorqut <qorqut@localhost>' in text


def test_stale_source_refuses_before_first_effect(installed):
    paths, source = installed
    guidance = source / 'qorqut-agentic/src/qorqut_agentic/resources/defaults/overseer/overseer/instructions/review.md'
    guidance.write_bytes(guidance.read_bytes() + b'\nSynthetic planned update.\n')
    refresh_export(source)
    plan = upgrade.prepare_upgrade(paths, source)
    guidance.write_bytes(guidance.read_bytes() + b'\nUnreviewed source change.\n')
    before = snapshot_instance(paths)
    with pytest.raises(DomainError):
        upgrade.apply_upgrade(paths, plan, attribution=ATTR)
    assert snapshot_instance(paths) == before
    assert not (paths.instance_root / 'config/agentic-layer-history').exists()
