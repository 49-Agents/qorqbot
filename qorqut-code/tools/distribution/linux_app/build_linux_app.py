"""Wrap an exact ubuntu24-x86_64 native archive in the Qorqbot desktop app for Linux, as an AppImage and a .deb.

Build tooling only; runs on Linux x86_64 with Node.js/npm. The app is an Electron shell (``electron/``) that installs,
runs and shows the local Qorqbot server in its own window. electron-builder (pinned in ``electron/package-lock.json``)
packages it; this script prepares its inputs (icons, the archive) and checks the result. It carries the archive
unchanged in ``resources/qorqbot-payload``; a self-contained archive brings its own Python, git and tmux, so the app
adds none. An archive built without ``--bundled-tools`` relies on the computer's own Python 3.12, git and tmux and is
refused unless ``--allow-system-tools`` is passed.

The .deb installs to /opt/Qorqbot, with a desktop entry, hicolor icons and (for Ubuntu 24.04's restriction on
unprivileged user namespaces) an AppArmor profile that electron-builder's maintainer scripts install and load.
The AppImage is a single file; it cannot install a profile, so on such systems it starts Chromium without its
sandbox (electron-builder's AppRun does that) - see docs/distribution/LINUX.md.

    python3 tools/distribution/linux_app/build_linux_app.py \
        --archive /abs/qorqbot-VERSION-ubuntu24-x86_64.tar.gz --output /abs/linux-app-out [--targets appimage,deb]
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
ELECTRON = HERE / 'electron'
MARK = HERE.parents[2] / 'biz-dashboard/css/brand/qorqbot-mark.svg'
ICON_SCRIPT = HERE / 'render_icons.cjs'
NOTICES = HERE / 'THIRD-PARTY-NOTICES.md'
LICENSE = HERE.parents[3] / 'LICENSE'
APP_ID = 'com.49agents.qorqbot'
PRODUCT = 'Qorqbot'
# The deb package keeps the plain name; the program is qorqbot-desktop so /usr/bin/qorqbot-desktop never
# collides with the qorqbot terminal command (Help > Install Terminal Command).
PACKAGE = 'qorqbot'
EXECUTABLE = 'qorqbot-desktop'
WINDOW_CLASS = 'qorqbot'
HOMEPAGE = 'https://qorqbot.com'
MAINTAINER = '49 Agents LLC'
SYNOPSIS = 'Proactive agent for business work'
# One sentence: electron-builder uses it for the menu entry's comment and the package's long description.
DESCRIPTION = 'Qorqbot runs on your computer, powered by the AI subscriptions you already pay for.'
LICENSE_ID = 'PolyForm-Noncommercial-1.0.0'
ARCHIVE_NAME = re.compile(r'^qorqbot-(?P<version>[0-9A-Za-z.+-]+)-ubuntu24-x86_64\.tar\.gz$')
PLATFORM = 'ubuntu24-x86_64'
# Hicolor sizes for the menu entry; 512 is also the picture the app's own windows use (icons/qorqbot.png).
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256, 512)
WINDOW_ICON_SIZE = 512
# Copied into the staged Electron project; tests, local builds and installed modules are never packaged from the source tree.
ELECTRON_SOURCES = ('package.json', 'package-lock.json', 'main.js', 'engine.js', 'desktop.js', 'preload.js', 'ui')
# What electron-builder packages from that project (modules the app needs at run time are added by electron-builder).
APP_FILES = ['package.json', 'main.js', 'engine.js', 'desktop.js', 'preload.js', 'ui/**/*', 'icons/qorqbot.png']
TARGETS = {'appimage': 'AppImage', 'deb': 'deb'}
# AppImage runtime: "0.0.0" needs libfuse2, which Ubuntu 24.04 does not install; the static runtime works with
# the fuse3 that Ubuntu ships. Both unpack with --appimage-extract.
APPIMAGE_TOOLSET = '1.0.3'
# electron-builder's defaults (GTK, NSS, notifications, XScreenSaver, xdg-utils, at-spi, libsecret...) with the packages
# Electron also needs and Ubuntu 24.04's renamed ("t64") libraries as alternatives; `a | b` takes whichever exists.
DEB_DEPENDS = ['libgtk-3-0t64 | libgtk-3-0', 'libnotify4', 'libnss3', 'libxss1', 'libxtst6', 'xdg-utils',
               'libatspi2.0-0t64 | libatspi2.0-0', 'libuuid1', 'libsecret-1-0',
               'libasound2t64 | libasound2', 'libgbm1']


def run(argv, **kwargs):
    subprocess.run([str(a) for a in argv], check=True, **kwargs)


def sha256(path):
    with open(path, 'rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def read_payload(archive):
    """qorqbot/runtime/PAYLOAD.json from inside the archive."""
    with tarfile.open(archive) as package:
        try:
            member = package.extractfile('qorqbot/runtime/PAYLOAD.json')
        except KeyError:
            member = None
        if member is None:
            raise ValueError('Archive has no qorqbot/runtime/PAYLOAD.json')
        return json.load(member)


def archive_version(archive, payload=None):
    """The version declared inside the archive must match its file name."""
    match = ARCHIVE_NAME.match(archive.name)
    if not match:
        raise ValueError('Expected a qorqbot-VERSION-ubuntu24-x86_64.tar.gz archive, got ' + archive.name)
    version = match['version']
    if not version[0].isdigit():
        raise ValueError('Version must start with a digit (the .deb format requires it): ' + version)
    payload = read_payload(archive) if payload is None else payload
    if payload.get('version') != version:
        raise ValueError(f"Archive name says {version} but its payload says {payload.get('version')}")
    if payload.get('platform', PLATFORM) != PLATFORM:
        raise ValueError(f"Archive is for {payload.get('platform')}, not {PLATFORM}")
    return version


def self_contained(payload):
    """A self-contained archive records one digest for each shipped tree (Python environment, native tools)."""
    return bool(payload.get('bundled'))


def artifact_names(version):
    return {'appimage': f'{PRODUCT}-{version}-x86_64.AppImage', 'deb': f'{PACKAGE}_{version}_amd64.deb'}


def parse_targets(text):
    names = [name.strip().lower() for name in text.split(',') if name.strip()]
    unknown = [name for name in names if name not in TARGETS]
    if not names or unknown:
        raise ValueError('--targets takes appimage, deb or both, separated by a comma' + (f' (not {", ".join(unknown)})' if unknown else ''))
    return list(dict.fromkeys(names))


def icon_svg(mark_text):
    """The same look as the Mac icon (black mark on a light rounded square), so Qorqbot is one picture everywhere."""
    sys.path.insert(0, str(HERE.parent / 'macos_app'))
    try:
        import build_dmg
    finally:
        sys.path.remove(str(HERE.parent / 'macos_app'))
    return build_dmg.icon_svg(mark_text)


def electron_binary(project):
    """Electron's own program inside an installed project. npm ci only installs the package; the program itself is
    fetched by Electron's install script (from the same download cache electron-builder uses)."""
    package = project / 'node_modules/electron'
    if not (package / 'path.txt').is_file():
        node = shutil.which('node')
        if not node:
            raise RuntimeError('node is required to build the desktop app (install Node.js 22 or newer)')
        run([node, package / 'install.js'], cwd=project)
    binary = package / 'dist' / (package / 'path.txt').read_text().strip()
    if not binary.is_file():
        raise RuntimeError('Electron did not install: ' + str(binary))
    return binary


