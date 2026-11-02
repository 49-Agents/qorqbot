#!/usr/bin/env python3
"""Build-only collector, run in the pinned Ubuntu builder, never on a user's host.

Collect the interpreter, native tools, their shared libraries and redistribution
notices. The Ubuntu 24.04 loader/glibc remain an explicit platform dependency.
No /etc account configuration, machine identity or build HOME is copied.
"""
from pathlib import Path
import json
import hashlib
import os
import re
import shutil
import subprocess
import sys


def collect(destination):
    root = Path(destination)
    root.mkdir(exist_ok=False)
    copied = set()

    def copy(source, target=None):
        source = Path(source)
        target = root / (target or str(source).lstrip('/'))
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, symlinks=False, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('__pycache__', 'test', 'tests', 'idlelib', 'tkinter', 'ensurepip'))
        else:
            shutil.copy2(source, target, follow_symlinks=True)
        copied.add(source)

    for name in ('python3.12', 'git', 'tmux', 'Xtigervnc', 'tigervncpasswd', 'openbox',
                 'dbus-daemon', 'dbus-launch', 'dbus-update-activation-environment',
                 'xauth', 'xdpyinfo', 'xset', 'xkbcomp', 'gnome-keyring-daemon'):
        executable = shutil.which(name)
        if not executable:
            raise RuntimeError('Missing native input: ' + name)
        copy(executable, 'bin/' + name)
    (root / 'bin/python3').symlink_to('python3.12')
    copy('/usr/lib/python3.12', 'lib/python3.12')
    copy('/usr/lib/git-core', 'lib/git-core')
    copy('/usr/share/zoneinfo', 'share/zoneinfo')
    copy('/usr/share/doc/tigervnc-standalone-server/TIGERVNC-BUILD.json', 'TIGERVNC-BUILD.json')
    for source, target in (('/usr/share/git-core/templates', 'share/git-core/templates'),
                           ('/usr/share/fonts/X11', 'share/fonts/X11'),
                           ('/usr/share/fonts/truetype/dejavu', 'share/fonts/truetype/dejavu'),
                           ('/usr/share/X11/xkb', 'share/X11/xkb'),
                           ('/usr/share/themes/Clearlooks/openbox-3', 'share/themes/Clearlooks/openbox-3'),
                           ('/etc/xdg/openbox/rc.xml', 'share/openbox/rc.xml'),
                           ('/etc/ssl/certs/ca-certificates.crt', 'share/ca-certificates.crt')):
        copy(source, target)
    # Relative font paths and an XDG cache keep the relocated desktop independent
    # of the builder's /etc and the customer's global font configuration.
    (root / 'share/fonts/fonts.conf').write_text('''<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<fontconfig><dir prefix="relative">.</dir><cachedir prefix="xdg">fontconfig</cachedir></fontconfig>
''')

    # Include dlopen libraries used by Chromium and NSS as well as ELF dependencies.
    library_root = Path('/usr/lib/x86_64-linux-gnu')
    for pattern in ('libnss*.so*', 'libnspr*.so*', 'libsoftokn*.so*', 'libfreebl*.so*',
                    'libatk*.so*', 'libcups*.so*', 'libdrm*.so*', 'libdbus*.so*', 'libxkbcommon*.so*',
                    'libX*.so*', 'libgbm*.so*', 'libasound*.so*', 'libpango*.so*', 'libcairo*.so*'):
        for source in library_root.glob(pattern):
            copy(source, 'lib/' + source.name)
    glibc = re.compile(r'^(?:ld-linux.*|lib(?:c|m|pthread|dl|rt|resolv|util|anl|BrokenLocale)\.so.*)$')
    queue = list(root.rglob('*'))
    seen = set()
    while queue:
        path = queue.pop()
        if not path.is_file() or path in seen:
            continue
        seen.add(path)
        with path.open('rb') as stream:
            if stream.read(4) != b'\x7fELF':
                continue
        result = subprocess.run(['ldd', str(path)], capture_output=True, text=True, check=False)
        if 'not found' in result.stdout:
            raise RuntimeError(f'Unresolved library in {path}: {result.stdout}')
        for match in re.finditer(r'(?:=>\s+)?(/[^\s]+)\s+\(', result.stdout):
            source = Path(match.group(1))
            if not glibc.match(source.name) and not (root / 'lib' / source.name).exists():
                copy(source, 'lib/' + source.name)
                queue.append(root / 'lib' / source.name)

    packages = subprocess.check_output(['dpkg-query', '-W', '-f=${binary:Package}\t${Version}\n'], text=True)
    (root / 'UBUNTU-PACKAGES.txt').write_text(packages)
    for package in packages.splitlines():
        name = package.split('\t')[0].split(':')[0]
        notice = Path('/usr/share/doc') / name / 'copyright'
        if notice.is_file():
            copy(notice, 'notices/' + name + '.copyright')
    # Git's callers deliberately sanitize their environment; its own wrapper
    # carries its relocated helpers/libraries without changing those policies.
    (root / 'bin/git').rename(root / 'bin/git.real')
    (root / 'bin/git').write_text('''#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
export LD_LIBRARY_PATH="$root/lib" GIT_EXEC_PATH="$root/lib/git-core" GIT_TEMPLATE_DIR="$root/share/git-core/templates"
export GIT_SSL_CAINFO="$root/share/ca-certificates.crt"
exec "$root/bin/git.real" "$@"
''')
    (root / 'bin/git').chmod(0o755)
    # Debian's Git commands are hardlinks; retain their shared storage after
    # collecting with copytree, and let the final tar keep the links as links.
    by_content = {}
    for path in (root / 'lib/git-core').iterdir():
        if path.is_file() and not path.is_symlink():
            key = (path.stat().st_mode, hashlib.sha256(path.read_bytes()).digest())
            if key in by_content:
                path.unlink()
                os.link(by_content[key], path)
            else:
                by_content[key] = path
    (root / 'NATIVE-PLATFORM.json').write_text(json.dumps({
        'format': 1, 'system': 'Linux', 'distribution': 'Ubuntu', 'version': '24.04',
        'architecture': 'x86_64', 'system_libraries': 'Ubuntu 24.04 glibc and ELF loader',
    }, indent=2) + '\n')


if __name__ == '__main__':
    collect(sys.argv[1])
