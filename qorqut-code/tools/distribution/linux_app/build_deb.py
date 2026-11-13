"""Wrap an exact ubuntu24-x86_64 native archive in the Qorqbot desktop app and a .deb.

Build tooling only; runs on Linux with Node.js/npm and dpkg-deb. The app is the
same Electron shell as the Mac app, packaged for Linux. It carries the archive
unchanged in ``/opt/Qorqbot/resources/qorqbot-payload`` and does not bundle
Python, git or tmux (the package depends on them). The package installs
``chrome-sandbox`` as root with mode 4755 so Chromium's sandbox works on
Ubuntu 24.04, which blocks unprivileged user namespaces.

    python3 tools/distribution/linux_app/build_deb.py \
        --archive /abs/qorqbot-VERSION-ubuntu24-x86_64.tar.gz --output /abs/deb-out
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
# The Electron project in macos_app/electron is shared by both platforms.
sys.path.insert(0, str(HERE.parent / 'macos_app'))
from build_dmg import MARK, icon_svg, run, sha256, stage_electron  # noqa: E402

ARCHIVE_NAME = re.compile(r'^qorqbot-(?P<version>[0-9A-Za-z.+-]+)-ubuntu24-x86_64\.tar\.gz$')
DEB_VERSION = re.compile(r'^[0-9][0-9A-Za-z.+~]*$')
INSTALL_DIR = 'opt/Qorqbot'
# Shared libraries the Electron 44.10.2 linux-x64 `Qorqbot` binary links against
# (direct NEEDED entries from readelf -d, resolved with ldd, mapped with dpkg -S
# on Ubuntu 24.04), sorted, plus the programs the Qorqbot folder itself needs.
LIBRARY_PACKAGES = (
    'libasound2t64',
    'libatk-bridge2.0-0t64',
    'libatk1.0-0t64',
    'libatspi2.0-0t64',
    'libc6',
    'libcairo2',
    'libcups2t64',
    'libdbus-1-3',
    'libexpat1',
    'libgbm1',
    'libgcc-s1',
    'libglib2.0-0t64',
    'libgtk-3-0t64',
    'libnspr4',
    'libnss3',
    'libpango-1.0-0',
    'libudev1',
    'libx11-6',
    'libxcb1',
    'libxcomposite1',
    'libxdamage1',
    'libxext6',
    'libxfixes3',
    'libxkbcommon0',
    'libxrandr2',
)
PROGRAM_PACKAGES = ('git', 'python3.12', 'python3.12-venv', 'tmux', 'xdg-utils')
DEPENDS = tuple(sorted(set(LIBRARY_PACKAGES + PROGRAM_PACKAGES)))
DESCRIPTION = (
    'Qorqbot desktop app',
    ('Qorqbot is a proactive agent for business workflows and long-running tasks. '
     'It runs on your own computer.',
     'The first launch sets up the Qorqbot folder at ~/Qorqbot and starts it in its own window.',
     'It needs your own Claude Code or Codex to bring agents online.'),
)
DESKTOP_ENTRY = '''[Desktop Entry]
Type=Application
Name=Qorqbot
Comment=Proactive agent for business work
Exec=/opt/Qorqbot/Qorqbot
Icon=qorqbot
Terminal=false
Categories=Office;
StartupWMClass=qorqbot
'''


def archive_version(archive):
    """The version declared inside the archive must match its file name."""
    match = ARCHIVE_NAME.match(archive.name)
    if not match:
        raise ValueError('Expected a qorqbot-VERSION-ubuntu24-x86_64.tar.gz archive, got ' + archive.name)
    with tarfile.open(archive) as package:
        member = package.extractfile('qorqbot/runtime/PAYLOAD.json')
        if member is None:
            raise ValueError('Archive has no qorqbot/runtime/PAYLOAD.json')
        payload = json.load(member)
    if payload.get('version') != match['version']:
        raise ValueError(f"Archive name says {match['version']} but its payload says {payload.get('version')}")
    return match['version']


def app_version(version):
    """Same numeric rule as the Mac builder: keep the leading numeric part."""
    numeric = re.match(r'^(\d+)(?:\.(\d+))?(?:\.(\d+))?', version)
    if not numeric:
        raise ValueError('Version has no leading numeric part: ' + version)
    return '.'.join(part for part in numeric.groups() if part is not None)


def deb_version(version):
    """Debian form of a SemVer string: the pre-release part sorts before the release."""
    head, dash, rest = version.partition('-')
    result = head + (('~' + rest.replace('-', '.')) if dash else '')
    if not DEB_VERSION.match(result):
        raise ValueError('Version cannot be used as a Debian version: ' + version)
    return result


def control_text(version, installed_size):
    synopsis, paragraphs = DESCRIPTION
    extended = '\n .\n'.join(' ' + paragraph for paragraph in paragraphs)
    return (f'Package: qorqbot\nVersion: {deb_version(version)}\nArchitecture: amd64\n'
            'Maintainer: 49 Agents LLC\nHomepage: https://qorqbot.com\nSection: utils\nPriority: optional\n'
            f'Installed-Size: {installed_size}\nDepends: {", ".join(DEPENDS)}\n'
            f'Description: {synopsis}\n{extended}\n')


def package_app(project, version, work):
    out = work / 'packaged'
    run([project / 'node_modules/.bin/electron-packager', project, 'Qorqbot',
         '--platform=linux', '--arch=x64', '--out', out, '--app-version', app_version(version),
         '--asar', '--prune=true', '--ignore', r'^/(test|out)(/|$)', '--quiet'], cwd=project)
    return out / 'Qorqbot-linux-x64'


def tree_size_kib(tree):
    total = 0
    for path in tree.rglob('*'):
        if path.is_symlink() or path.is_file():
            total += path.lstat().st_size
    return (total + 1023) // 1024


def normalise_modes(tree):
    """Same modes whatever the builder's umask: dpkg-deb keeps them as they are."""
    for path in [tree, *tree.rglob('*')]:
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        if path.is_dir():
            path.chmod(0o755)
        else:
            path.chmod(0o755 if mode & 0o111 else 0o644)
    (tree / INSTALL_DIR / 'chrome-sandbox').chmod(0o4755)
    (tree / 'DEBIAN').chmod(0o755)
    (tree / 'DEBIAN/control').chmod(0o644)