def render_icons(work, project):
    """Square PNGs for every hicolor size, plus the 512 pixel picture the app's windows use.

    Uses rsvg-convert when it is installed (apt install librsvg2-bin); otherwise the Electron in the staged project
    renders the same SVG, so the build needs nothing beyond Node.js.
    """
    source = work / 'icon.svg'
    source.write_text(icon_svg(MARK.read_text()))
    resources = work / 'build'
    icons = resources / 'icons'
    icons.mkdir(parents=True)
    converter = shutil.which('rsvg-convert')
    if converter:
        for size in ICON_SIZES:
            run([converter, '-w', size, '-h', size, source, '-o', icons / f'{size}x{size}.png'])
    else:
        binary = electron_binary(project)
        env = {key: value for key, value in os.environ.items() if key != 'ELECTRON_RUN_AS_NODE'}
        # No display is needed: Chromium's headless platform draws into memory. The sandbox is off only here, for a
        # page made of this one SVG, so the build also works where unprivileged user namespaces are restricted.
        done = subprocess.run([str(a) for a in (binary, '--no-sandbox', '--ozone-platform=headless', '--disable-gpu', ICON_SCRIPT,
                                                source, icons, *ICON_SIZES)], env=env, cwd=work, capture_output=True, text=True)
        if done.returncode != 0:
            raise RuntimeError('Electron could not render the icons: ' + done.stderr.strip()[-400:])
    for size in ICON_SIZES:
        picture = icons / f'{size}x{size}.png'
        if not picture.is_file() or picture.stat().st_size < 100:
            raise RuntimeError(f'The {size}x{size} icon was not rendered')
    return resources, icons


