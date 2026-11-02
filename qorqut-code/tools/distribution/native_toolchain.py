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


# Upstream annotated tag rust-v0.154.0 resolves to this exact commit. Notice
# selection must change explicitly when the pinned native CLI version changes.
CODEX_VERSION = '0.154.0'
CODEX_TAG = 'rust-v0.154.0'
CODEX_COMMIT = '6b9826e3aa83b1a5947db50f4332cb9c65f1b340'
CODEX_NOTICES = {
    'LICENSE': 'd17f227e4df5da1600391338865ce0f3055211760a36688f816941d58232d8dc',
    'NOTICE': '9d71575ecfd9a843fc1677b0efb08053c6ba9fd686a0de1a6f5382fd3c220915',
}


def codex_notices(output):
    """Retain exact upstream notices; this does not decide redistribution approval."""
    for package in ('codex', 'codex-linux-x64'):
        metadata = json.loads((output / 'clis/node_modules/@openai' / package / 'package.json').read_text())
        if metadata['version'] != CODEX_VERSION:
            raise ValueError('Codex notice selection does not match the installed CLI version')
    contents, records = {}, []
    for name, expected in CODEX_NOTICES.items():
        url = f'https://raw.githubusercontent.com/openai/codex/{CODEX_COMMIT}/{name}'
        with urllib.request.urlopen(url, timeout=60) as source:
            content = source.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024 or hashlib.sha256(content).hexdigest() != expected:
            raise ValueError('Codex notice content failed its pinned checksum: ' + name)
        contents[name] = content
        records.append({'file': name, 'url': url, 'sha256': expected, 'bytes': len(content)})
    destination = output / 'notices/codex'
    destination.mkdir(parents=True, exist_ok=False)
    for name, content in contents.items():
        (destination / name).write_bytes(content)
    record = {'format': 1, 'version': CODEX_VERSION, 'tag': CODEX_TAG,
              'commit': CODEX_COMMIT, 'files': records}
    (destination / 'SOURCE.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


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
        for name in ('native.Dockerfile', 'native_collect.py', 'native_xvnc.py', 'native_xvnc_relocate.py'):
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
        codex_notice_record = codex_notices(output)
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
                  'codex_notices': codex_notice_record,
                  'chrome_archive_sha256': CHROME_SHA256,
                  'native_cli_lock_sha256': hashlib.sha256((clis / 'package-lock.json').read_bytes()).hexdigest(),
                  'browser_lock_sha256': hashlib.sha256((browser / 'package-lock.json').read_bytes()).hexdigest()}
        report.write_text(json.dumps(record, indent=2) + '\n')


if __name__ == '__main__':
    main()
