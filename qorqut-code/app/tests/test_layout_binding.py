import base64
import copy
import json
import shutil

import pytest

from bazarlyk.agentic import installation, layout_binding, layout_files, layout_git, layout_intent, upgrade
from bazarlyk.agentic.support import canonical, digest
from test_layout_intent import ATTR, legacy


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file() and not p.is_symlink()}


@pytest.mark.parametrize('preserved', [False, True])
def test_candidate_is_read_only_and_preserves_legacy(legacy, tmp_path, preserved):
    paths, export = legacy
    local_export = tmp_path / 'export-copy'
    shutil.copytree(export, local_export, symlinks=True)
    preferences = paths.agentic_root / 'packages/user-preferences'
    if preserved:
        (preferences / 'SKILL.md').write_text('Edited synthetic preference.\n')
    else:
        shutil.rmtree(preferences)
    edited = paths.agentic_root / 'packages/agentic-management/bootstrap.md'
    edited.write_text('Owner edited synthetic guidance.\n')
    plan = upgrade.prepare_upgrade(paths, local_export)
    assert plan['complete'], plan['conflicts']
    prior, _ = upgrade.read_binding(paths)
    layout_intent.begin(paths, plan, attribution=ATTR)
    intent, = layout_intent.incomplete_intents(paths)
    assert intent['schema_version'] == 2
    assert digest(base64.b64decode(intent['export_manifest']['content_base64'])) == plan['export']['source_manifest_sha256']
    layout_files.relocate(paths, plan, attribution=ATTR)
    layout_git.publish(paths, plan, attribution=ATTR)
    shutil.rmtree(local_export)
    before = snapshot(paths.instance_root)
    record, raw, sha = layout_binding.build_candidate(paths, plan)
    assert (record, raw, sha) == layout_binding.build_candidate(paths, plan)
    assert installation._validate_binding(paths, record) == record
    assert snapshot(paths.instance_root) == before
    assert record['instance_id'] == prior['instance_id'] and record['initial_commit'] == prior['initial_commit']
    assert record['management_seed'] == prior['management_seed'] and record['overseer_seed'] == prior['overseer_seed']
    assert record['inventory']['sources'] == prior['inventory']['sources']
    assert [b for b in record['inventory']['bindings'] if b['kind'] == 'role'] == [b for b in plan['target_inventory']['bindings'] if b['kind'] == 'role']
    assert any(p['id'] == 'disabled-custom' and not p['native']['enabled'] for p in record['inventory']['packages'])
    assert (paths.agentic_root / 'plugins/custom-tools/echo.py').read_bytes() == base64.b64decode(intent['authored']['packages/custom-tools/echo.py']['content_base64'])
    projection = layout_binding.derive_record(prior, upgrade.history_snapshot(paths, intent['prior_binding_sha256']).read_bytes(), plan, intent)
    assert 'layout_upgrade' not in projection and projection['inventory'] == record['inventory']
    assert record['preferences_seed']['kind'] == ('preserved_existing' if preserved else 'layout_verified_export_seed')
    assert (paths.agentic_root / 'operations/agentic-management/bootstrap.md').read_text() == 'Owner edited synthetic guidance.\n'
    assert record['management_seed']['files']['bootstrap.md'] != digest(b'Owner edited synthetic guidance.\n')
    # A historical schema-1 receipt remains readable, but cannot prove a supplied seed.
    directory = upgrade.history_directory(paths)
    intent_path = directory / ('layout-' + plan['plan_digest'] + '.json')
    intent_raw = intent_path.read_bytes()
    old_intent = copy.deepcopy(intent)
    old_intent.pop('export_manifest')
    old_intent['schema_version'] = 1
    old_intent['receipt_sha256'] = digest({k: v for k, v in old_intent.items() if k != 'receipt_sha256'})
    intent_path.write_bytes(canonical(old_intent) + b'\n')
    assert layout_intent._read_intent(intent_path) == old_intent
    git_receipt = layout_git._read_journal(directory / ('git-layout-' + plan['plan_digest'] + '.json'))
    git_receipt['intent_sha256'] = old_intent['receipt_sha256']
    git_receipt['receipt_sha256'] = digest({k: v for k, v in git_receipt.items() if k != 'receipt_sha256'})
    prior_raw = upgrade.history_snapshot(paths, old_intent['prior_binding_sha256']).read_bytes()
    if preserved:
        assert layout_binding.derive_record(prior, prior_raw, plan, old_intent, git_receipt)['preferences_seed'] == record['preferences_seed']
    else:
        with pytest.raises(ValueError, match='Missing retained export manifest'):
            layout_binding.derive_record(prior, prior_raw, plan, old_intent, git_receipt)
    intent_path.write_bytes(intent_raw)
    for key in ('selected_extensions', 'initial_commit', 'source_manifest_sha256', 'management_seed', 'inventory', 'preferences_seed', 'layout_upgrade'):
        tampered = copy.deepcopy(record)
        if key == 'selected_extensions': tampered[key].append('bogus')
        elif key == 'initial_commit': tampered[key] = '0' * 40
        elif key == 'source_manifest_sha256': tampered[key] = '0' * 64
        elif key == 'management_seed': tampered[key]['version'] = 'tampered'
        elif key == 'inventory': tampered[key]['sources'][0]['files']['evil'] = '0' * 64
        elif key == 'preferences_seed': tampered[key]['kind'] = 'fake'
        else: tampered[key]['resulting_commit'] = '0' * 40
        with pytest.raises(Exception): installation._validate_binding(paths, tampered)


