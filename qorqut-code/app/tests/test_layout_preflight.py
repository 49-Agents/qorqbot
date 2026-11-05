"""Early inspection refuses unsupported synthetic Git state without effects."""
import os
from pathlib import Path
import subprocess

import pytest

from bazarlyk.agentic import layout_inspection, layout_intent, upgrade
from qorqut_operations.contracts import DomainError
from test_layout_intent import ATTR, legacy, snapshot


def command(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    command(root, 'init', '-q', '-b', 'synthetic')
    command(root, 'config', 'user.name', 'Synthetic')
    command(root, 'config', 'user.email', 'synthetic@example.invalid')
    (root / 'packages').mkdir()
    (root / 'packages/item').write_text('initial\n')
    command(root, 'add', '.')
    command(root, 'commit', '-qm', 'initial')
    return root


def evidence(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*')
            if p.is_file() and not p.is_symlink()}


@pytest.mark.parametrize('detached', [False, True])
def test_supported(repo, detached):
    if detached:
        command(repo, 'checkout', '-q', '--detach')
    (repo / 'other').write_text('staged\n')
    command(repo, 'add', 'other')
    command(repo, 'update-index', '--assume-unchanged', 'other')
    before = evidence(repo)
    result = layout_inspection.inspect_git(repo)
    assert not result['conflicts']
    assert result['head'] == command(repo, 'rev-parse', 'HEAD').decode().strip()
    assert evidence(repo) == before


def test_non_ascii_branch(repo):
    command(repo, 'checkout', '-qb', 'synthetic-é')
    before = evidence(repo)
    result = layout_inspection.inspect_git(repo)
    assert not result['conflicts']
    assert result['head'] == command(repo, 'rev-parse', 'HEAD').decode().strip()
    assert evidence(repo) == before


@pytest.mark.parametrize('included', [False, True])
def test_filter_refuses_before_status(repo, included):
    marker = repo / 'marker'
    (repo / '.gitattributes').write_text('packages/item filter=synthetic\n')
    (repo / 'packages/item').write_text('dirty\n')
    value = 'touch ' + str(marker)
    if included:
        (repo / '.git/extra-config').write_text('[filter "synthetic"]\n process = ' + value + '\n')
        command(repo, 'config', '--local', 'include.path', 'extra-config')
    else:
        command(repo, 'config', '--local', 'filter.synthetic.clean', value)
    before = evidence(repo)
    assert layout_inspection.inspect_git(repo)['conflicts']
    assert not marker.exists()
    assert evidence(repo) == before


@pytest.mark.parametrize('damage', ['v4', 'skip', 'intent', 'split', 'unmerged', 'lock', 'fifo', 'symlink', 'nested'])
def test_unsupported(repo, damage):
    index = repo / '.git/index'
    if damage == 'v4':
        command(repo, 'update-index', '--index-version=4')
    elif damage == 'skip':
        command(repo, 'update-index', '--skip-worktree', 'packages/item')
    elif damage == 'intent':
        (repo / 'new').write_text('new')
        command(repo, 'add', '-N', 'new')
    elif damage == 'split':
        command(repo, 'update-index', '--split-index')
    elif damage == 'unmerged':
        oid = command(repo, 'rev-parse', 'HEAD:packages/item').strip().decode()
        subprocess.run(['git', '-C', str(repo), 'update-index', '--index-info'],
                       input=f'0 {"0" * 40}\tpackages/item\n100644 {oid} 1\tpackages/item\n'.encode(), check=True)
    elif damage == 'lock':
        (repo / '.git/refs/heads/unknown.lock').write_text('lock')
    elif damage in ('fifo', 'symlink'):
        index.unlink()
        if damage == 'fifo':
            os.mkfifo(index)
        else:
            index.symlink_to('HEAD')
    else:
        (repo / '.git/refs/heads/synthetic').write_text('ref: refs/heads/other\n')
        (repo / '.git/refs/heads/other').write_text('0' * 40 + '\n')
    before = evidence(repo)
    assert layout_inspection.inspect_git(repo)['conflicts']
    assert evidence(repo) == before


@pytest.mark.parametrize('damage', ['filter', 'index'])
def test_begin_refuses_new_unsupported_state(legacy, damage):
    paths, export = legacy
    plan = upgrade.prepare_upgrade(paths, export)
    assert plan['complete'], plan['conflicts']
    assert not layout_intent.history_directory(paths).exists()
    unchanged = snapshot(paths.instance_root)
    assert upgrade.prepare_upgrade(paths, export) == plan
    assert snapshot(paths.instance_root) == unchanged
    assert not layout_intent.history_directory(paths).exists()
    if damage == 'filter':
        command(paths.agentic_root, 'config', '--local', 'filter.synthetic.clean', 'touch ' + str(paths.instance_root / 'marker'))
        (paths.agentic_root / '.gitattributes').write_text('packages/** filter=synthetic\n')
        target = paths.agentic_root / 'packages/agentic-management/bootstrap.md'
        target.write_bytes(target.read_bytes() + b'\ndirty\n')
    else:
        command(paths.agentic_root, 'update-index', '--index-version=4')
    before = snapshot(paths.instance_root)
    with pytest.raises(DomainError):
        layout_intent.begin(paths, plan, attribution=ATTR)
    assert snapshot(paths.instance_root) == before
    assert not layout_intent.history_directory(paths).exists()
    assert not (paths.instance_root / 'marker').exists()
