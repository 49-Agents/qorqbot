#!/usr/bin/env python3
"""Make a WSL2 handoff ZIP around an unchanged, checksum-selected Linux archive."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile
import zipfile


def build(archive, expected, output):
    archive, output = Path(archive).resolve(), Path(output).resolve()
    actual = hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest()
    if actual != expected.lower():
        raise ValueError('Linux archive checksum mismatch')
    if output.exists():
        raise ValueError('Output must be a new ZIP path')
    with tarfile.open(archive) as tar:
        payload = json.load(tar.extractfile('qorqbot/runtime/PAYLOAD.json'))
    if payload.get('platform', 'ubuntu24-x86_64') != 'ubuntu24-x86_64':
        raise ValueError('WSL2 x64 needs the Ubuntu x86_64 archive')
    guide = Path(__file__).resolve().parents[2] / 'docs/distribution/WINDOWS.md'
    record = {'format': 1, 'platform': 'Windows 11 x64 + user-installed WSL2 Ubuntu 24.04',
              'linux_archive': archive.name, 'linux_sha256': actual, 'version': payload['version'],
              'windows_host_tested': False, 'installs_wsl': False, 'bundles_vm_image': False}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_STORED) as package:
        package.write(archive, archive.name)
        package.writestr('README.txt', guide.read_text().replace('qorqbot-VERSION-ubuntu24-x86_64.tar.gz', archive.name))
        package.writestr('SHA256SUMS', actual + '  ' + archive.name + '\n')
        package.writestr('PACKAGE.json', json.dumps(record, indent=2) + '\n')
    return {**record, 'zip': output.name, 'zip_sha256': hashlib.file_digest(output.open('rb'), 'sha256').hexdigest()}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--linux-archive', type=Path, required=True)
    p.add_argument('--sha256', required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    print(json.dumps(build(args.linux_archive, args.sha256, args.output), indent=2))


if __name__ == '__main__':
    main()