def assemble_tree(packaged, archive, version, tree):
    """Lay out the package tree from a packaged Electron folder; needs no npm."""
    if not (packaged / 'chrome-sandbox').is_file():
        raise RuntimeError('Packaged app has no chrome-sandbox: ' + str(packaged))
    app = packaged / 'Qorqbot'
    if not app.is_file() or not os.access(app, os.X_OK):
        raise RuntimeError('Packaged app has no Qorqbot executable: ' + str(packaged))
    install = tree / INSTALL_DIR
    install.parent.mkdir(parents=True)
    shutil.copytree(packaged, install, symlinks=True)
    resources = install / 'resources'
    payload = resources / 'qorqbot-payload'
    payload.mkdir()
    shutil.copyfile(archive, payload / archive.name)
    (payload / 'ARCHIVE_NAME').write_text(archive.name + '\n')
    (payload / 'ARCHIVE_SHA256').write_text(sha256(archive) + '\n')
    (payload / 'VERSION').write_text(version + '\n')
    # Electron's own license and Chromium's third-party notices travel inside the app.
    shutil.copyfile(packaged / 'LICENSE', resources / 'LICENSE.electron.txt')
    shutil.copyfile(packaged / 'LICENSES.chromium.html', resources / 'LICENSES.chromium.html')
    shutil.copyfile(HERE / 'THIRD-PARTY-NOTICES.md', resources / 'THIRD-PARTY-NOTICES.md')
    (tree / 'usr/bin').mkdir(parents=True)
    os.symlink('/opt/Qorqbot/Qorqbot', tree / 'usr/bin/qorqbot-desktop')
    applications = tree / 'usr/share/applications'
    applications.mkdir(parents=True)
    (applications / 'qorqbot.desktop').write_text(DESKTOP_ENTRY)
    icons = tree / 'usr/share/icons/hicolor/scalable/apps'
    icons.mkdir(parents=True)
    (icons / 'qorqbot.svg').write_text(icon_svg(MARK.read_text()))
    (tree / 'DEBIAN').mkdir()
    control = tree / 'DEBIAN/control'
    control.write_text('')
    normalise_modes(tree)
    control.write_text(control_text(version, tree_size_kib(tree)))
    normalise_modes(tree)


def build_package(tree, deb):
    dpkg_deb = shutil.which('dpkg-deb')
    if not dpkg_deb:
        raise RuntimeError('dpkg-deb is required to build the package (run this on Ubuntu or Debian)')
    run([dpkg_deb, '--root-owner-group', '--build', tree, deb], stdout=subprocess.DEVNULL)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--archive', type=Path, required=True, help='exact qorqbot-VERSION-ubuntu24-x86_64.tar.gz')
    parser.add_argument('--output', type=Path, required=True, help='new or empty output directory')
    args = parser.parse_args(argv)
    archive = args.archive.resolve()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error('Output directory must be new or empty')
    version = archive_version(archive)
    debian = deb_version(version)
    output.mkdir(parents=True, exist_ok=True)
    deb = output / f'Qorqbot-{version}-ubuntu24-x86_64.deb'
    with tempfile.TemporaryDirectory(prefix='qorqbot-deb-') as temporary:
        work = Path(temporary)
        project = stage_electron(work)
        packaged = package_app(project, version, work)
        tree = work / 'tree'
        tree.mkdir()
        assemble_tree(packaged, archive, version, tree)
        build_package(tree, deb)
    digest = sha256(deb)
    (output / (deb.name + '.sha256')).write_text(f'{digest}  {deb.name}\n')
    print(json.dumps({'deb': str(deb), 'sha256': digest, 'version': version, 'deb_version': debian}, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, tarfile.TarError, subprocess.CalledProcessError) as error:
        print('build_deb: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
