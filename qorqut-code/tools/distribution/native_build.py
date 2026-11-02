#!/usr/bin/env python3
"""Assemble an offline Linux folder download from verified runtime preparation.

Build tooling only. Inputs are a prepare-local output of the filtered export and
the clean native tool bundle. Never point this at an initialized installation.
No venv, configuration, HOME, build log or instance is copied from either input.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.util
import io
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
    parser.add_argument('--native', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[0-9A-Za-z][0-9A-Za-z._-]{0,79}', args.version):
        raise ValueError('Invalid package version')
    prepared, native, output, report = (p.resolve() for p in (args.prepared, args.native, args.output, args.report))
    if output.exists() or report.exists():
        raise ValueError('Output and report must be new paths')
    for a in (prepared, native):
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
    blockers = [f for f in audit['findings'] if not (f.get('kind') == 'missing-required-asset' and f.get('path') == 'LICENSE')]
    if blockers:
        raise ValueError('Runtime source audit is not clean: ' + json.dumps(blockers[:5]))
    runtime = output / 'qorqbot/runtime'
    runtime.mkdir(parents=True)
    shutil.copytree(source, runtime / 'src', symlinks=True)
    copied_inodes = {}
    def copy_native(source, destination):
        stat = os.stat(source)
        key = (stat.st_dev, stat.st_ino)
        if key in copied_inodes:
            os.link(copied_inodes[key], destination)
        else:
            shutil.copy2(source, destination)
            copied_inodes[key] = destination
        return destination
    shutil.copytree(native, runtime / 'native', symlinks=True, copy_function=copy_native,
                    ignore=lambda directory, names: [n for n in names if n in DEV or n in {'build_runtime.py', '.package-lock.json'}])
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
                          if normalize(name) not in own and normalize(name) not in {'pytest'})
    if not any(r.startswith('pip==') for r in requirements):
        requirements.append(subprocess.check_output([python, '-I', '-c',
            'import importlib.metadata as m; print("pip=="+m.version("pip"))'], text=True).strip())
    downloads = report.parent / (report.stem + '-downloads')
    downloads.mkdir(parents=True, exist_ok=False)
    subprocess.run([python, '-I', '-m', 'pip', 'download', '--only-binary=:all:', '--no-deps', '--dest', downloads,
                    *requirements], check=True)
    (runtime / 'wheelhouse').mkdir()
    wheel_records = [strip_wheel(w, runtime / 'wheelhouse' / w.name) for w in sorted(downloads.glob('*.whl'))]
    shutil.copy2(Path(__file__).with_name('native_launch.py'), runtime / 'launch.py')
    shutil.copy2(Path(__file__).with_name('native_update.py'), runtime / 'update.py')
    shutil.copy2(Path(__file__).with_name('native_extension.py'), runtime / 'extension_install.py')
    launcher = output / 'qorqbot/qorqbot'
    launcher.write_text('''#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
export LD_LIBRARY_PATH="$root/runtime/native/lib"
unset PYTHONHOME PYTHONPATH
if [ "${1-}" = update ]; then
    shift
    updater="$root/runtime"
    if [ ! -d "$updater" ]; then updater="$root/.previous-runtime"; fi
    export LD_LIBRARY_PATH="$updater/native/lib"
    exec "$updater/native/bin/python3.12" -I "$updater/update.py" "$@"
fi
if [ "${1-}" = extension-install ]; then
    shift
    exec "$root/runtime/native/bin/python3.12" -I "$root/runtime/extension_install.py" "$@"
fi
exec "$root/runtime/native/bin/python3.12" -I "$root/runtime/launch.py" "$@"
''')
    launcher.chmod(0o755)
    (output / 'qorqbot/README.txt').write_text('''Qorqbot — Linux development candidate

Ubuntu 24.04, Intel/AMD 64-bit. Extract to a short writable folder and run:
  ./qorqbot
Open the printed local address in your browser. Press Ctrl+C to stop.
You can also run ./qorqbot status or ./qorqbot stop from another terminal.
Install a downloaded optional connector while Qorqbot and its agents are stopped:
  ./qorqbot extension-install /path/to/connector.tar.gz --sha256 VERIFIED_HASH
An interrupted installation can be continued with the same command and --resume.

The first launch installs bundled dependencies offline. Your work is stored in
instance/operations; settings, logs and profiles also stay inside this folder.
Keep this folder at the same location after first launch. Back it up while the
application and its agents are stopped. Deleting the folder deletes its data.

This candidate is private and unqualified for publication. License selection,
extension distribution and release qualification must be completed separately.
''')
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
         'schema_target': schema_target, 'files': files}, indent=2) + '\n')
    archive = output / ('qorqbot-' + args.version + '-ubuntu24-x86_64.tar.gz')
    def public_header(member):
        member.uid = member.gid = 0
        member.uname = member.gname = ''
        member.mtime = 0
        member.pax_headers = {}
        return member
    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(output / 'qorqbot', arcname='qorqbot', filter=public_header)
    record = {'format': 1, 'version': args.version, 'qualified': False, 'archive': str(archive),
              'sha256': sha(archive), 'third_party_wheels': wheel_records, 'source_audit': audit}
    report.write_text(json.dumps(record, indent=2) + '\n')
    print(archive)


if __name__ == '__main__':
    main()
