import copy
import os
from pathlib import Path

import pytest
from qorqut_operations.contracts import DomainError
from agentic_installation_support import snapshot
from instance_support import synthetic_paths
from bazarlyk.agentic.installation import initialize_installation, prepare_installation
from bazarlyk.agentic.managed_plan import prepare, public_plan, validate_plan
from bazarlyk.agentic.provisioning import apply as provision


@pytest.fixture
def installed(tmp_path):
    root = tmp_path / 'instance'
    root.mkdir()
    paths = synthetic_paths(root)
    source = snapshot(tmp_path / 'export')
    paths.operations.mkdir(parents=True, exist_ok=True)
    paths.runtime.mkdir(parents=True, exist_ok=True)
    initialize_installation(paths, prepare_installation(source, []))
    provision(paths)
    return paths, source


def test_repeat_and_digest(installed):
    paths, source = installed
    plan = prepare(paths, source)
    assert plan['complete'], plan['conflicts']
    assert plan['no_change']
    assert not plan['changes']
    assert validate_plan(paths, public_plan(plan))
    changed = copy.deepcopy(public_plan(plan))
    changed['grants'] = {'different': True}
    assert validate_plan(paths, changed)
    changed['evidence']['intent'] += '.other'
    with pytest.raises(DomainError):
        validate_plan(paths, changed)
    assert public_plan(prepare(paths, source)) == public_plan(plan)
    import json
    import sqlite3
    paths.database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(paths.database) as db:
        db.execute('CREATE TABLE agent_profiles (agent_id TEXT, profile_json TEXT, revision INTEGER, home_business_id TEXT, status TEXT)')
        profile = {'role': {'id': 'learning-applier'}, 'scope': {'global_capabilities': ['manage_agentic_layer']}}
        db.execute('INSERT INTO agent_profiles VALUES (?, ?, ?, ?, ?)', ('fixture-agent', json.dumps(profile), 1, None, 'active'))
    before = snapshot_instance(paths)
    updated_grants = prepare(paths, source)
    assert updated_grants['grants']['agent_id'] == 'fixture-agent'
    assert updated_grants['plan_digest'] == plan['plan_digest']
    assert updated_grants['evidence'] == plan['evidence']
    assert snapshot_instance(paths) == before


def refresh_export(root):
    import json
    from bazarlyk.source_snapshot import component_digest
    manifest_path = root / 'SOURCE-MANIFEST.json'
    manifest = json.loads(manifest_path.read_text())
    for component in manifest['components']:
        component['snapshot_sha256'] = component_digest(root, component, manifest['components'])
    manifest_path.write_text(json.dumps(manifest))


