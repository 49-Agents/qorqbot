"""An update or an extension installation closes managed agent sessions instead of refusing over terminals.

    python3.12 -m unittest discover -s tools/distribution/tests -p 'test_native_launch_managed_stop.py' -v

The shutdown command itself is tested in app/tests/test_managed_shutdown.py; here a stand-in Python plays it.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_launch  # noqa: E402


class Folder(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='ms-', dir='/tmp')).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / '.run').mkdir()

    def write_mode(self, mode):
        path = self.root / native_launch.MODE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'schema_version': 1, 'mode': mode}))
        return path

    def fake_python(self, body):
        python = self.root / 'runtime/venv/bin/python'
        python.parent.mkdir(parents=True)
        python.write_text('#!/bin/sh\n' + body)
        python.chmod(0o755)
        return python


class ServiceEnvironment(Folder):
    def environment(self, **extra):
        with mock.patch.dict(os.environ, {'HOME': str(self.root), 'PATH': '/usr/bin:/bin', 'QORQUT_TIMEZONE': 'UTC', **extra}, clear=True):
            return native_launch.environment(self.root, self.root / 'home')

    def test_terminal_sockets_stay_inside_this_installation_in_every_mode(self):
        self.assertEqual(self.environment()['TMUX_TMPDIR'], str(self.root / '.run'))
        self.write_mode('terminal')
        self.assertEqual(self.environment()['TMUX_TMPDIR'], str(self.root / '.run'))

    def test_a_shipped_tmux_is_never_put_on_the_path(self):
        env = self.environment()
        self.assertNotIn('tmux', ' '.join(env['PATH'].split(os.pathsep)))
        self.assertNotIn('BIZ_TMUX_SOCKET', env)

    def test_an_inherited_terminal_workspace_does_not_leak_in(self):
        self.assertNotIn('QORQUT_TERMINAL_WORKSPACE', self.environment(QORQUT_TERMINAL_WORKSPACE='/somewhere'))

    def test_readiness_notes_never_mention_tmux(self):
        self.assertFalse(any('tmux' in note for note in native_launch.readiness_notes('/nonexistent', system='Darwin')))


class ModeReader(Folder):
    def test_no_file_means_managed(self):
        self.assertEqual(native_launch.read_mode(self.root), 'managed')

    def test_terminal(self):
        self.write_mode('terminal')
        self.assertEqual(native_launch.read_mode(self.root), 'terminal')
        self.write_mode('managed')
        self.assertEqual(native_launch.read_mode(self.root), 'managed')

    def test_anything_else_means_managed(self):
        path = self.write_mode('terminal')
        for text in ('', 'not json', '[]', '{"schema_version": 2, "mode": "terminal"}', '{"mode": "terminal"}',
                     '{"schema_version": 1, "mode": "other"}', '{"schema_version": 1, "mode": ["terminal"]}',
                     json.dumps({'schema_version': 1, 'mode': 'terminal', 'note': 'x' * 5000})):
            with self.subTest(text=text[:40]):
                path.write_text(text)
                self.assertEqual(native_launch.read_mode(self.root), 'managed')

    def test_an_unreadable_file_means_managed(self):
        path = self.write_mode('terminal')
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        if os.access(path, os.R_OK):
            self.skipTest('permissions are not enforced for this user')
        self.assertEqual(native_launch.read_mode(self.root), 'managed')

    def test_a_directory_in_its_place_means_managed(self):
        path = self.write_mode('terminal')
        path.unlink()
        path.mkdir()
        self.assertEqual(native_launch.read_mode(self.root), 'managed')

    def test_it_agrees_with_the_products_own_reader(self):
        app = Path(__file__).resolve().parents[3] / 'app'
        sys.path.insert(0, str(app))
        self.addCleanup(sys.path.remove, str(app))
        from types import SimpleNamespace
        from qorqut.managed_runtime import mode
        paths = SimpleNamespace(runtime=self.root / 'instance/operations/.runtime')
        path = self.write_mode('terminal')
        cases = ['', '{"schema_version": 1, "mode": "terminal"}', '{"schema_version": 1, "mode": "managed"}',
                 '{"schema_version": 3, "mode": "terminal"}', '{"schema_version": 1, "mode": "x"}', 'garbage',
                 json.dumps({'schema_version': 1, 'mode': 'terminal', 'pad': 'x' * 4100})]
        for text in cases:
            with self.subTest(text=text[:40]):
                path.write_text(text)
                self.assertEqual(native_launch.read_mode(self.root), mode.current(paths))


class SocketRoom(Folder):
    def test_only_terminals_mode_checks_the_socket_path(self):
        run_dir = Path('/tmp/' + 'a' * 120 + '/.run')
        native_launch.require_terminal_socket_room(self.root, run_dir)              # managed: nothing to fit
        self.write_mode('terminal')
        with self.assertRaisesRegex(RuntimeError, 'local terminal sockets fit'):
            native_launch.require_terminal_socket_room(self.root, run_dir)
        native_launch.require_terminal_socket_room(self.root, self.root / '.run')   # a short path is fine in terminals too


class LeftoverTerminalSockets(Folder):
    def listen(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)
        self.addCleanup(server.close)

    def test_no_sockets_do_not_block(self):
        native_launch.require_no_terminal_agents(self.root)

    def test_a_socket_nobody_listens_on_does_not_block_forever(self):
        path = self.root / '.run/tmux-1000/qorqut'
        path.parent.mkdir()
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(path))
        stale.close()
        native_launch.require_no_terminal_agents(self.root)
        self.assertTrue(path.exists())                      # nothing is removed

    def test_a_live_server_blocks_and_names_the_cutover(self):
        path = self.root / '.run/tmux-1000/qorqut'
        self.listen(path)
        with self.assertRaisesRegex(ValueError, 'managed cutover'):
            native_launch.require_no_terminal_agents(self.root)
        self.assertTrue(path.exists())


class TerminalsGuard(Folder):
    """In Terminals mode the guard lists this installation's own tmux sessions (a stand-in tmux here) and ends nothing."""

    def fake_tmux(self, body):
        tmux = self.root / 'bin/tmux'
        tmux.parent.mkdir(exist_ok=True)
        tmux.write_text('#!/bin/sh\n' + body)
        tmux.chmod(0o755)
        return {'PATH': f'{tmux.parent}:/usr/bin:/bin', 'TMUX_TMPDIR': '/elsewhere'}

    def setUp(self):
        super().setUp()
        self.write_mode('terminal')
        self.log = self.root / 'calls.txt'

    def test_running_agent_terminals_refuse_with_a_plain_message_and_are_only_listed(self):
        env = self.fake_tmux(f'echo "$TMUX_TMPDIR $*" >> {self.log}\n'
                             'case "$*" in *list-sessions*) echo agent-one; exit 0;; esac\nexit 9\n')
        with self.assertRaises(ValueError) as caught:
            native_launch.require_no_terminal_agents(self.root, env)
        message = str(caught.exception)
        self.assertIn('run in terminals', message)
        self.assertIn('Managed sessions', message)
        self.assertNotIn('cutover', message)
        calls = self.log.read_text().splitlines()
        self.assertTrue(calls)
        for call in calls:                                   # only this installation's folder, only listing
            self.assertTrue(call.startswith(str(self.root / '.run') + ' '), call)
            self.assertIn('list-sessions', call)
            self.assertNotIn('kill', call)

    def test_no_running_session_lets_the_update_go_on(self):
        env = self.fake_tmux('echo "no server running on /x" >&2\nexit 1\n')
        native_launch.require_no_terminal_agents(self.root, env)

    def test_a_tmux_that_cannot_be_understood_refuses(self):
        env = self.fake_tmux('echo "unexpected failure" >&2\nexit 1\n')
        with self.assertRaisesRegex(ValueError, 'Could not prove'):
            native_launch.require_no_terminal_agents(self.root, env)

    def test_without_tmux_a_live_socket_still_blocks_and_a_stale_one_does_not(self):
        env = {'PATH': str(self.root / 'nowhere')}
        path = self.root / '.run/tmux-1000/qorqut'
        path.parent.mkdir()
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(path))
        stale.close()
        native_launch.require_no_terminal_agents(self.root, env)
        path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(path))
        server.listen(1)
        with self.assertRaisesRegex(ValueError, 'tmux is unavailable'):
            native_launch.require_no_terminal_agents(self.root, env)

    def test_managed_mode_ignores_tmux_and_keeps_the_cutover_message(self):
        self.write_mode('managed')
        env = self.fake_tmux(f'echo called >> {self.log}\nexit 0\n')
        native_launch.require_no_terminal_agents(self.root, env)
        self.assertFalse(self.log.exists())


