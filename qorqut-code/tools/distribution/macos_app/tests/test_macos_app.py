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
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
APP_TOOLS = HERE.parent
sys.path.insert(0, str(APP_TOOLS))
import build_dmg  # noqa: E402


def make_archive(directory, version, arch='arm64', platform_name=None):
    """A minimal archive named for ARCH; platform_name is what its PAYLOAD.json records (native_build.py records one)."""
    archive = Path(directory) / f'qorqbot-{version}-macos14-{arch}.tar.gz'
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
        payload = {'version': version, **({'platform': platform_name} if platform_name else {})}
        add('qorqbot/runtime/PAYLOAD.json', json.dumps(payload).encode())
    return archive


def host_arch():
    return 'arm64' if platform.machine().lower() in ('arm64', 'aarch64') else 'x86_64'


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

    def test_archive_names_give_version_and_chip_for_both_macs(self):
        self.assertEqual(build_dmg.parse_archive_name('qorqbot-1.3.0-macos14-arm64.tar.gz'), ('1.3.0', 'arm64'))
        self.assertEqual(build_dmg.parse_archive_name('qorqbot-1.3.0-macos14-x86_64.tar.gz'), ('1.3.0', 'x86_64'))
        # A version may itself contain hyphens and chip-like words.
        self.assertEqual(build_dmg.parse_archive_name('qorqbot-1.3.0-local.t2026.g1ab-macos14-x86_64.tar.gz'),
                         ('1.3.0-local.t2026.g1ab', 'x86_64'))
        for name in ('qorqbot-1.3.0-macos14-amd64.tar.gz', 'qorqbot-1.3.0-macos13-arm64.tar.gz',
                     'qorqbot-1.3.0-macos14-x86_64.zip', 'qorqbot-1.3.0-ubuntu24-x86_64.tar.gz',
                     'qorqbot--macos14-arm64.tar.gz'):
            with self.assertRaisesRegex(ValueError, 'macos14-x86_64'):
                build_dmg.parse_archive_name(name)

    def test_archive_info_reads_the_chip_for_each_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            for arch in ('arm64', 'x86_64'):
                archive = make_archive(directory, '1.2.3', arch, f'macos14-{arch}')
                self.assertEqual(build_dmg.archive_info(archive), ('1.2.3', arch))
                self.assertEqual(build_dmg.archive_version(archive), '1.2.3')
            # An archive without a recorded platform (an older or hand-made one) is accepted by its name.
            self.assertEqual(build_dmg.archive_info(make_archive(directory, '1.2.4', 'x86_64')), ('1.2.4', 'x86_64'))

    def test_archive_built_for_the_other_chip_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = make_archive(directory, '1.2.3', 'x86_64', 'macos14-arm64')
            with self.assertRaisesRegex(ValueError, 'macos14-x86_64 but its payload says macos14-arm64'):
                build_dmg.archive_info(archive)

    def test_disk_image_names_keep_arm64_and_name_the_chip_for_intel(self):
        self.assertEqual(build_dmg.dmg_name('1.3.0-test.1', 'arm64'), 'Qorqbot-1.3.0-test.1-macos14-arm64.dmg')
        self.assertEqual(build_dmg.dmg_name('1.3.0-test.1', 'x86_64'), 'Qorqbot-1.3.0-test.1-macos14-x86_64.dmg')

    def builder_config(self, directory, arch=None, identity='-'):
        base = Path(directory)
        project = base / 'electron'
        (project / 'node_modules/electron').mkdir(parents=True, exist_ok=True)
        (project / 'node_modules/electron/package.json').write_text('{"version": "44.10.2"}')
        (base / 'build').mkdir(exist_ok=True)
        args = (project, base, '1.3.0-test.1', base / 'build/AppIcon.icns', base / 'build/background.png',
                base / 'qorqbot-payload', identity)
        return build_dmg.builder_config(*args) if arch is None else build_dmg.builder_config(*args, arch)

    def test_builder_config_is_unchanged_for_arm64_and_follows_the_chip_for_intel(self):
        with tempfile.TemporaryDirectory() as directory:
            default = self.builder_config(directory)
            self.assertEqual(default, self.builder_config(directory, 'arm64'))
            self.assertEqual(default['mac']['target'], [{'target': 'dmg', 'arch': ['arm64']}])
            self.assertEqual(default['mac']['extendInfo']['LSArchitecturePriority'], ['arm64'])
            self.assertEqual(default['dmg']['artifactName'], 'Qorqbot-1.3.0-test.1-macos14-arm64.dmg')
            intel = self.builder_config(directory, 'x86_64')
            self.assertEqual(intel['mac']['target'], [{'target': 'dmg', 'arch': ['x64']}])
            self.assertEqual(intel['mac']['extendInfo']['LSArchitecturePriority'], ['x86_64'])
            self.assertEqual(intel['dmg']['artifactName'], 'Qorqbot-1.3.0-test.1-macos14-x86_64.dmg')
            # Nothing else depends on the chip.
            for config in (default, intel):
                config['mac']['target'] = config['mac']['extendInfo']['LSArchitecturePriority'] = None
                config['dmg']['artifactName'] = None
            self.assertEqual(default, intel)

    def test_electron_builder_is_asked_for_the_archives_chip(self):
        for arch, flag in (('arm64', '--arm64'), ('x86_64', '--x64')):
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                calls = []

                def fake_run(argv, **kwargs):
                    argv = [str(a) for a in argv]
                    calls.append(argv)
                    if argv[0].endswith('electron-builder'):
                        (base / 'dist/mac').mkdir(parents=True)
                        (base / 'dist/mac/Qorqbot.app').mkdir()
                        (base / 'dist/Qorqbot.dmg').write_text('x')

                config = {'directories': {'output': str(base / 'dist')}}
                with mock.patch.object(build_dmg, 'run', fake_run):
                    build_dmg.package(base / 'electron', base, config, arch)
                builder = calls[0]
                self.assertEqual(builder[1:5], ['--mac', 'dmg', flag, '--publish'])
                self.assertEqual({'--arm64', '--x64'} & set(builder), {flag})

    def test_tray_icon_is_the_mark_with_its_white_parts_cut_out(self):
        svg = build_dmg.tray_svg(build_dmg.MARK.read_text())
        root = ET.fromstring(svg)
        ns = '{http://www.w3.org/2000/svg}'
        box = [float(n) for n in root.get('viewBox').split()]
        self.assertEqual(box[2], box[3], 'the menu bar icon is square')
        mask = root.find(f'{ns}defs/{ns}mask')
        self.assertIsNotNone(mask)
        painted = root.findall(f'{ns}rect')
        self.assertEqual([(r.get('fill'), r.get('mask')) for r in painted], [('#000000', 'url(#body)')])
        self.assertNotIn('<title', svg)
        # Inside the mask the mark's colours are swapped: its body shows (white), its eyes and mustache are holes (black).
        mark = ET.fromstring(build_dmg.MARK.read_text())
        mark_fills = sorted(e.get('fill') for e in mark.iter() if e.get('fill'))
        mask_fills = sorted(e.get('fill') for e in mask.iter() if e.get('fill'))
        swap = {'#000000': '#ffffff', '#ffffff': '#000000'}
        self.assertEqual(mask_fills, sorted(swap[fill] for fill in mark_fills))
        self.assertIn('#ffffff', mask_fills)

    def test_every_packaged_module_is_listed_for_staging_and_packing(self):
        modules = sorted(path.name for path in build_dmg.ELECTRON.glob('*.js'))
        self.assertIn('tray-menu.js', modules)
        self.assertIn('badge.js', modules)
        for name in modules:
            self.assertIn(name, build_dmg.ELECTRON_SOURCES)
            self.assertIn(name, build_dmg.PACKAGED_FILES)
        for name in ('package.json', 'ui/**/*'):
            self.assertIn(name, build_dmg.PACKAGED_FILES)
        # Whatever the app's own modules load from each other must be among them.
        for name in modules:
            for needed in re.findall(r"require\('\./([\w-]+)'\)", (build_dmg.ELECTRON / name).read_text()):
                self.assertIn(needed + '.js', build_dmg.PACKAGED_FILES, f'{name} requires ./{needed}')

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


