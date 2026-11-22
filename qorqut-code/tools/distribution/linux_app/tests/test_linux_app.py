"""Qorqbot desktop app builder tests (Linux x86_64 for the build; the unit tests run anywhere).

    python3.12 -m unittest discover -s tools/distribution/linux_app/tests -v

The app's install/start/stop engine has its own Node tests:
    cd tools/distribution/linux_app/electron && npm test

The full build (BuilderIntegrationTests) downloads Electron and takes a few minutes; it runs only when
QORQBOT_LINUX_APP_BUILD=1 is set:
    QORQBOT_LINUX_APP_BUILD=1 python3.12 -m unittest discover -s tools/distribution/linux_app/tests -v
"""
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
APP_TOOLS = HERE.parent
sys.path.insert(0, str(APP_TOOLS))
import build_linux_app  # noqa: E402

LINUX_X64 = sys.platform == 'linux' and platform.machine() == 'x86_64'


def make_archive(directory, version, bundled=None, target='ubuntu24-x86_64'):
    archive = Path(directory) / f'qorqbot-{version}-ubuntu24-x86_64.tar.gz'
    payload = {'format': 1, 'version': version, 'platform': target}
    if bundled:
        payload['bundled'] = bundled
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
        add('qorqbot/runtime/PAYLOAD.json', json.dumps(payload).encode())
    return archive


def fake_project(directory):
    """Just enough of an installed Electron project for builder_config."""
    project = Path(directory) / 'electron'
    (project / 'node_modules/electron').mkdir(parents=True)
    (project / 'node_modules/electron/package.json').write_text(json.dumps({'version': '44.10.2'}))
    return project