class StopManagedHosts(Folder):
    def test_no_environment_means_no_agent_ever_ran(self):
        self.assertEqual(native_launch.stop_managed_hosts(self.root, {}), {})

    def test_the_installed_runtime_closes_the_hosts_and_its_report_is_returned(self):
        log = self.root / 'args.txt'
        report = json.dumps({'ok': True, 'agents': [], 'remaining': []})
        self.fake_python(f'echo "$@" > {log}\necho \'{report}\'\n')
        result = native_launch.stop_managed_hosts(self.root, dict(os.environ), timeout=7)
        self.assertEqual(result['ok'], True)
        arguments = log.read_text().split()
        self.assertIn('qorqut.managed_runtime.shutdown', arguments)
        self.assertEqual(arguments[arguments.index('--instance-root') + 1], str(self.root / 'instance'))
        self.assertEqual(arguments[arguments.index('--timeout') + 1], '7')

    def test_hosts_that_remain_refuse_with_a_plain_message(self):
        report = json.dumps({'ok': False, 'agents': [], 'remaining': ['agent-1']})
        self.fake_python(f'echo \'{report}\'\nexit 1\n')
        with self.assertRaisesRegex(ValueError, r'Running agents could not be closed \(agent-1\)'):
            native_launch.stop_managed_hosts(self.root, dict(os.environ))

    def test_a_command_that_fails_without_a_report_refuses_too(self):
        self.fake_python('echo broken >&2\nexit 3\n')
        with self.assertRaisesRegex(ValueError, 'could not be closed'):
            native_launch.stop_managed_hosts(self.root, dict(os.environ))


if __name__ == '__main__':
    unittest.main()
