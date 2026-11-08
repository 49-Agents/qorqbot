"""Ownership scan used by native updates and connector installation.

Run from qorqut-code/app with an interpreter that has psutil:
    python -m unittest tests.test_process_info_owned
"""
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from qorqut import process_info

ROOT = Path('/fake/qorqbot').resolve()
UID = os.geteuid()


class FakeTable:
    """pid -> dict(exe, cwd, env, parent, uid); a value of PermissionError means hidden."""

    def __init__(self, table):
        self.table = table

    def _get(self, pid, key):
        value = self.table[pid].get(key, PermissionError)
        if value is PermissionError:
            raise PermissionError('hidden: ' + key)
        return value

    def patches(self, darwin=True):
        return [
            mock.patch.object(process_info, 'darwin', return_value=darwin),
            mock.patch.object(process_info, 'pids', return_value=list(self.table)),
            mock.patch.object(process_info, 'uids', side_effect=lambda pid: (self.table[pid].get('uid', UID),) * 3),
            mock.patch.object(process_info, 'is_zombie', return_value=False),
            mock.patch.object(process_info, 'identity', side_effect=lambda pid: f'boot:{pid}'),
            mock.patch.object(process_info, 'exe', side_effect=lambda pid: Path(self._get(pid, 'exe'))),
            mock.patch.object(process_info, 'cwd', side_effect=lambda pid: Path(self._get(pid, 'cwd'))),
            mock.patch.object(process_info, 'environ', side_effect=lambda pid: self._get(pid, 'env')),
            mock.patch.object(process_info, 'parent_pid', side_effect=lambda pid: self._get(pid, 'parent')),
        ]

    def owned(self, darwin=True, exclude=()):
        patches = self.patches(darwin)
        for patch in patches:
            patch.start()
        try:
            return sorted(item['pid'] for item in process_info.owned(ROOT, exclude=exclude))
        finally:
            for patch in reversed(patches):
                patch.stop()


MARKER = {'QORQUT_FOLDER_ROOT': str(ROOT)}


class DarwinOwnedTests(unittest.TestCase):
    def test_hidden_platform_processes_do_not_abort_the_scan(self):
        table = FakeTable({
            10: {'exe': '/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder', 'parent': 1},
            11: {'parent': 1},  # everything hidden
            20: {'exe': str(ROOT / 'runtime/venv/bin/python'), 'cwd': str(ROOT), 'env': MARKER, 'parent': 1},
        })
        self.assertEqual(table.owned(), [20])

    def test_each_direct_signal_selects_a_process(self):
        table = FakeTable({
            1: {'exe': '/sbin/launchd', 'cwd': '/', 'env': {'PATH': '/bin'}, 'parent': 0, 'uid': 0},
            20: {'exe': str(ROOT / 'runtime/venv/bin/python'), 'parent': 1},
            21: {'exe': '/opt/homebrew/bin/tmux', 'cwd': '/', 'env': MARKER, 'parent': 1},
            22: {'exe': '/opt/homebrew/bin/node', 'cwd': '/Users/someone', 'env': {'PATH': '/bin'}, 'parent': 1},
        })
        self.assertEqual(table.owned(), [20, 21])

    def test_working_directory_alone_is_not_ownership(self):
        # An owner's Terminal (or the app launcher) inside the folder must not
        # block updates.
        table = FakeTable({
            90: {'exe': '/bin/zsh', 'cwd': str(ROOT), 'parent': 1},
            91: {'exe': '/opt/homebrew/bin/node', 'cwd': str(ROOT / 'instance'), 'env': {'PATH': '/bin'}, 'parent': 1},
        })
        self.assertEqual(table.owned(), [])

    def test_marker_for_a_different_folder_is_not_ours(self):
        table = FakeTable({
            30: {'exe': '/opt/homebrew/bin/tmux', 'cwd': '/', 'env': {'QORQUT_FOLDER_ROOT': '/other/qorqbot'}, 'parent': 1},
        })
        self.assertEqual(table.owned(), [])

    def test_hidden_children_of_owned_processes_are_owned(self):
        table = FakeTable({
            40: {'exe': '/opt/homebrew/bin/tmux', 'cwd': '/', 'env': MARKER, 'parent': 1},
            41: {'exe': '/bin/bash', 'parent': 40},          # platform shell, env hidden
            42: {'exe': '/bin/sleep', 'parent': 41},         # grandchild, env hidden
            50: {'exe': '/bin/bash', 'parent': 51},          # hidden, unrelated parent
            51: {'exe': '/Applications/Terminal.app/x', 'cwd': '/', 'env': {'PATH': '/bin'}, 'parent': 1},
        })
        self.assertEqual(table.owned(), [40, 41, 42])

    def test_excluded_parent_does_not_lend_ownership(self):
        # The updater (excluded) carries the marker; its own inspection child must
        # not be counted through ancestry, and neither must the updater.
        table = FakeTable({
            60: {'exe': '/opt/homebrew/bin/python3.12', 'cwd': '/', 'env': MARKER, 'parent': 1},
            61: {'exe': '/bin/sleep', 'parent': 60},
        })
        self.assertEqual(table.owned(exclude=[60]), [])

    def test_parent_cycles_terminate(self):
        table = FakeTable({
            70: {'exe': '/bin/a', 'parent': 71},
            71: {'exe': '/bin/b', 'parent': 70},
        })
        self.assertEqual(table.owned(), [])

    def test_other_users_are_ignored(self):
        table = FakeTable({
            80: {'exe': str(ROOT / 'runtime/venv/bin/python'), 'cwd': str(ROOT), 'env': MARKER, 'parent': 1, 'uid': UID + 1},
        })
        self.assertEqual(table.owned(), [])


