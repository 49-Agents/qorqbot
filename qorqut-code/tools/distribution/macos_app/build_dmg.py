"""Wrap an exact macos14-arm64 native archive in the Qorqbot desktop app and a DMG.

Build tooling only; runs on macOS with Node.js/npm. The app is an Electron shell
(``electron/``) that installs, runs and shows the local Qorqbot server in its own
window. electron-builder (pinned in ``electron/package-lock.json``) packages and
signs the app and lays out the disk image window; this script prepares its
inputs (icon, window background, the archive) and checks the result. It carries the archive unchanged in ``Contents/Resources/qorqbot-payload``;
a self-contained archive brings its own Python, git and tmux, so the app adds none.

Signing: ``--identity -`` (default) applies an ad-hoc signature. Downloaded
copies then show the Gatekeeper prompt once; users open it through System
Settings > Privacy & Security > Open Anyway. A Developer ID identity plus
``--notary-profile`` (an ``xcrun notarytool store-credentials`` profile) signs
with the hardened runtime, notarizes and staples the DMG so it opens without
that prompt (untested here).

    python3 tools/distribution/macos_app/build_dmg.py \
        --archive /abs/qorqbot-VERSION-macos14-arm64.tar.gz --output /abs/dmg-out
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
ELECTRON = HERE / 'electron'
ENTITLEMENTS = HERE / 'entitlements.plist'
MARK = HERE.parents[2] / 'biz-dashboard/css/brand/qorqbot-mark.svg'
BACKGROUND = HERE / 'dmg-background.svg'
# The disk image window, in points; the background is rendered at this size and at twice it.
WINDOW = (660, 420)
ICON_ROW = 190
BUNDLE_ID = 'com.49agents.qorqbot'
ARCHIVE_NAME = re.compile(r'^qorqbot-(?P<version>[0-9A-Za-z.+-]+)-macos14-arm64\.tar\.gz$')
ICON_SIZES = (16, 32, 128, 256, 512)
# Copied into the staged Electron project; tests, local builds and installed
# modules are never packaged from the source tree.
ELECTRON_SOURCES = ('package.json', 'package-lock.json', 'main.js', 'engine.js', 'placement.js', 'platform.js', 'preload.js', 'ui')


def run(argv, **kwargs):
    subprocess.run([str(a) for a in argv], check=True, **kwargs)


def sha256(path):
    with open(path, 'rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def archive_version(archive):
    """The version declared inside the archive must match its file name."""
    match = ARCHIVE_NAME.match(archive.name)
    if not match:
        raise ValueError('Expected a qorqbot-VERSION-macos14-arm64.tar.gz archive, got ' + archive.name)
    with tarfile.open(archive) as package:
        member = package.extractfile('qorqbot/runtime/PAYLOAD.json')
        if member is None:
            raise ValueError('Archive has no qorqbot/runtime/PAYLOAD.json')
        payload = json.load(member)
    if payload.get('version') != match['version']:
        raise ValueError(f"Archive name says {match['version']} but its payload says {payload.get('version')}")
    return match['version']


def bundle_version(version):
    """CFBundle versions must be dotted integers; keep the leading numeric part."""
    numeric = re.match(r'^(\d+)(?:\.(\d+))?(?:\.(\d+))?', version)
    if not numeric:
        raise ValueError('Version has no leading numeric part: ' + version)
    return '.'.join(part for part in numeric.groups() if part is not None)


def icon_svg(mark_text):
    """Black mark on a light rounded square so it stays visible on dark Docks."""
    inner = re.sub(r'^.*?<svg[^>]*>', '', mark_text, count=1, flags=re.S)
    inner = re.sub(r'</svg>\s*$', '', inner.strip())
    inner = re.sub(r'<title[^>]*>.*?</title>|<desc[^>]*>.*?</desc>', '', inner, flags=re.S)
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">'
            '<rect x="100" y="100" width="824" height="824" rx="185" fill="#f4f1ea"/>'
            '<svg x="152" y="132" width="720" height="720" viewBox="0 0 160 160">' + inner + '</svg></svg>')


def build_icon(work):
    converter = shutil.which('rsvg-convert')
    if not converter:
        raise RuntimeError('rsvg-convert is required to render the app icon: brew install librsvg')
    source = work / 'icon.svg'
    source.write_text(icon_svg(MARK.read_text()))
    iconset = work / 'AppIcon.iconset'
    iconset.mkdir()
    for size in ICON_SIZES:
        for scale in (1, 2):
            name = f'icon_{size}x{size}' + ('@2x' if scale == 2 else '') + '.png'
            pixels = size * scale
            run([converter, '-w', pixels, '-h', pixels, source, '-o', iconset / name])
    icns = work / 'AppIcon.icns'
    run(['/usr/bin/iconutil', '-c', 'icns', iconset, '-o', icns])
    return icns


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


def build_background(resources):
    """The picture behind the app and Applications icons, for ordinary and high-density screens."""
    converter = shutil.which('rsvg-convert')
    if not converter:
        raise RuntimeError('rsvg-convert is required to render the disk image background: brew install librsvg')
    for scale, name in ((1, 'background.png'), (2, 'background@2x.png')):
        run([converter, '-w', WINDOW[0] * scale, '-h', WINDOW[1] * scale, BACKGROUND, '-o', resources / name])
    return resources / 'background.png'


def stage_payload(work, archive, version):
    payload = work / 'qorqbot-payload'
    payload.mkdir()
    shutil.copyfile(archive, payload / archive.name)
    (payload / 'ARCHIVE_NAME').write_text(archive.name + '\n')
    (payload / 'ARCHIVE_SHA256').write_text(sha256(archive) + '\n')
    (payload / 'VERSION').write_text(version + '\n')
    return payload


def builder_config(project, work, version, icns, background, payload, identity):
    """The whole electron-builder configuration; nothing is read from package.json's build section."""
    numeric = bundle_version(version)
    signed = identity != '-'
    electron = json.loads((project / 'node_modules/electron/package.json').read_text())['version']
    mac = {
        'target': [{'target': 'dmg', 'arch': ['arm64']}],
        'icon': str(icns),
        'category': 'public.app-category.productivity',
        'minimumSystemVersion': '14.0',
        'bundleShortVersion': numeric,
        'bundleVersion': numeric,
        # electron-builder takes the certificate's name without its kind; '-' is an ad-hoc signature.
        'identity': re.sub(r'^Developer ID Application:\s*', '', identity),
        'hardenedRuntime': signed,
        'gatekeeperAssess': False,
        # The disk image is notarized and stapled below, after it is signed.
        'notarize': False,
        'extendInfo': {
            'LSArchitecturePriority': ['arm64'],
            'CFBundleDisplayName': 'Qorqbot',
            'NSHumanReadableCopyright': 'Copyright 49 Agents LLC. PolyForm Noncommercial License 1.0.0.',
        },
    }
    if signed:
        mac.update({'entitlements': str(ENTITLEMENTS), 'entitlementsInherit': str(ENTITLEMENTS)})
    return {
        'appId': BUNDLE_ID,
        'productName': 'Qorqbot',
        'electronVersion': electron,
        'directories': {'output': str(work / 'dist'), 'buildResources': str(icns.parent)},
        'files': ['package.json', 'main.js', 'engine.js', 'placement.js', 'platform.js', 'preload.js', 'ui/**/*'],
        'extraMetadata': {'version': version},
        'extraResources': [
            {'from': str(payload), 'to': 'qorqbot-payload'},
            {'from': str(HERE / 'THIRD-PARTY-NOTICES.md'), 'to': 'THIRD-PARTY-NOTICES.md'},
        ],  # electron-builder itself keeps Electron's license and Chromium's notices in Contents/Resources
        'asar': True,
        'npmRebuild': False,
        'publish': None,
        'mac': mac,
        'dmg': {
            'title': 'Qorqbot',
            'artifactName': f'Qorqbot-{version}-macos14-arm64.dmg',
            'background': str(background),
            'window': {'width': WINDOW[0], 'height': WINDOW[1]},
            'iconSize': 112,
            'iconTextSize': 13,
            'contents': [{'x': 180, 'y': ICON_ROW, 'type': 'file'},
                         {'x': 480, 'y': ICON_ROW, 'type': 'link', 'path': '/Applications'}],
            'format': 'UDZO',
            'writeUpdateInfo': False,
            'sign': False,
        },
    }


