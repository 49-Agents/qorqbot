"""Offline preparation against the retained synthetic T09 fixture and small Git repos."""
import base64
import os
from pathlib import Path
import subprocess

import pytest

from bazarlyk.agentic import layout_files, layout_git, layout_intent, upgrade
from qorqut_operations.contracts import DomainError
from test_layout_intent import ATTR, git, legacy


def test_full_prepare_and_retry(legacy, monkeypatch):
    paths, export = legacy
    root = paths.agentic_root
    old = 'packages/agentic-management/bootstrap.md'
    (root / old).write_bytes(b'staged authored\n')
    git(paths, 'add', '--', old)
    (root / old).write_bytes(b'working authored\n')
    deleted = root / 'packages/imported-email-management-templates/native/skills/email-management/instance-template/inboxes/README.md'
    deleted.unlink()
    executable = root / 'packages/custom-tools/echo.py'
    executable.chmod(0o755)
    extra = root / 'unrelated.txt'
    extra.write_bytes(b'unrelated staged\n')
    git(paths, 'add', '--', 'unrelated.txt')
    extra.write_bytes(b'unrelated working\n')
    plan = upgrade.prepare_upgrade(paths, export)
    assert plan['complete'], plan['conflicts']
    layout_intent.begin(paths, plan, attribution=ATTR)
    layout_files.relocate(paths, plan, attribution=ATTR)
    head = git(paths, 'rev-parse', 'HEAD').strip()
    index = (root / '.git/index').read_bytes()
    binding = upgrade.read_binding_bytes(paths)
    db = (paths.agentic_runtime / 'state.sqlite3').read_bytes()
    before_files, before_meta = layout_files._authored(root)
    refs = git(paths, 'for-each-ref', '--format=%(refname) %(objectname)')
    writer = layout_files._journal_write
    captured = {}
    def interrupt(path, journal):
        if journal['phase'] == 'prepared':
            captured.update(journal)
            raise OSError('interrupted before prepared receipt')
        return writer(path, journal)
    monkeypatch.setattr(layout_files, '_journal_write', interrupt)
    with pytest.raises(OSError, match='interrupted'):
        layout_git.prepare(paths, plan, attribution=ATTR)
    monkeypatch.undo()
    first = layout_git.prepare(paths, plan, attribution=ATTR)
    assert (first['commit'], first['tree']) == (captured['commit'], captured['tree'])
    assert first['phase'] == 'prepared' and first['worked']
    assert git(paths, 'rev-parse', 'HEAD').strip() == head
    assert git(paths, 'for-each-ref', '--format=%(refname) %(objectname)') == refs
    assert git(paths, 'rev-list', '--parents', '-n', '1', first['commit']).split() == [first['commit'].encode(), head]
    assert git(paths, 'ls-tree', first['commit'], '--', 'unrelated.txt') == git(paths, 'ls-tree', head.decode(), '--', 'unrelated.txt')
    target_working = plan['path_map'][old][0]
    assert git(paths, 'show', first['commit'] + ':' + target_working) == b'working authored\n'
    journal = layout_git._read_journal(upgrade.history_directory(paths) / ('git-layout-' + plan['plan_digest'] + '.json'))
    final = layout_git._index_entries(base64.b64decode(journal['final_index']['content_base64']))
    original = layout_git._index_entries(index)
    assert final['unrelated.txt'] == original['unrelated.txt']
    for old_path in plan['path_map']:
        assert old_path not in final
    target = plan['path_map']['packages/custom-tools/echo.py'][0]
    assert final[target][0] == 0o100755
    assert not deleted.exists()
    assert (root / '.git/index').read_bytes() == index
    assert extra.read_bytes() == b'unrelated working\n'
    assert layout_files._authored(root) == (before_files, before_meta)
    assert upgrade.read_binding_bytes(paths) == binding
    assert (paths.agentic_runtime / 'state.sqlite3').read_bytes() == db
    again = layout_git.prepare(paths, plan, attribution=ATTR)
    assert again == {**first, 'worked': False}
    assert (root / '.git/index').read_bytes() == index
    with pytest.raises(DomainError):
        layout_git.prepare(paths, plan, attribution={**ATTR, 'reason': 'different'})
    bad_plan = {**plan, 'layout_change': False}
    with pytest.raises(DomainError):
        layout_git.prepare(paths, bad_plan, attribution=ATTR)
    git(paths, 'branch', 'synthetic-other', head.decode())
    old_head = (root / '.git/HEAD').read_bytes()
    git(paths, 'symbolic-ref', 'HEAD', 'refs/heads/synthetic-other')
    with pytest.raises(DomainError):
        layout_git.prepare(paths, plan, attribution=ATTR)
    (root / '.git/HEAD').write_bytes(old_head)
    extra.write_bytes(b'changed working only\n')
    git(paths, 'add', '--', 'unrelated.txt')
    with pytest.raises(DomainError):
        layout_git.prepare(paths, plan, attribution=ATTR)