class LinuxOwnedTests(unittest.TestCase):
    def test_linux_keeps_exe_or_marker_rule(self):
        table = FakeTable({
            20: {'exe': str(ROOT / 'runtime/venv/bin/python'), 'env': {}},
            21: {'exe': '/usr/bin/tmux', 'env': MARKER},
            22: {'exe': '/usr/bin/node', 'cwd': str(ROOT), 'env': {}},  # cwd is not a Linux signal
        })
        self.assertEqual(table.owned(darwin=False), [20, 21])

    def test_linux_permission_error_still_reports_unavailable(self):
        table = FakeTable({20: {'exe': '/usr/bin/tmux'}})
        with self.assertRaises(PermissionError):
            table.owned(darwin=False)


@unittest.skipUnless(sys.platform == 'darwin', 'macOS integration')
class DarwinIntegrationTests(unittest.TestCase):
    def test_real_session_scan_finds_marker_process_and_hidden_child(self):
        with tempfile.TemporaryDirectory(prefix='qb-own-') as folder:
            root = Path(folder).resolve()
            env = dict(os.environ, QORQUT_FOLDER_ROOT=str(root))
            # A marker-carrying Python starts an Apple platform binary whose
            # environment macOS hides; both must be reported, nothing else.
            script = 'import subprocess,time; subprocess.Popen(["/bin/sleep","20"]); time.sleep(20)'
            parent = subprocess.Popen([sys.executable, '-c', script], env=env, cwd='/', start_new_session=True)
            try:
                deadline = time.monotonic() + 10
                children = []
                while time.monotonic() < deadline and not children:
                    import psutil
                    children = [c.pid for c in psutil.Process(parent.pid).children()]
                    time.sleep(0.2)
                self.assertTrue(children, 'child did not start')
                found = {item['pid'] for item in process_info.owned(root)}
                self.assertEqual(found, {parent.pid, *children})
            finally:
                os.killpg(parent.pid, 9)
                parent.wait()


if __name__ == '__main__':
    unittest.main()


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('cc'), 'macOS process names, needs a C compiler')
class DarwinClientNameTests(unittest.TestCase):
    def run_as(self, relative):
        base = Path(tempfile.mkdtemp(dir='/tmp'))
        target = base / relative
        target.parent.mkdir(parents=True)
        # A copied system binary is killed by code-signing checks; a freshly linked one runs anywhere.
        source = base / 'pause.c'
        source.write_text('#include <unistd.h>\nint main(void){sleep(30);return 0;}\n')
        subprocess.run(['cc', '-o', str(target), str(source)], check=True)
        process = subprocess.Popen([str(target)])
        self.addCleanup(shutil.rmtree, base, True)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        deadline = time.monotonic() + 5
        while process_info.exe(process.pid).name != target.name and time.monotonic() < deadline:
            time.sleep(0.05)
        return process.pid

    def test_versioned_claude_install_is_named_claude(self):
        self.assertEqual(process_info.name(self.run_as('claude/versions/2.1.317')), 'claude')

    def test_other_versioned_binaries_keep_their_name(self):
        self.assertEqual(process_info.name(self.run_as('tool/versions/2.1.317')), '2.1.317')
        self.assertEqual(process_info.name(self.run_as('bin/codex')), 'codex')
