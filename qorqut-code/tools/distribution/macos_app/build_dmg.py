"""Wrap an exact macos14-arm64 native archive in the Qorqbot desktop app and a DMG.

Build tooling only; runs on macOS with Node.js/npm. The app is an Electron shell
(``electron/``) that installs, runs and shows the local Qorqbot server in its own
window. It carries the archive unchanged in ``Contents/Resources/qorqbot-payload``
and does not bundle Python, git or tmux.

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
import plistlib
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
BUNDLE_ID = 'com.49agents.qorqbot'
ARCHIVE_NAME = re.compile(r'^qorqbot-(?P<version>[0-9A-Za-z.+-]+)-macos14-arm64\.tar\.gz$')
ICON_SIZES = (16, 32, 128, 256, 512)
# Copied into the staged Electron project; tests, local builds and installed
# modules are never packaged from the source tree.
ELECTRON_SOURCES = ('package.json', 'package-lock.json', 'main.js', 'engine.js', 'preload.js', 'ui')


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


def package_app(project, version, icns, work):
    extra_info = work / 'extra-info.plist'
    with open(extra_info, 'wb') as handle:
        plistlib.dump({
            'LSMinimumSystemVersion': '14.0',
            'LSArchitecturePriority': ['arm64'],
            'CFBundleDisplayName': 'Qorqbot',
            'NSHumanReadableCopyright': 'Copyright 49 Agents LLC. PolyForm Noncommercial License 1.0.0.',
        }, handle)
    out = work / 'packaged'
    numeric = bundle_version(version)
    run([project / 'node_modules/.bin/electron-packager', project, 'Qorqbot',
         '--platform=darwin', '--arch=arm64', '--out', out, '--icon', icns,
         '--app-bundle-id', BUNDLE_ID, '--app-version', numeric, '--build-version', numeric,
         '--extend-info', extra_info, '--app-category-type', 'public.app-category.productivity',
         '--asar', '--prune=true', '--ignore', r'^/(test|out)(/|$)', '--quiet'], cwd=project)
    built = out / 'Qorqbot-darwin-arm64'
    return built / 'Qorqbot.app', built


def add_payload(app, archive, version, built):
    resources = app / 'Contents/Resources'
    payload = resources / 'qorqbot-payload'
    payload.mkdir()
    shutil.copyfile(archive, payload / archive.name)
    (payload / 'ARCHIVE_NAME').write_text(archive.name + '\n')
    (payload / 'ARCHIVE_SHA256').write_text(sha256(archive) + '\n')
    (payload / 'VERSION').write_text(version + '\n')
    # Electron's own license and Chromium's third-party notices travel inside the app.
    shutil.copyfile(built / 'LICENSE', resources / 'LICENSE.electron.txt')
    shutil.copyfile(built / 'LICENSES.chromium.html', resources / 'LICENSES.chromium.html')
    shutil.copyfile(HERE / 'THIRD-PARTY-NOTICES.md', resources / 'THIRD-PARTY-NOTICES.md')


def sign(app, identity):
    argv = ['/usr/bin/codesign', '--force', '--deep', '--sign', identity]
    if identity != '-':
        argv += ['--options', 'runtime', '--timestamp', '--entitlements', ENTITLEMENTS]
    run(argv + [app])
    run(['/usr/bin/codesign', '--verify', '--deep', '--strict', '--verbose=1', app])


def build_dmg(app_parent, volume, dmg):
    os.symlink('/Applications', app_parent / 'Applications')
    run(['/usr/bin/hdiutil', 'create', '-quiet', '-volname', volume, '-srcfolder', app_parent,
         '-fs', 'HFS+', '-format', 'UDZO', '-imagekey', 'zlib-level=9', dmg])
    run(['/usr/bin/hdiutil', 'verify', '-quiet', dmg])


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
        icns = build_icon(work)
        project = stage_electron(work)
        app, built = package_app(project, version, icns, work)
        add_payload(app, archive, version, built)
        sign(app, args.identity)
        stage = work / 'stage'
        stage.mkdir()
        shutil.move(app, stage / 'Qorqbot.app')
        build_dmg(stage, 'Qorqbot', dmg)
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