class ArchiveTests(unittest.TestCase):
    def test_archive_version_must_match_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = make_archive(directory, '1.2.3')
            self.assertEqual(build_linux_app.archive_version(archive), '1.2.3')
            renamed = archive.with_name('qorqbot-9.9.9-ubuntu24-x86_64.tar.gz')
            archive.rename(renamed)
            with self.assertRaisesRegex(ValueError, 'payload says 1.2.3'):
                build_linux_app.archive_version(renamed)

    def test_mac_archive_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'ubuntu24-x86_64'):
            build_linux_app.archive_version(Path('/x/qorqbot-1.0.0-macos14-arm64.tar.gz'), {'version': '1.0.0'})

    def test_payload_for_another_platform_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = make_archive(directory, '1.2.3', target='macos14-arm64')
            with self.assertRaisesRegex(ValueError, 'macos14-arm64'):
                build_linux_app.archive_version(archive)

    def test_version_must_start_with_a_digit_for_the_deb(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = make_archive(directory, 'local-1')
            with self.assertRaisesRegex(ValueError, 'start with a digit'):
                build_linux_app.archive_version(archive)

    def test_prerelease_versions_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            for version in ('1.3.0-preview.2', '1.3.0-check.t20261118224956.g2f6f9bd7013a', '2.0.0+build.5'):
                self.assertEqual(build_linux_app.archive_version(make_archive(directory, version)), version)

    def test_archive_without_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'qorqbot-1.0.0-ubuntu24-x86_64.tar.gz'
            with tarfile.open(archive, 'w:gz') as package:
                info = tarfile.TarInfo('qorqbot/qorqbot')
                package.addfile(info, io.BytesIO(b''))
            with self.assertRaisesRegex(ValueError, 'no qorqbot/runtime/PAYLOAD.json'):
                build_linux_app.read_payload(archive)

    def test_self_contained_means_a_recorded_digest_for_the_shipped_trees(self):
        self.assertTrue(build_linux_app.self_contained({'bundled': {'venv': 'a' * 64, 'native': 'b' * 64}}))
        self.assertFalse(build_linux_app.self_contained({'version': '1.0.0'}))
        self.assertFalse(build_linux_app.self_contained({'bundled': {}}))

    def test_payload_is_read_from_the_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = make_archive(directory, '1.2.3', bundled={'venv': 'x', 'native': 'y'})
            self.assertTrue(build_linux_app.self_contained(build_linux_app.read_payload(archive)))


class NamesAndTargetsTests(unittest.TestCase):
    def test_artifact_names(self):
        self.assertEqual(build_linux_app.artifact_names('1.3.0-preview.2'),
                         {'appimage': 'Qorqbot-1.3.0-preview.2-x86_64.AppImage', 'deb': 'qorqbot_1.3.0-preview.2_amd64.deb'})

    def test_targets(self):
        self.assertEqual(build_linux_app.parse_targets('appimage,deb'), ['appimage', 'deb'])
        self.assertEqual(build_linux_app.parse_targets(' DEB , deb '), ['deb'])
        for bad in ('', 'rpm', 'appimage,snap'):
            with self.assertRaises(ValueError):
                build_linux_app.parse_targets(bad)

    def test_checksums_file_lists_every_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            files = []
            for name, data in (('b.deb', b'two'), ('a.AppImage', b'one')):
                (out / name).write_bytes(data)
                files.append(out / name)
            lines = build_linux_app.write_checksums(out, files).splitlines()
            self.assertEqual(lines, [f'{build_linux_app.sha256(out / name)}  {name}' for name in ('a.AppImage', 'b.deb')])
            self.assertEqual((out / 'SHA256SUMS').read_text().splitlines(), lines)


class SourceTests(unittest.TestCase):
    def test_icon_svg_embeds_mark_without_metadata(self):
        svg = build_linux_app.icon_svg(build_linux_app.MARK.read_text())
        self.assertTrue(svg.startswith('<svg') and svg.endswith('</svg></svg>'))
        self.assertNotIn('<title', svg)
        self.assertIn('fill="#000000"', svg)

    def test_every_packaged_source_exists(self):
        for name in build_linux_app.ELECTRON_SOURCES:
            self.assertTrue((build_linux_app.ELECTRON / name).exists(), name)
        for path in (build_linux_app.MARK, build_linux_app.ICON_SCRIPT, build_linux_app.NOTICES, build_linux_app.LICENSE):
            self.assertTrue(path.is_file(), path)

    def test_packaged_files_cover_the_staged_sources(self):
        packaged = set(build_linux_app.APP_FILES)
        for name in build_linux_app.ELECTRON_SOURCES:
            if name == 'package-lock.json':
                continue  # only used for the install
            self.assertTrue(name in packaged or f'{name}/**/*' in packaged, name)
        self.assertIn('icons/qorqbot.png', packaged)

    def test_main_script_requires_only_staged_files(self):
        """main.js must not require a file that the build neither copies nor packages."""
        import re
        staged = {name for name in build_linux_app.ELECTRON_SOURCES if name.endswith('.js')}
        for name in sorted(staged):
            text = (build_linux_app.ELECTRON / name).read_text()
            for local in re.findall(r"require\('\./([\w.-]+)'\)", text):
                self.assertIn(local if local.endswith('.js') else local + '.js', staged, f'{name} requires ./{local}')


class BuilderConfigTests(unittest.TestCase):
    def config(self, targets=('appimage', 'deb'), version='1.3.0-preview.2'):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        return build_linux_app.builder_config(fake_project(base), base, version, base / 'build', base / 'build/icons',
                                              base / 'qorqbot-payload', list(targets))

    def test_targets_follow_the_selection(self):
        both = self.config()['linux']['target']
        self.assertEqual([item['target'] for item in both], ['AppImage', 'deb'])
        self.assertTrue(all(item['arch'] == ['x64'] for item in both))
        self.assertEqual([item['target'] for item in self.config(['deb'])['linux']['target']], ['deb'])

    def test_identity_of_the_app(self):
        config = self.config()
        self.assertEqual(config['appId'], 'com.49agents.qorqbot')
        self.assertEqual(config['productName'], 'Qorqbot')
        self.assertEqual(config['electronVersion'], '44.10.2')
        self.assertEqual(config['extraMetadata']['version'], '1.3.0-preview.2')
        self.assertEqual(config['linux']['executableName'], 'qorqbot-desktop')
        # The menu entry is qorqbot.desktop (package.json's desktopName), not qorqbot-desktop.desktop.
        self.assertTrue(config['linux']['syncDesktopName'])
        self.assertEqual(config['linux']['category'], 'Office')
        self.assertEqual(config['linux']['maintainer'], '49 Agents LLC')
        self.assertEqual(config['linux']['desktop']['entry']['StartupWMClass'], 'qorqbot')
        self.assertEqual(config['linux']['desktop']['entry']['Name'], 'Qorqbot')

    def test_output_names(self):
        config = self.config()
        self.assertEqual(config['appImage']['artifactName'], 'Qorqbot-1.3.0-preview.2-x86_64.AppImage')
        self.assertEqual(config['deb']['artifactName'], 'qorqbot_1.3.0-preview.2_amd64.deb')
        self.assertEqual(config['deb']['packageName'], 'qorqbot')

    def test_deb_depends_name_both_ubuntu_24_04_and_older_libraries(self):
        depends = self.config()['deb']['depends']
        for expected in ('libgtk-3-0t64 | libgtk-3-0', 'libasound2t64 | libasound2', 'libatspi2.0-0t64 | libatspi2.0-0',
                         'libnss3', 'libgbm1', 'libnotify4', 'libxss1', 'xdg-utils'):
            self.assertIn(expected, depends)

    def test_payload_and_notices_ship_in_resources(self):
        resources = {item['to']: item['from'] for item in self.config()['extraResources']}
        self.assertTrue(resources['qorqbot-payload'].endswith('qorqbot-payload'))
        self.assertEqual(Path(resources['THIRD-PARTY-NOTICES.md']), build_linux_app.NOTICES)

    def test_license_ships_as_the_debian_copyright_file(self):
        self.assertEqual(self.config()['deb']['fpm'], [f'{build_linux_app.LICENSE}=/usr/share/doc/qorqbot/copyright'])

    def test_nothing_is_published_or_rebuilt(self):
        config = self.config()
        self.assertIsNone(config['publish'])
        self.assertFalse(config['npmRebuild'])
        self.assertTrue(config['asar'])

    def test_configuration_is_plain_json(self):
        json.dumps(self.config())


class PayloadStagingTests(unittest.TestCase):
    def test_payload_carries_the_archive_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = make_archive(base, '1.3.0-test.1')
            work = base / 'work'
            work.mkdir()
            payload = build_linux_app.stage_payload(work, archive, '1.3.0-test.1')
            digest = build_linux_app.sha256(archive)
            self.assertEqual(build_linux_app.sha256(payload / archive.name), digest)
            self.assertEqual((payload / 'ARCHIVE_SHA256').read_text(), digest + '\n')
            self.assertEqual((payload / 'ARCHIVE_NAME').read_text(), archive.name + '\n')
            self.assertEqual((payload / 'VERSION').read_text(), '1.3.0-test.1\n')


class CommandLineTests(unittest.TestCase):
    def run_builder(self, *args):
        return subprocess.run([sys.executable, APP_TOOLS / 'build_linux_app.py', *map(str, args)], capture_output=True, text=True)

    def test_output_must_be_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = make_archive(directory, '1.0.0')
            out = Path(directory) / 'out'
            out.mkdir()
            (out / 'existing').write_text('x')
            done = self.run_builder('--archive', archive, '--output', out)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn('new or empty', done.stderr)

    def test_unknown_target_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            done = self.run_builder('--archive', make_archive(directory, '1.0.0'), '--output', Path(directory) / 'out', '--targets', 'rpm')
            self.assertNotEqual(done.returncode, 0)
            self.assertIn('--targets', done.stderr)

    @unittest.skipUnless(LINUX_X64, 'the build runs on Linux x86_64')
    def test_archive_without_its_own_tools_is_refused_unless_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'out'
            done = self.run_builder('--archive', make_archive(directory, '1.0.0'), '--output', out)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn('not self-contained', done.stderr)
            self.assertIn('--allow-system-tools', done.stderr)
            self.assertFalse(out.exists())

    @unittest.skipUnless(LINUX_X64, 'the build runs on Linux x86_64')
    def test_mismatched_archive_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = make_archive(directory, '1.0.0', bundled={'venv': 'x', 'native': 'y'})
            renamed = archive.with_name('qorqbot-2.0.0-ubuntu24-x86_64.tar.gz')
            archive.rename(renamed)
            done = self.run_builder('--archive', renamed, '--output', Path(directory) / 'out')
            self.assertNotEqual(done.returncode, 0)
            self.assertIn('payload says 1.0.0', done.stderr)


@unittest.skipUnless(LINUX_X64 and shutil.which('npm') and shutil.which('dpkg-deb')
                     and os.environ.get('QORQBOT_LINUX_APP_BUILD') == '1',
                     'set QORQBOT_LINUX_APP_BUILD=1 on Linux x86_64 with npm and dpkg-deb')
class BuilderIntegrationTests(unittest.TestCase):
    def test_build_produces_an_appimage_and_a_deb(self):
        with tempfile.TemporaryDirectory(prefix='qla-') as directory:
            base = Path(directory)
            version = '1.3.0-test.1'
            archive = make_archive(base, version)
            out = base / 'out'
            subprocess.run([sys.executable, APP_TOOLS / 'build_linux_app.py', '--archive', archive, '--output', out,
                            '--allow-system-tools'], check=True, capture_output=True)
            appimage = out / f'Qorqbot-{version}-x86_64.AppImage'
            deb = out / f'qorqbot_{version}_amd64.deb'
            sums = dict(line.split('  ')[::-1] for line in (out / 'SHA256SUMS').read_text().splitlines())
            self.assertEqual(sums, {appimage.name: build_linux_app.sha256(appimage), deb.name: build_linux_app.sha256(deb)})
            digest = build_linux_app.sha256(archive)

            def control(field):
                return subprocess.run(['dpkg-deb', '-f', deb, field], check=True, capture_output=True, text=True).stdout.strip()
            self.assertEqual(control('Package'), 'qorqbot')
            self.assertEqual(control('Version'), '1.3.0~test.1')
            self.assertEqual(control('Architecture'), 'amd64')
            self.assertEqual(control('Maintainer'), '49 Agents LLC')
            self.assertIn('libgtk-3-0t64 | libgtk-3-0', control('Depends'))
            listing = subprocess.run(['dpkg-deb', '--contents', deb], check=True, capture_output=True, text=True).stdout
            for expected in ('./opt/Qorqbot/qorqbot-desktop', './opt/Qorqbot/resources/app.asar', './opt/Qorqbot/resources/apparmor-profile',
                             './opt/Qorqbot/resources/THIRD-PARTY-NOTICES.md', './opt/Qorqbot/LICENSE.electron.txt',
                             './opt/Qorqbot/LICENSES.chromium.html', './usr/share/applications/qorqbot.desktop',
                             './usr/share/doc/qorqbot/copyright', './usr/share/icons/hicolor/16x16/apps/qorqbot-desktop.png',
                             './usr/share/icons/hicolor/512x512/apps/qorqbot-desktop.png', archive.name):
                self.assertIn(expected, listing)
            scripts = base / 'control'
            subprocess.run(['dpkg-deb', '-e', deb, scripts], check=True)
            self.assertIn('/etc/apparmor.d/qorqbot-desktop', (scripts / 'postinst').read_text())
            self.assertIn('apparmor_parser --remove', (scripts / 'postrm').read_text())
            fakeroot = base / 'deb-root'
            subprocess.run(['dpkg-deb', '-x', deb, fakeroot], check=True)
            entry = (fakeroot / 'usr/share/applications/qorqbot.desktop').read_text()
            for line in ('Name=Qorqbot', 'Exec=/opt/Qorqbot/qorqbot-desktop %U', 'Icon=qorqbot-desktop', 'StartupWMClass=qorqbot', 'Categories=Office;'):
                self.assertIn(line, entry.splitlines())
            payload = fakeroot / 'opt/Qorqbot/resources/qorqbot-payload'
            self.assertEqual(build_linux_app.sha256(payload / archive.name), digest)
            self.assertEqual((payload / 'ARCHIVE_SHA256').read_text().strip(), digest)
            self.assertEqual((payload / 'VERSION').read_text().strip(), version)

            # The AppImage unpacks without FUSE and holds the same app.
            work = base / 'appimage'
            work.mkdir()
            subprocess.run([appimage, '--appimage-extract'], cwd=work, check=True, capture_output=True)
            root = work / 'squashfs-root'
            entry = (root / 'qorqbot.desktop').read_text().splitlines()
            for line in ('Name=Qorqbot', 'Icon=qorqbot-desktop', 'StartupWMClass=qorqbot', 'Categories=Office;'):
                self.assertIn(line, entry)
            self.assertEqual(build_linux_app.sha256(root / 'resources/qorqbot-payload' / archive.name), digest)
            self.assertTrue((root / 'resources/THIRD-PARTY-NOTICES.md').is_file())
            self.assertTrue((root / 'usr/share/icons/hicolor/256x256/apps/qorqbot-desktop.png').is_file())
            asar = APP_TOOLS / 'electron/node_modules/.bin/asar'
            if asar.exists():
                names = subprocess.run([asar, 'list', root / 'resources/app.asar'], check=True, capture_output=True, text=True).stdout.split()
                for expected in ('/main.js', '/engine.js', '/desktop.js', '/preload.js', '/ui/status.html', '/ui/mark.svg',
                                 '/icons/qorqbot.png', '/node_modules/semver/LICENSE'):
                    self.assertIn(expected, names)
                self.assertFalse([name for name in names if name.startswith('/test') or 'electron-builder' in name
                                  or name.startswith('/node_modules/electron/')])


if __name__ == '__main__':
    unittest.main()
