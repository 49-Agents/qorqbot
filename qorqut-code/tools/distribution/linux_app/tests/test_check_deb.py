import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_deb  # noqa: E402

ARCHIVE_NAME = 'qorqbot-1.3.1-ubuntu24-x86_64.tar.gz'
HAVE_DPKG = shutil.which('dpkg-deb') is not None
CONTROL = """Package: qorqbot
Version: 1.3.1-1
Architecture: amd64
Maintainer: 49 Agents LLC
Installed-Size: 10
Depends: libc6, python3.12, python3.12-venv, git, tmux
Description: test package
"""
DESKTOP = """[Desktop Entry]
Type=Application
Name=Qorqbot
Exec=qorqbot-desktop %U
Icon=qorqbot
"""


class ParserTests(unittest.TestCase):
    def test_file_line(self):
        entry = check_deb.parse_listing_line('-rwsr-xr-x root/root 1234 2026-01-01 10:00 ./opt/Qorqbot/chrome-sandbox')
        self.assertEqual((entry.mode, entry.owner, entry.name), ('-rwsr-xr-x', 'root/root', './opt/Qorqbot/chrome-sandbox'))

    def test_symlink_line(self):
        entry = check_deb.parse_listing_line(
            'lrwxrwxrwx root/root 0 2026-01-01 10:00 ./usr/bin/qorqbot-desktop -> /opt/Qorqbot/Qorqbot')
        self.assertTrue(entry.is_symlink)
        self.assertEqual((entry.name, entry.target), ('./usr/bin/qorqbot-desktop', '/opt/Qorqbot/Qorqbot'))

    def test_name_with_spaces(self):
        entry = check_deb.parse_listing_line('-rw-r--r-- root/root 5 2026-01-01 10:00 ./opt/a b.txt')
        self.assertEqual(entry.name, './opt/a b.txt')

    def test_garbage(self):
        with self.assertRaises(check_deb.CheckError):
            check_deb.parse_listing_line('nonsense')


class ProcessTests(unittest.TestCase):
    APP = '/opt/Qorqbot/Qorqbot'

    def test_command_line_split_on_nul_and_spaces(self):
        self.assertEqual(check_deb.split_command_line(b'/opt/Qorqbot/Qorqbot\0--no-sandbox\0'),
                         ['/opt/Qorqbot/Qorqbot', '--no-sandbox'])
        # Chromium child processes rewrite their command line as one string.
        self.assertEqual(check_deb.split_command_line(b'/opt/Qorqbot/Qorqbot --type=renderer --lang=en-US\0\0'),
                         ['/opt/Qorqbot/Qorqbot', '--type=renderer', '--lang=en-US'])

    def test_main_process_and_descendants(self):
        table = {
            10: (1, ['xvfb-run', '-a', self.APP], '/usr/bin/dash'),
            11: (10, [self.APP], self.APP),
            12: (11, [self.APP, '--type=zygote'], self.APP),
            13: (12, [self.APP, '--type=renderer'], self.APP),
            20: (1, ['sleep', '60'], '/usr/bin/sleep'),
        }
        main = check_deb.find_main(table, Path('/opt/Qorqbot'))
        self.assertEqual(main, 11)
        self.assertEqual(sorted(check_deb.descendants(table, main)), [12, 13])


class RootLengthTests(unittest.TestCase):
    def test_limit(self):
        # '/e/Qorqbot/qorqbot' adds 18 characters to the work folder
        ok = Path('/' + 'a' * (50 - 18 - 1))
        self.assertEqual(len(str(ok / 'e' / 'Qorqbot' / 'qorqbot')), 50)
        check_deb.check_root_length(ok)
        with self.assertRaises(check_deb.CheckError) as raised:
            check_deb.check_root_length(Path(str(ok) + 'b'))
        self.assertIn('too long', str(raised.exception))


