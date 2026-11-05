import hashlib
import json
from pathlib import Path

import pytest

from bazarlyk.release_worker import adoption_runtime, installation
from bazarlyk.release_worker.errors import ReleaseError


@pytest.mark.parametrize('drift', [None, 'config', 'manifest', 'current', 'python', 'core', 'sources', 'workspace'])
def test_persistent_adoption_requires_frozen_config_and_actual_running_source(tmp_path, monkeypatch, drift):
    root = tmp_path / 'installation'; root.mkdir()
    release = root / 'releases' / 'app'; release.mkdir(parents=True)
    (root / 'current').symlink_to(release)
    config = {'root': str(root), 'service': 'fixture.service', 'businesses_root': str(tmp_path / 'businesses'),
              'package_sources': str(root / 'sources.json')}
    config_path = root / 'installation.json'; config_path.write_text(json.dumps(config))
    manifest = {'python': str(root / 'python'), 'components': {'core': {'path': str(root / 'core')}}}
    manifest_path = release / 'manifest.json'; manifest_path.write_text(json.dumps(manifest))
    selection = {'config': str(config_path), 'config_sha256': hashlib.sha256(config_path.read_bytes()).hexdigest(),
                 'manifest_sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest()}
    argv = [manifest['python'], '-c', 'loader', manifest['components']['core']['path'], '--workspace', str(root)]
    env = 'QORQUT_PACKAGE_SOURCES=' + config['package_sources']
    if drift in {'config', 'manifest'}:
        path = config_path if drift == 'config' else manifest_path
        path.write_text(path.read_text() + '\n')
    elif drift == 'current':
        (root / 'other').mkdir(); (root / 'current').unlink(); (root / 'current').symlink_to(root / 'other')
    elif drift == 'python': argv[0] = '/other/python'
    elif drift == 'core': argv[3] = '/other/core'
    elif drift == 'sources': env = 'QORQUT_PACKAGE_SOURCES=/other/sources'
    elif drift == 'workspace': argv[-1] = '/other/workspace'
    read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, 'read_bytes', lambda p: ('\0'.join(argv)).encode() if str(p) == '/proc/424242/cmdline'
                        else env.encode() if str(p) == '/proc/424242/environ' else read_bytes(p))
    monkeypatch.setattr(adoption_runtime.subprocess, 'check_output', lambda *a, **k: b'424242')
    monkeypatch.setattr(installation, 'validate_config', lambda c: c)
    monkeypatch.setenv('QORQUT_INSTALLATION_CONFIG', '')
    monkeypatch.setenv('QORQUT_PACKAGE_SOURCES', '')
    plan = {'installation': {'service': 'fixture.service'}}
    recipe = {'businesses_root': config['businesses_root'], 'persistent_installation': selection}
    if drift:
        with pytest.raises(ReleaseError): adoption_runtime.resolve(plan, recipe, release)
    else:
        assert adoption_runtime.resolve(plan, recipe, release) == (root, manifest['python'])
