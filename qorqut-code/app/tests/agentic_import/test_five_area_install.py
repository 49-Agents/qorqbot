"""Synthetic distribution and host inventory checks for the fresh layout."""
import shutil
import subprocess

import pytest
import yaml

from bazarlyk.agentic import inventory_sources, build_import_plan, apply_import_plan
from bazarlyk.agentic.view import HostAgenticView
from qorqut_agentic import AgenticLayer
from qorqut_operations.contracts import DomainError


def synthetic_inventory(sources):
    root = sources['roles']
    shutil.move(root / 'skills', root / 'old-skills')
    shutil.move(root / 'roles', root / 'old-roles')
    skills = root / 'operations/imported-role-skills/native/skills'
    skills.parent.mkdir(parents=True)
    shutil.move(root / 'old-skills', skills)
    for role in ('ordubashy', 'global-worker', 'learning-applier'):
        old = root / 'old-roles' / role
        target = root / 'roles/imported-roles/native/roles' / role
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(old, target)
        manifest = yaml.safe_load((target / 'role.yaml').read_text())
        manifest['brief'] = f'roles/imported-roles/native/roles/{role}/BRIEF.md'
        manifest['skills'][0]['path'] = 'operations/imported-role-skills/native/skills/shared/SKILL.md'
        if role == 'learning-applier':
            manifest['skills'].append({'id': 'learning-application', 'path': 'overseer/overseer-role-skills/native/skills/learning-application/SKILL.md'})
        (target / 'role.yaml').write_text(yaml.safe_dump(manifest))
    (root / 'old-roles').rmdir()
    learning = root / 'overseer/overseer-role-skills/native/skills/learning-application/SKILL.md'
    learning.parent.mkdir(parents=True, exist_ok=True)
    learning.write_text('---\nname: learning-application\ndescription: Synthetic learning\n---\nSynthetic guidance.\n')
    from hashlib import sha256
    def entry(path, kind):
        return {'id': 'asset-' + sha256(path.encode()).hexdigest()[:20], 'kind': kind,
                'path': path, 'description': 'Synthetic guidance', 'when_to_use': 'When relevant'}
    for area, package_id, entries in [
        ('roles', 'imported-roles', [entry(f'native/roles/{role}/BRIEF.md', 'instruction') for role in ('ordubashy', 'global-worker', 'learning-applier')]),
        ('operations', 'imported-role-skills', [entry('native/skills/shared/SKILL.md', 'skill')]),
        ('overseer', 'overseer-role-skills', [entry('native/skills/learning-application/SKILL.md', 'skill')])]:
        target = root / area / package_id / 'agentic.yaml'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(dict(schema_version=1, id=package_id, name=package_id,
                                              description='Synthetic guidance', entries=entries)))
    subprocess.run(['git', '-C', str(root), 'add', '-A'], check=True)
    subprocess.run(['git', '-C', str(root), 'commit', '-qm', 'Synthetic defaults'], check=True)
    inventory = inventory_sources({key: str(value) for key, value in sources.items() if key != 'custom'}, ['email-management', 'agent-terminal-control'],
                                  [dict(id='supplied', root=str(sources['custom']), owner='fixture-owner', kind='plugin', enabled=False)])
    assert inventory['coverage']['complete'], inventory['diagnostics']
    return inventory


def test_synthetic_role_inventory_and_protected_import(sources, tmp_path):
    inventory = synthetic_inventory(sources)
    view = HostAgenticView(None, inventory)
    assert inventory['schema_version'] == 2
    custom = next(b for b in inventory['bindings'] if b['native_id'] == 'custom-procedure')
    assert not custom['enabled'] and custom['source_id'] == 'custom-supplied'
    assert all(not b['enabled'] for b in inventory['bindings'] if b['native_id'] in {'paper', 'fastmail', 'content'})
    assert all(p['path'].startswith('plugins/') for p in inventory['packages'] if p['native']['owner'] in {'content', 'paper', 'fastmail'})
    bindings = [b for b in inventory['bindings'] if b['kind'] == 'role']
    assert len(bindings) == 3
    assert len({b['skills'][0]['canonical_entry'] for b in bindings}) == 1
    applier = next(b for b in bindings if b['native_id'] == 'learning-applier')
    assert applier['skills'][1]['canonical_entry'].startswith('overseer-role-skills/')
    assert bindings[0]['skills'][0]['canonical_entry'].startswith('imported-role-skills/')
    plan = build_import_plan(inventory)
    assert any(p.startswith('roles/imported-roles/') for p in plan['desired_files'])
    assert any(p.startswith('operations/imported-role-skills/') for p in plan['desired_files'])
    layer = AgenticLayer.initialize(tmp_path / 'authored', tmp_path / 'state')
    with pytest.raises(Exception):
        apply_import_plan(layer, plan, dict(actor_id='owner', actor_kind='owner', reason='synthetic'))
    bad = dict(inventory, schema_version=1)
    with pytest.raises(DomainError, match='explicit upgrade'):
        HostAgenticView(None, bad)


