"""Before a runtime change the launcher closes only this installation's own recorded browser supervisors.

    python3.12 -m unittest discover -s tools/distribution/tests -v
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_launch  # noqa: E402


@unittest.skipUnless(sys.platform == 'linux', 'reads /proc; macOS inspection needs an installed runtime')
class StopAgentBrowsersTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()
        self.root = self.base / 'qorqbot'
        self.state = self.root / 'instance/operations/.runtime/agent-browsers/agent'
        self.state.mkdir(parents=True)
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        shutil.rmtree(self.base)

    def process(self, owned=True):
        env = {'PATH': os.environ.get('PATH', os.defpath)}
        if owned:
            env['QORQUT_FOLDER_ROOT'] = str(self.root)
        child = subprocess.Popen(['sleep', '60'], env=env)
        self.children.append(child)
        deadline = time.monotonic() + 5
        while owned and child.pid not in native_launch.installation_processes(self.root):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.05)
        return child

    def record(self, child, identity=None):
        current = native_launch.identity(child.pid)
        (self.state / 'ready.json').write_text(json.dumps({
            'supervisor_pid': child.pid, 'supervisor_identity': identity or current['boot'] + ':' + current['start']}))

    def test_stops_the_recorded_supervisor_and_waits_for_it(self):
        child = self.process()
        self.record(child)
        started = time.monotonic()
        native_launch.stop_agent_browsers(self.root, timeout=20)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)
        self.assertEqual(native_launch.installation_processes(self.root), [])

    def test_leaves_a_process_this_installation_does_not_own(self):
        child = self.process(owned=False)
        self.record(child)
        native_launch.stop_agent_browsers(self.root, timeout=20)
        self.assertIsNone(child.poll())

    def test_leaves_a_process_whose_identity_differs_from_the_record(self):
        child = self.process()
        self.record(child, identity='another-boot:1')
        started = time.monotonic()
        native_launch.stop_agent_browsers(self.root, timeout=20)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNone(child.poll())
        self.assertEqual(native_launch.installation_processes(self.root), [child.pid])

    def test_ignores_an_unreadable_record(self):
        child = self.process()
        (self.state / 'ready.json').write_text('{"supervisor_pid": "' + str(child.pid) + '"')
        native_launch.stop_agent_browsers(self.root, timeout=20)
        self.assertIsNone(child.poll())

    def test_inspects_nothing_when_no_browser_was_ever_recorded(self):
        with mock.patch.object(native_launch, 'installation_processes', side_effect=AssertionError):
            native_launch.stop_agent_browsers(self.root)


if __name__ == '__main__':
    unittest.main()
