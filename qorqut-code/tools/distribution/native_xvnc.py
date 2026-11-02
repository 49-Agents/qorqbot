#!/usr/bin/env python3
"""Build-only TigerVNC relocation using its upstream compiler-directory option."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


VERSION = '1.13.1+dfsg-2build2'
XORG_VERSION = '2:21.1.12-1ubuntu1.9'
XORG_SHA256 = '55827eba70accfad8905431a83f03be37a3b10ae0e5e567f29cde3fa996747da'
ARCHIVES = {
    'tigervnc_1.13.1+dfsg-2build2.dsc': 'e7dc8aca0db0cff3055a009ba3177e11da580d988a9c8616697b06aa82d1f739',
    'tigervnc_1.13.1+dfsg.orig.tar.xz': '9a3d26d3c7e51a6809c590a6f61c8cc4a68a001f50852895d45ec67f59a69347',
    'tigervnc_1.13.1+dfsg-2build2.debian.tar.xz': '2edf772bdda00c89c969cef549f96b0d9aa8cee85df950d9a61b2b5342c9f141',
}


def build():
    work = Path('/build')
    xorg = Path('/usr/src/xorg-server.tar.xz')
    xorg_version = subprocess.check_output(['dpkg-query', '-W', '-f=${Version}', 'xorg-server-source'], text=True)
    if xorg_version != XORG_VERSION or hashlib.sha256(xorg.read_bytes()).hexdigest() != XORG_SHA256:
        raise RuntimeError('Xorg source does not match the approved native build input')
    subprocess.run(['apt-get', 'source', '--download-only', 'tigervnc=' + VERSION], cwd=work, check=True)
    for name, expected in ARCHIVES.items():
        if hashlib.sha256((work / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError('TigerVNC source checksum mismatch: ' + name)
    source = work / 'tigervnc'
    subprocess.run(['dpkg-source', '-x', work / ('tigervnc_' + VERSION + '.dsc'), source], check=True)
    rules = source / 'debian/rules'
    content = rules.read_text()
    marker = 'DEB_CONFIGURE_EXTRA_FLAGS := \\\n'
    if content.count(marker) != 1:
        raise RuntimeError('Unexpected TigerVNC build recipe')
    rules.write_text(content.replace(marker, marker + '\t--with-xkb-bin-directory= --with-xkb-output=. \\\n', 1))
    subprocess.run(['make', '-f', 'debian/rules', 'build', 'DEB_BUILD_OPTIONS=parallel=8'], cwd=source, check=True)
    binary = source / 'obj-x86_64-linux-gnu/unix/xserver/hw/vnc/Xvnc'
    shutil.copy2(binary, work / 'Xtigervnc')
    subprocess.run(['strip', '--strip-unneeded', work / 'Xtigervnc'], check=True)
    record = {
        'format': 1, 'tigervnc_source_version': VERSION, 'source_archives_sha256': ARCHIVES,
        'xorg_source_package': xorg_version,
        'xorg_source_archive_sha256': hashlib.sha256(xorg.read_bytes()).hexdigest(),
        'configure_addition': '--with-xkb-bin-directory= --with-xkb-output=.',
        'binary_sha256': hashlib.sha256((work / 'Xtigervnc').read_bytes()).hexdigest(),
    }
    (work / 'TIGERVNC-BUILD.json').write_text(json.dumps(record, indent=2) + '\n')


if __name__ == '__main__':
    build()
