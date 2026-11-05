"""NFS binding evidence is simulated; these tests do not verify real independence."""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
import uuid

import pytest

from bazarlyk.release_worker import session_backup as sb
from bazarlyk.release_worker.errors import ReleaseError


def fixture(tmp_path):
    mount = tmp_path / 'simulated-mount'; data = mount / 'dataset'; data.mkdir(parents=True)
    destination, dataset = str(uuid.uuid4()), str(uuid.uuid4())
    config = {'destination_id': destination, 'mountpoint': str(mount), 'dataset_root': str(data), 'dataset_id': dataset,
              'server': 'backup.invalid', 'export': '/simulated', 'nfs_version': '4.2',
              'verification_path': str(tmp_path / 'verification.json'), 'receipt': str(tmp_path / 'receipt.json')}
    (data / 'dataset.json').write_text(json.dumps({'schema_version': 1, 'destination_id': destination, 'dataset_id': dataset}))
    evidence = tmp_path / 'restore.json'; evidence.write_text(json.dumps({'outcome': 'verified', 'destination_id': destination, 'host_loss_exercised': True, 'note': 'SIMULATED'}))
    verification = {k: config[k] for k in ('destination_id', 'mountpoint', 'dataset_root', 'dataset_id', 'server', 'export', 'nfs_version')}
    verification.update(schema_version=1, verified_by='simulated-test', verified_at='2026-09-29T00:00:00Z', different_host=True,
                        survives_gallery_host_loss=True, restore_evidence={'path': str(evidence), 'sha256': hashlib.sha256(evidence.read_bytes()).hexdigest()})
    Path(config['verification_path']).write_text(json.dumps(verification))
    config['verification_sha256'] = hashlib.sha256(Path(config['verification_path']).read_bytes()).hexdigest()
    mountinfo = [{'mount_id': '42', 'root': '/', 'mountpoint': str(mount), 'filesystem': 'nfs',
                  'source': 'backup.invalid:/simulated', 'options': ['rw', 'vers=4.2']}]
    return config, mountinfo


def test_binding_requires_mount_identity_attestation_restore_and_exact_plan(tmp_path):
    config, mountinfo = fixture(tmp_path)
    sb.validate_backup_selection(config)
    plan = {'plan_digest': 'sha256:' + 'a' * 64, 'installation': {'session_backup': config}}
    with patch.object(sb, 'mount_observations', return_value=mountinfo), patch.object(sb, 'local_server', return_value=False):
        receipt = sb.bind_backup(plan)
        bound = {**config, 'plan_digest': plan['plan_digest']}
        assert sb.verify_backup_binding(bound)['binding_verification'] == config['verification_sha256']
        assert sb.bind_backup(plan) == receipt
        for key, value in [('plan_digest', 'other'), ('verification_sha256', '0' * 64), ('dataset_id', 'wrong'), ('nfs_version', '3')]:
            with pytest.raises(ReleaseError): sb.verify_backup_binding({**bound, key: value})
        Path(config['verification_path']).unlink()
        with pytest.raises(ReleaseError): sb.verify_backup_binding(bound)
    with patch.object(sb, 'mount_observations', return_value=[]), patch.object(sb, 'local_server', return_value=False):
        with pytest.raises(ReleaseError): sb.bind_backup(plan)
    with patch.object(sb, 'mount_observations', return_value=mountinfo), patch.object(sb, 'local_server', return_value=True):
        with pytest.raises(ReleaseError): sb.bind_backup(plan)


def test_marker_and_boolean_alone_do_not_protect_local_directory(tmp_path):
    config, _ = fixture(tmp_path)
    with patch.object(sb, 'local_server', return_value=False):
        with pytest.raises(ReleaseError): sb.verify_backup_binding({**config, 'plan_digest': 'fixture', 'independent': True})


def test_local_interface_alias_and_nested_generation_mount_are_refused(tmp_path):
    lookup = lambda host, _port: [(2, 1, 6, '', ('192.0.2.10' if host == 'backup.invalid' else '127.0.0.1', 0))]
    with patch.object(sb, 'local_addresses', return_value={'192.0.2.10'}), \
            patch.object(sb.socket, 'getaddrinfo', side_effect=lookup):
        assert sb.local_server('backup.invalid')
    config, observations = fixture(tmp_path)
    nested = {**observations[0], 'mount_id': 'local-substitution', 'filesystem': 'ext4',
              'mountpoint': config['dataset_root'] + '/generations', 'source': '/dev/local'}
    with patch.object(sb, 'mount_observations', return_value=[*observations, nested]), \
            patch.object(sb, 'local_server', return_value=False):
        with pytest.raises(ReleaseError): sb.bind_backup({'plan_digest': 'fixture', 'installation': {'session_backup': config}})
