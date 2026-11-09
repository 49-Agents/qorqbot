"""Resolve the exact running runtime for a frozen package adoption."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

from .errors import ReleaseError


def resolve(plan, recipe, runtime_path):
    runtime_path = Path(runtime_path).resolve(strict=True)
    pid = int(subprocess.check_output([
        'systemctl', 'show', plan['installation']['service'], '-p', 'MainPID', '--value']))
    argv = Path(f'/proc/{pid}/cmdline').read_bytes().decode().split('\0')
    selection = recipe.get('persistent_installation')
    if selection is None:
        intended = Path(plan['installation']['release_root']) / plan['release_id']
        if runtime_path != intended.resolve():
            raise ReleaseError('runtime is not this release')
        workspace = runtime_path
        sources = workspace / 'package-sources.json'
        interpreter = workspace / 'biz-core/.venv/bin/python'
    else:
        if set(selection) != {'config', 'config_sha256', 'manifest_sha256'}:
            raise ReleaseError('Invalid persistent adoption selection')
        config_path = Path(selection['config'])
        if not config_path.is_absolute():
            raise ReleaseError('Persistent adoption config must be absolute')
        raw = config_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != selection['config_sha256']:
            raise ReleaseError('Persistent adoption configuration changed')
        from .installation import validate_config
        config = validate_config(json.loads(raw))
        workspace = Path(config['root'])
        manifest_raw = (runtime_path / 'manifest.json').read_bytes()
        if (workspace.joinpath('current').resolve(strict=True) != runtime_path
                or hashlib.sha256(manifest_raw).hexdigest() != selection['manifest_sha256']
                or config['service'] != plan['installation']['service']
                or config['businesses_root'] != recipe['businesses_root']):
            raise ReleaseError('Persistent adoption runtime changed')
        manifest = json.loads(manifest_raw)
        interpreter = Path(manifest['python'])
        sources = Path(config['package_sources'])
        live_env = dict(v.split('=', 1) for v in
                        Path(f'/proc/{pid}/environ').read_bytes().decode().split('\0') if '=' in v)
        if (argv[0] != str(interpreter)
                or manifest['components']['core']['path'] not in argv
                or live_env.get('QORQUT_PACKAGE_SOURCES') != str(sources)):
            raise ReleaseError('Persistent adoption service identity differs')
        os.environ['QORQUT_INSTALLATION_CONFIG'] = str(config_path)
    if '--workspace' not in argv or Path(argv[argv.index('--workspace') + 1]).resolve() != workspace:
        raise ReleaseError('selected runtime is not the running service')
    os.environ['QORQUT_PACKAGE_SOURCES'] = str(sources)
    return workspace, str(interpreter)
