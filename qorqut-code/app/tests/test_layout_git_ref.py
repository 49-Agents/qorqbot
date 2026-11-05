"""Real native Git ref transactions and the offline relocation integration."""
import base64
import os
from pathlib import Path
import subprocess

import pytest

from bazarlyk.agentic import layout_files, layout_git, layout_intent, upgrade
from qorqut_agentic.history import GitRunner
from qorqut_operations.contracts import DomainError
from test_layout_intent import ATTR, git, legacy


def native_repository(tmp_path, *, detached=False):
    root = tmp_path / 'repo'
    root.mkdir()
    def cmd(*args, input=None):
        return subprocess.check_output(['git', '-C', str(root), *args], input=input)
    cmd('init', '-q', '-b', 'synthetic')
    cmd('config', 'user.name', 'Synthetic')
    cmd('config', 'user.email', 'synthetic@example.invalid')
    (root / 'file').write_bytes(b'parent\n')
    cmd('add', 'file')
    cmd('commit', '-qm', 'parent')
    parent = cmd('rev-parse', 'HEAD').strip().decode()
    cmd('branch', 'alternate', parent)
    tree = cmd('rev-parse', 'HEAD^{tree}').strip().decode()
    candidate = cmd('commit-tree', tree, '-p', parent, input=b'candidate\n').strip().decode()
    if detached:
        (root / '.git/HEAD').write_text(parent + '\n')
    rootfd = layout_files._open_root(root)
    gitdir, raw = layout_files._head_identity(rootfd)
    journal = {'gitdir': gitdir, 'head_identity': raw, 'parent': parent, 'commit': candidate}
    return root, cmd, rootfd, layout_git._runner(root, tmp_path), journal


@pytest.mark.parametrize('detached', [False, True])
def test_native_transaction_branch_and_detached(tmp_path, detached):
    root, cmd, rootfd, runner, journal = native_repository(tmp_path, detached=detached)
    try:
        dest = layout_git._destination(runner, journal)
        assert dest == ('HEAD' if detached else 'refs/heads/synthetic')
        index = (root / '.git/index').read_bytes()
        refs = cmd('rev-parse', 'refs/heads/alternate')
        layout_git._check_operation_locks(root, dest)
        def guard():
            layout_git._check_operation_locks(root, dest, transaction=True)
            assert layout_git._head_state(runner, rootfd, journal, dest) == 'original'
        layout_git._transaction(runner, journal['parent'], journal['commit'], guard)
        assert layout_git._head_state(runner, rootfd, journal, dest) == 'candidate'
        layout_git._check_operation_locks(root, dest)
        assert (root / '.git/index').read_bytes() == index
        assert (root / 'file').read_bytes() == b'parent\n'
        assert cmd('rev-parse', 'refs/heads/alternate') == refs
    finally:
        os.close(rootfd)


def test_native_guard_refuses_same_oid_branch_switch(tmp_path):
    root, cmd, rootfd, runner, journal = native_repository(tmp_path)
    try:
        dest = layout_git._destination(runner, journal)
        cmd('symbolic-ref', 'HEAD', 'refs/heads/alternate')
        def guard():
            layout_git._check_operation_locks(root, dest, transaction=True)
            layout_git._head_state(runner, rootfd, journal, dest)
        with pytest.raises(DomainError):
            layout_git._transaction(runner, journal['parent'], journal['commit'], guard)
        assert cmd('rev-parse', 'refs/heads/synthetic').strip().decode() == journal['parent']
        assert cmd('rev-parse', 'refs/heads/alternate').strip().decode() == journal['parent']
        assert not (root / '.git/HEAD.lock').exists()
    finally:
        os.close(rootfd)


