"""Offline relocation tests use only the archived synthetic T09 fixture."""
import base64
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_layout_intent import ATTR, legacy, git
from bazarlyk.agentic import layout_files, layout_intent, upgrade
from qorqut_operations.contracts import DomainError


def prepared(legacy):
    paths, export = legacy
    plan = upgrade.prepare_upgrade(paths, export)
    assert plan['complete'], plan['conflicts']
    layout_intent.begin(paths, plan, attribution=ATTR)
    return paths, plan


def test_relocate_and_retry(legacy):
    paths, export = legacy
    custom = paths.agentic_root / 'packages/custom-tools/echo.py'
    custom.chmod(0o755)
    staged = paths.agentic_root / 'packages/agentic-management/bootstrap.md'
    staged.write_bytes(b'staged synthetic\n')
    git(paths, 'add', '--', 'packages/agentic-management/bootstrap.md')
    staged.write_bytes(b'working synthetic\n')
    deleted = paths.agentic_root / 'packages/imported-email-management-templates/native/skills/email-management/instance-template/inboxes/README.md'
    deleted.unlink()
    extra = paths.agentic_root / 'plugins/local-extra'
    extra.mkdir(parents=True)
    (extra / 'agentic.yaml').write_text('schema_version: 1\nid: local-extra\nname: Extra\ndescription: Synthetic\nentries: []\n')
    (extra / 'note.md').write_bytes(b'preserved synthetic\n')
    original_index = (paths.agentic_root / '.git/index').read_bytes()
    original_head = git(paths, 'rev-parse', 'HEAD')
    binding = upgrade.read_binding_bytes(paths)
    plan = upgrade.prepare_upgrade(paths, export)
    assert plan['complete'], plan['conflicts']
    assert any(not item.get('enabled') for item in plan['target_inventory']['bindings'])
    layout_intent.begin(paths, plan, attribution=ATTR)
    db = (paths.agentic_runtime / 'state.sqlite3').read_bytes()
    result = layout_files.relocate(paths, plan, attribution=ATTR)
    assert result['worked'] and result['phase'] == 'files_published'
    assert (paths.agentic_root / plan['path_map']['packages/custom-tools/echo.py'][0]).stat().st_mode & 0o777 == 0o755
    assert not custom.exists()
    target_modes = {target: plan['legacy_authored_files'][source]['mode'] for source, targets in plan['path_map'].items() for target in targets}
    for path, item in plan['files'].items():
        published = paths.agentic_root / path
        assert published.read_bytes() == base64.b64decode(item['content_base64'])
        assert published.stat().st_mode & 0o777 == target_modes.get(path, 0o644)
    assert (extra / 'note.md').read_bytes() == b'preserved synthetic\n'
    assert all(not (paths.agentic_root / old).exists() for old in plan['path_map'])
    assert (paths.agentic_root / '.git/index').read_bytes() == original_index
    assert git(paths, 'rev-parse', 'HEAD') == original_head
    assert upgrade.read_binding_bytes(paths) == binding
    assert (paths.agentic_runtime / 'state.sqlite3').read_bytes() == db
    assert not layout_files.relocate(paths, plan, attribution=ATTR)['worked']
    (extra / 'note.md').write_bytes(b'changed after completion')
    with pytest.raises(DomainError):
        layout_files.relocate(paths, plan, attribution=ATTR)


def test_ambiguous_directory_refuses(legacy, monkeypatch):
    paths, plan = prepared(legacy)
    original = layout_files._journal_write
    calls = 0
    def fail(path, journal):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('interrupt before directory identity')
        return original(path, journal)
    monkeypatch.setattr(layout_files, '_journal_write', fail)
    with pytest.raises(OSError):
        layout_files.relocate(paths, plan, attribution=ATTR)
    monkeypatch.undo()
    with pytest.raises(DomainError, match='Ambiguous directory'):
        layout_files.relocate(paths, plan, attribution=ATTR)


def test_special_mode_refuses(legacy):
    paths, export = legacy
    target = paths.agentic_root / 'packages/custom-tools/echo.py'
    target.chmod(0o4755)
    plan = upgrade.prepare_upgrade(paths, export)
    assert plan['complete'], plan['conflicts']
    layout_intent.begin(paths, plan, attribution=ATTR)
    with pytest.raises(DomainError, match='Special authored'):
        layout_files.relocate(paths, plan, attribution=ATTR)
    assert target.exists()