@unittest.skipUnless(shutil.which('rsvg-convert'), 'rsvg-convert (librsvg)')
class TrayIconRenderTests(unittest.TestCase):
    def test_both_menu_bar_icons_are_rendered_at_one_and_two_times_size(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'out').mkdir()
            icons = build_dmg.build_tray_icons(base, base / 'out')
            self.assertEqual([icon.name for icon in icons], ['trayTemplate.png', 'trayTemplate@2x.png'])
            for icon, pixels in zip(icons, (16, 32)):
                header = icon.read_bytes()[:26]
                self.assertEqual(header[:8], b'\x89PNG\r\n\x1a\n')
                self.assertEqual(struct.unpack('>II', header[16:24]), (pixels, pixels))
                self.assertEqual(header[25], 6, 'RGBA, so the template image keeps its transparency')


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('rsvg-convert') and shutil.which('npm'),
                     'macOS with rsvg-convert and npm')
class BuilderIntegrationTests(unittest.TestCase):
    def test_build_produces_signed_electron_app_inside_dmg(self):
        arch = host_arch()   # the app is built for the chip of the Mac that builds it
        with tempfile.TemporaryDirectory(prefix='qbd-') as directory:
            base = Path(directory)
            archive = make_archive(base, '1.3.0-test.1', arch, f'macos14-{arch}')
            out = base / 'out'
            subprocess.run([sys.executable, APP_TOOLS / 'build_dmg.py', '--archive', archive, '--output', out],
                           check=True, capture_output=True)
            dmg = out / build_dmg.dmg_name('1.3.0-test.1', arch)   # Qorqbot-1.3.0-test.1-macos14-arm64.dmg on Apple silicon
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
                self.assertEqual(info['LSArchitecturePriority'], [arch])
                executable = subprocess.run(['/usr/bin/lipo', '-archs', app / 'Contents/MacOS/Qorqbot'],
                                            check=True, capture_output=True, text=True).stdout.split()
                self.assertEqual(executable, [arch])
                resources = app / 'Contents/Resources'
                payload = resources / 'qorqbot-payload'
                self.assertEqual(build_dmg.sha256(payload / archive.name), build_dmg.sha256(archive))
                self.assertEqual((payload / 'ARCHIVE_SHA256').read_text().strip(), build_dmg.sha256(archive))
                self.assertEqual((payload / 'VERSION').read_text().strip(), '1.3.0-test.1')
                self.assertTrue((resources / 'app.asar').is_file())
                self.assertTrue((resources / 'LICENSES.chromium.html').is_file())
                self.assertTrue((resources / 'LICENSE.electron.txt').is_file())
                self.assertTrue((resources / 'THIRD-PARTY-NOTICES.md').is_file())
                icon = info['CFBundleIconFile']
                icon = icon if icon.endswith('.icns') else icon + '.icns'
                self.assertGreater((resources / icon).stat().st_size, 10_000)
                asar = APP_TOOLS / 'electron/node_modules/.bin/asar'
                listing = subprocess.run([asar, 'list', resources / 'app.asar'],
                                         check=True, capture_output=True, text=True).stdout.split()
                for expected in ('/main.js', '/engine.js', '/placement.js', '/preload.js', '/tray-menu.js', '/badge.js',
                                 '/ui/status.html', '/ui/help.html', '/ui/start.html', '/ui/start.js', '/ui/mark.svg',
                                 '/ui/trayTemplate.png', '/ui/trayTemplate@2x.png'):
                    self.assertIn(expected, listing)
                self.assertIn('/node_modules/semver/LICENSE', listing)
                self.assertFalse([name for name in listing if name.startswith('/test') or 'electron-builder' in name or name.startswith('/node_modules/electron/')])
                subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', app], check=True)
            finally:
                subprocess.run(['/usr/bin/hdiutil', 'detach', '-quiet', mount], check=True)


if __name__ == '__main__':
    unittest.main()
