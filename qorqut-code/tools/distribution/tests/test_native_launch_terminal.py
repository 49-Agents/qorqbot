"""Terminal web mode: the qorqbot command, browser opening, remembered port and readiness hints.

    python3.12 -m unittest discover -s tools/distribution/tests -v
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_launch  # noqa: E402
from native_launch import (COMMAND_MARKER, browser_command, command_directory, default_port,  # noqa: E402
                           install_command, install_hint, readiness_notes)


class InstallCommandTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.root = self.base / 'Qorqbot it\'s here/qorqbot'
        self.root.mkdir(parents=True)
        launcher = self.root / 'qorqbot'
        launcher.write_text('#!/bin/sh\necho "ran with $*"\n')
        launcher.chmod(0o755)
        self.bin = self.base / 'bin'

    def test_writes_a_runnable_shim_that_passes_arguments(self):
        message = install_command(self.root, self.bin, path_value=str(self.bin), home=self.base)
        target = self.bin / 'qorqbot'
        self.assertIn('Installed', message)
        self.assertNotIn('not on your PATH', message)
        self.assertTrue(os.access(target, os.X_OK))
        output = subprocess.run([str(target), 'status', 'two words'], capture_output=True, text=True, check=True).stdout
        self.assertEqual(output.strip(), 'ran with status two words')

    def test_reinstall_replaces_its_own_shim(self):
        install_command(self.root, self.bin, path_value='', home=self.base)
        other = self.base / 'moved/qorqbot'
        other.mkdir(parents=True)
        install_command(other, self.bin, path_value='', home=self.base)
        self.assertIn(str(other / 'qorqbot'), (self.bin / 'qorqbot').read_text())

    def test_warns_when_directory_is_not_on_path(self):
        message = install_command(self.root, self.bin, path_value='/usr/bin', home=self.base)
        self.assertIn('not on your PATH', message)
        self.assertIn(f'export PATH="{self.bin}:$PATH"', message)

    def test_foreign_file_is_never_replaced_or_removed(self):
        self.bin.mkdir()
        (self.bin / 'qorqbot').write_text('#!/bin/sh\necho someone else\n')
        for remove in (False, True):
            with self.subTest(remove=remove), self.assertRaisesRegex(RuntimeError, 'not created by Qorqbot'):
                install_command(self.root, self.bin, remove=remove, path_value='', home=self.base)
        self.assertIn('someone else', (self.bin / 'qorqbot').read_text())

    def test_symlink_is_treated_as_foreign(self):
        self.bin.mkdir()
        (self.bin / 'qorqbot').symlink_to(self.root / 'qorqbot')
        with self.assertRaisesRegex(RuntimeError, 'not created by Qorqbot'):
            install_command(self.root, self.bin, path_value='', home=self.base)

    def test_remove(self):
        install_command(self.root, self.bin, path_value='', home=self.base)
        self.assertIn('Removed', install_command(self.root, self.bin, remove=True, path_value='', home=self.base))
        self.assertFalse((self.bin / 'qorqbot').exists())
        self.assertIn('There is no', install_command(self.root, self.bin, remove=True, path_value='', home=self.base))

    def test_shim_carries_marker(self):
        install_command(self.root, self.bin, path_value='', home=self.base)
        self.assertIn(COMMAND_MARKER, (self.bin / 'qorqbot').read_text())


class CommandDirectoryTests(unittest.TestCase):
    def test_linux_uses_local_bin(self):
        home = Path(tempfile.mkdtemp())
        self.assertEqual(command_directory(str(home / '.local/bin'), home, system='Linux'), (home / '.local/bin', True))
        self.assertEqual(command_directory('/usr/bin', home, system='Linux'), (home / '.local/bin', False))

    def test_mac_prefers_writable_homebrew_bin_on_path(self):
        home = Path(tempfile.mkdtemp())
        with mock.patch.object(Path, 'is_dir', return_value=True), mock.patch('os.access', return_value=True):
            self.assertEqual(command_directory('/usr/bin:/opt/homebrew/bin', home, system='Darwin'),
                             (Path('/opt/homebrew/bin'), True))
        with mock.patch.object(Path, 'is_dir', return_value=True), mock.patch('os.access', return_value=False):
            self.assertEqual(command_directory('/opt/homebrew/bin', home, system='Darwin'), (home / '.local/bin', False))


class BrowserTests(unittest.TestCase):
    url = 'http://127.0.0.1:1049'

    def test_mac_uses_open(self):
        self.assertEqual(browser_command(self.url, system='Darwin'), ['open', self.url])

    def test_wsl_prefers_wslview_then_explorer(self):
        release = '5.15.153.1-microsoft-standard-WSL2'
        with mock.patch('shutil.which', side_effect=lambda name: name == 'explorer.exe'):
            self.assertEqual(browser_command(self.url, system='Linux', release=release), ['explorer.exe', self.url])
        with mock.patch('shutil.which', return_value='/usr/bin/x'):
            self.assertEqual(browser_command(self.url, system='Linux', release=release), ['wslview', self.url])
        with mock.patch('shutil.which', return_value=None):
            self.assertIsNone(browser_command(self.url, system='Linux', release=release))

    def test_linux_desktop_uses_xdg_open_when_present(self):
        with mock.patch('shutil.which', return_value='/usr/bin/xdg-open'):
            self.assertEqual(browser_command(self.url, system='Linux', release='6.8.0-generic'), ['xdg-open', self.url])
        with mock.patch('shutil.which', return_value=None):
            self.assertIsNone(browser_command(self.url, system='Linux', release='6.8.0-generic'))

    def test_open_failure_is_reported_not_raised(self):
        with mock.patch.object(native_launch, 'browser_command', return_value=['missing-opener', self.url]), \
                mock.patch('subprocess.run', side_effect=OSError):
            self.assertFalse(native_launch.open_browser(self.url))


class PortAndHintTests(unittest.TestCase):
    def test_default_port_reuses_saved_value(self):
        run_dir = Path(tempfile.mkdtemp())
        self.assertEqual(default_port(run_dir), 1049)
        (run_dir / 'port').write_text('1052\n')
        self.assertEqual(default_port(run_dir), 1052)
        for bad in ('80', 'abc', '70000', ''):
            (run_dir / 'port').write_text(bad)
            self.assertEqual(default_port(run_dir), 1049)

    def test_install_hints_match_platform(self):
        self.assertEqual(install_hint('python3.12', 'python3.12-venv', system='Darwin'), 'brew install python@3.12')
        self.assertEqual(install_hint('git', system='Linux'), 'sudo apt install git')

    def test_readiness_notes_name_missing_tools(self):
        empty = tempfile.mkdtemp()
        notes = readiness_notes(empty, system='Darwin')
        self.assertTrue(any('brew install tmux' in note for note in notes))
        self.assertTrue(any('claude' in note and 'codex' in note for note in notes))
        tools = Path(tempfile.mkdtemp())
        for name in ('tmux', 'codex', 'google-chrome'):
            (tools / name).write_text('#!/bin/sh\n')
            (tools / name).chmod(0o755)
        self.assertEqual(readiness_notes(str(tools), system='Linux'), [])


class MainTests(unittest.TestCase):
    def test_install_command_needs_no_installation_state(self):
        directory = Path(tempfile.mkdtemp())
        with mock.patch('sys.stdout'):
            self.assertEqual(native_launch.main(['install-command', '--dir', str(directory)]), 0)
        self.assertTrue((directory / 'qorqbot').is_file())

    def test_start_when_running_prints_address_and_succeeds(self):
        running = {'pid': 1, 'identity': {}, 'url': 'http://127.0.0.1:1050'}
        with mock.patch.object(native_launch, 'require_platform'), \
                mock.patch.object(native_launch, 'active', return_value=running), \
                mock.patch.object(native_launch, 'open_browser') as opened, \
                mock.patch.object(native_launch, '__file__', '/tmp/q/runtime/launch.py'), \
                mock.patch.object(Path, 'mkdir'), \
                mock.patch('builtins.print') as printed:
            self.assertEqual(native_launch.main(['--no-open']), 0)
        printed.assert_called_with('Qorqbot is already running at http://127.0.0.1:1050', flush=True)
        opened.assert_not_called()

    def test_status_names_a_terminal_started_server(self):
        for terminal, expected in ((True, 'Running at http://127.0.0.1:1050 from Terminal'), (False, 'Running at http://127.0.0.1:1050')):
            running = {'pid': 1, 'identity': {}, 'url': 'http://127.0.0.1:1050', 'terminal': terminal}
            with self.subTest(terminal=terminal), mock.patch.object(native_launch, 'require_platform'), \
                    mock.patch.object(native_launch, 'active', return_value=running), \
                    mock.patch.object(native_launch, '__file__', '/tmp/q/runtime/launch.py'), \
                    mock.patch.object(Path, 'mkdir'), mock.patch('builtins.print') as printed:
                self.assertEqual(native_launch.main(['status']), 0)
                printed.assert_called_with(expected)


if __name__ == '__main__':
    unittest.main()
