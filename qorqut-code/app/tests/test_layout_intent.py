"""Stage-1 evidence against the archived-source synthetic installation.

Set T09_SYNTHETIC_FIXTURE to the scratch directory prepared from the synthetic
archived-source installation; no real instance contents are used.
"""
import base64
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from bazarlyk.agentic import layout_intent, upgrade
from bazarlyk.agentic.support import canonical
from instance_support import synthetic_paths
from qorqut_operations.contracts import DomainError
from qorqut_agentic.errors import AgenticError

ATTR = {'actor_id': 'synthetic-admin', 'actor_kind': 'human', 'reason': 'Offline layout maintenance'}


@pytest.fixture
def legacy(tmp_path):
    base = os.environ.get('T09_SYNTHETIC_FIXTURE')
    if not base:
        pytest.skip('archived-source synthetic fixture not configured')
    instance = tmp_path / 'instance'
    shutil.copytree(Path(base) / 'legacy-template', instance, symlinks=True)
    paths = synthetic_paths(instance)
    binding_path = instance / 'config' / 'agentic-layer.json'
    binding = json.loads(binding_path.read_bytes())
    binding['root'], binding['state'] = str(paths.agentic_root), str(paths.agentic_runtime)
    binding_path.write_bytes(canonical(binding) + b'\n')
    with sqlite3.connect(paths.agentic_runtime / 'state.sqlite3') as db:
        db.execute("UPDATE meta SET value=? WHERE key='root_path'", (str(paths.agentic_root),))
    export = Path(base) / 'export'
    return paths, export


def git(paths, *args):
    return subprocess.check_output(['git', '-C', str(paths.agentic_root), *args])


def snapshot(instance):
    return {str(p.relative_to(instance)): p.read_bytes() for p in instance.rglob('*')
            if p.is_file() and 'agentic-layer-history' not in p.parts}


def test_prepared_intent_preserves_mixed_index_and_reuses(legacy):
    paths, export = legacy
    target = 'packages/agentic-management/bootstrap.md'
    path = paths.agentic_root / target
    original_head = git(paths, 'show', 'HEAD:' + target)
    assert original_head not in (b'staged synthetic\n', b'working synthetic\n')
    path.write_bytes(b'staged synthetic\n')
    git(paths, 'add', '--', target)
    path.write_bytes(b'working synthetic\n')
    unrelated = paths.agentic_root / 'unrelated.txt'
    unrelated.write_bytes(b'unrelated staged\n')
    git(paths, 'add', '--', 'unrelated.txt')
    unrelated.write_bytes(b'unrelated working\n')
    plan = upgrade.prepare_upgrade(paths, export)
    assert plan['complete'], plan['conflicts']
    before = snapshot(paths.instance_root)
    before_index = (paths.agentic_root / '.git' / 'index').read_bytes()
    before_head = git(paths, 'rev-parse', 'HEAD')
    assert layout_intent.begin(paths, plan, attribution=ATTR)['created']
    intent, = layout_intent.incomplete_intents(paths)
    assert base64.b64decode(intent['index']['content_base64']) == before_index
    assert intent['head'] == before_head.decode().strip()
    assert base64.b64decode(intent['authored'][target]['content_base64']) == b'working synthetic\n'
    staged = next(x for x in intent['staged'] if x['path'] == target)
    assert base64.b64decode(staged['content_base64']) == b'staged synthetic\n'
    assert snapshot(paths.instance_root) == before
    assert not layout_intent.begin(paths, plan, attribution=ATTR)['created']
    assert snapshot(paths.instance_root) == before


@pytest.mark.parametrize('damage', ['runtime', 'content', 'index', 'binding', 'intent', 'unsafe', 'prior', 'lock'])
def test_refuses_changed_evidence(legacy, damage):
    paths, export = legacy
    plan = upgrade.prepare_upgrade(paths, export)
    assert plan['complete'], plan['conflicts']
    if damage in ('intent', 'unsafe', 'prior'):
        layout_intent.begin(paths, plan, attribution=ATTR)
        path = layout_intent.history_directory(paths) / ('layout-' + plan['plan_digest'] + '.json')
        if damage == 'intent':
            path.write_bytes(b'{}\n')
        elif damage == 'unsafe':
            path.unlink()
            path.symlink_to(paths.instance_root / 'config' / 'agentic-layer.json')
        else:
            upgrade.history_snapshot(paths, plan['binding']['record_sha256']).write_bytes(b'changed')
    elif damage == 'runtime':
        with sqlite3.connect(paths.agentic_runtime / 'state.sqlite3') as db:
            db.execute("UPDATE consumers SET cursor=cursor+1 WHERE name='synthetic-consumer'")
    elif damage == 'content':
        (paths.agentic_root / 'packages/agentic-management/bootstrap.md').write_bytes(b'changed')
    elif damage == 'index':
        (paths.agentic_root / 'unrelated.txt').write_bytes(b'staged')
        git(paths, 'add', '--', 'unrelated.txt')
    elif damage == 'lock':
        (paths.agentic_runtime / 'authoring.lock').unlink()
    else:
        binding = paths.instance_root / 'config/agentic-layer.json'
        binding.write_bytes(binding.read_bytes() + b' ')
    before = snapshot(paths.instance_root)
    with pytest.raises((DomainError, AgenticError, OSError)):
        layout_intent.begin(paths, plan, attribution=ATTR)
    assert snapshot(paths.instance_root) == before