def stage_electron(work):
    """Copy the Electron project, add the brand mark, install pinned modules."""
    npm = shutil.which('npm')
    if not npm:
        raise RuntimeError('npm is required to build the desktop app (install Node.js 22 or newer)')
    project = work / 'electron'
    project.mkdir()
    for name in ELECTRON_SOURCES:
        source = ELECTRON / name
        if source.is_dir():
            shutil.copytree(source, project / name)
        else:
            shutil.copyfile(source, project / name)
    shutil.copyfile(MARK, project / 'ui/mark.svg')
    run([npm, 'ci', '--no-audit', '--no-fund', '--loglevel=error'], cwd=project)
    return project


def stage_icons(project, icons):
    """The picture the app's own windows and tray use, inside the packaged app."""
    (project / 'icons').mkdir(exist_ok=True)
    shutil.copyfile(icons / f'{WINDOW_ICON_SIZE}x{WINDOW_ICON_SIZE}.png', project / 'icons/qorqbot.png')


def stage_payload(work, archive, version):
    payload = work / 'qorqbot-payload'
    payload.mkdir()
    shutil.copyfile(archive, payload / archive.name)
    (payload / 'ARCHIVE_NAME').write_text(archive.name + '\n')
    (payload / 'ARCHIVE_SHA256').write_text(sha256(archive) + '\n')
    (payload / 'VERSION').write_text(version + '\n')
    return payload


def builder_config(project, work, version, resources, icons, payload, targets):
    """The whole electron-builder configuration; nothing is read from package.json's build section."""
    electron = json.loads((project / 'node_modules/electron/package.json').read_text())['version']
    names = artifact_names(version)
    # The window class Electron gives the app: package.json's desktopName without ".desktop" (the X11 class is
    # "qorqbot", not the product name), so desktops group its windows under this entry. first_open_check.sh checks it.
    entry = {'Name': PRODUCT, 'StartupWMClass': WINDOW_CLASS, 'Keywords': 'Qorqbot;agents;assistant;business;'}
    config = {
        'appId': APP_ID,
        'productName': PRODUCT,
        'electronVersion': electron,
        'directories': {'output': str(work / 'dist'), 'buildResources': str(resources)},
        'files': APP_FILES,
        # electron-builder asks for a project page and a version; the Debian version it derives turns the first '-'
        # of a pre-release into '~', so 1.3.0-preview.2 sorts before 1.3.0.
        'extraMetadata': {'version': version, 'homepage': HOMEPAGE, 'license': LICENSE_ID},
        'extraResources': [
            {'from': str(payload), 'to': 'qorqbot-payload'},
            {'from': str(NOTICES), 'to': 'THIRD-PARTY-NOTICES.md'},
        ],  # electron-builder itself keeps Electron's license and Chromium's notices next to the program
        'asar': True,
        'npmRebuild': False,
        'publish': None,
        'toolsets': {'appimage': APPIMAGE_TOOLSET},
        'linux': {
            'target': [{'target': TARGETS[name], 'arch': ['x64']} for name in targets],
            'icon': str(icons),
            'category': 'Office',
            'executableName': EXECUTABLE,
            # Name the menu entry after package.json's desktopName (qorqbot.desktop, the app id Electron reports on
            # Wayland and what main.js and the AppImage's own entry use), not after the program (qorqbot-desktop).
            'syncDesktopName': True,
            'synopsis': SYNOPSIS,
            'description': DESCRIPTION,
            'maintainer': MAINTAINER,
            'vendor': MAINTAINER,
            'desktop': {'entry': entry},
        },
        'appImage': {'artifactName': names['appimage'], 'executableArgs': []},
        'deb': {
            'artifactName': names['deb'],
            'packageName': PACKAGE,
            'priority': 'optional',
            'packageCategory': 'utils',
            'depends': DEB_DEPENDS,
            # Debian packages carry their license as /usr/share/doc/PACKAGE/copyright.
            'fpm': [f'{LICENSE}=/usr/share/doc/{PACKAGE}/copyright'],
        },
    }
    return config