@unittest.skipUnless(HAVE_DPKG, 'dpkg-deb is not available')
class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='qd-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.archive = self.tmp / ARCHIVE_NAME
        self.archive.write_bytes(b'archive bytes')

    def build(self, *, sandbox_mode=0o4755, extra_file=None, script=False, payload=None, desktop=True):
        tree = self.tmp / 'tree'
        shutil.rmtree(tree, ignore_errors=True)
        app = tree / 'opt/Qorqbot'
        payload_dir = app / 'resources/qorqbot-payload'
        payload_dir.mkdir(parents=True)
        (tree / 'usr/bin').mkdir(parents=True)
        (tree / 'usr/share/icons/hicolor/scalable/apps').mkdir(parents=True)
        (tree / 'usr/share/applications').mkdir(parents=True)
        (tree / 'DEBIAN').mkdir()
        (tree / 'DEBIAN/control').write_text(CONTROL)
        data = self.archive.read_bytes() if payload is None else payload
        (payload_dir / ARCHIVE_NAME).write_bytes(data)
        (payload_dir / 'ARCHIVE_NAME').write_text(ARCHIVE_NAME + '\n')
        (payload_dir / 'ARCHIVE_SHA256').write_text(hashlib.sha256(data).hexdigest() + '\n')
        (payload_dir / 'VERSION').write_text('1.3.1\n')
        (app / 'Qorqbot').write_text('x')
        (app / 'Qorqbot').chmod(0o755)
        (app / 'chrome-sandbox').write_text('x')
        (app / 'chrome-sandbox').chmod(sandbox_mode)
        (app / 'resources/app.asar').write_text('x')
        for name in ('LICENSE.electron.txt', 'LICENSES.chromium.html', 'THIRD-PARTY-NOTICES.md'):
            (app / 'resources' / name).write_text('x')
        os.symlink('/opt/Qorqbot/Qorqbot', tree / 'usr/bin/qorqbot-desktop')
        (tree / 'usr/share/icons/hicolor/scalable/apps/qorqbot.svg').write_text('<svg/>')
        if desktop:
            (tree / 'usr/share/applications/qorqbot.desktop').write_text(DESKTOP)
        if extra_file:
            (app / 'loose.txt').write_text('x')
            (app / 'loose.txt').chmod(extra_file)
        if script:
            (tree / 'DEBIAN/postinst').write_text('#!/bin/sh\n')
            (tree / 'DEBIAN/postinst').chmod(0o755)
        fixed = {app / 'Qorqbot': 0o755, app / 'chrome-sandbox': sandbox_mode,
                 app / 'loose.txt': extra_file or 0o644, tree / 'DEBIAN/postinst': 0o755}
        for path in tree.rglob('*'):
            if path.is_symlink() or path.parent == tree / 'DEBIAN':
                continue
            path.chmod(0o755 if path.is_dir() else fixed.get(path, 0o644))
        tree.chmod(0o755)
        (tree / 'DEBIAN').chmod(0o755)
        if script:
            (tree / 'DEBIAN/postinst').chmod(0o755)
        deb = self.tmp / 'test.deb'
        subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(tree), str(deb)],
                       check=True, capture_output=True)
        return deb

    def assertFails(self, deb, text):
        with self.assertRaises(check_deb.CheckError) as raised:
            check_deb.check_package(deb, self.archive)
        self.assertIn(text, str(raised.exception))

    def test_correct_package_passes(self):
        version, checks = check_deb.check_package(self.build(), self.archive)
        self.assertEqual(version, '1.3.1')
        self.assertTrue(checks)

    def test_sandbox_mode(self):
        self.assertFails(self.build(sandbox_mode=0o755), 'chrome-sandbox has mode')

    def test_world_writable(self):
        self.assertFails(self.build(extra_file=0o666), 'writable')

    def test_other_setuid(self):
        self.assertFails(self.build(extra_file=0o4755), 'setuid')

    def test_maintainer_script(self):
        self.assertFails(self.build(script=True), 'maintainer scripts')

    def test_payload_differs(self):
        self.assertFails(self.build(payload=b'other bytes'), 'differs from --archive')

    def test_missing_desktop_file(self):
        self.assertFails(self.build(desktop=False), 'qorqbot.desktop')


if __name__ == '__main__':
    unittest.main()
