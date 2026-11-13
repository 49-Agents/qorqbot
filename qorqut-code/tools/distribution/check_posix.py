#!/usr/bin/env python3
"""Check an exact native archive on macOS or Linux with synthetic local records.

Runs on the target OS. It never installs OS tools, connects accounts or changes
an existing installation. Use a new short --output path, e.g. /tmp/qb-mac-check.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import tarfile
import time


def owned_processes_report(root, output, label):
    """Diagnostic only: record the processes the launcher counts as this installation's. Never raises, never changes a result."""
    lines = ['label: ' + label, 'root: ' + str(root), 'time: ' + time.strftime('%Y-%m-%dT%H:%M:%S%z')]
    try:
        keep = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location('qorqbot_launcher_diagnostic', root / 'runtime/launch.py')
            launcher = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(launcher)
            owned = sorted(int(pid) for pid in launcher.installation_processes(root))
            lines.append('launcher-owned pids: ' + (' '.join(map(str, owned)) or 'none'))
        finally:
            sys.dont_write_bytecode = keep
    except BaseException as error:
        owned = None
        lines.append('launcher detection failed: ' + type(error).__name__ + ': ' + str(error)[:500])
    try:
        table = subprocess.run(['ps', '-axo', 'pid=,ppid=,etime=,command='], capture_output=True, text=True, timeout=20).stdout.splitlines()
        lines.append('pid ppid elapsed command')
        for row in table:
            fields = row.split(None, 3)
            if len(fields) < 4 or not fields[0].isdigit():
                continue
            if owned is not None and int(fields[0]) in owned:
                lines.append(row.strip())
            elif str(root) in fields[3]:
                lines.append('(command mentions root, not launcher-owned) ' + row.strip())
    except BaseException as error:
        lines.append('ps failed: ' + type(error).__name__ + ': ' + str(error)[:500])
    try:
        (output / ('owned-processes-' + label + '.log')).write_text('\n'.join(lines) + '\n')
    except BaseException:
        pass


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--port', type=int, default=18449)
    a = p.parse_args()
    archive, output = a.archive.resolve(), a.output.resolve()
    if output.exists():
        raise ValueError('Output must be a new short path')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', a.port))
    output.mkdir(parents=True, mode=0o700)
    installation = output / 'i'
    installation.mkdir()
    root = installation / 'qorqbot'
    env = {'PATH': os.environ.get('PATH', os.defpath), 'LANG': 'C.UTF-8'}
    if os.environ.get('QORQUT_PYTHON'):
        env['QORQUT_PYTHON'] = os.environ['QORQUT_PYTHON']
    caller = output / 'user'
    caller.mkdir()
    env.update(HOME=str(caller), QORQUT_TIMEZONE='UTC', QORQBOT_CHECK_ROOT=str(root),
               QORQBOT_CHECK_URL=f'http://127.0.0.1:{a.port}', PYTHONDONTWRITEBYTECODE='1',
               CODEX_HOME=str(caller / '.codex'), CLAUDE_CONFIG_DIR=str(caller / '.claude'))
    result = {'format': 1, 'archive': archive.name, 'archive_sha256': hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest(),
              'system': platform.system(), 'machine': platform.machine(), 'os_version': platform.platform(),
              'real_accounts_used': False, 'checks': [], 'passed': False}
    app = None
    def command(argv, label, expected=0, timeout=300):
        with (output / (label + '.log')).open('ab') as log:
            done = subprocess.run(list(map(str, argv)), env=env, cwd=root, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        if done.returncode != expected:
            owned_processes_report(root, output, label + '-failed')
            raise RuntimeError(f'{label} returned {done.returncode}; inspect its log')
        result['checks'].append(label)
    def records(phase):
        command([sys.executable, '-I', '-B', Path(__file__).with_name('check_native.py'), '--inside', phase], phase + '-records')
    def start(label):
        nonlocal app
        with (output / (label + '-app.log')).open('ab') as log:
            app = subprocess.Popen(['./qorqbot', '--port', str(a.port)], cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
        with (output / (label + '-ready.log')).open('ab') as log:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if app.poll() is not None:
                    raise RuntimeError('Application exited: ' + label)
                ready = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).with_name('check_native.py')), '--inside', 'ready'],
                                       env=env, stdout=log, stderr=subprocess.STDOUT, timeout=10)
                if ready.returncode == 0:
                    result['checks'].append(label)
                    return
                time.sleep(1)
        raise RuntimeError('Readiness timed out: ' + label)
    def stop(label):
        nonlocal app
        command(['./qorqbot', 'stop'], label)
        if app.wait(timeout=60) != 0:
            raise RuntimeError('Application did not shut down cleanly')
        app = None
    try:
        with tarfile.open(archive) as package:
            package.extractall(installation, filter='data')
        command(['./qorqbot', 'verify'], 'verify')
        command(['./qorqbot', 'setup'], 'offline-setup')
        command(['./qorqbot', 'status'], 'stopped-status', expected=1)
        start('first-start'); records('write')
        command(['./qorqbot', 'update', archive, '--sha256', result['archive_sha256']], 'running-update-refused', expected=1)
        stop('first-stop')
        start('restart'); records('restart'); stop('restart-stop')
        command(['./qorqbot', 'update', archive, '--sha256', '0' * 64], 'bad-checksum-refused', expected=1)
        for phase in ('update', 'repeat-update'):
            if phase == 'update':
                owned_processes_report(root, output, 'before-update-install')
            command(['./qorqbot', 'update', archive, '--sha256', result['archive_sha256']], phase + '-install')
            start(phase); records(phase); stop(phase + '-stop')
        command(['./qorqbot', 'verify'], 'verify-after')
        if any((root / 'runtime/src').rglob('__pycache__')):
            raise RuntimeError('Bytecode written into verified source snapshot')
        result['passed'] = True
    except BaseException as error:
        result['error'] = str(error)
        raise
    finally:
        if app is not None:
            subprocess.run(['./qorqbot', 'stop'], cwd=root, env=env, capture_output=True, timeout=60)
            app.wait(timeout=60)
        (output / 'RESULT.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'passed': result['passed'], 'checks': len(result['checks']), 'report': str(output / 'RESULT.json')}))


if __name__ == '__main__':
    main()
