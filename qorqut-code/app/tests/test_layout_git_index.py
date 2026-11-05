"""Owned index.lock publication with real isolated Git indexes."""
import base64
import copy
import os
from pathlib import Path
import subprocess

import pytest

from bazarlyk.agentic import layout_files, layout_git, layout_intent, upgrade
from qorqut_operations.contracts import DomainError
from test_layout_intent import ATTR, git, legacy


@pytest.fixture
def small(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    root.mkdir()
    def cmd(*args):
        return subprocess.check_output(['git', '-C', str(root), *args])
    cmd('init', '-q', '-b', 'synthetic')
    cmd('config', 'user.name', 'Synthetic')
    cmd('config', 'user.email', 'synthetic@example.invalid')
    (root / 'packages').mkdir()
    (root / 'packages/old').write_bytes(b'old authored\n')
    (root / 'unrelated').write_bytes(b'head\n')
    cmd('add', '.')
    cmd('commit', '-qm', 'parent')
    (root / 'unrelated').write_bytes(b'staged\n')
    cmd('add', 'unrelated')
    cmd('update-index', '--assume-unchanged', 'unrelated')
    (root / 'unrelated').write_bytes(b'working\n')
    original = (root / '.git/index').read_bytes()
    parent = cmd('rev-parse', 'HEAD').strip().decode()
    journal = {'phase': 'ref_published', 'parent': parent, 'timestamp': 1700000000, 'message': 'synthetic\n'}
    intent = {'index': {'content_base64': base64.b64encode(original).decode()}}
    runner = layout_git._runner(root, tmp_path)
    authored = {'plugins/new': (b'new authored\n', 0o755)}
    tree, candidate, final = layout_git._make_candidates(runner, tmp_path, intent, journal, authored)
    cmd('update-ref', 'HEAD', candidate, parent)
    gitfd = os.open(root / '.git', os.O_RDONLY | os.O_DIRECTORY)
    saved = {}
    def save(path, value):
        saved.clear()
        saved.update(copy.deepcopy(value))
    monkeypatch.setattr(layout_files, '_journal_write', save)
    def check(held, *, expected):
        state, _, fd = layout_git._index_state(gitfd, journal, original, final)
        try:
            if state != expected or (fd is None) != (held is None):
                raise DomainError('operation_conflict', 'index changed')
            owned = None
            if held is not None:
                owned = {k: journal['index_publication']['replacement'][k] for k in ('device', 'inode', 'mode', 'size', 'mtime_ns')}
                layout_git._verify_open_index(gitfd, 'index.lock', held, final, owned)
            layout_git._check_operation_locks(root, 'refs/heads/synthetic', owned_index=owned)
            if cmd('rev-parse', 'HEAD').strip().decode() != candidate:
                raise DomainError('operation_conflict', 'ref changed')
        finally:
            if fd is not None:
                os.close(fd)
    yield root, cmd, gitfd, journal, saved, original, final, check, candidate
    os.close(gitfd)


@pytest.mark.parametrize('boundary', ['before_link', 'after_link', 'after_rename'])
def test_index_boundaries_recover(small, monkeypatch, boundary, tmp_path):
    root, cmd, fd, journal, saved, original, final, check, candidate = small
    if boundary == 'before_link':
        def fail(*args):
            raise OSError('before link')
        monkeypatch.setattr(layout_git, '_link_index', fail)
    elif boundary == 'after_link':
        def fail(*args):
            raise OSError('after link')
        monkeypatch.setattr(layout_git, '_replace_index', fail)
    else:
        original_write = layout_files._journal_write
        def fail(path, value):
            if value['phase'] == 'git_published':
                raise OSError('after rename')
            original_write(path, value)
        monkeypatch.setattr(layout_files, '_journal_write', fail)
    with pytest.raises(OSError):
        layout_git._index_effect(fd, tmp_path / 'unused.json', journal, original, final, check)
    journal.clear()
    journal.update(copy.deepcopy(saved))
    # Restore only fault injection; retain the in-memory journal writer.
    monkeypatch.undo()
    def save(path, value):
        saved.clear()
        saved.update(copy.deepcopy(value))
    monkeypatch.setattr(layout_files, '_journal_write', save)
    if boundary == 'before_link':
        assert (root / '.git/index').read_bytes() == original
        assert not (root / '.git/index.lock').exists()
    elif boundary == 'after_link':
        assert (root / '.git/index').read_bytes() == original
        assert (root / '.git/index.lock').read_bytes() == final
    else:
        assert (root / '.git/index').read_bytes() == final
        assert not (root / '.git/index.lock').exists()
    assert layout_git._index_effect(fd, tmp_path / 'unused.json', journal, original, final, check)
    assert journal['phase'] == 'git_published'
    assert not layout_git._index_effect(fd, tmp_path / 'unused.json', journal, original, final, check)
    assert (root / '.git/index').read_bytes() == final
    before = layout_git._index_entries(original)
    after = layout_git._index_entries(final)
    assert before['unrelated'] == after['unrelated'] and after['unrelated'][2]
    assert after['plugins/new'][0] == 0o100755
    assert cmd('rev-parse', 'HEAD').strip().decode() == candidate
    assert (root / 'unrelated').read_bytes() == b'working\n'


@pytest.mark.parametrize('damage', ['foreign_lock', 'replaced_lock', 'edited_lock', 'changed_index', 'stale_ref'])
def test_divergent_index_state_refuses(small, monkeypatch, damage, tmp_path):
    root, cmd, fd, journal, saved, original, final, check, candidate = small
    def stop(*args):
        raise OSError('stop after lock')
    monkeypatch.setattr(layout_git, '_replace_index', stop)
    with pytest.raises(OSError):
        layout_git._index_effect(fd, tmp_path / 'unused.json', journal, original, final, check)
    monkeypatch.undo()
    def save(path, value):
        saved.clear()
        saved.update(copy.deepcopy(value))
    monkeypatch.setattr(layout_files, '_journal_write', save)
    journal.clear()
    journal.update(copy.deepcopy(saved))
    lock = root / '.git/index.lock'
    if damage in ('foreign_lock', 'replaced_lock'):
        replacement = root / '.git/foreign'
        replacement.write_bytes(final)
        replacement.chmod(lock.stat().st_mode & 0o777)
        os.replace(replacement, lock)
    elif damage == 'edited_lock':
        lock.write_bytes(b'changed lock')
    elif damage == 'changed_index':
        # Emulate a non-cooperating writer, bypassing the held Git lock.
        (root / '.git/index').write_bytes(b'different index')
    else:
        cmd('update-ref', 'HEAD', journal['parent'], candidate)
    before_index, before_lock = (root / '.git/index').read_bytes(), lock.read_bytes()
    with pytest.raises((DomainError, OSError)):
        layout_git._index_effect(fd, tmp_path / 'unused.json', journal, original, final, check)
    assert (root / '.git/index').read_bytes() == before_index
    assert lock.read_bytes() == before_lock


def test_full_internal_publish_and_retry(legacy):
    paths, export = legacy
    root = paths.agentic_root
    old = 'packages/agentic-management/bootstrap.md'
    (root / old).write_bytes(b'staged authored\n')
    git(paths, 'add', '--', old)
    (root / old).write_bytes(b'working authored\n')
    deleted = root / 'packages/imported-email-management-templates/native/skills/email-management/instance-template/inboxes/README.md'
    deleted.unlink()
    (root / 'packages/custom-tools/echo.py').chmod(0o755)
    unrelated = root / 'unrelated.txt'
    unrelated.write_bytes(b'staged unrelated\n')
    git(paths, 'add', '--', 'unrelated.txt')
    git(paths, 'update-index', '--assume-unchanged', 'unrelated.txt')
    unrelated.write_bytes(b'working unrelated\n')
    plan = upgrade.prepare_upgrade(paths, export)
    assert plan['complete'], plan['conflicts']
    layout_intent.begin(paths, plan, attribution=ATTR)
    layout_files.relocate(paths, plan, attribution=ATTR)
    directory = upgrade.history_directory(paths)
    intent_path = directory / ('layout-' + plan['plan_digest'] + '.json')
    relocation_path = directory / ('relocation-' + plan['plan_digest'] + '.json')
    retained = (intent_path.read_bytes(), relocation_path.read_bytes())
    old_index = (root / '.git/index').read_bytes()
    old_entries = layout_git._index_entries(old_index)
    parent = git(paths, 'rev-parse', 'HEAD').strip().decode()
    authored = layout_files._authored(root)
    binding = upgrade.read_binding_bytes(paths)
    database = (paths.agentic_runtime / 'state.sqlite3').read_bytes()
    first = layout_git.publish(paths, plan, attribution=ATTR)
    assert first['phase'] == 'git_published' and first['worked']
    assert first['parent'] == parent and first['active_index'] == 'final'
    assert not first['runtime_binding_activated']
    assert git(paths, 'rev-list', '--parents', '-n', '1', 'HEAD').split() == [first['commit'].encode(), parent.encode()]
    new_index = (root / '.git/index').read_bytes()
    journal = layout_git._read_journal(directory / ('git-layout-' + plan['plan_digest'] + '.json'))
    assert new_index == base64.b64decode(journal['final_index']['content_base64'])
    new_entries = layout_git._index_entries(new_index)
    assert new_entries['unrelated.txt'] == old_entries['unrelated.txt'] and new_entries['unrelated.txt'][2]
    tree = {}
    for entry in git(paths, 'ls-tree', '-r', '-z', 'HEAD').split(b'\0'):
        if entry:
            header, path = entry.split(b'\t', 1)
            mode, kind, oid = header.split(b' ')
            name = path.decode()
            if layout_git._authored_path(name):
                tree[name] = (int(mode, 8), oid.decode(), False)
    assert {p: v for p, v in new_entries.items() if layout_git._authored_path(p)} == tree
    assert git(paths, 'show', 'HEAD:' + plan['path_map'][old][0]) == b'working authored\n'
    assert new_entries[plan['path_map']['packages/custom-tools/echo.py'][0]][0] == 0o100755
    assert unrelated.read_bytes() == b'working unrelated\n' and not deleted.exists()
    assert layout_files._authored(root) == authored
    assert upgrade.read_binding_bytes(paths) == binding
    assert (paths.agentic_runtime / 'state.sqlite3').read_bytes() == database
    assert (intent_path.read_bytes(), relocation_path.read_bytes()) == retained
    first_inode = (root / '.git/index').stat().st_ino
    second = layout_git.publish(paths, plan, attribution=ATTR)
    assert not second['worked'] and second['commit'] == first['commit']
    assert (root / '.git/index').read_bytes() == new_index
    assert (root / '.git/index').stat().st_ino == first_inode
    assert git(paths, 'rev-parse', 'HEAD').strip().decode() == first['commit']


def test_identical_index_bytes_still_require_owned_inode(small, tmp_path):
    root, cmd, fd, journal, saved, original, _final, _check, candidate = small
    old_inode = (root / '.git/index').stat().st_ino
    def check(held, *, expected):
        state, _, other = layout_git._index_state(fd, journal, original, original)
        try:
            assert state == expected and (held is None) == (other is None)
        finally:
            if other is not None:
                os.close(other)
    assert layout_git._index_effect(fd, tmp_path / 'unused', journal, original, original, check)
    assert (root / '.git/index').read_bytes() == original
    assert (root / '.git/index').stat().st_ino != old_inode
    assert not layout_git._index_effect(fd, tmp_path / 'unused', journal, original, original, check)


@pytest.mark.parametrize('damage', ['foreign_lock', 'foreign_index'])
def test_same_content_foreign_inode_refuses(small, monkeypatch, tmp_path, damage):
    root, cmd, fd, journal, saved, original, final, check, candidate = small
    def stop(*args):
        raise OSError('stop before link')
    monkeypatch.setattr(layout_git, '_link_index', stop)
    with pytest.raises(OSError):
        layout_git._index_effect(fd, tmp_path / 'unused', journal, original, final, check)
    monkeypatch.undo()
    journal.clear()
    journal.update(copy.deepcopy(saved))
    gitdir = root / '.git'
    if damage == 'foreign_lock':
        path = gitdir / 'index.lock'
        path.write_bytes(final)
        path.chmod(journal['index_publication']['replacement']['mode'])
    else:
        path = gitdir / 'index'
        replacement = gitdir / 'other-index'
        replacement.write_bytes(original)
        replacement.chmod(journal['index_publication']['original']['mode'])
        os.replace(replacement, path)
    before = path.read_bytes(), path.stat().st_ino
    with pytest.raises((DomainError, OSError)):
        layout_git._index_effect(fd, tmp_path / 'unused', journal, original, final, check)
    assert (path.read_bytes(), path.stat().st_ino) == before


def test_index_publication_receipt_requires_exact_phase_shape(small, tmp_path):
    root, cmd, fd, journal, saved, original, final, check, candidate = small
    base = {'schema_version': 1, 'phase': 'index_publishing', 'plan_digest': 'a' * 64,
            'intent_sha256': 'i', 'relocation_sha256': 'r', 'attribution': {}, 'root': {}, 'gitdir': {},
            'head_identity': '', 'parent': journal['parent'], 'original_index_sha256': 'o',
            'identity': layout_git._IDENTITY, 'timestamp': 1,
            'message': 'Agentic layout migration\n\nAgentic-layout-plan: ' + 'a' * 64 + '\n',
            'tree': 'b' * 40, 'commit': candidate,
            'final_index': {'sha256': layout_git.digest(final), 'content_base64': base64.b64encode(final).decode()},
            'index_publication': {'original': {'device': 1, 'inode': 2, 'mode': 0o644, 'size': len(original),
                                               'mtime_ns': 1, 'ctime_ns': 1},
                                  'replacement': {'device': 1, 'inode': 3, 'mode': 0o644, 'size': len(final), 'mtime_ns': 1,
                                                  'sha256': layout_git.digest(final)}}}
    path = tmp_path / 'receipt.json'
    # Use the real private journal writer, not the fixture's in-memory recorder.
    from bazarlyk.agentic.upgrade import _write_private_temp
    from bazarlyk.agentic.support import canonical, digest
    def write(value):
        body = {k: v for k, v in value.items() if k != 'receipt_sha256'}
        record = {**body, 'receipt_sha256': digest(body)}
        temp = _write_private_temp(tmp_path, canonical(record) + b'\n')
        os.replace(temp, path)
    write(base)
    assert layout_git._read_journal(path)['phase'] == 'index_publishing'
    missing = copy.deepcopy(base)
    del missing['index_publication']
    write(missing)
    with pytest.raises(DomainError):
        layout_git._read_journal(path)
    invalid = copy.deepcopy(base)
    invalid['index_publication']['replacement']['mode'] = 0o600
    write(invalid)
    with pytest.raises(DomainError):
        layout_git._read_journal(path)