@pytest.mark.parametrize('kind,when', [('publish', 'before'), ('publish', 'after'), ('remove', 'before'), ('remove', 'after')])
def test_file_boundary_retry(legacy, monkeypatch, kind, when):
    paths, plan = prepared(legacy)
    original = layout_files._apply_file
    hit = False
    def interrupt(rootfd, journal, effect, originals, targets):
        nonlocal hit
        if effect['kind'] == kind and not hit:
            hit = True
            if when == 'after':
                original(rootfd, journal, effect, originals, targets)
            raise OSError('synthetic boundary interruption')
        return original(rootfd, journal, effect, originals, targets)
    monkeypatch.setattr(layout_files, '_apply_file', interrupt)
    with pytest.raises(OSError, match='boundary'):
        layout_files.relocate(paths, plan, attribution=ATTR)
    assert hit
    monkeypatch.undo()
    assert layout_files.relocate(paths, plan, attribution=ATTR)['phase'] == 'files_published'
    assert not layout_files.relocate(paths, plan, attribution=ATTR)['worked']


@pytest.mark.parametrize('damage', ['bytes', 'mode', 'addition', 'symlink', 'directory'])
def test_divergent_intermediate_refuses(legacy, monkeypatch, tmp_path, damage):
    paths, plan = prepared(legacy)
    original = layout_files._apply_file
    selected = None
    def interrupt(rootfd, journal, effect, originals, targets):
        nonlocal selected
        if effect['kind'] == 'publish':
            original(rootfd, journal, effect, originals, targets)
            selected = effect['path']
            raise OSError('synthetic interruption')
        return original(rootfd, journal, effect, originals, targets)
    monkeypatch.setattr(layout_files, '_apply_file', interrupt)
    with pytest.raises(OSError):
        layout_files.relocate(paths, plan, attribution=ATTR)
    monkeypatch.undo()
    target = paths.agentic_root / selected
    if damage == 'bytes':
        target.write_bytes(b'divergent')
    elif damage == 'mode':
        target.chmod((target.stat().st_mode & 0o777) ^ 0o100)
    elif damage == 'addition':
        (target.parent / 'unexpected.md').write_bytes(b'unexpected')
    else:
        parent = target.parent
        moved = parent.with_name(parent.name + '-displaced')
        parent.rename(moved)
        if damage == 'symlink':
            outside = tmp_path / 'outside'
            outside.mkdir()
            sentinel = outside / 'sentinel'
            sentinel.write_bytes(b'unchanged')
            parent.symlink_to(outside, target_is_directory=True)
        else:
            parent.mkdir()
    with pytest.raises((DomainError, OSError)):
        layout_files.relocate(paths, plan, attribution=ATTR)
    if damage == 'symlink':
        assert sentinel.read_bytes() == b'unchanged'


@pytest.mark.parametrize('damage', ['binding', 'index', 'runtime', 'journal'])
def test_changed_resume_evidence_refuses(legacy, monkeypatch, damage):
    paths, plan = prepared(legacy)
    def interrupt(rootfd, journal, effect, originals, targets):
        raise OSError('stop before publish')
    monkeypatch.setattr(layout_files, '_apply_file', interrupt)
    with pytest.raises(OSError, match='stop before publish'):
        layout_files.relocate(paths, plan, attribution=ATTR)
    monkeypatch.undo()
    if damage == 'binding':
        path = paths.instance_root / 'config/agentic-layer.json'
        path.write_bytes(path.read_bytes() + b' ')
    elif damage == 'index':
        (paths.agentic_root / 'unrelated.txt').write_bytes(b'synthetic')
        git(paths, 'add', '--', 'unrelated.txt')
    elif damage == 'runtime':
        import sqlite3
        with sqlite3.connect(paths.agentic_runtime / 'state.sqlite3') as db:
            db.execute("UPDATE consumers SET cursor=cursor+1 WHERE name='synthetic-consumer'")
    else:
        path = upgrade.history_directory(paths) / ('relocation-' + plan['plan_digest'] + '.json')
        path.write_bytes(b'{}\n')
    with pytest.raises((DomainError, OSError)):
        layout_files.relocate(paths, plan, attribution=ATTR)


def test_special_directory_mode_refuses(legacy):
    paths, export = legacy
    directory = paths.agentic_root / 'packages/custom-tools'
    directory.chmod(0o2755)
    plan = upgrade.prepare_upgrade(paths, export)
    assert plan['complete'], plan['conflicts']
    layout_intent.begin(paths, plan, attribution=ATTR)
    with pytest.raises(DomainError, match='Special directory mode'):
        layout_files.relocate(paths, plan, attribution=ATTR)
    assert directory.is_dir()


