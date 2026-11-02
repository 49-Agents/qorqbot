#!/usr/bin/env python3
"""Qualify a private native archive in disposable, offline Ubuntu 24.04.

Build/test tooling only. Docker is needed here, never in the customer download.
All records are synthetic; no account, service or existing instance is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

IMAGE = 'ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac'
INSTALL = '/install/qorqbot'


def inside(phase):
    import sqlite3
    import urllib.error
    import urllib.request
    root, base = Path(INSTALL), 'http://127.0.0.1:1049'
    if phase == 'tools':
        import importlib.util
        import shutil
        spec = importlib.util.spec_from_file_location('launcher', root / 'runtime/launch.py')
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        env = launcher.environment(root, root / 'instance/home')
        for name, flag in (('git', '--version'), ('tmux', '-V'), ('node', '--version'),
                           ('codex', '--version'), ('claude', '--version')):
            path = shutil.which(name, path=env['PATH'])
            assert path and Path(path).is_relative_to(root), name
            subprocess.run([path, flag], env=env, cwd=root, check=True, timeout=60)
        subprocess.run([root / 'runtime/native/node/bin/node',
                        root / 'runtime/native/browser/node_modules/agent-browser/bin/agent-browser.js', '--version'],
                       env=env, cwd=root, check=True, timeout=60)
        return
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data,
            headers={'Origin': base, 'Content-Type': 'application/json'})
        try:
            response = opener.open(req, timeout=60)
        except urllib.error.HTTPError as error:
            raise RuntimeError(error.read().decode()) from error
        with response:
            assert response.status == 200
            value = response.read()
            return json.loads(value) if 'application/json' in response.headers.get('Content-Type', '') else value
    assert request('/health/live')['live']
    assert request('/health/ready')['data']['ready']
    if phase == 'ready':
        return
    assert b'<html' in request('/').lower()
    session = request('/api/session')['data']
    assert session['is_installation_owner'] and session['operational_timezone'] == 'UTC'
    assert request('/api/root/extensions')['data'] == {'extensions': []}
    state = Path('/install/acceptance-state.json')
    db = root / 'instance/operations/.runtime/operations.sqlite3'
    marker = root / 'instance/operations/acceptance.txt'
    if phase == 'write':
        result = request('/api/root/operations/tasks_create', {'scope': 'global',
            'title': 'Synthetic native package acceptance', 'idempotency_key': 'native-acceptance-task-1'})
        assert result['status'] == 'ok', result
        with sqlite3.connect(db) as connection:
            task_id = connection.execute('SELECT id FROM tasks WHERE title=?',
                ('Synthetic native package acceptance',)).fetchone()[0]
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not {'business_sandboxes', 'business_previews'} & tables
        assert not any(name.startswith('telegram_') for name in tables)
        marker.write_text('Synthetic acceptance record: preserve across restart and update.\n')
        state.write_text(json.dumps({'task_id': task_id, 'database_inode': db.stat().st_ino,
            'marker_sha256': hashlib.sha256(marker.read_bytes()).hexdigest(), 'owner': session['principal_id']}))
    else:
        saved = json.loads(state.read_text())
        assert session['principal_id'] == saved['owner']
        result = request('/api/root/operations/tasks_get', {'scope': 'global', 'task_id': saved['task_id']})
        assert result['status'] == 'ok', result
        assert db.stat().st_ino == saved['database_inode']
        assert hashlib.sha256(marker.read_bytes()).hexdigest() == saved['marker_sha256']
    print(json.dumps({'phase': phase, 'dashboard': 'ok', 'readiness': 'ok', 'owner': 'ok', 'record': 'ok'}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--inside', choices=('ready', 'tools', 'write', 'restart', 'update', 'repeat-update'))
    args = parser.parse_args()
    if args.inside:
        inside(args.inside)
        return
    if not args.archive or not args.output:
        parser.error('--archive and --output are required')
    archive, output = args.archive.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    installation = output / 'install'
    installation.mkdir()
    name = 'qorqbot-acceptance-' + uuid.uuid4().hex[:12]
    result = {'format': 1, 'archive_sha256': hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest(),
              'image': IMAGE, 'network': 'none', 'uid': os.getuid(), 'checks': [], 'passed': False}
    application = None
    def command(argv, label, *, expected=0, timeout=600):
        with (output / (label + '.log')).open('ab') as log:
            completed = subprocess.run(list(map(str, argv)), stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        if completed.returncode != expected:
            raise RuntimeError(f'{label} returned {completed.returncode}; inspect its log')
        result['checks'].append(label)
        return completed
    def execute(argv, label, **kwargs):
        return command(['docker', 'exec', '-w', INSTALL, name, *argv], label, **kwargs)
    python = ['env', 'LD_LIBRARY_PATH=' + INSTALL + '/runtime/native/lib',
              INSTALL + '/runtime/native/bin/python3.12', '-I', '-B', '/check.py', '--inside']
    def start(label):
        nonlocal application
        with (output / (label + '-app.log')).open('ab') as log:
            application = subprocess.Popen(['docker', 'exec', '-w', INSTALL, name, './qorqbot'], stdout=log, stderr=subprocess.STDOUT)
        with (output / (label + '-ready.log')).open('ab') as log:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if application.poll() is not None:
                    raise RuntimeError('Application exited: ' + label)
                probe = subprocess.run(['docker', 'exec', name, *python, 'ready'], stdout=log, stderr=subprocess.STDOUT, timeout=15)
                if probe.returncode == 0:
                    return
                time.sleep(1)
        raise RuntimeError('Application readiness timed out: ' + label)
    def stop(label):
        nonlocal application
        execute(['./qorqbot', 'stop'], label)
        if application.wait(timeout=60) != 0:
            raise RuntimeError('Application did not shut down cleanly: ' + label)
        application = None
    try:
        command(['docker', 'run', '-d', '--name', name, '--network', 'none', '--user', f'{os.getuid()}:{os.getgid()}',
            '--mount', f'type=bind,src={archive},dst=/download.tar.gz,readonly',
            '--mount', f'type=bind,src={installation},dst=/install',
            '--mount', f'type=bind,src={Path(__file__).resolve()},dst=/check.py,readonly',
            IMAGE, '/bin/sleep', 'infinity'], 'container')
        # Docker can add mount-point stubs to the container layer itself. The
        # application must introduce no changes beyond this initial layer.
        command(['docker', 'diff', name], 'container-baseline')
        command(['docker', 'exec', name, 'tar', '-xzf', '/download.tar.gz', '-C', '/install'], 'extract')
        execute(['./qorqbot', 'verify'], 'verify')
        execute(['./qorqbot', 'setup'], 'offline-setup')
        execute(python + ['tools'], 'bundled-tools')
        execute(['./qorqbot', 'status'], 'stopped-status', expected=1)
        start('first-start')
        execute(python + ['write'], 'write')
        execute(['./qorqbot', 'update', '/download.tar.gz', '--sha256', result['archive_sha256']], 'running-update-refused', expected=1)
        stop('first-stop')
        if list((installation / 'qorqbot/runtime/src').rglob('__pycache__')):
            raise RuntimeError('Application startup wrote bytecode into its verified source snapshot')
        start('restart')
        execute(python + ['restart'], 'restart')
        stop('restart-stop')
        execute(['./qorqbot', 'update', '/download.tar.gz', '--sha256', '0' * 64], 'bad-checksum-refused', expected=1)
        for phase in ('update', 'repeat-update'):
            execute(['./qorqbot', 'update', '/download.tar.gz', '--sha256', result['archive_sha256']], phase + '-install')
            start(phase)
            execute(python + [phase], phase + '-records')
            stop(phase + '-stop')
        command(['docker', 'diff', name], 'container-writes')
        if (sorted((output / 'container-writes.log').read_text().splitlines()) !=
                sorted((output / 'container-baseline.log').read_text().splitlines())):
            raise RuntimeError('Unexpected writes outside the mounted installation; inspect container-writes.log')
        if {path.name for path in installation.iterdir()} != {'qorqbot', 'acceptance-state.json'}:
            raise RuntimeError('Unexpected writes beside the installation folder')
        result['passed'] = True
    finally:
        if application is not None:
            subprocess.run(['docker', 'exec', '-w', INSTALL, name, './qorqbot', 'stop'], capture_output=True, timeout=60)
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=60)
        (output / 'RESULT.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'passed': result['passed'], 'checks': len(result['checks']), 'report': str(output / 'RESULT.json')}))


if __name__ == '__main__':
    main()
