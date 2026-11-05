#!/usr/bin/env python3
"""Assemble a native folder download using system tools and standard Python wheels.

Build tooling only. Input is a prepare-local output of the filtered export. Never point this at an initialized installation.
No venv, configuration, HOME, build log or instance is copied from either input.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.util
import io
from email.parser import BytesParser
import re
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import zipfile


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


DEV = {'tests', 'test', '__tests__', '__pycache__', '.github', '.git', '.gitlab', '.pytest_cache'}


def strip_wheel(source, target):
    """Strip third-party test/development trees and regenerate the wheel RECORD.

    The original wheel hash stays in the private build report. License files and
    metadata are retained, and pip verifies the new RECORD at installation.
    """
    removed, files = [], {}
    with zipfile.ZipFile(source) as wheel:
        for item in wheel.infolist():
            path = Path(item.filename)
            if path.is_absolute() or '..' in path.parts or item.filename in files:
                raise ValueError('Unsafe or duplicate wheel member')
            if item.is_dir():
                continue
            if (set(path.parts) & DEV or path.suffix in {'.pyc', '.pyo'}
                    or path.name in {'RECORD', 'RECORD.jws', 'RECORD.p7s'}):
                removed.append(item.filename)
                continue
            files[item.filename] = (item, wheel.read(item))
        records = [name for name in wheel.namelist() if name.endswith('.dist-info/RECORD')]
        if len(records) != 1:
            raise ValueError('Wheel needs exactly one RECORD')
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\n')
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as wheel:
        for name, (item, data) in files.items():
            wheel.writestr(item, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')
            writer.writerow((name, 'sha256=' + digest, len(data)))
        writer.writerow((records[0], '', ''))
        wheel.writestr(records[0], output.getvalue())
    return {'file': source.name, 'original_sha256': sha(source), 'runtime_sha256': sha(target), 'removed': removed}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepared', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--target', choices=('ubuntu24-x86_64', 'macos14-arm64'), default='ubuntu24-x86_64')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[0-9A-Za-z][0-9A-Za-z._-]{0,79}', args.version):
        raise ValueError('Invalid package version')
    prepared, output, report = (p.resolve() for p in (args.prepared, args.output, args.report))
    if output.exists() or report.exists():
        raise ValueError('Output and report must be new paths')
    for a in (prepared,):
        if output.is_relative_to(a) or a.is_relative_to(output):
            raise ValueError('Build inputs and output must be disjoint')
    if report.is_relative_to(output):
        raise ValueError('The private report must be outside the package')
    import runtime_source
    source = prepared / 'src'
    runtime_source.load_snapshot().validate_export(source)
    receipt = json.loads((prepared / 'PREPARATION-RECEIPT.json').read_text())
    if receipt.get('state') != 'prepared-not-configured' or set(receipt['environments']) != {'venv'}:
        raise ValueError('Expected a core-only prepared environment')
    audit = runtime_source.audit(source, wheels=prepared / 'wheels')
    # Qualification is handled by the release gate; this assembler never publishes.
    blockers = audit['findings']
    if blockers:
        raise ValueError('Runtime source audit is not clean: ' + json.dumps(blockers[:5]))
    runtime = output / 'qorqbot/runtime'
    runtime.mkdir(parents=True)
    shutil.copytree(source, runtime / 'src', symlinks=True)
    shutil.copytree(prepared / 'wheels', runtime / 'wheels')
    shutil.copy2(prepared / 'PREPARATION-RECEIPT.json', runtime / 'PREPARATION-RECEIPT.json')
    for row in receipt['wheels']:
        if sha(runtime / row['file']) != row['sha256']:
            raise ValueError('First-party wheel checksum mismatch: ' + row['file'])
    python = prepared / 'venv/bin/python'
    packages = receipt['environments']['venv']['packages']
    normalize = lambda value: value.lower().replace('_', '-').replace('.', '-')
    own = {normalize(row['name']) for row in receipt['wheels']}
    requirements = sorted(f'{name}=={version}' for name, version in packages.items()
                          if normalize(name) not in own and normalize(name) not in {'pytest', 'pip'})
    download_options = []
    if args.target == 'macos14-arm64':
        download_options = ['--platform', 'macosx_14_0_arm64', '--python-version', '3.12', '--implementation', 'cp', '--abi', 'cp312']
        if not any(normalize(name) == 'psutil' for name in packages):
            requirements.append('psutil>=7.0,<8')
    downloads = report.parent / (report.stem + '-downloads')
    downloads.mkdir(parents=True, exist_ok=False)
    subprocess.run([python, '-I', '-m', 'pip', 'download', '--only-binary=:all:', '--no-deps', '--dest', downloads,
                    *download_options, *requirements], check=True)
    (runtime / 'wheelhouse').mkdir()
    wheel_records = [strip_wheel(w, runtime / 'wheelhouse' / w.name) for w in sorted(downloads.glob('*.whl'))]
    shutil.copy2(Path(__file__).with_name('native_launch.py'), runtime / 'launch.py')
    shutil.copy2(Path(__file__).with_name('native_update.py'), runtime / 'update.py')
    shutil.copy2(Path(__file__).with_name('native_extension.py'), runtime / 'extension_install.py')
    launcher = output / 'qorqbot/qorqbot'
    launcher.write_text("""#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
