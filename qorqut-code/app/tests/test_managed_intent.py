import copy
import shutil

import pytest
from qorqut_operations.contracts import DomainError
from bazarlyk.agentic import managed_binding, managed_intent
from bazarlyk.agentic.managed_plan import prepare
from bazarlyk.agentic.upgrade import read_binding_bytes
from test_agentic_managed_plan import installed, refresh_export, snapshot_instance, scan_and_commit

ATTR = {'actor_id': 'synthetic-admin', 'actor_kind': 'human', 'reason': 'Offline managed update'}


def test_capture_candidate_and_removed_export(installed):
    paths, source = installed
    local = paths.agentic_root / 'user-preferences/SKILL.md'
    local.write_text('Synthetic owner preference.\n')
    custom = paths.agentic_root / 'plugins/custom-extra'
    custom.mkdir(parents=True)
    (custom / 'owner.txt').write_text('Synthetic custom content.\n')
    (custom / 'agentic.yaml').write_text('schema_version: 1\nid: custom-extra\nname: Custom\ndescription: Custom\nentries: []\n')
    scan_and_commit(paths)
    desired = source / 'qorqut-agentic/src/qorqut_agentic/resources/defaults/overseer/overseer/instructions/review.md'
    desired.write_bytes(desired.read_bytes() + b'\nSynthetic supplier edit.\n')
    refresh_export(source)
    plan = prepare(paths, source)
    assert plan['complete'], plan['conflicts']
    before = snapshot_instance(paths)
    prior = read_binding_bytes(paths)
    history = paths.instance_root / 'config/agentic-layer-history'
    assert not history.exists()
    intent = managed_intent.capture(paths, plan, attribution=ATTR)
    assert snapshot_instance(paths) == before
    assert not history.exists()
    encoded = managed_intent.encode(paths, intent, prior)
    assert managed_intent.decode(paths, encoded, prior) == intent
    private = paths.instance_root.parent / 'retained'
    private.mkdir(mode=0o700)
    evidence = private / 'intent.json'
    evidence.write_bytes(encoded)
    evidence.chmod(0o600)
    assert managed_intent.read(paths, evidence, prior) == intent
    with pytest.raises(DomainError):
        managed_intent.decode(paths, encoded + b' ', prior)
    shutil.rmtree(source)
    assert managed_intent.decode(paths, encoded, prior) == intent
    record, raw, sha = managed_binding.candidate(paths, prior, intent, 'a' * 40)
    assert record['preferences_seed'] == __import__('json').loads(prior)['preferences_seed']
    assert record['import_manifest']['files'] == plan['import']['desired_files']
    assert (custom / 'owner.txt').read_text() == 'Synthetic custom content.\n'
    assert record['managed_upgrade']['intent_sha256'] == intent['receipt_sha256']
    assert sha == __import__('hashlib').sha256(raw).hexdigest()
    assert snapshot_instance(paths) == before
    altered = copy.deepcopy(intent)
    change = altered['plan']['changes'][0]
    change['expected_sha256'] = '0' * 64
    altered['plan']['plan_digest'] = __import__('bazarlyk.agentic.managed_plan', fromlist=['plan_digest']).plan_digest(altered['plan'])
    directory = __import__('pathlib').Path(altered['plan']['history']['directory'])
    pd = altered['plan']['plan_digest']
    altered['plan']['evidence'] = {'intent': str(directory / ('managed-intent-' + pd + '.json')),
                                   'journal': str(directory / ('managed-journal-' + pd + '.json'))}
    altered['receipt_sha256'] = __import__('bazarlyk.agentic.support', fromlist=['digest']).digest({k: v for k, v in altered.items() if k != 'receipt_sha256'})
    with pytest.raises(DomainError):
        managed_intent.validate(paths, altered, prior)


def test_noop_capture(installed):
    paths, source = installed
    plan = prepare(paths, source)
    before = snapshot_instance(paths)
    assert managed_intent.capture(paths, plan, attribution=ATTR) is None
    assert snapshot_instance(paths) == before


def test_binding_only_capture_without_event_or_commit(installed):
    import json
    from bazarlyk.agentic.support import digest
    paths, source = installed
    manifest_path = source / 'SOURCE-MANIFEST.json'
    value = json.loads(manifest_path.read_text())
    value['components'][0]['commit'] = 'c' * 40
    manifest_path.write_text(json.dumps(value))
    plan = prepare(paths, source)
    assert plan['complete'] and plan['changes']
    for change in plan['changes']:
        assert change['path'].endswith('agentic.yaml')
        (paths.agentic_root / change['path']).write_bytes(__import__('base64').b64decode(change['content_base64']))
    scan_and_commit(paths)
    plan = prepare(paths, source)
    assert plan['complete'] and not plan['no_change'] and not plan['changes']
    before = snapshot_instance(paths)
    intent = managed_intent.capture(paths, plan, attribution=ATTR)
    assert intent['fixed'] is None and intent['runtime_before']['tables'] == intent['runtime_before']['protected']
    prior = read_binding_bytes(paths)
    assert managed_binding.candidate(paths, prior, intent, intent['head'])[0]['managed_upgrade']['resulting_commit'] == intent['head']
    with pytest.raises(DomainError):
        managed_binding.candidate(paths, prior, intent, 'f' * 40)
    assert snapshot_instance(paths) == before


