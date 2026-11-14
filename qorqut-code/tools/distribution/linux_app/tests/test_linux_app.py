"""Qorqbot Ubuntu desktop app builder tests (no npm, no downloads).

    python3.12 -m unittest discover -s tools/distribution/linux_app/tests -v
"""
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
APP_TOOLS = HERE.parent
sys.path.insert(0, str(APP_TOOLS))
import build_deb  # noqa: E402


def make_archive(directory, version):
    archive = Path(directory) / f'qorqbot-{version}-ubuntu24-x86_64.tar.gz'
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


def make_packaged(directory):
    packaged = Path(directory) / 'Qorqbot-linux-x64'
    (packaged / 'resources').mkdir(parents=True)
    (packaged / 'locales').mkdir()
    (packaged / 'Qorqbot').write_bytes(b'#!/bin/sh\n')
    (packaged / 'Qorqbot').chmod(0o700)
    (packaged / 'chrome-sandbox').write_bytes(b'sandbox')
    (packaged / 'chrome-sandbox').chmod(0o700)
    (packaged / 'resources/app.asar').write_bytes(b'asar')
    (packaged / 'locales/en-US.pak').write_bytes(b'pak')
    (packaged / 'LICENSE').write_text('MIT\n')
    (packaged / 'LICENSES.chromium.html').write_text('<html></html>\n')
    return packaged


class BuilderUnitTests(unittest.TestCase):
    def test_archive_version_must_match_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = make_archive(directory, '1.2.3')
            self.assertEqual(build_deb.archive_version(archive), '1.2.3')
            renamed = archive.with_name('qorqbot-9.9.9-ubuntu24-x86_64.tar.gz')
            archive.rename(renamed)
            with self.assertRaisesRegex(ValueError, 'payload says 1.2.3'):
                build_deb.archive_version(renamed)

    def test_macos_archive_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'ubuntu24-x86_64'):
            build_deb.archive_version(Path('/x/qorqbot-1.0.0-macos14-arm64.tar.gz'))

    def test_app_version_keeps_numeric_prefix(self):
        self.assertEqual(build_deb.app_version('1.3.0-check.t20261104193012.gfad178051f1a'), '1.3.0')
        self.assertEqual(build_deb.app_version('2.1'), '2.1')
        with self.assertRaises(ValueError):
            build_deb.app_version('local')

    def test_deb_version(self):
        self.assertEqual(build_deb.deb_version('1.3.1'), '1.3.1')
        self.assertEqual(build_deb.deb_version('1.3.0-check.t20261104193012.gfad178051f1a'),
                         '1.3.0~check.t20261104193012.gfad178051f1a')
        self.assertEqual(build_deb.deb_version('1.4.0-rc.1'), '1.4.0~rc.1')
        self.assertEqual(build_deb.deb_version('1.4.0-rc-1'), '1.4.0~rc.1')
        with self.assertRaises(ValueError):
            build_deb.deb_version('local')

    def test_control_text(self):
        text = build_deb.control_text('1.4.0-rc.1', 123)
        fields = dict(re.findall(r'^([A-Za-z-]+): (.*)$', text, re.M))
        for name, value in (('Package', 'qorqbot'), ('Version', '1.4.0~rc.1'), ('Architecture', 'amd64'),
                            ('Maintainer', '49 Agents LLC'), ('Homepage', 'https://qorqbot.com'),
                            ('Section', 'utils'), ('Priority', 'optional'), ('Installed-Size', '123')):
            self.assertEqual(fields[name], value)
        depends = fields['Depends'].split(', ')
        for program in ('python3.12', 'python3.12-venv', 'git', 'tmux'):
            self.assertIn(program, depends)
        self.assertIn('libc6', depends)
        self.assertEqual(depends, sorted(depends))
        self.assertFalse([name for name in depends if re.search(r'[()<>=]', name)])
        self.assertNotIn('License', text)
        description = text.split('Description: ', 1)[1].split('\n')
        self.assertTrue(description[0] and not description[0].startswith(' '))
        extended = description[1:-1]
        self.assertTrue(extended and all(line.startswith(' ') for line in extended))
        self.assertIn(' .', extended)
        self.assertTrue(text.endswith('\n') and not text.endswith('\n\n'))

    def test_desktop_entry_text(self):
        lines = build_deb.DESKTOP_ENTRY.splitlines()
        self.assertEqual(lines[0], '[Desktop Entry]')
        for line in ('Type=Application', 'Name=Qorqbot', 'Exec=/opt/Qorqbot/Qorqbot', 'Icon=qorqbot',
                     'Terminal=false', 'Categories=Office;', 'StartupWMClass=qorqbot'):
            self.assertIn(line, lines)

    @unittest.skipUnless(shutil.which('desktop-file-validate'), 'desktop-file-validate')
    def test_desktop_entry_validates(self):
        with tempfile.TemporaryDirectory() as directory:
            entry = Path(directory) / 'qorqbot.desktop'
            entry.write_text(build_deb.DESKTOP_ENTRY)
            subprocess.run(['desktop-file-validate', entry], check=True)

    def test_output_must_be_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = make_archive(directory, '1.0.0')
            out = Path(directory) / 'out'
            out.mkdir()
            (out / 'existing').write_text('x')
            done = subprocess.run([sys.executable, APP_TOOLS / 'build_deb.py', '--archive', archive, '--output', out],
                                  capture_output=True, text=True)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn('new or empty', done.stderr)

    def test_packaged_app_needs_sandbox_helper_and_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = make_archive(base, '1.0.0')
            packaged = make_packaged(base)
            (packaged / 'chrome-sandbox').unlink()
            with self.assertRaisesRegex(RuntimeError, 'chrome-sandbox'):
                build_deb.assemble_tree(packaged, archive, '1.0.0', base / 'tree1')
            (packaged / 'chrome-sandbox').write_bytes(b'x')
            (packaged / 'Qorqbot').unlink()
            with self.assertRaisesRegex(RuntimeError, 'Qorqbot executable'):
                build_deb.assemble_tree(packaged, archive, '1.0.0', base / 'tree2')


