"""Synthetic coverage for authored areas and their fixed write boundary."""
import json
import shutil

import pytest

from qorqut_agentic.errors import AgenticError
from qorqut_agentic.authoring import package_revision
from qorqut_agentic.files import DiskSource
from tests.conftest import ATTR, EXAMPLES, file_sha, git, sha


def seed(root, area, package_id):
    target = root / area / package_id
    shutil.copytree(EXAMPLES / 'email-composition', target)
    manifest = target / 'agentic.yaml'
    manifest.write_text(manifest.read_text().replace('email-composition', package_id))
    return target


def test_catalog_areas_revision_duplicates_and_legacy(layer):
    for area in ('roles', 'operations', 'overseer', 'plugins'):
        seed(layer.root, area, f'{area}-sample')
    prefs = layer.root / 'user-preferences'
    (prefs / 'agentic.yaml').write_text(json.dumps({'schema_version': 1, 'id': 'user-preferences', 'name': 'Preferences', 'description': 'Preferences', 'entries': [{'id': 'preferences', 'kind': 'skill', 'path': 'SKILL.md', 'description': 'Preferences', 'when_to_use': 'Always'}]}))
    (prefs / 'SKILL.md').write_text('Preferences\n')
    listing = layer.catalog.list()
    for area in ('roles', 'operations', 'overseer', 'plugins', 'user-preferences'):
        entry = next(e for e in listing['entries'] if e['area'] == area)
        assert entry['editable'] == (area in ('plugins', 'user-preferences'))
        assert layer.catalog.read_resource(entry['package_id'], entry['path'])['root_path'] == entry['root_path']
        path = layer.root / entry['root_path']
        path.write_text(path.read_text() + '\n')
        updated = layer.catalog.list()
        assert updated['catalog_revision'] != listing['catalog_revision']
        listing = updated
    seed(layer.root, 'roles', 'duplicate')
    seed(layer.root, 'plugins', 'duplicate')
    listing = layer.catalog.list()
    assert not any(e['package_id'] == 'duplicate' for e in listing['entries'])
    assert any(i['code'] == 'duplicate_package_id' for i in listing['issues'])
    (layer.root / 'packages').mkdir()
    (layer.root / 'packages' / 'old').write_text('legacy')
    assert any(i['code'] == 'layout_migration_required' for i in layer.catalog.list()['issues'])


def test_mixed_batch_and_protected_import_are_atomic(layer):
    role = seed(layer.root, 'roles', 'locked')
    before = (role / 'agentic.yaml').read_bytes()
    with pytest.raises(AgenticError) as err:
        layer.authoring.apply(ATTR, [{'op': 'create', 'path': 'plugins/ok/new.txt', 'content': 'yes'},
                                    {'op': 'edit', 'path': 'roles/locked/agentic.yaml', 'expected_sha256': sha(before), 'content': 'no'}])
    assert err.value.code == 'protected_agentic_area'
    assert not (layer.root / 'plugins/ok/new.txt').exists()
    assert (role / 'agentic.yaml').read_bytes() == before
    with pytest.raises(AgenticError) as err:
        layer.authoring.import_package(ATTR, 'locked', {'directory': str(EXAMPLES / 'email-composition')})
    assert err.value.code == 'protected_agentic_area'


def test_history_selected_commit_keeps_protected_staging(layer):
    role = seed(layer.root, 'roles', 'locked')
    git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'baseline')
    (role / 'new.txt').write_text('protected')
    git(layer, 'add', 'roles/locked/new.txt')
    editable = layer.root / 'user-preferences' / 'note.md'
    editable.write_text('editable')
    before = git(layer, 'diff', '--cached', '--binary').stdout
    with pytest.raises(AgenticError) as err:
        layer.history.commit('everything')
    assert err.value.code == 'protected_agentic_area'
    assert git(layer, 'diff', '--cached', '--binary').stdout == before
    layer.history.commit('selected', paths=['user-preferences/note.md'])
    assert git(layer, 'diff', '--cached', '--binary').stdout == before
    assert git(layer, 'show', '--name-only', '--format=', 'HEAD').stdout.strip() == 'user-preferences/note.md'


def test_preference_import_update_restore_and_remove(layer):
    manifest = {'schema_version': 1, 'id': 'user-preferences', 'name': 'Preferences', 'description': 'Preferences',
                'entries': [{'id': 'preferences', 'kind': 'skill', 'path': 'SKILL.md', 'description': 'Preferences', 'when_to_use': 'Always'}]}
    snapshot = {'files': [{'path': 'SKILL.md', 'content': 'first\n'}]}
    result = layer.authoring.import_package(ATTR, 'user-preferences', snapshot, manifest=manifest, provenance={'kind': 'synthetic'})
    assert result['change']['kind'] == 'import'
    assert layer.catalog.read_entry('user-preferences/preferences')['entry']['source']['snapshot_digest'] == result['snapshot_digest']
    commit = layer.history.commit('preferences baseline')['commit']
    revision = package_revision(DiskSource(layer._ctx.tree).files, 'user-preferences')
    layer.authoring.import_package(ATTR, 'user-preferences', {'files': [{'path': 'SKILL.md', 'content': 'second\n'}]}, manifest=manifest, expected_package_revision=revision)
    assert (layer.root / 'user-preferences/SKILL.md').read_text() == 'second\n'
    layer.history.restore(ATTR, commit, ['user-preferences/SKILL.md'])
    assert (layer.root / 'user-preferences/SKILL.md').read_text() == 'first\n'
    revision = package_revision(DiskSource(layer._ctx.tree).files, 'user-preferences')
    layer.authoring.remove_package(ATTR, 'user-preferences', revision)
    assert not (layer.root / 'user-preferences/SKILL.md').exists()
    assert layer.changes.list()['changes'][-1]['kind'] == 'remove_package'