def snapshot_instance(paths):
    import hashlib
    result = {}
    for root in (paths.agentic_root, paths.agentic_runtime, paths.instance_root / 'config'):
        if root.exists():
            for p in sorted(root.rglob('*')):
                if p.is_file() and not p.is_symlink():
                    result[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return result


def scan_and_commit(paths):
    import subprocess
    from qorqut_agentic import AgenticLayer
    layer = AgenticLayer.open(paths.agentic_root, paths.agentic_runtime)
    layer.changes.scan()
    subprocess.run(['git', '-C', str(paths.agentic_root), 'add', '-A'], check=True)
    subprocess.run(['git', '-C', str(paths.agentic_root), '-c', 'user.name=Fixture', '-c',
                    'user.email=fixture@example.invalid', 'commit', '-qm', 'Synthetic owner changes'], check=True)


def test_update_preserves_owner_preferences_and_custom_plugin(installed):
    paths, source = installed
    preferences = paths.agentic_root / 'user-preferences/SKILL.md'
    preferences.write_text('Synthetic owner preferences.\n')
    custom = paths.agentic_root / 'plugins/custom-extra/owner.txt'
    custom.parent.mkdir(parents=True)
    custom.write_bytes(b'owner data')
    (custom.parent / 'agentic.yaml').write_text('schema_version: 1\nid: custom-extra\nname: Custom\ndescription: Custom\nentries: []\n')
    local = paths.agentic_root / 'operations/agentic-management/bootstrap.md'
    local.write_bytes(local.read_bytes() + b'\nOwner annotation.\n')
    scan_and_commit(paths)
    desired = source / 'qorqut-agentic/src/qorqut_agentic/resources/defaults/overseer/overseer/instructions/review.md'
    desired.write_bytes(desired.read_bytes() + b'\nSupplier annotation.\n')
    refresh_export(source)
    before = snapshot_instance(paths)
    plan = prepare(paths, source)
    assert plan['complete'], plan['conflicts']
    edits = {x['path']: x for x in plan['changes']}
    assert 'overseer/overseer/instructions/review.md' in edits
    assert all(path.endswith('agentic.yaml') or path == 'overseer/overseer/instructions/review.md' for path in edits)
    import base64
    guidance = edits['overseer/overseer/instructions/review.md']
    assert base64.b64decode(guidance['content_base64']) == desired.read_bytes()
    assert guidance['expected_sha256'] == __import__('hashlib').sha256((paths.agentic_root / guidance['path']).read_bytes()).hexdigest()
    assert guidance['desired_sha256'] == __import__('hashlib').sha256(desired.read_bytes()).hexdigest()
    assert guidance['mode'] == (paths.agentic_root / guidance['path']).stat().st_mode & 0o7777
    assert plan['overseer_seed']['desired']['supplier_commit'] == next(s['commit'] for s in plan['target_inventory']['sources'] if s['id'] == 'roles')
    assert plan['management_seed']['preserved_local_paths'][0]['path'] == 'operations/agentic-management/bootstrap.md'
    assert plan['management_seed']['desired']['files']['bootstrap.md'] != __import__('hashlib').sha256(local.read_bytes()).hexdigest()
    assert 'user-preferences/SKILL.md' in plan['preservation']['files']
    assert 'plugins/custom-extra/owner.txt' in plan['preservation']['files']
    assert snapshot_instance(paths) == before


def test_conflict_and_stale_inventory(installed):
    paths, source = installed
    local = paths.agentic_root / 'operations/agentic-management/bootstrap.md'
    local.write_bytes(local.read_bytes() + b'\nOwner change.\n')
    (local.parent / 'collision.md').write_text('Owner content')
    scan_and_commit(paths)
    (source / 'qorqut-agentic/src/qorqut_agentic/resources/defaults/operations/agentic-management/collision.md').write_text('Supplier content')
    desired = source / 'qorqut-agentic/src/qorqut_agentic/resources/defaults/operations/agentic-management/bootstrap.md'
    desired.write_bytes(desired.read_bytes() + b'\nSupplier change.\n')
    refresh_export(source)
    before = snapshot_instance(paths)
    plan = prepare(paths, source)
    assert not plan['complete']
    assert any(c['code'] == 'local_upstream_conflict' and c['path'].endswith('bootstrap.md') for c in plan['conflicts'])
    assert any(c['code'] == 'local_upstream_conflict' and c['path'].endswith('collision.md') and c['previous_sha256'] is None for c in plan['conflicts'])
    assert snapshot_instance(paths) == before
    # Commit without scanning, leaving a clean Git tree but stale observed inventory.
    local.write_bytes(local.read_bytes() + b'\nAnother edit.\n')
    import subprocess
    subprocess.run(['git', '-C', str(paths.agentic_root), 'add', '-A'], check=True)
    subprocess.run(['git', '-C', str(paths.agentic_root), '-c', 'user.name=Fixture', '-c',
                    'user.email=fixture@example.invalid', 'commit', '-qm', 'Unobserved edit'], check=True)
    plan = prepare(paths, source)
    assert any(c['code'] == 'unobserved_inventory_divergence' for c in plan['conflicts'])


def test_dirty_and_selection_refused(installed):
    paths, source = installed
    (paths.agentic_root / 'untracked.txt').write_text('untracked')
    before = snapshot_instance(paths)
    plan = prepare(paths, source)
    assert any(c['code'] == 'dirty_git_worktree' for c in plan['conflicts'])
    assert snapshot_instance(paths) == before
    with pytest.raises(DomainError, match='selection'):
        prepare(paths, source, ['paper'])


def test_provenance_only(installed):
    paths, source = installed
    import json
    manifest = source / 'SOURCE-MANIFEST.json'
    doc = json.loads(manifest.read_text())
    doc['components'][0]['commit'] = 'c' * 40
    manifest.write_text(json.dumps(doc))
    plan = prepare(paths, source)
    assert plan['complete'], plan['conflicts']
    assert not plan['no_change']
    # Generated import manifests can include changed supplier provenance.
    assert plan['changes']
    assert all(change['path'].endswith('agentic.yaml') for change in plan['changes'])
    import base64
    for change in plan['changes']:
        assert change['op'] == 'edit'
        (paths.agentic_root / change['path']).write_bytes(base64.b64decode(change['content_base64']))
    scan_and_commit(paths)
    before = snapshot_instance(paths)
    binding_only = prepare(paths, source)
    assert binding_only['complete'], binding_only['conflicts']
    assert not binding_only['no_change']
    assert binding_only['changes'] == []
    assert snapshot_instance(paths) == before


def test_local_deletion_is_preserved(tmp_path):
    root = tmp_path / 'instance'
    root.mkdir()
    paths = synthetic_paths(root)
    source = snapshot(tmp_path / 'export')
    extra = source / 'qorqut-agentic/src/qorqut_agentic/resources/defaults/operations/agentic-management/notes.md'
    extra.write_text('Supplier note.\n')
    refresh_export(source)
    paths.operations.mkdir(parents=True, exist_ok=True)
    paths.runtime.mkdir(parents=True, exist_ok=True)
    initialize_installation(paths, prepare_installation(source, []))
    provision(paths)
    (paths.agentic_root / 'operations/agentic-management/notes.md').unlink()
    scan_and_commit(paths)
    new = source / 'qorqut-agentic/src/qorqut_agentic/resources/defaults/overseer/overseer/instructions/review.md'
    new.write_bytes(new.read_bytes() + b'\nNew supplier guidance.\n')
    refresh_export(source)
    plan = prepare(paths, source)
    assert plan['complete'], plan['conflicts']
    assert any(p['path'].endswith('/notes.md') and p['current_sha256'] is None for p in plan['management_seed']['preserved_local_paths'])
    assert any(c['path'] == 'overseer/overseer/instructions/review.md' for c in plan['changes'])
    assert all(not c['path'].endswith('/notes.md') for c in plan['changes'])


def test_missing_required_native_asset_and_unsafe_path(installed):
    paths, source = installed
    from bazarlyk.agentic.upgrade import read_binding
    record, _ = read_binding(paths)
    path = next(a['destination'] for a in record['inventory']['assets']
                if a.get('enabled') and a.get('kind') == 'instruction' and a.get('destination') and (paths.agentic_root / a['destination']).is_file())
    (paths.agentic_root / path).unlink()
    scan_and_commit(paths)
    plan = prepare(paths, source)
    assert not plan['complete']
    assert any(c['code'] == 'missing_required_projected_asset' and c['path'] == path for c in plan['conflicts'])
    unsafe = paths.agentic_root / 'plugins/unsafe'
    unsafe.symlink_to('/tmp')
    before = snapshot_instance(paths)
    plan = prepare(paths, source)
    assert not plan['complete']
    assert any(c['code'] == 'unsafe_authored_file' for c in plan['conflicts'])
    assert snapshot_instance(paths) == before


def test_invalid_binding_runtime_and_catalog_are_refused(installed):
    import json
    paths, source = installed
    binding = paths.instance_root / 'config/agentic-layer.json'
    original = binding.read_bytes()
    record = json.loads(original)
    record['inventory']['schema_version'] = 1
    binding.write_text(json.dumps(record))
    before = snapshot_instance(paths)
    with pytest.raises(DomainError, match='prerequisite'):
        prepare(paths, source)
    assert snapshot_instance(paths) == before
    binding.write_bytes(original)
    database = paths.agentic_runtime / 'state.sqlite3'
    moved = database.with_name('held-state')
    database.rename(moved)
    before = snapshot_instance(paths)
    try:
        plan = prepare(paths, source)
        assert not plan['complete']
        assert any(c['code'] == 'runtime_inspection_failed' for c in plan['conflicts'])
        assert snapshot_instance(paths) == before
    finally:
        moved.rename(database)
    catalog = paths.agentic_root / 'user-preferences/agentic.yaml'
    catalog.write_text('invalid: [yaml')
    scan_and_commit(paths)
    before = snapshot_instance(paths)
    plan = prepare(paths, source)
    assert not plan['complete']
    assert any(c['code'].startswith('current_catalogue_') for c in plan['conflicts'])
    assert snapshot_instance(paths) == before


def test_missing_selected_export_refused(tmp_path):
    import json
    root = tmp_path / 'instance'
    root.mkdir()
    paths = synthetic_paths(root)
    source = snapshot(tmp_path / 'export', optional=('paper',))
    paths.operations.mkdir(parents=True, exist_ok=True)
    paths.runtime.mkdir(parents=True, exist_ok=True)
    initialize_installation(paths, prepare_installation(source, ['paper']))
    manifest = source / 'SOURCE-MANIFEST.json'
    value = json.loads(manifest.read_text())
    value['components'] = [component for component in value['components'] if component['name'] != 'qorqut-paper']
    manifest.write_text(json.dumps(value))
    before = snapshot_instance(paths)
    with pytest.raises(DomainError) as error:
        prepare(paths, source)
    assert 'qorqut-paper' in error.value.details['missing_components']
    assert snapshot_instance(paths) == before


def test_detached_head_refused(installed):
    import subprocess
    paths, source = installed
    subprocess.run(['git', '-C', str(paths.agentic_root), 'checkout', '--quiet', '--detach'], check=True)
    before = snapshot_instance(paths)
    plan = prepare(paths, source)
    assert plan['git']['branch'] is None
    assert any(c['code'] == 'selected_branch_required' for c in plan['conflicts'])
    assert not plan['complete']
    assert snapshot_instance(paths) == before


def test_enabled_native_ref_must_remain_in_catalog(installed):
    import yaml
    from bazarlyk.agentic.upgrade import read_binding
    paths, source = installed
    record, _ = read_binding(paths)
    binding = next(b for b in record['inventory']['bindings'] if b['kind'] == 'role' and b['enabled'] and b['entry_refs'])
    ref = binding['entry_refs'][0]
    package_id, entry_id = ref.split('/', 1)
    package = next(p for p in record['inventory']['packages'] if p['id'] == package_id)
    manifest = paths.agentic_root / package['path'] / 'agentic.yaml'
    content = yaml.safe_load(manifest.read_text())
    entry = next(e for e in content['entries'] if e['id'] == entry_id)
    assert (manifest.parent / entry['path']).is_file()
    content['entries'] = [e for e in content['entries'] if e['id'] != entry_id]
    manifest.write_text(yaml.safe_dump(content, sort_keys=True, allow_unicode=True))
    scan_and_commit(paths)
    before = snapshot_instance(paths)
    plan = prepare(paths, source)
    assert not plan['complete']
    assert any(c['code'] == 'current_catalogue_missing_canonical_entry' and c['detail']['entry_ref'] == ref for c in plan['conflicts'])
    assert snapshot_instance(paths) == before


from test_layout_intent import legacy, ATTR


def test_migrated_preference_origin_is_preserved(legacy, tmp_path):
    from bazarlyk.agentic import upgrade
    from bazarlyk.agentic.installation import open_installation
    paths, relocation_export = legacy
    preference = paths.agentic_root / 'packages/user-preferences/SKILL.md'
    preference.write_text('Preserve these synthetic owner preferences after migration.\n')
    layout = upgrade.prepare_upgrade(paths, relocation_export)
    assert layout['complete'], layout['conflicts']
    assert not upgrade.apply_upgrade(paths, layout, attribution=ATTR)['no_change']
    bound, _ = upgrade.read_binding(paths)
    assert bound['preferences_seed']['kind'] == 'preserved_existing'
    open_installation(paths, tuple(bound['selected_extensions']))
    target = snapshot(tmp_path / 'current-export', optional=bound['selected_extensions'])
    before = snapshot_instance(paths)
    managed = prepare(paths, target)
    assert managed['preferences_seed'] == bound['preferences_seed']
    assert not any(c['path'].startswith('user-preferences/') for c in managed['changes'])
    assert (paths.agentic_root / 'user-preferences/SKILL.md').read_text() == 'Preserve these synthetic owner preferences after migration.\n'
    assert snapshot_instance(paths) == before


def test_migrated_supplier_comparison_and_custom_retention(legacy, tmp_path):
    from bazarlyk.agentic import upgrade
    from bazarlyk.agentic.installation import open_installation
    paths, relocation_export = legacy
    preference = paths.agentic_root / 'packages/user-preferences/SKILL.md'
    preference.write_text('Synthetic preserved preferences.\n')
    layout = upgrade.prepare_upgrade(paths, relocation_export)
    assert layout['complete'], layout['conflicts']
    upgrade.apply_upgrade(paths, layout, attribution=ATTR)
    bound, _ = upgrade.read_binding(paths)
    open_installation(paths, tuple(bound['selected_extensions']))
    target = snapshot(tmp_path / 'current-export', optional=bound['selected_extensions'])
    before = snapshot_instance(paths)
    conflicted = prepare(paths, target)
    assert snapshot_instance(paths) == before
    conflicts = {c.get('path') for c in conflicted['conflicts'] if c['code'] == 'local_upstream_conflict'}
    assert 'operations/agentic-management/bootstrap.md' in conflicts
    rewritten = ['operations/imported-role-skills/agentic.yaml', 'overseer/overseer-role-skills/agentic.yaml',
                 *['roles/imported-roles/native/roles/' + role + '/role.yaml'
                   for role in ('global-worker', 'learning-applier', 'ordubashy')]]
    assert not conflicts.intersection(rewritten)
    proof = conflicted['comparison_baseline']
    assert proof['layout_proof']['plan_digest'] == bound['layout_upgrade']['plan_digest']
    assert all(proof['files'][p] == __import__('hashlib').sha256((paths.agentic_root / p).read_bytes()).hexdigest() for p in rewritten)
    source = next(s for s in bound['inventory']['sources'] if s['id'] == 'custom-synthetic')
    assert next(s for s in conflicted['target_inventory']['sources'] if s['id'] == source['id']) == source
    old_binding = next(b for b in bound['inventory']['bindings'] if b['source_id'] == source['id'])
    assert not old_binding['enabled']
    assert old_binding in conflicted['target_inventory']['bindings']
    custom_package = next(p for p in bound['inventory']['packages'] if p['id'] == 'disabled-custom')
    assert next(p for p in conflicted['target_inventory']['packages'] if p['id'] == 'disabled-custom') == custom_package
    custom_assets = [a for a in bound['inventory']['assets'] if a['source_id'] == source['id']]
    assert custom_assets and all(a in conflicted['target_inventory']['assets'] for a in custom_assets)
    for path, item in custom_package['files'].items():
        assert conflicted['import']['desired_files'][custom_package['path'] + '/' + path] == item['sha256']
    assert conflicted['_prepared']['inventory'] != conflicted['target_inventory']
    assert not any(c['path'].startswith('user-preferences/') for c in conflicted['changes'])
    assert conflicted['preferences_seed'] == bound['preferences_seed']
    assert not any(c['path'].startswith('plugins/disabled-custom/') for c in conflicted['changes'])
    from bazarlyk.agentic.managed_sources import target_inventory
    standard = conflicted['_prepared']['inventory']
    for kind in ('package', 'asset', 'binding'):
        collision = copy.deepcopy(standard)
        if kind == 'package':
            item = copy.deepcopy(next(p for p in bound['inventory']['packages'] if p['id'] == 'disabled-custom'))
            item['description'] += ' incompatible'
            collision['packages'].append(item)
        elif kind == 'asset':
            item = copy.deepcopy(next(a for a in bound['inventory']['assets'] if a['source_id'] == source['id']))
            item['source_id'] = 'host'
            collision['assets'].append(item)
        else:
            item = copy.deepcopy(old_binding)
            item['source_id'] = 'host'
            collision['bindings'].append(item)
        with pytest.raises(ValueError, match='Conflicting custom'):
            target_inventory(bound['inventory'], collision, bound['selected_extensions'],
                             conflicted['export']['source_manifest_sha256'])

    # Explicit fixture-owner resolution, recorded by a real scan and Git commit.
    bootstrap = paths.agentic_root / 'operations/agentic-management/bootstrap.md'
    supplier = target / 'qorqut-agentic/src/qorqut_agentic/resources/defaults/operations/agentic-management/bootstrap.md'
    bootstrap.write_bytes(supplier.read_bytes())
    scan_and_commit(paths)
    before = snapshot_instance(paths)
    plan = prepare(paths, target)
    assert plan['complete'], plan['conflicts']
    assert snapshot_instance(paths) == before
    assert any(c['path'] in rewritten for c in plan['changes'])
    assert all(c['expected_sha256'] == proof['files'][c['path']] for c in plan['changes'] if c['path'] in rewritten)
    edited = paths.agentic_root / rewritten[-1]
    edited.write_bytes(edited.read_bytes() + b'\n# Synthetic owner edit\n')
    scan_and_commit(paths)
    before = snapshot_instance(paths)
    plan = prepare(paths, target)
    assert any(c.get('path') == rewritten[-1] and c['code'] == 'local_upstream_conflict' for c in plan['conflicts'])
    assert snapshot_instance(paths) == before

    # Keep committed synthetic WAL frames present while ordinary installed proof reads them.
    import sqlite3
    db = sqlite3.connect(paths.agentic_runtime / 'state.sqlite3')
    try:
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute("INSERT INTO meta(key,value) VALUES ('synthetic_wal_test','present')")
        db.commit()
        wal = paths.agentic_runtime / 'state.sqlite3-wal'
        assert wal.stat().st_size > 0
        open_installation(paths, tuple(bound['selected_extensions']))
        before = snapshot_instance(paths)
        with pytest.raises(DomainError, match='WAL'):
            prepare(paths, target)
        assert snapshot_instance(paths) == before
    finally:
        db.close()

    import json
    receipt_key = 'layout_upgrade:' + bound['layout_upgrade']['plan_digest']
    db = sqlite3.connect(paths.agentic_runtime / 'state.sqlite3')
    try:
        saved_receipt = db.execute('SELECT value FROM meta WHERE key=?', (receipt_key,)).fetchone()[0]
        altered = json.loads(saved_receipt)
        altered['receipt_sha256'] = '0' * 64
        db.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps(altered, sort_keys=True, separators=(',', ':')), receipt_key))
        db.commit()
    finally:
        db.close()
    try:
        before = snapshot_instance(paths)
        with pytest.raises(DomainError, match='receipt'):
            prepare(paths, target)
        assert snapshot_instance(paths) == before
    finally:
        db = sqlite3.connect(paths.agentic_runtime / 'state.sqlite3')
        try:
            db.execute('UPDATE meta SET value=? WHERE key=?', (saved_receipt, receipt_key))
            db.commit()
        finally:
            db.close()

    journal = paths.instance_root / 'config/agentic-layer-history' / ('activation-layout-' + bound['layout_upgrade']['plan_digest'] + '.json')
    original = journal.read_bytes()
    try:
        tampered = original.replace(b'"phase":"complete"', b'"phase":"invalid"')
        assert tampered != original
        journal.write_bytes(tampered)
        before = snapshot_instance(paths)
        with pytest.raises(DomainError):
            prepare(paths, target)
        assert snapshot_instance(paths) == before
    finally:
        journal.write_bytes(original)


def test_custom_source_collisions_are_refused(installed):
    from bazarlyk.agentic.managed_sources import target_inventory
    from bazarlyk.agentic.upgrade import read_binding
    paths, source = installed
    prior, _ = read_binding(paths)
    inventory = prior['inventory']
    standard = copy.deepcopy(inventory)
    custom = copy.deepcopy(inventory['sources'][0])
    custom['id'] = 'custom-collision'
    prior_inventory = copy.deepcopy(inventory)
    prior_inventory['sources'].append(custom)
    prior_inventory['source_digest'] = __import__('bazarlyk.agentic.support', fromlist=['digest']).digest(prior_inventory['sources'])
    conflicting = copy.deepcopy(custom)
    conflicting['enabled'] = not custom['enabled']
    standard['sources'].append(conflicting)
    with pytest.raises(ValueError, match='Conflicting explicit custom source'):
        target_inventory(prior_inventory, standard, [], prior['source_manifest_sha256'])