def test_directory_at_target_refuses_before_effects(legacy):
    paths, plan = prepared(legacy)
    target = paths.agentic_root / next(iter(plan['files']))
    target.mkdir(parents=True)
    with pytest.raises(DomainError):
        layout_files.relocate(paths, plan, attribution=ATTR)
    assert target.is_dir()
    assert not (upgrade.history_directory(paths) / ('relocation-' + plan['plan_digest'] + '.json')).exists()


@pytest.mark.parametrize('damage', ['source_bytes', 'source_mode', 'destination_bytes', 'destination_mode'])
def test_remove_effect_rechecks_source_and_destination(tmp_path, damage):
    root = tmp_path / 'root'
    source = root / 'packages/old/source'
    target = root / 'plugins/new/target'
    source.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    source.write_bytes(b'original source')
    target.write_bytes(b'published target')
    source.chmod(0o644)
    target.chmod(0o755)
    originals = {'packages/old/source': (b'original source', 0o644)}
    targets = {'plugins/new/target': (b'published target', 0o755)}
    effect = {'kind': 'remove', 'path': 'packages/old/source', 'targets': ['plugins/new/target']}
    fd = layout_files._open_root(root)
    try:
        journal = {'initial_dirs': layout_files._directories(fd), 'created_dirs': {},
                   'root': layout_files._identity(os.fstat(fd)), 'next_effect': 1,
                   'effects': [{'kind': 'publish', 'path': 'plugins/new/target'}, effect]}
        layout_files._verify_tree(SimpleNamespace(agentic_root=root), fd, journal, originals, targets)
        if damage == 'source_bytes':
            source.write_bytes(b'edited source')
        elif damage == 'source_mode':
            source.chmod(0o755)
        elif damage == 'destination_bytes':
            target.write_bytes(b'edited target')
        else:
            target.chmod(0o644)
        source_before = (source.read_bytes(), source.stat().st_mode & 0o777)
        with pytest.raises(DomainError):
            layout_files._apply_file(fd, journal, effect, originals, targets)
        assert (source.read_bytes(), source.stat().st_mode & 0o777) == source_before
    finally:
        os.close(fd)


def test_publish_effect_rechecks_occupied_target(tmp_path):
    root = tmp_path / 'root'
    target = root / 'plugins/new/target'
    target.parent.mkdir(parents=True)
    fd = layout_files._open_root(root)
    try:
        journal = {'initial_dirs': layout_files._directories(fd), 'created_dirs': {},
                   'root': layout_files._identity(os.fstat(fd)), 'next_effect': 0,
                   'effects': [{'kind': 'publish', 'path': 'plugins/new/target'}]}
        layout_files._verify_tree(SimpleNamespace(agentic_root=root), fd, journal, {},
                                  {'plugins/new/target': (b'expected bytes', 0o644)})
        target.write_bytes(b'unexpected occupied target')
        with pytest.raises(DomainError):
            layout_files._apply_file(fd, journal, {'kind': 'publish', 'path': 'plugins/new/target'}, {},
                                     {'plugins/new/target': (b'expected bytes', 0o644)})
        assert target.read_bytes() == b'unexpected occupied target'
    finally:
        os.close(fd)


def test_relocate_preserves_source_edited_after_global_check(legacy, monkeypatch):
    paths, plan = prepared(legacy)
    original = layout_files._apply_file
    changed = None
    def edit_before_effect(rootfd, journal, effect, originals, targets):
        nonlocal changed
        if effect['kind'] == 'remove' and changed is None:
            changed = paths.agentic_root / effect['path']
            changed.write_bytes(b'edit after global verification')
        return original(rootfd, journal, effect, originals, targets)
    monkeypatch.setattr(layout_files, '_apply_file', edit_before_effect)
    with pytest.raises(DomainError):
        layout_files.relocate(paths, plan, attribution=ATTR)
    assert changed is not None and changed.read_bytes() == b'edit after global verification'
    journal_path = upgrade.history_directory(paths) / ('relocation-' + plan['plan_digest'] + '.json')
    journal = layout_files._journal_read(journal_path)
    assert journal['effects'][journal['next_effect']]['path'] == str(changed.relative_to(paths.agentic_root))
