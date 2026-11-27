"""The launcher starts the service again when it exits with the restart code, and ends on any other exit as before.

    python3.12 -m unittest discover -s tools/distribution/tests -p 'test_native_launch_restart.py' -v

A stand-in child answers one readiness request on the port, then exits with the next code of its list.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_launch  # noqa: E402

CHILD = r'''
import http.server, json, sys
from pathlib import Path
codes, starts = Path(sys.argv[1]), Path(sys.argv[2])
started = json.loads(starts.read_text()) if starts.exists() else []
started.append(sys.argv[3:])
starts.write_text(json.dumps(started))
code = json.loads(codes.read_text())[len(started) - 1]
class Ready(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
    def log_message(self, *args):
        pass
server = http.server.HTTPServer(('127.0.0.1', int(sys.argv[3])), Ready)
server.allow_reuse_address = True
server.handle_request()
server.server_close()
sys.exit(code)
'''


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class RestartTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='rs-')).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        handlers = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
        self.addCleanup(signal.signal, signal.SIGINT, handlers[1])
        self.addCleanup(signal.signal, signal.SIGTERM, handlers[0])
        self.state = self.root / 'app.json'
        self.port = free_port()

    def run_child(self, codes):
        (self.root / 'codes.json').write_text(json.dumps(codes))
        argv = [sys.executable, '-I', '-c', CHILD, self.root / 'codes.json', self.root / 'starts.json', str(self.port)]
        states = []
        real_save = native_launch.save_json
        def save(path, value):
            states.append(value)
            real_save(path, value)
        native_launch.save_json, saved = save, native_launch.save_json
        self.addCleanup(setattr, native_launch, 'save_json', saved)
        output = io.StringIO()
        with (self.root / 'application.log').open('ab') as log, contextlib.redirect_stdout(output):
            code = native_launch.run_service(argv, env=dict(os.environ), cwd=self.root, log=log, state=self.state,
                                             url=f'http://127.0.0.1:{self.port}', show=False)
        starts = json.loads((self.root / 'starts.json').read_text())
        return code, starts, states, output.getvalue()

    def test_the_restart_code_starts_the_same_child_again(self):
        code, starts, states, output = self.run_child([native_launch.RESTART, native_launch.RESTART, 0])
        self.assertEqual(code, 0)
        self.assertEqual(starts, [[str(self.port)]] * 3)
        # One record of this launcher for the whole run, removed at the end.
        self.assertEqual([state['pid'] for state in states], [os.getpid()])
        self.assertFalse(self.state.exists())
        self.assertEqual(output.count('Qorqbot restarted and is running at'), 2)
        self.assertEqual(output.count('Press Ctrl+C to stop it.'), 1)

    def test_zero_ends_the_run(self):
        code, starts, _, _ = self.run_child([0])
        self.assertEqual((code, len(starts)), (0, 1))

    def test_one_ends_the_run_with_one(self):
        code, starts, _, _ = self.run_child([native_launch.RESTART, 1])
        self.assertEqual((code, len(starts)), (1, 2))

    def test_the_code_is_ex_tempfail(self):
        self.assertEqual(native_launch.RESTART, 75)


if __name__ == '__main__':
    unittest.main()