@unittest.skipUnless(shutil.which('dpkg-deb'), 'dpkg-deb')
class AssemblyTests(unittest.TestCase):
    def test_deb_has_root_owned_entries_and_expected_modes(self):
        old = os.umask(0o077)
        try:
            with tempfile.TemporaryDirectory(prefix='qbl-') as directory:
                base = Path(directory)
                version = '1.3.0-test.1'
                archive = make_archive(base, version)
                packaged = make_packaged(base)
                tree = base / 'tree'
                tree.mkdir()
                build_deb.assemble_tree(packaged, archive, version, tree)
                deb = base / 'out.deb'
                build_deb.build_package(tree, deb)
                listing = subprocess.run(['dpkg-deb', '-c', deb], check=True, capture_output=True, text=True).stdout
                entries = [line.split(None, 5) for line in listing.splitlines()]
                self.assertTrue(entries)
                for fields in entries:
                    self.assertEqual(fields[1], 'root/root', fields)
                    if fields[0].startswith('d'):
                        self.assertEqual(fields[0], 'drwxr-xr-x', fields)
                by_name = {fields[5].split(' -> ')[0].lstrip('./'): fields for fields in entries if len(fields) > 5}
                self.assertEqual(by_name['opt/Qorqbot/chrome-sandbox'][0], '-rwsr-xr-x')
                self.assertEqual(by_name['opt/Qorqbot/Qorqbot'][0], '-rwxr-xr-x')
                self.assertEqual(by_name['opt/Qorqbot/resources/app.asar'][0], '-rw-r--r--')
                link = by_name['usr/bin/qorqbot-desktop']
                self.assertTrue(link[0].startswith('l'))
                self.assertTrue(link[5].endswith('-> /opt/Qorqbot/Qorqbot'), link)
                payload = 'opt/Qorqbot/resources/qorqbot-payload/'
                for name in (archive.name, 'ARCHIVE_NAME', 'ARCHIVE_SHA256', 'VERSION'):
                    self.assertIn(payload + name, by_name)
                for name in ('resources/LICENSE.electron.txt', 'resources/LICENSES.chromium.html',
                             'resources/THIRD-PARTY-NOTICES.md'):
                    self.assertIn('opt/Qorqbot/' + name, by_name)
                self.assertIn('usr/share/applications/qorqbot.desktop', by_name)
                self.assertIn('usr/share/icons/hicolor/scalable/apps/qorqbot.svg', by_name)

                def field(name):
                    return subprocess.run(['dpkg-deb', '-f', deb, name], check=True,
                                          capture_output=True, text=True).stdout.strip()
                self.assertEqual(field('Package'), 'qorqbot')
                self.assertEqual(field('Version'), '1.3.0~test.1')
                self.assertEqual(field('Architecture'), 'amd64')
                self.assertGreater(int(field('Installed-Size')), 0)

                extracted = base / 'x'
                extracted.mkdir()
                subprocess.run(['dpkg-deb', '-x', deb, extracted], check=True)
                copied = extracted / payload / archive.name
                self.assertEqual(build_deb.sha256(copied), build_deb.sha256(archive))
                self.assertEqual((extracted / payload / 'VERSION').read_text(), version + '\n')
        finally:
            os.umask(old)


if __name__ == '__main__':
    unittest.main()