def test_protected_plan_and_restore_refused(layer):
    seed(layer.root, 'roles', 'locked')
    git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'baseline')
    path = 'roles/locked/agentic.yaml'
    with pytest.raises(AgenticError) as err:
        layer.history.restore(ATTR, 'HEAD', [path])
    assert err.value.code == 'protected_agentic_area'
    with pytest.raises(AgenticError) as err:
        layer.overseer.applications._executable_plan({'changes': [{'path': path}], 'checks': []})
    assert err.value.code == 'protected_agentic_area'


def test_interrupted_protected_authoring_is_not_replayed(layer):
    from tests.conftest import SimulatedCrash, crash_at, reopen
    seed(layer.root, 'plugins', 'example')
    crash_at(layer, 'after_journal')
    with pytest.raises(SimulatedCrash):
        layer.authoring.apply(ATTR, [{'op': 'create', 'path': 'plugins/example/new.txt', 'content': 'data'}])
    with layer._ctx.store.transaction() as conn:
        conn.execute("UPDATE pending_files SET path='roles/locked/new.txt'")
    resumed = reopen(layer)
    with pytest.raises(AgenticError) as err:
        resumed.authoring.recover()
    assert err.value.code == 'protected_agentic_area'
    assert not (layer.root / 'roles/locked/new.txt').exists()
    assert resumed.authoring.recovery_status()['required']


def test_history_rejects_protected_rename_and_delete_without_index_changes(layer):
    role = seed(layer.root, 'roles', 'locked')
    git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'baseline')
    source = role / 'preferences/style.md'
    target = layer.root / 'user-preferences' / 'moved.md'
    source.rename(target)
    git(layer, 'add', '-A')
    before = git(layer, 'diff', '--cached', '--binary').stdout
    with pytest.raises(AgenticError) as err:
        layer.history.commit('rename')
    assert err.value.code == 'protected_agentic_area'
    assert git(layer, 'diff', '--cached', '--binary').stdout == before


def test_protected_helpers_and_dry_run_refuse(layer):
    role = seed(layer.root, 'roles', 'locked')
    manifest = 'roles/locked/agentic.yaml'
    old = (role / 'agentic.yaml').read_bytes()
    revision = package_revision(DiskSource(layer._ctx.tree).files, 'roles/locked')
    operations = [
        lambda: layer.authoring.apply(ATTR, [{'op': 'create', 'path': 'roles/locked/new', 'content': 'x'}], dry_run=True),
        lambda: layer.authoring.apply(ATTR, [{'op': 'delete', 'path': manifest, 'expected_sha256': sha(old)}]),
        lambda: layer.authoring.update_entry(ATTR, 'locked', {'id': 'new'}, sha(old)),
        lambda: layer.authoring.remove_package(ATTR, 'locked', revision),
    ]
    head = layer.changes.list()['head']
    for operation in operations:
        with pytest.raises(AgenticError) as error:
            operation()
        assert error.value.code == 'protected_agentic_area'
    assert (role / 'agentic.yaml').read_bytes() == old
    assert layer.changes.list()['head'] == head


@pytest.mark.parametrize('kind', ['file', 'zero', 'hidden', 'symlink', 'fifo', 'reserved', 'git'])
def test_legacy_empty_scaffolding_and_blocking_nodes(layer, kind):
    import os
    from qorqut_agentic.files import SafeTree
    nested = layer.root / 'packages' / 'old' / 'nested'
    nested.mkdir(parents=True)
    assert not DiskSource(SafeTree(layer.root)).legacy_nonempty
    assert not any(i['code'] == 'layout_migration_required' for i in layer.catalog.list()['issues'])
    node = nested / ({'hidden': '.hidden', 'reserved': '.agentic-tmp-test', 'git': '.git'}.get(kind, 'node'))
    if kind == 'symlink':
        node.symlink_to(layer.root / 'plugins', target_is_directory=True)
    elif kind == 'fifo':
        os.mkfifo(node)
    elif kind == 'git':
        node.mkdir()
    else:
        node.write_bytes(b'' if kind == 'zero' else b'legacy')
    try:
        source = DiskSource(SafeTree(layer.root))
    except AgenticError:
        return  # Reserved names and unsafe paths refuse rather than proving emptiness.
    assert source.legacy_nonempty
    assert any(i['code'] == 'layout_migration_required' for i in layer.catalog.list()['issues'])


def test_legacy_inspection_depth_bound(layer):
    from qorqut_agentic.files import SafeTree
    directory = layer.root / 'packages'
    directory.mkdir()
    for _ in range(64):
        directory /= 'd'
        directory.mkdir()
    with pytest.raises(AgenticError):
        DiskSource(SafeTree(layer.root))