def test_interrupted_intent_publication_does_not_accept_partial(legacy, monkeypatch):
    paths, export = legacy
    plan = upgrade.prepare_upgrade(paths, export)
    before = snapshot(paths.instance_root)
    def fail(*args, **kwargs):
        raise OSError('synthetic interruption')
    monkeypatch.setattr(layout_intent, '_publish', fail)
    with pytest.raises(OSError):
        layout_intent.begin(paths, plan, attribution=ATTR)
    assert layout_intent.incomplete_intents(paths) == []
    assert snapshot(paths.instance_root) == before
    monkeypatch.undo()
    assert layout_intent.begin(paths, plan, attribution=ATTR)['created']


def test_preexisting_area_edit_requires_new_plan(legacy):
    paths, export = legacy
    package = paths.agentic_root / 'plugins' / 'local-extra'
    package.mkdir(parents=True)
    (package / 'agentic.yaml').write_text(
        'schema_version: 1\nid: local-extra\nname: Extra\ndescription: Synthetic\nentries: []\n')
    note = package / 'note.md'
    note.write_bytes(b'original synthetic\n')
    old = upgrade.prepare_upgrade(paths, export)
    assert old['complete'], old['conflicts']
    status = git(paths, 'status', '--porcelain', '--untracked-files=all', '--', 'plugins')
    note.write_bytes(b'changed synthetic\n')
    assert git(paths, 'status', '--porcelain', '--untracked-files=all', '--', 'plugins') == status
    before = snapshot(paths.instance_root)
    with pytest.raises(DomainError):
        layout_intent.begin(paths, old, attribution=ATTR)
    assert snapshot(paths.instance_root) == before
    new = upgrade.prepare_upgrade(paths, export)
    assert new['complete'], new['conflicts']
    layout_intent.begin(paths, new, attribution=ATTR)
    intent, = layout_intent.incomplete_intents(paths)
    item = intent['authored']['plugins/local-extra/note.md']
    assert base64.b64decode(item['content_base64']) == b'changed synthetic\n'
    assert item['mode'] == new['existing_authored_files']['plugins/local-extra/note.md']['mode']
    assert snapshot(paths.instance_root) == before


def test_another_prepared_intent_refuses(legacy):
    paths, export = legacy
    path = paths.agentic_root / 'packages/agentic-management/bootstrap.md'
    original = path.read_bytes()
    first = upgrade.prepare_upgrade(paths, export)
    layout_intent.begin(paths, first, attribution=ATTR)
    path.write_bytes(original + b'\nchanged synthetic\n')
    second = upgrade.prepare_upgrade(paths, export)
    assert second['complete'] and second['plan_digest'] != first['plan_digest']
    before = snapshot(paths.instance_root)
    with pytest.raises(DomainError):
        layout_intent.begin(paths, second, attribution=ATTR)
    assert snapshot(paths.instance_root) == before
    assert len(layout_intent.incomplete_intents(paths)) == 1


def test_legacy_mode_change_requires_new_plan(legacy):
    paths, export = legacy
    target = paths.agentic_root / 'packages' / 'custom-tools' / 'echo.py'
    target.chmod(0o644)
    plan = upgrade.prepare_upgrade(paths, export)
    assert plan['complete'], plan['conflicts']
    assert plan['legacy_authored_files']['packages/custom-tools/echo.py']['mode'] == 0o644
    target.chmod(0o755)
    before = snapshot(paths.instance_root)
    with pytest.raises(DomainError):
        layout_intent.begin(paths, plan, attribution=ATTR)
    assert snapshot(paths.instance_root) == before
    fresh = upgrade.prepare_upgrade(paths, export)
    assert fresh['legacy_authored_files']['packages/custom-tools/echo.py']['mode'] == 0o755