python=${QORQUT_PYTHON:-python3.12}
if ! command -v "$python" >/dev/null 2>&1; then
    echo "Qorqbot requires Python 3.12 with venv support." >&2
    if [ "$(uname -s)" = Darwin ]; then
        echo "Install it with: brew install python@3.12" >&2
    else
        echo "Install it with: sudo apt install python3.12 python3.12-venv" >&2
    fi
    exit 1
fi
unset PYTHONHOME PYTHONPATH
if [ "${1-}" = update ]; then
    shift
    updater="$root/runtime"
    if [ ! -d "$updater" ]; then updater="$root/.previous-runtime"; fi
    exec "$python" -I -B "$updater/update.py" "$@"
fi
if [ "${1-}" = extension-install ]; then
    shift
    exec "$python" -I -B "$root/runtime/extension_install.py" "$@"
fi
exec "$python" -I -B "$root/runtime/launch.py" "$@"
""")
    launcher.chmod(0o755)
    guide = 'MACOS.md' if args.target == 'macos14-arm64' else 'README.md'
    shutil.copy2(Path(__file__).resolve().parents[2] / ('docs/distribution/' + guide), runtime / 'README.txt')
    (output / 'qorqbot/README.txt').symlink_to('runtime/README.txt')
    for name in ('LICENSE', 'LICENSE-SCOPE.md', 'LICENSES/Apache-2.0.txt', 'RELEASE-LICENSES.json'):
        target = output / 'qorqbot' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        # These links always follow the active runtime, including after an
        # update or recovery, so notices cannot silently lag behind its code.
        target.symlink_to(os.path.relpath(runtime / 'src/qorqut-code' / name, target.parent))
    locked = []
    for wheel_path in sorted((runtime / 'wheels').glob('*.whl')) + sorted((runtime / 'wheelhouse').glob('*.whl')):
        with zipfile.ZipFile(wheel_path) as wheel:
            paths = [n for n in wheel.namelist() if n.endswith('.dist-info/METADATA')]
            if len(paths) != 1:
                raise ValueError('Wheel needs one METADATA: ' + wheel_path.name)
            metadata = BytesParser().parsebytes(wheel.read(paths[0]))
        # First-party initialization verifies pip's direct-URL provenance against
        # the receipt wheel. A named find-links requirement does not produce it.
        requirement = ('./runtime/wheels/' + wheel_path.name if wheel_path.parent.name == 'wheels'
                       else f"{metadata['Name']}=={metadata['Version']}")
        locked.append(f"{requirement} --hash=sha256:{sha(wheel_path)}")
    (runtime / 'requirements.lock').write_text('\n'.join(sorted(locked)) + '\n')
    (runtime / 'DEPENDENCIES.json').write_text(json.dumps({
        'platform': 'macOS 14+ Apple silicon' if args.target == 'macos14-arm64' else 'Ubuntu 24.04 x86_64',
        'required': {'python': '3.12.x with venv/pip support', 'git': '2.43 or newer'},
        'optional': {'tmux': '3.4 or newer for agent terminals', 'agent_client': 'user-installed Codex or Claude Code',
                     'browser': 'user-installed compatible Chrome and agent-browser',
                     'node': 'as required by the selected external client/browser tool',
                     'desktop': 'Linux only: TigerVNC, Openbox, X11, D-Bus and keyring if a desktop is configured'},
        'python_libraries': 'requirements.lock', 'external_tools_bundled': False,
    }, indent=2) + '\n')
    files = {}
    for path in sorted(runtime.rglob('*')):
        if path.is_file():
            if not path.resolve().is_relative_to(runtime):
                raise ValueError('Runtime symlink escapes package: ' + str(path))
            files[str(path.relative_to(runtime))] = sha(path)
    schema_code = ('import json;from bazarlyk.composition.assembly import host_assembly;'
                   'from bazarlyk.service.changes import ChangeFeed;'
                   't=host_assembly(ChangeFeed()).target;print(json.dumps([t.name,t.version,t.digest]))')
    schema_target = json.loads(subprocess.check_output([python, '-I', '-c', schema_code], text=True))
    (runtime / 'PAYLOAD.json').write_text(json.dumps({'format': 1, 'version': args.version,
         'platform': args.target, 'schema_target': schema_target, 'files': files}, indent=2) + '\n')
    archive = output / ('qorqbot-' + args.version + '-' + args.target + '.tar.gz')
    def public_header(member):
        member.uid = member.gid = 0
        member.uname = member.gname = ''
        member.mtime = 0
        member.pax_headers = {}
        return member
    with tarfile.open(archive, 'w:gz', compresslevel=6) as tar:
        tar.add(output / 'qorqbot', arcname='qorqbot', filter=public_header)
    record = {'format': 1, 'version': args.version, 'platform': args.target, 'qualified': False, 'external_tools_bundled': False, 'archive': str(archive),
              'sha256': sha(archive), 'third_party_wheels': wheel_records, 'source_audit': audit}
    report.write_text(json.dumps(record, indent=2) + '\n')
    print(archive)


if __name__ == '__main__':
    main()