def test_stale_plan_and_tampered_retained_evidence(installed):
    from bazarlyk.agentic.support import digest
    paths, source = installed
    guidance = source / 'qorqut-agentic/src/qorqut_agentic/resources/defaults/overseer/overseer/instructions/review.md'
    guidance.write_bytes(guidance.read_bytes() + b'\nSynthetic supplier change.\n')
    refresh_export(source)
    plan = prepare(paths, source)
    (paths.agentic_root / 'owner-untracked.txt').write_text('untracked')
    before = snapshot_instance(paths)
    with pytest.raises(DomainError):
        managed_intent.capture(paths, plan, attribution=ATTR)
    assert snapshot_instance(paths) == before
    (paths.agentic_root / 'owner-untracked.txt').unlink()
    intent = managed_intent.capture(paths, plan, attribution=ATTR)
    prior = read_binding_bytes(paths)
    with pytest.raises(DomainError):
        managed_intent.validate(paths, intent, prior + b' ')
    for kind in ('authored', 'seed', 'runtime', 'source'):
        broken = copy.deepcopy(intent)
        if kind == 'authored':
            item = next(iter(broken['authored'].values()))
            item['mode'] ^= 0o100
        elif kind == 'seed':
            item = broken['seeds']['overseer_seed']['instructions/review.md']
            item['sha256'] = '0' * 64
        elif kind == 'runtime':
            broken['runtime_before']['tables']['meta']['sha256'] = '0' * 64
        else:
            component = next(iter(broken['source_witnesses'].values()))
            component['records'][0]['path'] = '../escape'
        broken['receipt_sha256'] = digest({k: v for k, v in broken.items() if k != 'receipt_sha256'})
        with pytest.raises(DomainError):
            managed_intent.validate(paths, broken, prior)
        with pytest.raises(DomainError):
            managed_intent.encode(paths, broken, prior)


from test_layout_intent import legacy


def test_migrated_capture_retains_legacy_origin(legacy, tmp_path):
    from agentic_installation_support import snapshot
    from bazarlyk.agentic import upgrade
    from test_layout_intent import ATTR as LAYOUT_ATTR
    paths, relocation = legacy
    (paths.agentic_root / 'packages/user-preferences/SKILL.md').write_text('Legacy owner preference.\n')
    layout = upgrade.prepare_upgrade(paths, relocation)
    upgrade.apply_upgrade(paths, layout, attribution=LAYOUT_ATTR)
    bound, _ = upgrade.read_binding(paths)
    source = snapshot(tmp_path / 'source', optional=bound['selected_extensions'])
    bootstrap = source / 'qorqut-agentic/src/qorqut_agentic/resources/defaults/operations/agentic-management/bootstrap.md'
    (paths.agentic_root / 'operations/agentic-management/bootstrap.md').write_bytes(bootstrap.read_bytes())
    scan_and_commit(paths)
    plan = prepare(paths, source)
    assert plan['complete'], plan['conflicts']
    before = snapshot_instance(paths)
    prior = read_binding_bytes(paths)
    intent = managed_intent.capture(paths, plan, attribution=ATTR)
    record, _, _ = managed_binding.candidate(paths, prior, intent, 'a' * 40)
    assert record['preferences_seed'] == bound['preferences_seed']
    assert 'layout_upgrade' not in record
    assert any(s['id'] == 'custom-synthetic' and not s['enabled'] for s in record['inventory']['sources'])
    shutil.rmtree(source)
    assert managed_intent.decode(paths, managed_intent.encode(paths, intent, prior), prior) == intent
    assert snapshot_instance(paths) == before


def test_empty_required_directory_capture_replay_and_absence(installed):
    import json
    from bazarlyk.agentic.installation import prepare_installation
    from bazarlyk.agentic.support import digest
    from bazarlyk.source_snapshot import component_digest
    paths, source = installed
    directory = source / 'qorqut-code/app/bazarlyk/bootstrap/instructions'
    shutil.rmtree(directory)
    directory.mkdir()
    refresh_export(source)
    manifest = json.loads((source / 'SOURCE-MANIFEST.json').read_bytes())
    component = next(c for c in manifest['components'] if c['name'] == 'qorqut-code')
    original_hash = component_digest(source, component, manifest['components'])
    prepared = prepare_installation(source, [])
    assert prepared['inventory']['coverage']['complete']
    plan = prepare(paths, source)
    assert plan['complete'], plan['conflicts']
    before = snapshot_instance(paths)
    prior = read_binding_bytes(paths)
    intent = managed_intent.capture(paths, plan, attribution=ATTR)
    assert snapshot_instance(paths) == before
    directory_key = 'app/bazarlyk/bootstrap/instructions'
    assert directory_key in intent['source_witnesses']['qorqut-code']['directories']
    candidate = managed_binding.candidate(paths, prior, intent, 'a' * 40)
    directory.rmdir()
    assert component_digest(source, component, manifest['components']) == original_hash
    with pytest.raises(DomainError):
        prepare_installation(source, [])
    with pytest.raises(DomainError):
        managed_intent.capture(paths, plan, attribution=ATTR)
    assert snapshot_instance(paths) == before
    shutil.rmtree(source)
    assert managed_binding.candidate(paths, prior, intent, 'a' * 40) == candidate
    bad = copy.deepcopy(intent)
    bad['source_witnesses']['qorqut-code']['directories'].remove(directory_key)
    bad['receipt_sha256'] = digest({k: v for k, v in bad.items() if k != 'receipt_sha256'})
    with pytest.raises(DomainError):
        managed_intent.validate(paths, bad, prior)
    assert snapshot_instance(paths) == before