def test_fresh_install_and_reopen_with_synthetic_defaults(sources, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from bazarlyk.agentic import installation
    from bazarlyk.agentic.support import digest

    inventory = synthetic_inventory(sources)
    source_digest = 'a' * 64
    for source in inventory['sources']:
        source.update(provenance_kind='committed_export', clean=None, tree='b' * 40,
                      snapshot_sha256='c' * 64, source_manifest_sha256=source_digest)
    inventory['source_digest'] = digest(inventory['sources'])
    plan = build_import_plan(inventory)
    resources = __import__('pathlib').Path(__file__).resolve().parents[4] / 'qorqut-agentic/src/qorqut_agentic/resources'
    def read_seed(name):
        directory = resources / name
        return {p.relative_to(directory).as_posix(): p.read_bytes() for p in directory.rglob('*') if p.is_file()}
    preferences = {'agentic.yaml': b'schema_version: 1\nid: user-preferences\nname: Preferences\ndescription: Generic preferences\nentries:\n  - id: preferences\n    kind: skill\n    description: Universal preferences\n    when_to_use: Always\n    path: SKILL.md\n',
                   'SKILL.md': b'---\nname: preferences\ndescription: Universal preferences\n---\nGeneric preferences.\n'}
    prepared = dict(plan=plan, inventory=inventory, seed=read_seed('defaults/operations/agentic-management'), overseer_seed=read_seed('defaults/overseer/overseer'),
                    preferences_seed=preferences, git=shutil.which('git'), selected_extensions=[], source_manifest_sha256=source_digest, export_root='synthetic-export')
    monkeypatch.setattr(installation.metadata, 'version', lambda _: 'synthetic')
    monkeypatch.setattr(installation, 'prepare_installation', lambda *_: prepared)
    instance = tmp_path / 'instance'
    instance.mkdir()
    paths = SimpleNamespace(instance_root=instance, agentic_root=instance / 'agentic', agentic_runtime=instance / 'runtime')
    result = installation.initialize_installation(paths, prepared)
    layer, view = installation.open_installation(paths, [])
    assert result['instance_id'] == layer.instance_id
    assert layer.catalog.read_entry('user-preferences/preferences')['body'].endswith('Generic preferences.\n')
    assert layer.changes.list()['changes'][0]['attribution']['actor_id'] == 'system:installer'
    assert 'overseer-role-skills' not in view.selection({'role': {'id': 'ordubashy'}})['package_ids']
    assert 'overseer-role-skills' in view.selection({'role': {'id': 'learning-applier'}})['package_ids']
    assert 'imported-role-skills' in view.selection({'role': {'id': 'ordubashy'}})['package_ids']
    role = SimpleNamespace(kind='role', id='ordubashy', skills=[{'id': 'shared'}])
    selection = view.selection({'role': {'id': 'ordubashy'}})
    assert b'Common role instructions' in view.skill_files('role', role, 'shared', selection=selection)['SKILL.md']
    attribution = dict(actor_id='owner', actor_kind='owner', reason='Synthetic edit')
    protected = 'operations/imported-role-skills/native/skills/shared/SKILL.md'
    with pytest.raises(Exception):
        layer.authoring.apply(attribution, [{'op': 'edit', 'path': protected, 'content': 'overwrite',
                                             'expected_sha256': digest((paths.agentic_root / protected).read_bytes())}])
    with pytest.raises(Exception):
        layer.history.restore(attribution, result['initial_commit'], [protected])
    original = (paths.agentic_root / protected).read_bytes()
    (paths.agentic_root / protected).write_bytes(original + b'changed')
    with pytest.raises(Exception):
        layer.history.commit('Protected change', author={'name': 'Fixture', 'email': 'fixture@example.invalid'})
    (paths.agentic_root / protected).write_bytes(original)
    plugin_manifest = 'schema_version: 1\nid: synthetic-plugin\nname: Synthetic\ndescription: Synthetic\nentries: []\n'
    layer.authoring.apply(attribution, [{'op': 'create', 'path': 'plugins/synthetic-plugin/agentic.yaml', 'content': plugin_manifest}])
    assert (paths.agentic_root / 'plugins/synthetic-plugin/agentic.yaml').exists()
    pref = 'user-preferences/SKILL.md'
    layer.authoring.apply(attribution, [{'op': 'edit', 'path': pref, 'content': '---\nname: preferences\ndescription: Generic\n---\nEdited.\n',
                                         'expected_sha256': digest((paths.agentic_root / pref).read_bytes())}])
    assert b'Edited.' in (paths.agentic_root / pref).read_bytes()
    with pytest.raises(Exception):
        installation.initialize_installation(paths, prepared)


def test_inventory_rejects_mismatched_paths_and_symlink_target(sources, tmp_path, monkeypatch):
    from copy import deepcopy
    from types import SimpleNamespace
    from bazarlyk.agentic import installation
    inventory = synthetic_inventory(sources)
    bad = deepcopy(inventory)
    bad['packages'][0]['path'] = 'plugins/' + bad['packages'][0]['id']
    with pytest.raises(DomainError):
        HostAgenticView(None, bad)
    prepared = {'export_root': 'synthetic', 'selected_extensions': []}
    monkeypatch.setattr(installation, 'prepare_installation', lambda *_: prepared)
    instance = tmp_path / 'instance'
    instance.mkdir()
    actual = tmp_path / 'actual'
    actual.mkdir()
    (instance / 'agentic').symlink_to(actual, target_is_directory=True)
    paths = SimpleNamespace(instance_root=instance, agentic_root=instance / 'agentic', agentic_runtime=instance / 'runtime')
    with pytest.raises(DomainError):
        installation.initialize_installation(paths, prepared)
    assert list(actual.iterdir()) == []


def test_verified_synthetic_export_install(sources, tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from types import SimpleNamespace
    from bazarlyk.agentic import installation
    from bazarlyk.source_snapshot import component_digest

    synthetic_inventory(sources)
    export = tmp_path / 'export'
    for source, dest in [('host', 'qorqut-code'), ('email', 'dependencies/email-management')]:
        shutil.copytree(sources[source], export / dest, ignore=shutil.ignore_patterns('.git'))
    defaults = export / 'qorqut-agentic/src/qorqut_agentic/resources/defaults'
    shutil.copytree(sources['roles'], defaults, ignore=shutil.ignore_patterns('.git'))
    resources = Path(__file__).resolve().parents[4] / 'qorqut-agentic/src/qorqut_agentic/resources'
    shutil.copytree(resources / 'defaults/operations/agentic-management', defaults / 'operations/agentic-management')
    shutil.copytree(resources / 'defaults/overseer/overseer', defaults / 'overseer/overseer')
    preferences = defaults / 'user-preferences'
    preferences.mkdir()
    (preferences / 'agentic.yaml').write_text('schema_version: 1\nid: user-preferences\nname: Preferences\ndescription: Generic preferences\nentries:\n  - id: preferences\n    kind: skill\n    description: Universal preferences\n    when_to_use: Always\n    path: SKILL.md\n')
    (preferences / 'SKILL.md').write_text('---\nname: preferences\ndescription: Universal preferences\n---\nGeneric preferences.\n')
    components = [dict(name=name, destination=dest, commit='a'*40, tree='b'*40) for name, dest in [
        ('qorqut-code', 'qorqut-code'), ('qorqut-agentic', 'qorqut-agentic'), ('email-management', 'dependencies/email-management')]]
    for component in components:
        component['snapshot_sha256'] = component_digest(export, component, components)
    (export / 'SOURCE-MANIFEST.json').write_text(json.dumps({'format': 1, 'components': components}))
    monkeypatch.setattr(installation.metadata, 'version', lambda _: 'synthetic')
    prepared = installation.prepare_installation(export, [])
    instance = tmp_path / 'instance'
    instance.mkdir()
    paths = SimpleNamespace(instance_root=instance, agentic_root=instance / 'agentic', agentic_runtime=instance / 'runtime')
    installation.initialize_installation(paths, prepared)
    layer, view = installation.open_installation(paths, [])
    assert layer.catalog.read_entry('user-preferences/preferences')['body'].endswith('Generic preferences.\n')
    assert view.selection({'role': {'id': 'ordubashy'}})['package_ids']


def test_approved_defaults_in_verified_export(sources, tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from types import SimpleNamespace
    from bazarlyk.agentic import installation
    from bazarlyk.source_snapshot import component_digest
    export = tmp_path / 'export'
    for source, dest in [('host', 'qorqut-code'), ('email', 'dependencies/email-management')]:
        shutil.copytree(sources[source], export / dest, ignore=shutil.ignore_patterns('.git'))
    shutil.rmtree(export / 'dependencies/email-management')
    shutil.copytree(Path(__file__).resolve().parents[4] / 'dependencies/email-management',
                    export / 'dependencies/email-management', ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))
    host_app = Path(__file__).resolve().parents[2]
    shutil.rmtree(export / 'qorqut-code/app')
    shutil.copytree(host_app, export / 'qorqut-code/app',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.pytest_cache'))
    defaults = Path(__file__).resolve().parents[4] / 'qorqut-agentic/src/qorqut_agentic/resources/defaults'
    shutil.copytree(defaults, export / 'qorqut-agentic/src/qorqut_agentic/resources/defaults')
    components = [dict(name=name, destination=dest, commit='a'*40, tree='b'*40) for name, dest in [
        ('qorqut-code', 'qorqut-code'), ('qorqut-agentic', 'qorqut-agentic'), ('email-management', 'dependencies/email-management')]]
    for component in components:
        component['snapshot_sha256'] = component_digest(export, component, components)
    (export / 'SOURCE-MANIFEST.json').write_text(json.dumps({'format': 1, 'components': components}))
    monkeypatch.setattr(installation.metadata, 'version', lambda _: 'synthetic')
    prepared = installation.prepare_installation(export, [])
    instance = tmp_path / 'instance'
    instance.mkdir()
    paths = SimpleNamespace(instance_root=instance, agentic_root=instance / 'agentic', agentic_runtime=instance / 'runtime')
    installation.initialize_installation(paths, prepared)
    layer, view = installation.open_installation(paths, [])
    assert layer.catalog.read_entry('user-preferences/preferences')['body'] == (defaults / 'user-preferences/SKILL.md').read_text()
    assert view.selection({'role': {'id': 'learning-applier'}})['package_ids']
    from types import SimpleNamespace
    for role_id in ('ordubashy', 'global-worker', 'learning-applier'):
        role = SimpleNamespace(kind='role', id=role_id)
        brief = view.role_brief(role)
        path = f'roles/imported-roles/native/roles/{role_id}/BRIEF.md'
        assert brief['path'] == path
        assert brief['body'] == (defaults / path).read_text()
        assert brief['revision'] and brief['sha256'].startswith('sha256:')
    with pytest.raises(DomainError):
        view.role_brief(SimpleNamespace(kind='plugin', id='ordubashy'))
    from bazarlyk.agents.context import compose_context
    from bazarlyk.agents.preferences import load_preferences
    from text_support import inputs
    for role_id in ('ordubashy', 'global-worker', 'learning-applier'):
        profile, role, context_sources, kwargs = inputs('plain')
        role.id, role.kind = role_id, 'role'
        role.manifest['brief'] = f'roles/{role_id}/BRIEF.md'  # legacy pin, deliberately old path
        context_sources['user-preferences/preferences'] = load_preferences(view)
        result = compose_context(profile, role, context_sources, **kwargs, agentic_view=view)
        brief = view.role_brief(role)
        entry = next(e for e in result['entries'] if e['source_id'].startswith('role:'))
        assert brief['body'].strip() in result['rendered']
        assert (entry['source_path'], entry['revision'], entry['hash']) == (brief['path'], brief['revision'], brief['sha256'])
        assert context_sources['user-preferences/preferences'].text in result['rendered']