def package(project, work, config):
    """Run electron-builder; return the app it packaged and the disk image it made."""
    path = work / 'electron-builder.json'
    path.write_text(json.dumps(config, indent=2) + '\n')
    run([project / 'node_modules/.bin/electron-builder', '--mac', 'dmg', '--arm64', '--publish', 'never',
         '--config', path], cwd=project)
    dist = Path(config['directories']['output'])
    apps, images = sorted(dist.glob('mac*/Qorqbot.app')), sorted(dist.glob('*.dmg'))
    if len(apps) != 1 or len(images) != 1:
        raise RuntimeError(f'Expected one app and one disk image from electron-builder, found {len(apps)} and {len(images)}')
    run(['/usr/bin/codesign', '--verify', '--deep', '--strict', '--verbose=1', apps[0]])
    run(['/usr/bin/hdiutil', 'verify', '-quiet', images[0]])
    return apps[0], images[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--archive', type=Path, required=True, help='exact qorqbot-VERSION-macos14-arm64.tar.gz')
    parser.add_argument('--output', type=Path, required=True, help='new or empty output directory')
    parser.add_argument('--identity', default='-', help="codesign identity; '-' (default) is ad-hoc")
    parser.add_argument('--notary-profile', help='notarytool keychain profile; requires a Developer ID identity')
    args = parser.parse_args(argv)
    if args.notary_profile and args.identity == '-':
        parser.error('Notarization needs a Developer ID Application identity, not ad-hoc signing')
    archive = args.archive.resolve()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error('Output directory must be new or empty')
    if sys.platform != 'darwin':
        parser.error('Build the Mac app on macOS')
    version = archive_version(archive)
    output.mkdir(parents=True, exist_ok=True)
    dmg = output / f'Qorqbot-{version}-macos14-arm64.dmg'
    with tempfile.TemporaryDirectory(prefix='qorqbot-dmg-') as temporary:
        work = Path(temporary)
        resources = work / 'build'
        resources.mkdir()
        icns = build_icon(resources)
        background = build_background(resources)
        project = stage_electron(work)
        payload = stage_payload(work, archive, version)
        _app, image = package(project, work, builder_config(project, work, version, icns, background, payload, args.identity))
        shutil.move(image, dmg)
    if args.identity != '-':
        run(['/usr/bin/codesign', '--force', '--sign', args.identity, '--timestamp', dmg])
    if args.notary_profile:
        run(['/usr/bin/xcrun', 'notarytool', 'submit', dmg, '--keychain-profile', args.notary_profile, '--wait'])
        run(['/usr/bin/xcrun', 'stapler', 'staple', dmg])
    digest = sha256(dmg)
    (output / (dmg.name + '.sha256')).write_text(f'{digest}  {dmg.name}\n')
    print(json.dumps({'dmg': str(dmg), 'sha256': digest, 'version': version,
                      'signing': 'ad-hoc' if args.identity == '-' else args.identity,
                      'notarized': bool(args.notary_profile)}, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, tarfile.TarError, subprocess.CalledProcessError) as error:
        print('build_dmg: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
