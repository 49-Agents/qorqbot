#!/usr/bin/env python3
"""Build the native tools in disposable containers/directories; modify no host installation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile

CHROME_VERSION = '156.0.8078.80'
CHROME_URL = f'https://storage.googleapis.com/chrome-for-testing-public/{CHROME_VERSION}/linux64/chrome-linux64.zip'
CHROME_SHA256 = 'b711c720f774be355197f32edf37e0651418ba036d92446ee2da19ac017540a7'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    output, report = args.output.resolve(), args.report.resolve()
    if output.exists() or report.exists() or report.is_relative_to(output):
        raise ValueError('Use new output and an external report path')
    here = Path(__file__).resolve().parent
    app_tools = here.parents[1] / 'app/tools'
    report.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='native-build-', dir=report.parent) as directory:
        work = Path(directory)
        context = work / 'context'
        context.mkdir()
        for name in ('native.Dockerfile', 'native_collect.py', 'native_xvnc.py'):
            shutil.copy2(here / name, context / name)
        image = 'qorqbot-native:' + hashlib.sha256(b''.join(p.read_bytes() for p in sorted(context.iterdir()))).hexdigest()[:16]
        subprocess.run(['docker', 'build', '-f', str(context / 'native.Dockerfile'), '-t', image, context], check=True)
        container = subprocess.check_output(['docker', 'create', image], text=True).strip()
        try:
            subprocess.run(['docker', 'cp', container + ':/native', output], check=True)
        finally:
            subprocess.run(['docker', 'rm', container], check=True)
        browser = output / 'browser'
        browser.mkdir()
        for name in ('build_runtime.py', 'package.json', 'package-lock.json', 'run-agent-browser'):
            shutil.copy2(app_tools / 'browser' / name, browser / name)
        subprocess.run(['/usr/bin/python3', '-I', browser / 'build_runtime.py'], check=True)
        (browser / 'node').rename(output / 'node')
        (browser / 'node').symlink_to('../node')
        clis = output / 'clis'
        clis.mkdir()
        for name in ('package.json', 'package-lock.json'):
            shutil.copy2(app_tools / 'native_clis' / name, clis / name)
        home = work / 'home'
        home.mkdir()
        env = dict(os.environ, HOME=str(home), PATH=str(output / 'node/bin') + ':/usr/bin:/bin',
                   npm_config_cache=str(work / 'npm-cache'), npm_config_audit='false', npm_config_fund='false')
        subprocess.run([output / 'node/bin/node', output / 'node/lib/node_modules/npm/bin/npm-cli.js', 'ci'],
                       cwd=clis, env=env, check=True)
        archive = work / 'chrome.zip'
        digest, size = hashlib.sha256(), 0
        with urllib.request.urlopen(CHROME_URL, timeout=60) as source, archive.open('xb') as target:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > 512 * 1024 * 1024:
                    raise ValueError('Chrome archive is larger than the approved bound')
                digest.update(chunk)
                target.write(chunk)
        if digest.hexdigest() != CHROME_SHA256:
            raise ValueError('Chrome archive checksum mismatch')
        chrome = output / 'chrome'
        chrome.mkdir()
        with zipfile.ZipFile(archive) as z:
            for item in z.infolist():
                path = Path(item.filename)
                if path.is_absolute() or '..' in path.parts or path.parts[0] != 'chrome-linux64':
                    raise ValueError('Unsafe Chrome archive member')
                target = chrome.joinpath(*path.parts[1:])
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(z.read(item))
                    target.chmod(0o755 if (item.external_attr >> 16) & 0o111 else 0o644)
        wrapper = output / 'bin/chrome'
        wrapper.write_text('''#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
exec "$root/chrome/chrome" "$@"
''')
        wrapper.chmod(0o755)
        # Only Node itself is used by runtime wrappers; npm and C headers were build inputs.
        for relative in ('node/include', 'node/lib/node_modules/npm', 'node/share/man',
                         'browser/node_modules/agent-browser/scripts', 'browser/node_modules/agent-browser/skill-data',
                         'browser/node_modules/agent-browser/skills'):
            shutil.rmtree(output / relative, ignore_errors=True)
        for name in ('npm', 'npx', 'corepack'):
            (output / 'node/bin' / name).unlink(missing_ok=True)
        (browser / 'build_runtime.py').unlink()
        image_id = subprocess.check_output(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'], text=True).strip()
        record = {'format': 1, 'ubuntu_builder': image_id, 'chrome_version': CHROME_VERSION,
                  'chrome_archive_sha256': CHROME_SHA256,
                  'native_cli_lock_sha256': hashlib.sha256((clis / 'package-lock.json').read_bytes()).hexdigest(),
                  'browser_lock_sha256': hashlib.sha256((browser / 'package-lock.json').read_bytes()).hexdigest()}
        report.write_text(json.dumps(record, indent=2) + '\n')


if __name__ == '__main__':
    main()
