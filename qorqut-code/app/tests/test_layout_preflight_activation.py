import copy
import os
from types import SimpleNamespace

import pytest

from bazarlyk.agentic import layout_activation, upgrade
from bazarlyk.agentic.support import digest
from qorqut_operations.contracts import DomainError
from test_layout_intent import ATTR, legacy


@pytest.mark.parametrize('bad', ['/tmp/outside/file', '../outside/file', '.git/config', 'unrelated/file', 'plugins/../outside/file', 'plugins/bad\nname/file'])
@pytest.mark.parametrize('collection', ['path_map', 'files', 'destination'])
def test_invalid_authored_probe_paths_refuse_before_any_probe(tmp_path, monkeypatch, bad, collection):
    root = tmp_path / 'instance' / 'agentic'
    root.mkdir(parents=True)
    sibling = tmp_path / 'outside'
    sibling.mkdir()
    if bad == '/tmp/outside/file':
        bad = str(sibling / 'file')
    paths = SimpleNamespace(agentic_root=root, instance_root=root.parent)
    plan = {'path_map': {'packages/good/file': ['plugins/good/file']}, 'files': {'plugins/good/file': {}}}
    if collection == 'path_map':
        plan['path_map'][bad] = ['plugins/good/other']
    elif collection == 'files':
        plan['files'][bad] = {}
    else:
        plan['path_map']['packages/good/file'].append(bad)
    calls = []
    monkeypatch.setattr(layout_activation, '_probe', lambda parent: calls.append(parent))
    with pytest.raises((DomainError, ValueError)):
        layout_activation._probe_filesystems(paths, plan)
    assert not calls and not list(sibling.iterdir())


def test_stale_first_plan_refuses_before_probe(legacy, monkeypatch):
    paths, export = legacy
    original = upgrade.prepare_upgrade(paths, export)
    assert original['complete'], original['conflicts']
    stale = copy.deepcopy(original)
    stale['path_map']['packages/synthetic/file'] = ['plugins/synthetic/file']
    stale['plan_digest'] = digest({k: v for k, v in stale.items() if k != 'plan_digest'})
    calls = []
    monkeypatch.setattr(upgrade, 'prepare_upgrade', lambda *args, **kwargs: original)
    monkeypatch.setattr(layout_activation, '_probe', lambda parent: calls.append(parent))
    with pytest.raises(DomainError, match='changed'):
        upgrade.apply_upgrade(paths, stale, attribution=ATTR)
    assert not calls


def test_probe_replaces_distinct_inode_and_removes_source(tmp_path, monkeypatch):
    real_replace = os.replace
    seen = []
    def observe(src, dst, *, src_dir_fd, dst_dir_fd):
        source = os.stat(src, dir_fd=src_dir_fd, follow_symlinks=False)
        target = os.stat(dst, dir_fd=dst_dir_fd, follow_symlinks=False)
        seen.append(((source.st_dev, source.st_ino), (target.st_dev, target.st_ino)))
        real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        with pytest.raises(FileNotFoundError):
            os.stat(src, dir_fd=src_dir_fd, follow_symlinks=False)
    monkeypatch.setattr(layout_activation.os, 'replace', observe)
    layout_activation._probe(tmp_path)
    assert len(seen) == 1 and seen[0][0] != seen[0][1]
    assert not list(tmp_path.iterdir())


def test_probe_refuses_noop_replacement(tmp_path, monkeypatch):
    monkeypatch.setattr(layout_activation.os, 'replace', lambda *args, **kwargs: None)
    with pytest.raises(DomainError, match='did not remove'):
        layout_activation._probe(tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('field', ['path_map', 'files'])
def test_probe_rejects_non_mapping_collections(tmp_path, monkeypatch, field):
    root = tmp_path / 'agentic'
    root.mkdir()
    paths = SimpleNamespace(agentic_root=root, instance_root=tmp_path)
    plan = {'path_map': {}, 'files': {}}
    plan[field] = []
    calls = []
    monkeypatch.setattr(layout_activation, '_probe', calls.append)
    with pytest.raises(DomainError):
        layout_activation._probe_filesystems(paths, plan)
    assert calls == []


def test_probe_rejects_excessive_depth_before_probe(tmp_path, monkeypatch):
    root = tmp_path / 'agentic'
    root.mkdir()
    paths = SimpleNamespace(agentic_root=root, instance_root=tmp_path)
    plan = {'path_map': {'packages/good/file': ['plugins/good/file']},
            'files': {'plugins/' + '/'.join(['a'] * 64): {}}}
    calls = []
    monkeypatch.setattr(layout_activation, '_probe', calls.append)
    with pytest.raises(DomainError):
        layout_activation._probe_filesystems(paths, plan)
    assert calls == []


def test_completed_replay_does_not_probe_or_prepare(legacy, monkeypatch):
    from bazarlyk.agentic import installation, layout_intent
    from bazarlyk.agentic.support import canonical
    paths, export = legacy
    plan = upgrade.prepare_upgrade(paths, export)
    intent = {'plan': plan, 'attribution': ATTR}
    candidate = {'selected_extensions': []}
    result = {'commit': 'synthetic', 'binding_sha256': 'synthetic', 'change_id': None,
              'sequence': None, 'activation': True}
    monkeypatch.setattr(layout_intent, 'retained_intents', lambda _: [intent])
    monkeypatch.setattr(layout_intent, 'incomplete_intents', lambda _: [])
    monkeypatch.setattr(layout_activation, '_read', lambda _: {'phase': 'complete'})
    monkeypatch.setattr(layout_activation, '_verify', lambda *args: (result, candidate))
    monkeypatch.setattr(layout_activation, 'read_binding_bytes', lambda _: canonical(candidate) + b'\n')
    monkeypatch.setattr(installation, '_open_record', lambda *args: None)
    def forbidden(*args, **kwargs):
        raise AssertionError('completed replay must not probe or reprepare')
    monkeypatch.setattr(layout_activation, '_probe', forbidden)
    monkeypatch.setattr(upgrade, 'prepare_upgrade', forbidden)
    assert not upgrade.apply_upgrade(paths, plan, attribution=ATTR)['worked']