def test_native_timeout_reaps_process(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    script = tmp_path / 'stalled-git'
    pidfile = tmp_path / 'stalled.pid'
    script.write_text('#!/usr/bin/python3\nimport os, time\nopen(' + repr(str(pidfile)) + ",'w').write(str(os.getpid()))\ntime.sleep(60)\n")
    script.chmod(0o755)
    runner = GitRunner(str(script), root, tmp_path)
    with pytest.raises(DomainError, match='timed out'):
        layout_git._transaction(runner, 'a' * 40, 'b' * 40, lambda: None, timeout=0.2)
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_full_ref_interruption_and_recovery(legacy, monkeypatch):
    paths, export = legacy
    root = paths.agentic_root
    unrelated = root / 'unrelated.txt'
    unrelated.write_bytes(b'unrelated staged\n')
    git(paths, 'add', '--', 'unrelated.txt')
    unrelated.write_bytes(b'unrelated working\n')
    plan = upgrade.prepare_upgrade(paths, export)
    assert plan['complete'], plan['conflicts']
    layout_intent.begin(paths, plan, attribution=ATTR)
    layout_files.relocate(paths, plan, attribution=ATTR)
    prepared = layout_git.prepare(paths, plan, attribution=ATTR)
    before_index = (root / '.git/index').read_bytes()
    before_authored = layout_files._authored(root)
    before_binding = upgrade.read_binding_bytes(paths)
    before_database = (paths.agentic_runtime / 'state.sqlite3').read_bytes()
    before_refs = git(paths, 'for-each-ref', '--format=%(refname) %(objectname)')
    def before_transaction(*_args, **_kwargs):
        raise OSError('interrupted before ref transaction')
    monkeypatch.setattr(layout_git, '_transaction', before_transaction)
    with pytest.raises(OSError, match='before ref transaction'):
        layout_git.publish_ref(paths, plan, attribution=ATTR)
    assert git(paths, 'rev-parse', 'HEAD').strip().decode() == prepared['parent']
    monkeypatch.undo()
    write = layout_files._journal_write
    def interrupt(path, journal):
        if journal['phase'] == 'ref_published':
            raise OSError('interrupted after ref commit')
        return write(path, journal)
    monkeypatch.setattr(layout_files, '_journal_write', interrupt)
    with pytest.raises(OSError, match='after ref commit'):
        layout_git.publish_ref(paths, plan, attribution=ATTR)
    monkeypatch.undo()
    assert git(paths, 'rev-parse', 'HEAD').strip().decode() == prepared['commit']
    assert git(paths, 'rev-list', '--parents', '-n', '1', 'HEAD').split() == [prepared['commit'].encode(), prepared['parent'].encode()]
    lock = root / '.git/index.lock'
    lock.write_bytes(b'unknown lock')
    with pytest.raises(DomainError):
        layout_git.publish_ref(paths, plan, attribution=ATTR)
    assert lock.read_bytes() == b'unknown lock'
    lock.unlink()
    unrelated.write_bytes(b'changed index\n')
    git(paths, 'add', '--', 'unrelated.txt')
    with pytest.raises(DomainError):
        layout_git.publish_ref(paths, plan, attribution=ATTR)
    (root / '.git/index').write_bytes(before_index)
    unrelated.write_bytes(b'unrelated working\n')
    result = layout_git.publish_ref(paths, plan, attribution=ATTR)
    assert result['worked'] and result['phase'] == 'ref_published'
    assert result['active_index'] == 'original' and not result['runtime_binding_activated']
    assert result['original_index_sha256'] != result['final_index_sha256']
    assert not layout_git.publish_ref(paths, plan, attribution=ATTR)['worked']
    assert (root / '.git/index').read_bytes() == before_index
    assert layout_files._authored(root) == before_authored
    assert unrelated.read_bytes() == b'unrelated working\n'
    assert upgrade.read_binding_bytes(paths) == before_binding
    assert (paths.agentic_runtime / 'state.sqlite3').read_bytes() == before_database
    after_refs = git(paths, 'for-each-ref', '--format=%(refname) %(objectname)')
    old_other = [line for line in before_refs.splitlines() if not line.startswith(result['destination'].encode() + b' ')]
    new_other = [line for line in after_refs.splitlines() if not line.startswith(result['destination'].encode() + b' ')]
    assert old_other == new_other


def test_stale_reference_and_nested_symbolic_branch_refuse(tmp_path):
    root, cmd, rootfd, runner, journal = native_repository(tmp_path)
    try:
        dest = layout_git._destination(runner, journal)
        tree = cmd('rev-parse', 'HEAD^{tree}').strip().decode()
        other = cmd('commit-tree', tree, '-p', journal['parent'], input=b'other\n').strip().decode()
        cmd('update-ref', dest, other)
        with pytest.raises(DomainError):
            layout_git._head_state(runner, rootfd, journal, dest)
        cmd('update-ref', dest, journal['parent'])
        cmd('symbolic-ref', dest, 'refs/heads/alternate')
        with pytest.raises(DomainError, match='not direct'):
            layout_git._head_state(runner, rootfd, journal, dest)
        assert cmd('rev-parse', 'refs/heads/alternate').strip().decode() == journal['parent']
    finally:
        os.close(rootfd)


def test_guard_aborts_changed_index_and_preserves_unknown_lock(tmp_path):
    root, cmd, rootfd, runner, journal = native_repository(tmp_path)
    try:
        dest = layout_git._destination(runner, journal)
        lock = root / '.git/index.lock'
        lock.write_bytes(b'foreign lock')
        with pytest.raises(DomainError):
            layout_git._check_operation_locks(root, dest)
        assert lock.read_bytes() == b'foreign lock'
        lock.unlink()
        original = (root / '.git/index').read_bytes()
        def guard():
            (root / 'file').write_bytes(b'concurrent staged content\n')
            cmd('add', 'file')
            if (root / '.git/index').read_bytes() != original:
                raise DomainError('operation_conflict', 'index changed')
        with pytest.raises(DomainError, match='index changed'):
            layout_git._transaction(runner, journal['parent'], journal['commit'], guard)
        assert cmd('rev-parse', 'HEAD').strip().decode() == journal['parent']
        assert (root / '.git/index').read_bytes() != original
        assert (root / 'file').read_bytes() == b'concurrent staged content\n'
    finally:
        os.close(rootfd)
