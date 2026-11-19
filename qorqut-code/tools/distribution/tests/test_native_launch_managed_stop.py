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

    def fake_python(self, body):
        python = self.root / 'runtime/venv/bin/python'
        python.parent.mkdir(parents=True)
        python.write_text('#!/bin/sh\n' + body)
        python.chmod(0o755)
        return python


class ServiceEnvironment(Folder):
    def test_no_terminal_variables_are_set(self):
        with mock.patch.dict(os.environ, {'HOME': str(self.root), 'PATH': '/usr/bin:/bin', 'QORQUT_TIMEZONE': 'UTC'}, clear=True):
            env = native_launch.environment(self.root, self.root / 'home')
        self.assertNotIn('TMUX_TMPDIR', env)
        self.assertNotIn('BIZ_TMUX_SOCKET', env)

    def test_readiness_notes_never_mention_tmux(self):
        self.assertFalse(any('tmux' in note for note in native_launch.readiness_notes('/nonexistent', system='Darwin')))


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
