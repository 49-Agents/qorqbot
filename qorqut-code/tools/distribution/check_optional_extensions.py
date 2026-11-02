#!/usr/bin/env python3
"""Qualify explicit optional connector bundles against a disposable Linux download."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--bundle', action='append', required=True, metavar='ID=PATH')
    parser.add_argument('--report-directory', type=Path, required=True)
    parser.add_argument('--image', default='qorqbot-check-prerequisites:ubuntu24')
    parser.add_argument('--core-check', type=Path, default=Path(__file__).with_name('check_native.py'))
    parser.add_argument('--probe', type=Path, default=Path(__file__).with_name('check_optional_extensions_probe.py'))
    args = parser.parse_args()
    archive, report = args.archive.resolve(), args.report_directory.resolve()
    bundles = []
    for value in args.bundle:
        name, separator, raw = value.partition('=')
        if not separator or name not in {'telegram', 'fastmail', 'whatsapp'} or name in {n for n, _, _ in bundles}:
            parser.error('Each bundle must be a unique telegram=PATH, fastmail=PATH or whatsapp=PATH')
        path = Path(raw).resolve()
        bundles.append((name, path, checksum(path)))
    if not archive.is_file() or not args.core_check.is_file() or not args.probe.is_file():
        parser.error('Archive, core-check and probe must be existing files')
    report.mkdir(parents=True, exist_ok=False)
    install = report / 'install'
    install.mkdir()
    root = '/install/qorqbot'
    container = 'qorqbot-connectors-' + uuid.uuid4().hex[:12]
    result = {'format': 1, 'archive_sha256': checksum(archive), 'image': args.image,
              'image_id': subprocess.check_output(['docker', 'image', 'inspect', args.image, '--format', '{{.Id}}'], text=True).strip(),
              'network': 'none', 'uid': os.getuid(), 'checks': [], 'passed': False,
              'bundles': {name: {'filename': path.name, 'sha256': digest} for name, path, digest in bundles}}
    app = None

    def command(argv, label, expected=0, timeout=600):
        with (report / (label + '.log')).open('ab') as log:
            done = subprocess.run(list(map(str, argv)), stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        if done.returncode != expected:
            raise RuntimeError(f'{label} returned {done.returncode}; inspect log')
        result['checks'].append(label)
        print(label, flush=True)

    def execute(argv, label, **kwargs):
        command(['docker', 'exec', '-w', root, container, *argv], label, **kwargs)

    def start(label):
        nonlocal app
        with (report / (label + '-app.log')).open('ab') as log:
            app = subprocess.Popen(['docker', 'exec', '-w', root, container, './qorqbot'], stdout=log, stderr=subprocess.STDOUT)
        with (report / (label + '-ready.log')).open('ab') as log:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if app.poll() is not None:
                    raise RuntimeError('Application exited: ' + label)
                ready = subprocess.run(['docker', 'exec', container, '/usr/bin/python3.12', '-I', '-B', '/core-check.py', '--inside', 'ready'], stdout=log, stderr=subprocess.STDOUT, timeout=15)
                if ready.returncode == 0:
                    result['checks'].append(label)
                    print(label, flush=True)
                    return
                time.sleep(1)
        raise RuntimeError('Readiness timeout: ' + label)

    def stop(label):
        nonlocal app
        execute(['./qorqbot', 'stop'], label)
        if app.wait(timeout=60) != 0:
            raise RuntimeError('Application shutdown failed: ' + label)
        app = None

    def records(mode, label, selected=()):
        execute(['/usr/bin/python3.12', '-I', '-B', '/probe.py', mode, '--selected', ','.join(selected)], label)

    try:
        mounts = ['--mount', f'type=bind,src={archive},dst=/download.tar.gz,readonly',
                  '--mount', f'type=bind,src={install},dst=/install',
                  '--mount', f'type=bind,src={args.core_check.resolve()},dst=/core-check.py,readonly',
                  '--mount', f'type=bind,src={args.probe.resolve()},dst=/probe.py,readonly']
        for name, path, _ in bundles:
            mounts += ['--mount', f'type=bind,src={path},dst=/bundle-{name}.tar.gz,readonly']
        command(['docker', 'run', '-d', '--name', container, '--network', 'none', '--user', f'{os.getuid()}:{os.getgid()}',
                 '--env', 'TZ=UTC', *mounts, args.image, '/bin/sleep', 'infinity'], 'container')
        command(['docker', 'diff', container], 'container-baseline')
        command(['docker', 'exec', container, 'tar', '-xzf', '/download.tar.gz', '-C', '/install'], 'extract')
        execute(['./qorqbot', 'verify'], 'verify')
        execute(['./qorqbot', 'setup'], 'offline-setup')
        start('initial-start')
        records('write', 'synthetic-write')
        first, _, first_sha = bundles[0]
        execute(['./qorqbot', 'extension-install', f'/bundle-{first}.tar.gz', '--sha256', first_sha], 'running-extension-refused', expected=1)
        stop('initial-stop')
        execute(['./qorqbot', 'extension-install', f'/bundle-{first}.tar.gz', '--sha256', '0' * 64], 'bad-bundle-checksum-refused', expected=1)
        if any(name == 'fastmail' for name, _, _ in bundles):
            private = install / 'qorqbot/instance/config'
            token = private / 'synthetic-fastmail-token'
            token.write_text('SYNTHETIC-NO-SERVICE-TOKEN-' + 'x' * 48 + '\n'); token.chmod(0o600)
            config = private / 'synthetic-fastmail.json'
            config.write_text(json.dumps({'engine_url': 'http://127.0.0.1:18787', 'admin_token_file': root + '/instance/config/synthetic-fastmail-token'})); config.chmod(0o600)
        selected = []
        for name, _, digest in bundles:
            config_args = ['--config', root + '/instance/config/synthetic-fastmail.json'] if name == 'fastmail' else []
            execute(['./qorqbot', 'extension-install', f'/bundle-{name}.tar.gz', '--sha256', digest, *config_args], name + '-install')
            selected.append(name)
            start(name + '-start')
            records('verify', name + '-retained-records', selected)
            stop(name + '-stop')
        execute(['./qorqbot', 'update', '/download.tar.gz', '--sha256', result['archive_sha256']], 'optional-core-update-refused', expected=1)
        start('all-connectors-restart')
        records('verify', 'all-connectors-retained-records', selected)
        stop('final-stop')
        command(['docker', 'diff', container], 'container-writes')
        if set((report / 'container-writes.log').read_text().splitlines()) != set((report / 'container-baseline.log').read_text().splitlines()):
            raise RuntimeError('Writes outside installation folder; inspect container-writes.log')
        if {p.name for p in install.iterdir()} != {'qorqbot', 'extension-acceptance-state.json'}:
            raise RuntimeError('Unexpected files beside installation')
        result['passed'] = True
    except BaseException as exc:
        result['error'] = str(exc)
        raise
    finally:
        if app is not None:
            subprocess.run(['docker', 'exec', '-w', root, container, './qorqbot', 'stop'], capture_output=True, timeout=60)
        subprocess.run(['docker', 'rm', '-f', container], capture_output=True, timeout=60)
        (report / 'RESULT.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'passed': result['passed'], 'checks': len(result['checks']), 'report': str(report / 'RESULT.json')}))


if __name__ == '__main__':
    main()