def test_manifest_component_and_checksum_refusal():
    manifest = {'format': 1, 'components': [{'name': 'qorqut-agentic', 'destination': 'src/engine', 'commit': '1' * 40, 'tree': '2' * 40, 'snapshot_sha256': '3' * 64}]}
    raw = canonical(manifest) + b'\n'
    seed = {'source_manifest_sha256': digest(raw), 'resource': 'qorqut-agentic/src/qorqut_agentic/resources/defaults/user-preferences', 'supplier_commit': '1' * 40, 'supplier_tree': '2' * 40, 'files': {}}
    plan = {'export': {'source_manifest_sha256': digest(raw)}, 'preference_seed': seed}
    assert layout_intent._manifest(raw, plan) == manifest
    for broken in (raw + b' ', b'{"format":1,"format":1,"components":[]}'):
        with pytest.raises(ValueError): layout_intent._manifest(broken, plan)
    bad = copy.deepcopy(plan)
    bad['preference_seed']['supplier_tree'] = '0' * 40
    with pytest.raises(ValueError, match='supplier mismatch'): layout_intent._manifest(raw, bad)


def test_unsupported_legacy_provenance_refuses():
    with pytest.raises(ValueError, match='Unsupported legacy management_seed provenance'):
        layout_binding.validate_legacy_provenance({})


def test_fresh_binding_keeps_seed_validation(tmp_path):
    from agentic_installation_support import snapshot as export_snapshot
    from instance_support import synthetic_paths
    root = tmp_path / 'fresh-export'
    root.mkdir()
    export_snapshot(root)
    prepared = installation.prepare_installation(root, [])
    plan, inventory = prepared['plan'], prepared['inventory']
    imported = {k: copy.deepcopy(plan[k]) for k in ('source_digest', 'plan_digest', 'previous_import_digest', 'existing_digest', 'bindings')}
    imported.update(schema_version=1, files=copy.deepcopy(plan['desired_files']))
    imported['manifest_digest'] = digest(imported)
    paths = synthetic_paths(tmp_path / 'fresh-instance')
    record = {'version': 2, 'instance_id': 'inst_' + '1' * 32, 'root': str(paths.agentic_root), 'state': str(paths.agentic_runtime),
              'inventory': inventory, 'inventory_sha256': digest(inventory), 'import_manifest': imported,
              'source_manifest_sha256': prepared['source_manifest_sha256'], 'selected_extensions': [], 'initial_commit': '1' * 40,
              'management_seed': installation._seed_provenance('agentic-management', prepared['seed']),
              'overseer_seed': installation._seed_provenance('overseer', prepared['overseer_seed']),
              'preferences_seed': installation._seed_provenance('user-preferences', prepared['preferences_seed'])}
    assert installation._validate_binding(paths, record) == record
    for field in ('management_seed', 'overseer_seed', 'preferences_seed'):
        invalid = copy.deepcopy(record)
        invalid[field]['resource'] = 'qorqut_agentic/resources/management'
        with pytest.raises(ValueError): installation._validate_binding(paths, invalid)


def test_capture_manifest_refuses_unsafe_or_changed_file(tmp_path):
    manifest = {'format': 1, 'components': [{'name': 'qorqut-agentic', 'destination': 'engine', 'commit': '1' * 40, 'tree': '2' * 40, 'snapshot_sha256': '3' * 64}]}
    raw = canonical(manifest) + b'\n'
    plan = {'export': {'root': str(tmp_path), 'source_manifest_sha256': digest(raw)}, 'preference_seed': None}
    path = tmp_path / 'SOURCE-MANIFEST.json'
    path.write_bytes(raw)
    captured = layout_intent._capture_manifest(plan)
    assert base64.b64decode(captured['content_base64']) == raw
    path.write_bytes(b'{"format":1,"format":1,"components":[]}')
    with pytest.raises(ValueError): layout_intent._capture_manifest(plan)
    path.unlink()
    path.symlink_to(tmp_path / 'other')
    with pytest.raises(OSError): layout_intent._capture_manifest(plan)
