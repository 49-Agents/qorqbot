from types import SimpleNamespace

import pytest

from bazarlyk.agentic.installation import current_seed_files
from bazarlyk.agentic.upgrade import apply_upgrade
from qorqut_operations.contracts import DomainError


def test_seed_inspection_uses_all_current_destinations(tmp_path):
    paths = SimpleNamespace(agentic_root=tmp_path)
    for package, target in (("agentic-management", "operations/agentic-management"),
                            ("overseer", "overseer/overseer"),
                            ("user-preferences", "user-preferences")):
        root = tmp_path / target
        root.mkdir(parents=True)
        (root / "SKILL.md").write_bytes(package.encode())
        assert current_seed_files(paths, package) == {"SKILL.md": package.encode()}
    with pytest.raises(KeyError):
        current_seed_files(paths, "unknown")


def test_layout_apply_rejects_incomplete_plan():
    with pytest.raises(DomainError) as error:
        apply_upgrade(None, {"schema_version": 2, "kind": "agentic_layout_v2"}, attribution={"actor_id": "admin", "actor_kind": "human", "reason": "offline maintenance"})
    assert error.value.code == "invalid_plan"


def test_unresolved_verification_is_not_confused_with_idle_running_control():
    import sqlite3
    from bazarlyk.agentic.layout_inspection import unfinished_work

    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE runs(run_id TEXT, control TEXT);
        CREATE TABLE invocations(invocation_id TEXT, status TEXT, runner TEXT);
        CREATE TABLE run_attempts(run_id TEXT, step_id TEXT, operation_id TEXT,
                                  invocation_id TEXT, status TEXT);
        INSERT INTO runs VALUES ('idle', 'running'), ('paused', 'paused');
    """)
    assert unfinished_work(db) == []
    db.execute("INSERT INTO run_attempts VALUES ('idle', 'step', 'op', 'inv', 'verifying')")
    before = list(db.iterdump())
    assert unfinished_work(db) == [{"code": "unresolved_verification", "run_id": "idle",
                                    "step_id": "step", "operation_id": "op", "invocation_id": "inv",
                                    "attempt_status": "verifying", "receipt_status": None}]
    assert list(db.iterdump()) == before


@pytest.mark.parametrize(("package", "path", "destination"), [
    ("agentic-management", "bootstrap.md", "operations/agentic-management/bootstrap.md"),
    ("overseer", "workflows/apply.yaml", "overseer/overseer/workflows/apply.yaml"),
    ("imported-host-builtins-templates", "native/x", "operations/imported-host-builtins-templates/native/x"),
    ("imported-roles", "native/roles/global-worker/BRIEF.md", "roles/imported-roles/native/roles/global-worker/BRIEF.md"),
    ("imported-roles", "native/skills/learning-application/SKILL.md", "overseer/overseer-role-skills/native/skills/learning-application/SKILL.md"),
    ("imported-roles", "native/skills/calendar/SKILL.md", "operations/imported-role-skills/native/skills/calendar/SKILL.md"),
    ("global-preferences", "SKILL.md", "plugins/global-preferences/SKILL.md"),
])
def test_fixed_legacy_resource_mapping(package, path, destination):
    from bazarlyk.agentic.layout_inspection import legacy_destination
    assert legacy_destination(package, path) == destination


def test_legacy_tree_reads_regular_content_and_reports_symlink(tmp_path):
    from bazarlyk.agentic.layout_inspection import read_legacy_tree
    root = tmp_path / 'instance'
    package = root / 'packages' / 'custom'
    package.mkdir(parents=True)
    (package / 'edited.md').write_bytes(b'edited')
    (package / 'unsafe').symlink_to(package / 'edited.md')
    files, conflicts = read_legacy_tree(root)
    assert files == {'packages/custom/edited.md': b'edited'}
    assert conflicts == [{'code': 'unsafe_authored_file', 'path': 'packages/custom/unsafe'}]


def test_runtime_wal_refusal_preserves_files(tmp_path):
    import sqlite3
    from bazarlyk.agentic.layout_inspection import inspect_runtime

    state = tmp_path / 'state'
    state.mkdir()
    database = state / 'state.sqlite3'
    db = sqlite3.connect(database)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    db.execute("INSERT INTO meta VALUES ('schema_version', '3')")
    db.commit()
    before = {p.name: p.read_bytes() for p in state.iterdir()}
    assert before['state.sqlite3-wal']
    inspection = inspect_runtime(state, tmp_path / 'root', 'inst_x')
    assert inspection['inspected'] is False
    assert [c['code'] for c in inspection['conflicts']] == ['runtime_wal_present']
    assert {p.name: p.read_bytes() for p in state.iterdir()} == before
    db.close()


def test_runtime_inspection_absent_or_empty_wal(tmp_path):
    import sqlite3
    from bazarlyk.agentic.layout_inspection import inspect_runtime
    from qorqut_agentic.store import DDL

    state = tmp_path / 'state'
    state.mkdir()
    root = tmp_path / 'root'
    db = sqlite3.connect(state / 'state.sqlite3')
    db.executescript(DDL)
    db.executemany('INSERT INTO meta VALUES (?, ?)', [('schema_version', '1'), ('root_path', str(root)), ('instance_id', 'inst_x'), ('initialized_at', 'synthetic')])
    db.commit()
    db.close()
    for empty_wal in (False, True):
        if empty_wal:
            (state / 'state.sqlite3-wal').touch()
        before = {p.name: p.read_bytes() for p in state.iterdir()}
        inspection = inspect_runtime(state, root, 'inst_x')
        assert inspection['inspected'] and inspection['migration_required']
        assert not inspection['conflicts']
        assert {p.name: p.read_bytes() for p in state.iterdir()} == before


def test_split_manifest_keeps_ids_prose_and_body_bytes():
    from qorqut_agentic.util import load_yaml
    from bazarlyk.agentic.layout_inspection import transform_legacy
    original = b'''schema_version: 1\nid: imported-roles\nname: Original role bundle\ndescription: Keep this exact meaning.\nentries:\n- id: old-role\n  kind: instruction\n  description: role prose\n  when_to_use: role prose\n  path: native/roles/example/BRIEF.md\n  requires: [imported-roles/old-skill]\n- id: old-skill\n  kind: skill\n  description: skill prose\n  when_to_use: skill prose\n  path: native/skills/calendar/SKILL.md\n'''
    role = b'custom locally edited role body'
    skill = b'custom locally edited skill body'
    files = {'packages/imported-roles/agentic.yaml': original,
             'packages/imported-roles/native/roles/example/BRIEF.md': role,
             'packages/imported-roles/native/skills/calendar/SKILL.md': skill}
    result = transform_legacy(files)
    assert not result['conflicts']
    assert result['files']['roles/imported-roles/native/roles/example/BRIEF.md'] == role
    assert result['files']['operations/imported-role-skills/native/skills/calendar/SKILL.md'] == skill
    assert result['ref_map']['imported-roles/old-skill'] == 'imported-role-skills/old-skill'
    manifest = load_yaml(result['files']['roles/imported-roles/agentic.yaml'].decode())
    assert manifest['description'] == 'Keep this exact meaning.'
    assert manifest['entries'][0]['requires'] == ['imported-role-skills/old-skill']
    assert len(result['path_map']['packages/imported-roles/agentic.yaml']) == 2
    assert len(result['rewrites']) == 2


def test_native_role_paths_change_but_policy_and_version_are_preserved():
    from qorqut_agentic.util import load_yaml
    from bazarlyk.agentic.layout_inspection import transform_legacy
    role = b'''id: example\nversion: 1.2.3\nbrief: roles/example/BRIEF.md\nskills:\n- id: calendar\n  path: skills/calendar/SKILL.md\ndefault_tool_policy:\n  existing: allow\n'''
    manifest = b'''schema_version: 1\nid: imported-roles\nname: Roles\ndescription: Kept\nentries: []\n'''
    result = transform_legacy({'packages/imported-roles/agentic.yaml': manifest,
        'packages/imported-roles/native/roles/example/role.yaml': role,
        'packages/imported-roles/native/roles/example/BRIEF.md': b'brief',
        'packages/imported-roles/native/skills/calendar/SKILL.md': b'skill'})
    assert not result['conflicts']
    target = 'roles/imported-roles/native/roles/example/role.yaml'
    parsed = load_yaml(result['files'][target].decode())
    assert parsed['version'] == '1.2.3' and parsed['default_tool_policy'] == {'existing': 'allow'}
    assert parsed['brief'] == 'roles/imported-roles/native/roles/example/BRIEF.md'
    assert parsed['skills'][0]['path'] == 'operations/imported-role-skills/native/skills/calendar/SKILL.md'
    assert any(row['new_path'] == target for row in result['rewrites'])


def test_git_inspection_captures_staged_and_untracked_without_writes(tmp_path):
    import subprocess
    from bazarlyk.agentic.layout_inspection import inspect_git

    root = tmp_path / 'repo'
    root.mkdir()
    def git(*args):
        subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)
    git('init', '-q')
    git('config', 'user.name', 'Synthetic')
    git('config', 'user.email', 'synthetic@example.invalid')
    package = root / 'packages' / 'custom'
    package.mkdir(parents=True)
    (package / 'tracked').write_bytes(b'original')
    git('add', '.')
    git('commit', '-qm', 'initial')
    (package / 'tracked').write_bytes(b'edited')
    git('add', 'packages/custom/tracked')
    (package / 'untracked').write_bytes(b'new')
    before = (root / '.git' / 'index').read_bytes()
    result = inspect_git(root)
    assert not result['conflicts']
    assert result['head'] and result['index_sha256'] and result['index_entries']
    assert any('packages/custom/tracked' in row for row in result['dirty'])
    assert any('packages/custom/untracked' in row for row in result['dirty'])
    assert (root / '.git' / 'index').read_bytes() == before


def test_inventory_split_preserves_original_supplier_and_binding_metadata():
    from bazarlyk.agentic.layout_inspection import transform_inventory, transform_legacy
    manifest = b'''schema_version: 1\nid: imported-roles\nname: Roles\ndescription: kept\nentries:\n- id: r\n  kind: instruction\n  description: role\n  when_to_use: role\n  path: native/roles/a/BRIEF.md\n- id: s\n  kind: skill\n  description: skill\n  when_to_use: skill\n  path: native/skills/calendar/SKILL.md\n'''
    mapping = transform_legacy({'packages/imported-roles/agentic.yaml': manifest,
        'packages/imported-roles/native/roles/a/BRIEF.md': b'role',
        'packages/imported-roles/native/skills/calendar/SKILL.md': b'skill'})
    entries = [{'id': 'r', 'kind': 'instruction', 'path': 'native/roles/a/BRIEF.md'},
               {'id': 's', 'kind': 'skill', 'path': 'native/skills/calendar/SKILL.md'}]
    source = {'id': 'roles', 'commit': 'old-commit'}
    items = {entry['path']: {'sha256': 'original', 'provenance': {'source_id': 'roles', 'path': entry['path'], 'commit': 'old-commit'}} for entry in entries}
    inv = {'schema_version': 1, 'sources': [source], 'source_digest': 'old-digest',
           'packages': [{'id': 'imported-roles', 'name': 'Roles', 'description': 'kept',
                         'native': {'enabled': True}, 'entries': entries, 'files': items}],
           'assets': [{'source_id': 'roles', 'path': 'skills/calendar/SKILL.md', 'package_id': 'imported-roles',
                       'destination': 'packages/imported-roles/native/skills/calendar/SKILL.md', 'sha256': 'original'}],
           'bindings': [{'kind': 'role', 'native_id': 'a', 'package_id': 'imported-roles',
                         'entry_refs': ['imported-roles/r', 'imported-roles/s'],
                         'skills': [{'native_skill_id': 'calendar', 'canonical_entry': 'imported-roles/s'}]}],
           'coverage': {'package_count': 1}}
    result, conflicts = transform_inventory(inv, mapping)
    assert not conflicts and result['schema_version'] == 2
    assert result['sources'] == [source] and result['source_digest'] == 'old-digest'
    assert {p['path'] for p in result['packages']} == {'roles/imported-roles', 'operations/imported-role-skills'}
    assert result['assets'][0]['destination'] == 'operations/imported-role-skills/native/skills/calendar/SKILL.md'
    assert result['bindings'][0]['skills'][0]['canonical_entry'] == 'imported-role-skills/s'
    assert result['bindings'][0]['package_id'] == 'imported-roles'


def test_planned_catalogue_detects_unresolved_ref_without_rewriting_body():
    from bazarlyk.agentic.layout_inspection import validate_catalog
    manifest = b'''schema_version: 1\nid: custom\nname: Custom\ndescription: Custom\nentries:\n- id: text\n  kind: instruction\n  description: Text\n  when_to_use: Text\n  path: body.md\n  requires: [missing/entry]\n'''
    body = b'remain unchanged'
    issues = validate_catalog({'plugins/custom/agentic.yaml': manifest, 'plugins/custom/body.md': body})
    assert issues and any('missing/entry' in item['message'] for item in issues)
    assert body == b'remain unchanged'


def test_layout_plan_is_deterministic_and_preserves_legacy_instance(tmp_path):
    import sqlite3
    import subprocess
    from bazarlyk.agentic.layout_upgrade import prepare_layout
    from bazarlyk.agentic.support import digest
    from qorqut_agentic.store import DDL

    root, state = tmp_path / 'authored', tmp_path / 'state'
    root.mkdir()
    state.mkdir()
    subprocess.run(['git', '-C', str(root), 'init', '-q'], check=True)
    subprocess.run(['git', '-C', str(root), 'config', 'user.name', 'Synthetic'], check=True)
    subprocess.run(['git', '-C', str(root), 'config', 'user.email', 'synthetic@example.invalid'], check=True)
    package = root / 'packages' / 'custom'
    package.mkdir(parents=True)
    (package / 'agentic.yaml').write_text('schema_version: 1\nid: custom\nname: Custom\ndescription: Synthetic\nentries: []\n')
    (package / 'untracked.md').write_bytes(b'locally authored')
    subprocess.run(['git', '-C', str(root), 'add', 'packages/custom/agentic.yaml'], check=True)
    subprocess.run(['git', '-C', str(root), 'commit', '-qm', 'fixture'], check=True)
    identity = 'inst_' + 'a' * 32
    database = state / 'state.sqlite3'
    connection = sqlite3.connect(database)
    connection.executescript(DDL)
    connection.executemany('INSERT INTO meta VALUES (?, ?)', [('schema_version', '1'), ('root_path', str(root)), ('instance_id', identity), ('initialized_at', 'synthetic')])
    connection.execute("INSERT INTO consumers VALUES ('historical', 0, 'synthetic')")
    connection.commit()
    connection.close()
    inventory = {'schema_version': 1, 'packages': [], 'sources': [], 'bindings': [], 'assets': [],
                 'source_digest': digest([]), 'coverage': {'package_count': 0, 'complete': True}, 'diagnostics': []}
    receipt = {'schema_version': 1, 'files': {'packages/custom/deleted.md': 'd' * 64}}
    receipt['manifest_digest'] = digest(receipt)
    record = {'version': 1, 'instance_id': identity, 'root': str(root), 'state': str(state),
              'inventory': inventory, 'inventory_sha256': digest(inventory), 'import_manifest': receipt,
              'source_manifest_sha256': 'old-supplier'}
    prepared = {'export_root': '/synthetic/export', 'source_manifest_sha256': 'new-export',
                'selected_extensions': [], 'preferences_seed': {'agentic.yaml': b'fixture', 'SKILL.md': b'fixture'}}
    paths = SimpleNamespace(agentic_root=root, agentic_runtime=state)
    def snapshot():
        return {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    before = snapshot()
    first = prepare_layout(paths, record, 'old-binding-sha', prepared)
    second = prepare_layout(paths, record, 'old-binding-sha', prepared)
    assert first['plan_digest'] == second['plan_digest']
    assert first['path_map']['packages/custom/untracked.md'] == ['plugins/custom/untracked.md']
    assert first['files']['plugins/custom/untracked.md']['sha256'] == digest(b'locally authored')
    assert first['preservation']['locally_absent_files'] == [{'old_path': 'packages/custom/deleted.md',
        'new_path': 'plugins/custom/deleted.md', 'recorded_sha256': 'd' * 64}]
    assert not first['complete'] and any(item['code'] == 'missing_required_package' for item in first['conflicts'])
    assert snapshot() == before
    collision = root / 'plugins' / 'custom'
    collision.mkdir(parents=True)
    (collision / 'agentic.yaml').write_text('existing destination')
    refused = prepare_layout(paths, record, 'old-binding-sha', prepared)
    assert any(item['code'] == 'destination_conflict' and item['path'] == 'plugins/custom' for item in refused['conflicts'])


@pytest.mark.parametrize('version', ['1', '2', '3'])
def test_real_runtime_schema_versions_are_inspected_without_mutation(tmp_path, version):
    import sqlite3
    from bazarlyk.agentic.layout_inspection import inspect_runtime
    from qorqut_agentic.store import DDL
    from qorqut_agentic.overseer.schema import install, install_v3

    state, root = tmp_path / 'state', tmp_path / 'root'
    state.mkdir()
    database = state / 'state.sqlite3'
    connection = sqlite3.connect(database)
    connection.executescript(DDL)
    if version in ('2', '3'):
        install(connection)
    if version == '3':
        install_v3(connection)
    connection.executemany('INSERT INTO meta VALUES (?, ?)', [('schema_version', version), ('root_path', str(root)),
                         ('instance_id', 'inst_synthetic'), ('initialized_at', 'synthetic')])
    connection.commit()
    connection.close()
    before = database.read_bytes()
    result = inspect_runtime(state, root, 'inst_synthetic')
    assert result['inspected'] and not result['conflicts']
    assert result['schema_version'] == version
    assert result['migration_required'] == (version != '3')
    assert database.read_bytes() == before and list(state.iterdir()) == [database]


def _singleton_inventory(path='user-preferences'):
    import base64
    from bazarlyk.agentic.support import digest
    body = b'---\nname: preferences\ndescription: Synthetic\n---\n\nOwner edited.\n'
    source = {'id': 'custom-synthetic', 'enabled': True, 'commit': 'a' * 40}
    entry = {'id': 'preferences', 'kind': 'skill', 'description': 'Preference',
             'when_to_use': 'All work', 'path': 'SKILL.md'}
    item = {'content_base64': base64.b64encode(body).decode(), 'sha256': digest(body),
            'provenance': {'source_id': 'custom-synthetic', 'path': 'source/SKILL.md', 'commit': 'a' * 40}}
    return {'schema_version': 2, 'sources': [source], 'source_digest': digest([source]),
            'packages': [{'id': 'user-preferences', 'path': path, 'name': 'Preferences', 'description': 'Owner text',
                          'native': {'enabled': True}, 'entries': [entry], 'files': {'SKILL.md': item}}],
            'assets': [{'source_id': 'custom-synthetic', 'path': 'source/SKILL.md', 'package_id': 'user-preferences',
                        'destination': path + '/SKILL.md', 'sha256': digest(body), 'enabled': True, 'template': False}],
            'bindings': [], 'coverage': {'complete': True}, 'diagnostics': []}


def test_singleton_inventory_path_is_canonical_and_preserves_provenance():
    from bazarlyk.agentic.inventory import Inventory, package_path
    from bazarlyk.agentic.plans import build_import_plan
    from bazarlyk.agentic.view import HostAgenticView
    inventory = _singleton_inventory()
    assert package_path('user-preferences') == 'user-preferences'
    assert Inventory().package('user-preferences', 'synthetic', True)['path'] == 'user-preferences'
    HostAgenticView(None, inventory)
    plan = build_import_plan(inventory)
    assert 'user-preferences/SKILL.md' in plan['desired_files']
    assert inventory['packages'][0]['files']['SKILL.md']['provenance']['path'] == 'source/SKILL.md'
    assert inventory['sources'][0]['commit'] == 'a' * 40


@pytest.mark.parametrize('path', ['plugins/user-preferences', 'user-preferences/user-preferences', 'custom'])
def test_singleton_inventory_rejects_wrong_destinations(path):
    from bazarlyk.agentic.plans import build_import_plan
    from bazarlyk.agentic.view import HostAgenticView
    from bazarlyk.agentic.support import ImportFailure
    inventory = _singleton_inventory(path)
    with pytest.raises(ImportFailure):
        build_import_plan(inventory)
    with pytest.raises(DomainError):
        HostAgenticView(None, inventory)


def test_singleton_requires_canonical_skill():
    from bazarlyk.agentic.plans import build_import_plan
    from bazarlyk.agentic.view import HostAgenticView
    from bazarlyk.agentic.support import ImportFailure
    inventory = _singleton_inventory()
    inventory['packages'][0]['entries'][0]['id'] = 'other'
    with pytest.raises(ImportFailure):
        build_import_plan(inventory)
    with pytest.raises(DomainError):
        HostAgenticView(None, inventory)


def test_legacy_inventory_singleton_moves_with_original_source():
    from bazarlyk.agentic.layout_inspection import transform_inventory, transform_legacy
    from bazarlyk.agentic.view import HostAgenticView
    inventory = _singleton_inventory('packages/user-preferences')
    inventory['schema_version'] = 1
    inventory['packages'][0].pop('path')
    manifest = b'''schema_version: 1\nid: user-preferences\nname: Preferences\ndescription: Owner text\nentries:\n- id: preferences\n  kind: skill\n  description: Preference\n  when_to_use: All work\n  path: SKILL.md\n'''
    import base64
    body = base64.b64decode(inventory['packages'][0]['files']['SKILL.md']['content_base64'])
    mapping = transform_legacy({'packages/user-preferences/agentic.yaml': manifest,
                                'packages/user-preferences/SKILL.md': body})
    target, conflicts = transform_inventory(inventory, mapping)
    assert not conflicts
    assert target['packages'][0]['path'] == 'user-preferences'
    assert target['sources'] == inventory['sources']
    assert target['packages'][0]['files']['SKILL.md']['provenance'] == inventory['packages'][0]['files']['SKILL.md']['provenance']
    HostAgenticView(None, target)


@pytest.mark.parametrize('damage', ['none', 'empty_legacy', 'legacy_file', 'legacy_zero', 'legacy_hidden', 'legacy_symlink', 'legacy_fifo', 'legacy_reserved', 'legacy_depth', 'inventory', 'binding', 'missing_preferences', 'malformed_preferences', 'blank_preferences', 'wrong_preference_ref', 'missing_guidance'])
def test_current_layout_returns_no_change_without_reinstall(tmp_path, monkeypatch, damage):
    import json
    from agentic_installation_support import snapshot
    from bazarlyk.agentic import installation, upgrade
    from bazarlyk.agentic.support import canonical, digest
    export = snapshot(tmp_path / 'export')
    monkeypatch.setattr(installation.metadata, 'version', lambda _: 'synthetic')
    prepared = installation.prepare_installation(export, [])
    instance = tmp_path / 'instance'
    instance.mkdir()
    paths = SimpleNamespace(instance_root=instance, agentic_root=instance / 'agentic', agentic_runtime=instance / 'runtime')
    installation.initialize_installation(paths, prepared)
    if damage.startswith('legacy_') or damage == 'empty_legacy':
        import os
        nested = paths.agentic_root / 'packages/old/nested'
        nested.mkdir(parents=True)
        if damage == 'legacy_depth':
            for _ in range(64):
                nested = nested / 'd'
                nested.mkdir()
        elif damage == 'legacy_symlink':
            (nested / 'link').symlink_to(paths.agentic_root / 'plugins', target_is_directory=True)
        elif damage == 'legacy_fifo':
            os.mkfifo(nested / 'pipe')
        elif damage != 'empty_legacy':
            name = '.hidden' if damage == 'legacy_hidden' else '.agentic-tmp-test' if damage == 'legacy_reserved' else 'file'
            (nested / name).write_bytes(b'' if damage == 'legacy_zero' else b'legacy')
    if damage in ('inventory', 'binding'):
        binding_path = instance / 'config' / 'agentic-layer.json'
        binding = json.loads(binding_path.read_bytes())
        if damage == 'inventory':
            binding['inventory']['coverage']['complete'] = False
        else:
            binding['inventory']['bindings'][0]['entry_refs'].append('missing/entry')
        binding['inventory_sha256'] = digest(binding['inventory'])
        binding_path.write_bytes(canonical(binding) + b'\n')
    elif damage == 'missing_preferences':
        (paths.agentic_root / 'user-preferences' / 'SKILL.md').unlink()
    elif damage == 'malformed_preferences':
        (paths.agentic_root / 'user-preferences' / 'SKILL.md').write_bytes(b'\xff')
    elif damage == 'blank_preferences':
        (paths.agentic_root / 'user-preferences' / 'SKILL.md').write_bytes(b' \n\t\n')
    elif damage == 'wrong_preference_ref':
        manifest = paths.agentic_root / 'user-preferences' / 'agentic.yaml'
        manifest.write_text(manifest.read_text().replace('id: preferences', 'id: other'))
    elif damage == 'missing_guidance':
        (paths.agentic_root / 'operations' / 'agentic-management' / 'bootstrap.md').unlink()
    def snapshot_files():
        return {str(p.relative_to(instance)): p.read_bytes() for p in instance.rglob('*') if p.is_file() and not p.is_symlink()}
    before = snapshot_files()
    plan = upgrade.prepare_upgrade(paths, export)
    assert plan['kind'] == 'agentic_layout_v2' and not plan['layout_change']
    assert plan['complete'] == (damage in ('none', 'empty_legacy'))
    if damage in ('inventory', 'binding'):
        assert any(item['code'] == 'invalid_current_binding' for item in plan['conflicts'])
    elif damage == 'blank_preferences':
        assert any(item['code'] == 'catalogue_empty_canonical_preferences' for item in plan['conflicts'])
    elif damage.startswith('legacy_'):
        assert plan['conflicts']
    elif damage not in ('none', 'empty_legacy'):
        assert any(item['code'].startswith('catalogue_') for item in plan['conflicts'])
    assert not plan['files'] and not plan['path_map']
    assert snapshot_files() == before
    if damage in ('none', 'empty_legacy'):
        installation.open_installation(paths, ())
        receipt = upgrade.apply_upgrade(paths, plan, attribution={
            'actor_id': 'administrator', 'actor_kind': 'human', 'reason': 'Offline maintenance'})
        assert receipt['no_change'] is True
        assert receipt['plan_digest'] == plan['plan_digest']
        from bazarlyk.agentic import layout_intent
        with monkeypatch.context() as patch:
            patch.setattr(layout_intent, 'incomplete_intents', lambda _: [object()])
            with pytest.raises(DomainError, match='unfinished'):
                upgrade.apply_upgrade(paths, plan, attribution={'actor_id': 'administrator', 'actor_kind': 'human', 'reason': 'Offline maintenance'})
        assert snapshot_files() == before
        import sqlite3
        with sqlite3.connect(paths.agentic_runtime / 'state.sqlite3') as db:
            db.execute("INSERT INTO consumers VALUES ('synthetic', 0, '2026-01-01')")
        changed = snapshot_files()
        with pytest.raises(DomainError) as error:
            upgrade.apply_upgrade(paths, plan, attribution={
                'actor_id': 'administrator', 'actor_kind': 'human', 'reason': 'Offline maintenance'})
        assert error.value.code == 'operation_conflict'
        assert snapshot_files() == changed


def test_legacy_manifest_collisions_and_invalid_singleton_refuse():
    from bazarlyk.agentic.layout_inspection import transform_legacy
    role_manifest = b'''schema_version: 1\nid: imported-roles\nname: Roles\ndescription: Synthetic\nentries: []\n'''
    collision_manifest = b'''schema_version: 1\nid: imported-role-skills\nname: Custom\ndescription: Synthetic\nentries: []\n'''
    singleton = b'''schema_version: 1\nid: user-preferences\nname: Preferences\ndescription: Custom\nentries:\n- id: other\n  kind: skill\n  path: SKILL.md\n'''
    result = transform_legacy({'packages/imported-roles/agentic.yaml': role_manifest,
        'packages/imported-roles/native/skills/calendar/SKILL.md': b'skill',
        'packages/imported-role-skills/agentic.yaml': collision_manifest,
        'packages/user-preferences/agentic.yaml': singleton,
        'packages/user-preferences/SKILL.md': b'content'})
    assert any(item['code'] == 'duplicate_package_id' for item in result['conflicts'])
    assert any(item['code'] == 'malformed_manifest' and item['path'] == 'packages/user-preferences/agentic.yaml' for item in result['conflicts'])


def test_active_receipt_and_unfinished_publication_are_conflicts():
    import sqlite3
    from bazarlyk.agentic.layout_inspection import unfinished_work
    db = sqlite3.connect(':memory:')
    db.executescript('''
        CREATE TABLE invocations(invocation_id TEXT,status TEXT,runner TEXT);
        CREATE TABLE overseer_publications(change_id TEXT,operation_id TEXT,stage TEXT);
        INSERT INTO invocations VALUES ('running-tool','in_flight','synthetic-runner');
        INSERT INTO invocations VALUES ('finished-tool','succeeded',NULL);
        INSERT INTO overseer_publications VALUES ('active','op1','publishing');
        INSERT INTO overseer_publications VALUES ('saved-idle','op2','prepared');
    ''')
    conflicts = unfinished_work(db)
    assert {item['code'] for item in conflicts} == {'in_flight_invocation', 'unfinished_publication'}
    assert all(item.get('change_id') != 'saved-idle' and item.get('invocation_id') != 'finished-tool' for item in conflicts)


def test_unsettled_merged_applications_block_but_completed_and_premerge_do_not(tmp_path):
    import sqlite3
    from bazarlyk.agentic.layout_inspection import unfinished_work
    database = tmp_path / 'runtime.sqlite3'
    db = sqlite3.connect(database)
    db.execute('CREATE TABLE delta_applications(id TEXT, delta_id TEXT, phase TEXT, merge TEXT, checks TEXT NOT NULL)')
    db.executemany('INSERT INTO delta_applications VALUES (?, ?, ?, ?, ?)', [
        ('waiting', 'delta-a', 'merged', '{"merged_commit":"a"}', '{"state":"pending","phase":"post_merge"}'),
        ('failed', 'delta-b', 'working', '{"merged_commit":"b"}', '{"state":"failed","phase":"post_merge"}'),
        ('phaseonly', 'delta-e', 'merged', None, '{"state":"pending","phase":"post_merge"}'),
        ('done', 'delta-c', 'complete', '{"merged_commit":"c"}', '{"state":"passed","phase":"post_merge"}'),
        ('idle', 'delta-d', 'planning', None, '{"state":"pending","phase":"pre_merge"}'),
    ])
    db.commit()
    db.close()
    before = database.read_bytes()
    db = sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        conflicts = unfinished_work(db)
    finally:
        db.close()
    assert {item['application_id'] for item in conflicts} == {'waiting', 'failed', 'phaseonly'}
    assert {item['check_state'] for item in conflicts} == {'pending', 'failed'}
    assert {item['delta_id'] for item in conflicts} == {'delta-a', 'delta-b', 'delta-e'}
    assert all(item['code'] == 'unsettled_merged_application' and item['check_phase'] == 'post_merge' for item in conflicts)
    assert database.read_bytes() == before and list(tmp_path.iterdir()) == [database]