def package(project, work, config, targets):
    """Run electron-builder; return {target: file} for what it made."""
    path = work / 'electron-builder.json'
    path.write_text(json.dumps(config, indent=2) + '\n')
    run([project / 'node_modules/.bin/electron-builder', '--linux', *(TARGETS[name] for name in targets), '--x64',
         '--publish', 'never', '--config', path], cwd=project)
    dist = Path(config['directories']['output'])
    wanted = {name: dist / artifact_names(config['extraMetadata']['version'])[name] for name in targets}
    missing = [str(file.name) for file in wanted.values() if not file.is_file()]
    if missing:
        raise RuntimeError('electron-builder did not produce ' + ', '.join(missing))
    return wanted


def write_checksums(output, files):
    lines = ''.join(f'{sha256(file)}  {file.name}\n' for file in sorted(files, key=lambda f: f.name))
    (output / 'SHA256SUMS').write_text(lines)
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--archive', type=Path, required=True, help='exact qorqbot-VERSION-ubuntu24-x86_64.tar.gz')
    parser.add_argument('--output', type=Path, required=True, help='new or empty output directory')
    parser.add_argument('--targets', default='appimage,deb', help='appimage, deb or both, separated by a comma (default: both)')
    parser.add_argument('--allow-system-tools', action='store_true',
                        help="accept an archive that is not self-contained (it then uses the computer's own Python 3.12, git and tmux)")
    args = parser.parse_args(argv)
    try:
        targets = parse_targets(args.targets)
    except ValueError as error:
        parser.error(str(error))
    archive = args.archive.resolve()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error('Output directory must be new or empty')
    if sys.platform != 'linux' or platform.machine() != 'x86_64':
        parser.error('Build the Linux app on Linux x86_64')
    payload = read_payload(archive)
    version = archive_version(archive, payload)
    if not self_contained(payload) and not args.allow_system_tools:
        parser.error('The archive is not self-contained (built without --bundled-tools): the app would need the '
                     "computer's Python 3.12, git and tmux. Pass --allow-system-tools to build it anyway.")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='qorqbot-linux-app-') as temporary:
        work = Path(temporary)
        project = stage_electron(work)
        resources, icons = render_icons(work, project)
        stage_icons(project, icons)
        payload_dir = stage_payload(work, archive, version)
        built = package(project, work, builder_config(project, work, version, resources, icons, payload_dir, targets), targets)
        files = []
        for file in built.values():
            shutil.move(file, output / file.name)
            files.append(output / file.name)
    write_checksums(output, files)
    print(json.dumps({'files': {name: str(output / file.name) for name, file in built.items()},
                      'sha256': {file.name: sha256(file) for file in files}, 'version': version,
                      'self_contained': self_contained(payload)}, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, tarfile.TarError, subprocess.CalledProcessError) as error:
        print('build_linux_app: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