def test_small_two_indexes_and_stable_candidate(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    def cmd(*args):
        return subprocess.check_output(['git', '-C', str(root), *args])
    cmd('init', '-q', '-b', 'synthetic')
    cmd('config', 'user.name', 'Synthetic')
    cmd('config', 'user.email', 'synthetic@example.invalid')
    (root / 'packages').mkdir()
    (root / 'packages/old').write_bytes(b'old\n')
    (root / 'unrelated').write_bytes(b'head\n')
    cmd('add', '.')
    cmd('commit', '-qm', 'initial')
    (root / 'unrelated').write_bytes(b'staged\n')
    cmd('add', 'unrelated')
    (root / 'unrelated').write_bytes(b'working\n')
    cmd('update-index', '--assume-unchanged', 'unrelated')
    raw = (root / '.git/index').read_bytes()
    assert layout_git._index_entries(raw)['unrelated'][2]
    parent = cmd('rev-parse', 'HEAD').strip().decode()
    j = {'parent': parent, 'timestamp': 1700000000, 'message': 'synthetic\n'}
    intent = {'index': {'content_base64': base64.b64encode(raw).decode()}}
    authored = {'plugins/example': (b'new\n', 0o755)}
    runner = layout_git._runner(root, tmp_path)
    first = layout_git._make_candidates(runner, tmp_path, intent, j, authored)
    second = layout_git._make_candidates(runner, tmp_path, intent, j, authored)
    assert first == second
    tree, commit, final = first
    assert cmd('show', commit + ':unrelated') == b'head\n'
    assert cmd('show', commit + ':plugins/example') == b'new\n'
    assert layout_git._index_entries(final)['unrelated'] == layout_git._index_entries(raw)['unrelated']
    assert (root / '.git/index').read_bytes() == raw
    assert cmd('rev-parse', 'HEAD').strip().decode() == parent
    assert (root / 'unrelated').read_bytes() == b'working\n'


def test_unsupported_index_refuses(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    def cmd(*args):
        return subprocess.check_output(['git', '-C', str(root), *args])
    cmd('init', '-q', '-b', 'synthetic')
    (root / 'entry').write_bytes(b'example')
    cmd('add', 'entry')
    cmd('update-index', '--skip-worktree', 'entry')
    with pytest.raises(DomainError):
        layout_git._index_entries((root / '.git/index').read_bytes())


def test_empty_unrelated_tree_refuses(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    def cmd(*args, data=None):
        return subprocess.check_output(['git', '-C', str(root), *args], input=data)
    cmd('init', '-q', '-b', 'synthetic')
    cmd('config', 'user.name', 'Synthetic')
    cmd('config', 'user.email', 'synthetic@example.invalid')
    (root / 'unrelated').write_bytes(b'head\n')
    cmd('add', '.')
    cmd('commit', '-qm', 'initial')
    empty = cmd('mktree', data=b'').strip()
    entries = cmd('ls-tree', 'HEAD') + b'040000 tree ' + empty + b'\tempty\n'
    tree = cmd('mktree', data=entries).strip()
    parent = cmd('commit-tree', tree.decode(), '-p', 'HEAD', data=b'include empty tree\n').strip()
    cmd('update-ref', 'HEAD', parent.decode())
    raw = (root / '.git/index').read_bytes()
    intent = {'index': {'content_base64': base64.b64encode(raw).decode()}}
    journal = {'parent': parent.decode(), 'timestamp': 1700000000, 'message': 'synthetic\n'}
    runner = layout_git._runner(root, tmp_path)
    authored = {'plugins/example': (b'new\n', 0o644)}
    tree, commit, final = layout_git._make_candidates(runner, tmp_path, intent, journal, authored)
    journal.update(tree=tree, commit=commit, final_index={'content_base64': base64.b64encode(final).decode()})
    with pytest.raises(DomainError, match='unrelated tree'):
        layout_git._verify_candidate(runner, journal, intent, authored)
    assert cmd('rev-parse', 'HEAD').strip() == parent
    assert (root / '.git/index').read_bytes() == raw


def test_filter_configuration_refused(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    subprocess.check_call(['git', '-C', str(root), 'init', '-q', '-b', 'synthetic'])
    subprocess.check_call(['git', '-C', str(root), 'config', 'filter.example.clean', 'false'])
    with pytest.raises(DomainError, match='filter drivers'):
        layout_git._reject_filters(layout_git._runner(root, tmp_path))


@pytest.mark.parametrize('mode', [0o644, 0o755, 0o654, 0o645])
def test_candidate_mode_matches_git_filemode(tmp_path, mode):
    root = tmp_path / 'repo'
    root.mkdir()
    def cmd(*args):
        return subprocess.check_output(['git', '-C', str(root), *args])
    cmd('init', '-q', '-b', 'synthetic')
    cmd('config', 'user.name', 'Synthetic')
    cmd('config', 'user.email', 'synthetic@example.invalid')
    cmd('config', 'core.filemode', 'true')
    (root / 'seed').write_bytes(b'base\n')
    cmd('add', 'seed')
    cmd('commit', '-qm', 'initial')
    probe = root / 'probe'
    probe.write_bytes(b'executable mode probe\n')
    probe.chmod(mode)
    cmd('add', 'probe')
    git_mode = int(cmd('ls-files', '--stage', '--', 'probe').split()[0], 8)
    raw = (root / '.git/index').read_bytes()
    intent = {'index': {'content_base64': base64.b64encode(raw).decode()}}
    journal = {'parent': cmd('rev-parse', 'HEAD').strip().decode(),
               'timestamp': 1700000000, 'message': 'synthetic\n'}
    authored = {'plugins/example': (probe.read_bytes(), mode)}
    runner = layout_git._runner(root, tmp_path)
    tree, commit, final = layout_git._make_candidates(runner, tmp_path, intent, journal, authored)
    journal.update(tree=tree, commit=commit,
                   final_index={'content_base64': base64.b64encode(final).decode()})
    layout_git._verify_candidate(runner, journal, intent, authored)
    tree_mode = int(cmd('ls-tree', commit, '--', 'plugins/example').split()[0], 8)
    index_mode = layout_git._index_entries(final)['plugins/example'][0]
    assert tree_mode == index_mode == git_mode
    assert probe.stat().st_mode & 0o777 == mode
    assert (root / '.git/index').read_bytes() == raw
