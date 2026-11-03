"""Qorqbot desktop app builder tests (macOS only).

    python3.12 -m unittest discover -s tools/distribution/macos_app/tests -v

The app's install/start/stop engine has its own Node tests:
    cd tools/distribution/macos_app/electron && npm test
"""
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
APP_TOOLS = HERE.parent
sys.path.insert(0, str(APP_TOOLS))
import build_dmg  # noqa: E402


def make_archive(directory, version):
    archive = Path(directory) / f'qorqbot-{version}-macos14-arm64.tar.gz'
    with tarfile.open(archive, 'w:gz') as package:
        def add(name, data, mode=0o644):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            package.addfile(info, io.BytesIO(data))
        for folder in ('qorqbot', 'qorqbot/runtime'):
            info = tarfile.TarInfo(folder)
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            package.addfile(info)
        add('qorqbot/qorqbot', b'#!/bin/sh\n', 0o755)
        add('qorqbot/runtime/PAYLOAD.json', json.dumps({'version': version}).encode())
    return archive


class BuilderUnitTests(unittest.TestCase):
    def test_archive_version_must_match_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = make_archive(directory, '1.2.3')
            self.assertEqual(build_dmg.archive_version(archive), '1.2.3')
            renamed = archive.with_name('qorqbot-9.9.9-macos14-arm64.tar.gz')
            archive.rename(renamed)
            with self.assertRaisesRegex(ValueError, 'payload says 1.2.3'):
                build_dmg.archive_version(renamed)

    def test_linux_archive_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'macos14-arm64'):
            build_dmg.archive_version(Path('/x/qorqbot-1.0.0-ubuntu24-x86_64.tar.gz'))

    def test_bundle_version_keeps_numeric_prefix(self):
        self.assertEqual(build_dmg.bundle_version('1.3.0-local.28958df6d358'), '1.3.0')
        self.assertEqual(build_dmg.bundle_version('2.1'), '2.1')
        with self.assertRaises(ValueError):
            build_dmg.bundle_version('local')

    def test_icon_svg_embeds_mark_without_metadata(self):
        svg = build_dmg.icon_svg(build_dmg.MARK.read_text())
        self.assertTrue(svg.startswith('<svg') and svg.endswith('</svg></svg>'))
        self.assertNotIn('<title', svg)
        self.assertIn('fill="#000000"', svg)

    def test_every_packaged_source_exists(self):
        for name in build_dmg.ELECTRON_SOURCES:
            self.assertTrue((build_dmg.ELECTRON / name).exists(), name)

    def test_output_must_be_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = make_archive(directory, '1.0.0')
            out = Path(directory) / 'out'
            out.mkdir()
            (out / 'existing').write_text('x')
            done = subprocess.run([sys.executable, APP_TOOLS / 'build_dmg.py', '--archive', archive, '--output', out],
                                  capture_output=True, text=True)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn('new or empty', done.stderr)


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('rsvg-convert') and shutil.which('npm'),
                     'macOS with rsvg-convert and npm')
class BuilderIntegrationTests(unittest.TestCase):
    def test_build_produces_signed_electron_app_inside_dmg(self):
        with tempfile.TemporaryDirectory(prefix='qbd-') as directory:
            base = Path(directory)
            archive = make_archive(base, '1.3.0-test.1')
            out = base / 'out'
            subprocess.run([sys.executable, APP_TOOLS / 'build_dmg.py', '--archive', archive, '--output', out],
                           check=True, capture_output=True)
            dmg = out / 'Qorqbot-1.3.0-test.1-macos14-arm64.dmg'
            recorded = (out / (dmg.name + '.sha256')).read_text().split()[0]
            self.assertEqual(recorded, build_dmg.sha256(dmg))
            mount = base / 'mnt'
            mount.mkdir()
            subprocess.run(['/usr/bin/hdiutil', 'attach', '-quiet', '-nobrowse', '-readonly',
                            '-mountpoint', mount, dmg], check=True)
            try:
                app = mount / 'Qorqbot.app'
                self.assertEqual(os.readlink(mount / 'Applications'), '/Applications')
                with open(app / 'Contents/Info.plist', 'rb') as handle:
                    info = plistlib.load(handle)
                self.assertEqual(info['CFBundleIdentifier'], 'com.49agents.qorqbot')
                self.assertEqual(info['CFBundleExecutable'], 'Qorqbot')
                self.assertEqual(info['CFBundleShortVersionString'], '1.3.0')
                self.assertEqual(info['LSMinimumSystemVersion'], '14.0')
                resources = app / 'Contents/Resources'
                payload = resources / 'qorqbot-payload'
                self.assertEqual(build_dmg.sha256(payload / archive.name), build_dmg.sha256(archive))
                self.assertEqual((payload / 'ARCHIVE_SHA256').read_text().strip(), build_dmg.sha256(archive))
                self.assertEqual((payload / 'VERSION').read_text().strip(), '1.3.0-test.1')
                self.assertTrue((resources / 'app.asar').is_file())
                self.assertTrue((resources / 'LICENSES.chromium.html').is_file())
                self.assertTrue((resources / 'LICENSE.electron.txt').is_file())
                icon = info['CFBundleIconFile']
                icon = icon if icon.endswith('.icns') else icon + '.icns'
                self.assertGreater((resources / icon).stat().st_size, 10_000)
                asar = APP_TOOLS / 'electron/node_modules/.bin/asar'
                listing = subprocess.run([asar, 'list', resources / 'app.asar'],
                                         check=True, capture_output=True, text=True).stdout.split()
                for expected in ('/main.js', '/engine.js', '/preload.js', '/ui/status.html', '/ui/help.html', '/ui/start.html', '/ui/start.js', '/ui/mark.svg'):
                    self.assertIn(expected, listing)
                self.assertFalse([name for name in listing if name.startswith('/test') or 'electron-packager' in name])
                subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', app], check=True)
            finally:
                subprocess.run(['/usr/bin/hdiutil', 'detach', '-quiet', mount], check=True)


if __name__ == '__main__':
    unittest.main()
