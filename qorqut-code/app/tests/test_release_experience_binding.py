"""Persistent source-view bindings never mutate immutable packages or delete data."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch
import uuid

import pytest

from bazarlyk.catalog.packages import load_package
from bazarlyk.release_worker import experience_binding as eb
from bazarlyk.release_worker.errors import ReleaseError
from bazarlyk.release_worker.io import digest
from bazarlyk.release_worker.manifest import plan_manifest

ROOT = Path(__file__).resolve().parents[1]


CONTRACT = {'schema_version': 1, 'channel_id': 'example-channel', 'session_format': 3, 'binding': 'experiences/sessions',
            'dataset_layout': 'plugin-experiences/example-channel/sessions'}


def make_fixture(tmp_path):
    """A channel plugin source: the generic ``basic`` template renamed, plus the experience contract sidecar (the creative-channel
    scaffold itself belongs to the content extension)."""
    source = tmp_path / 'installed/source'
    shutil.copytree(ROOT / 'templates/plugins/basic', source)
    for file in source.rglob('*'):
        if file.is_file():
            file.write_text(file.read_text().replace('example-procedure', 'example-channel'))
    (source / 'skills/example-procedure').rename(source / 'skills/example-channel')
    (source / 'experiences').mkdir()
    (source / 'experiences/contract.json').write_text(json.dumps(CONTRACT, indent=2) + '\n')
    artifact = tmp_path / 'releases/artifact'; shutil.copytree(source, artifact)
    channel = 'example-channel'
    sha = hashlib.sha256((source / 'experiences/contract.json').read_bytes()).hexdigest()
    data = tmp_path / 'persistent'
    selection = {'schema_version': 1, 'channel_id': channel, 'data_root': str(data), 'dataset_id': str(uuid.uuid4()),
                 'dataset_path': str(data / 'plugin-experiences' / channel / 'sessions'), 'source_view': str(source),
                 'contract_sha256': sha, 'receipt_path': str(source / 'experiences/binding.json')}
    component = {'id': channel, 'category': 'plugin', 'source_commit': 'a' * 40, 'experience_binding': selection}
    plan = {'release_id': 'fixture-release', 'installation': {'id': 'fixture', 'release_root': str(tmp_path / 'releases')},
            'components': [component], 'plan_digest': 'sha256:' + 'b' * 64}
    return source, artifact, component, plan


def test_install_retry_replace_uninstall_reinstall_digest_stability(tmp_path):
    source, artifact, component, plan = make_fixture(tmp_path)
    before = load_package(source, 'plugin.yaml', 'plugin', None).digest
    receipt = eb.bind_experiences(plan, component, artifact)
    dataset = Path(receipt['dataset_path']); saved = dataset / 'retained-session.txt'; saved.write_bytes(b'original bytes')
    assert eb.bind_experiences(plan, component, artifact) == receipt
    assert load_package(source, 'plugin.yaml', 'plugin', None).digest == before
    assert not any('experiences/' in n for n in load_package(source, 'plugin.yaml', 'plugin', None).files)
    eb.unbind_experiences(plan, component)
    assert saved.read_bytes() == b'original bytes'
    assert not (source / 'experiences/sessions').exists()
    with pytest.raises(ReleaseError): eb.verify_binding(plan, component)
    eb.bind_experiences(plan, component, artifact)
    assert saved.read_bytes() == b'original bytes'
    replaced = {**plan, 'release_id': 'new-release', 'plan_digest': 'sha256:' + 'c' * 64}
    eb.bind_experiences(replaced, component, artifact)
    assert eb.verify_binding(replaced, component)['plan_digest'] == replaced['plan_digest']
    assert saved.read_bytes() == b'original bytes'
    with pytest.raises(ReleaseError): eb.verify_binding(plan, component)


def test_interrupted_receipt_retry_and_mismatched_binding_keep_data(tmp_path):
    source, artifact, component, plan = make_fixture(tmp_path)
    original = eb.atomic_json
    def interrupted(path, value):
        if path == Path(component['experience_binding']['receipt_path']): raise OSError('simulated interruption')
        return original(path, value)
    with patch.object(eb, 'atomic_json', side_effect=interrupted):
        with pytest.raises(OSError): eb.bind_experiences(plan, component, artifact)
    eb.bind_experiences(plan, component, artifact)
    binding = source / 'experiences/sessions'; binding.unlink()
    wrong = tmp_path / 'wrong'; wrong.mkdir(); (wrong / 'keep').write_text('keep')
    binding.symlink_to(wrong)
    with pytest.raises(ReleaseError): eb.bind_experiences(plan, component, artifact)
    assert (wrong / 'keep').read_text() == 'keep'
    assert binding.resolve() == wrong


def test_unsafe_ownership_and_nonempty_unowned_path_fail(tmp_path):
    source, artifact, component, plan = make_fixture(tmp_path)
    for field, bad in [('data_root', str(tmp_path / 'worktrees/task/data')), ('source_view', str(tmp_path / 'releases/plugin')),
                       ('dataset_path', str(tmp_path / 'unrelated'))]:
        altered = copy.deepcopy(component); altered['experience_binding'][field] = bad
        with pytest.raises(ReleaseError): eb.validate_selection(altered['experience_binding'], altered, plan['installation'])
    parent = Path(component['experience_binding']['dataset_path']).parent; parent.mkdir(parents=True)
    (parent / 'unowned').write_text('retain')
    with pytest.raises(ReleaseError): eb.bind_experiences(plan, component, artifact)
    assert (parent / 'unowned').read_text() == 'retain'


def test_conflicting_binding_and_marker_fail_before_provisioning(tmp_path):
    source, artifact, component, plan = make_fixture(tmp_path)
    dataset = Path(component['experience_binding']['dataset_path'])
    binding = source / 'experiences/sessions'; binding.mkdir()
    (binding / 'original').write_text('retain')
    with pytest.raises(ReleaseError): eb.bind_experiences(plan, component, artifact)
    assert not dataset.parent.exists()
    assert (binding / 'original').read_text() == 'retain'
    binding.rename(source / 'experiences/original-unowned')
    dataset.parent.mkdir(parents=True)
    marker = dataset.parent / 'dataset.json'; marker.write_text('{"schema_version": 99}')
    before = marker.read_bytes()
    with pytest.raises(ReleaseError): eb.bind_experiences(plan, component, artifact)
    assert marker.read_bytes() == before
    assert not dataset.exists()


@pytest.mark.parametrize('session_format', [2, 3])
def test_plan_binds_source_sidecar_and_rejects_changed_hash(tmp_path, session_format):
    source, artifact, component, plan = make_fixture(tmp_path)
    if session_format == 3:select_format(source, artifact, component, 3)
    def git(*argv):
        return subprocess.run(['git', '-C', str(source), *argv], check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-q', '-b', 'main'); git('config', 'user.name', 'fixture'); git('config', 'user.email', 'fixture@example.invalid')
    git('add', '.'); git('commit', '-qm', 'fixture')
    manifest = {'schema_version': 1, 'release_id': 'fixture', 'installation': plan['installation'],
                'components': [{**component, 'repository': str(source), 'source_commit': git('rev-parse', 'HEAD')}]}
    frozen = plan_manifest(manifest)
    assert frozen['components'][0]['experience_binding']['contract_sha256'] == component['experience_binding']['contract_sha256']
    altered = copy.deepcopy(manifest); altered['components'][0]['experience_binding']['contract_sha256'] = '0' * 64
    with pytest.raises(ReleaseError): plan_manifest(altered)


@pytest.mark.parametrize('failure_component', ['root', 'collection', 'channel'])
def test_new_dataset_directory_entries_precede_receipt_and_retry(tmp_path, failure_component):
    import os
    import stat
    from bazarlyk.release_worker import io
    source, artifact, component, plan = make_fixture(tmp_path)
    selection = component['experience_binding']
    root = Path(selection['data_root']); dataset = Path(selection['dataset_path'])
    failing = {'root': tmp_path, 'collection': root, 'channel': root / 'plugin-experiences'}[failure_component]
    calls = []; original_fsync = os.fsync; original_json = eb.atomic_json
    def flush(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory = Path(os.readlink(f'/proc/self/fd/{fd}'))
            calls.append(directory)
            if directory == failing: raise OSError('injected ancestor fsync failure')
        return original_fsync(fd)
    with patch.object(os, 'fsync', side_effect=flush):
        with pytest.raises(OSError): eb.bind_experiences(plan, component, artifact)
    assert not Path(selection['receipt_path']).exists()
    assert not (source / 'experiences/sessions').exists()
    original_contract = (source / 'experiences/contract.json').read_bytes()
    calls.clear()
    def trace(fd):
        directory = Path(os.readlink(f'/proc/self/fd/{fd}')) if stat.S_ISDIR(os.fstat(fd).st_mode) else None
        result = original_fsync(fd)
        if directory is not None: calls.append(directory)
        return result
    def write(path, value):
        if path == Path(selection['receipt_path']):
            assert {tmp_path, root, root / 'plugin-experiences', dataset.parent, dataset}.issubset(calls)
        return original_json(path, value)
    with patch.object(os, 'fsync', side_effect=trace), patch.object(eb, 'atomic_json', side_effect=write):
        receipt = eb.bind_experiences(plan, component, artifact)
    assert receipt['outcome'] == 'verified'
    assert (source / 'experiences/contract.json').read_bytes() == original_contract
    saved = dataset / 'retained'; saved.write_text('prior data')
    assert eb.bind_experiences(plan, component, artifact) == receipt
    assert saved.read_text() == 'prior data'
    with pytest.raises(ReleaseError): io.durable_makedirs(tmp_path / 'missing-mount/dataset', anchor=tmp_path / 'missing-mount')
    assert not (tmp_path / 'missing-mount').exists()


def select_format(source, artifact, component, version):
    for root in (source, artifact):
        path = root / 'experiences/contract.json'
        value = json.loads(path.read_text()); value['session_format'] = version
        path.write_text(json.dumps(value))
    component['experience_binding']['contract_sha256'] = hashlib.sha256((source / 'experiences/contract.json').read_bytes()).hexdigest()


def test_v3_exact_contract_and_old_writer_fence_without_marker_migration(tmp_path):
    source, artifact, component, plan = make_fixture(tmp_path)
    select_format(source, artifact, component, 3)
    receipt = eb.bind_experiences(plan, component, artifact)
    marker = Path(receipt['dataset_path']).parent / 'dataset.json'
    assert json.loads(marker.read_text())['session_format'] == 3
    assert eb.verify_binding(plan, component) == receipt
    original = marker.read_bytes()
    select_format(source, artifact, component, 2)
    with pytest.raises(ReleaseError):eb.bind_experiences(plan, component, artifact)
    assert marker.read_bytes() == original
    with pytest.raises(ReleaseError):eb.verify_dataset(component['experience_binding'])


def test_v2_dataset_is_not_silently_upgraded_and_unknown_sidecars_fail(tmp_path):
    source, artifact, component, plan = make_fixture(tmp_path)
    select_format(source, artifact, component, 2)
    receipt = eb.bind_experiences(plan, component, artifact)
    marker = Path(receipt['dataset_path']).parent / 'dataset.json'; original = marker.read_bytes()
    saved = Path(receipt['dataset_path']) / 'v2-evidence';saved.write_bytes(b'old exact prefix')
    select_format(source, artifact, component, 3)
    with pytest.raises(ReleaseError):eb.bind_experiences(plan, component, artifact)
    assert marker.read_bytes() == original and saved.read_bytes() == b'old exact prefix'
    for version in (True, 4):
        select_format(source, artifact, component, version)
        with pytest.raises(ReleaseError):eb.contract_bytes(source / 'experiences/contract.json', component['id'], component['experience_binding']['contract_sha256'])
