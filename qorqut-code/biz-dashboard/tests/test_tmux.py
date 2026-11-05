#!/usr/bin/env python3
"""tmux-held shells through the retained broker, with isolated config and socket.
Runs against a real tmux server on a private socket (`-L bazarlyk-test-<pid>`), killed at the end.
Skipped when tmux is not installed."""

import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import pty_broker  # noqa: E402
from test_pty import POLLUTED, STRIPPED

SOCK = f"bazarlyk-test-{os.getpid()}"


def wait_for(pred, secs=5.0):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


@unittest.skipIf(shutil.which("tmux") is None, "tmux not installed")
class TmuxCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temporary = tempfile.TemporaryDirectory()
        cls._config = mock.patch.object(pty_broker, "TMUX_CONFIG_DIR", cls._temporary.name)
        cls._config.start()
        cls._sock = mock.patch.object(pty_broker, "TMUX_SOCK", SOCK)
        cls._sock.start()

    @classmethod
    def tearDownClass(cls):
        pty_broker.tmux("kill-server")
        cls._sock.stop()
        cls._config.stop()
        cls._temporary.cleanup()

    def tearDown(self):
        for n in pty_broker.list_sessions():
            pty_broker.kill_session(n)
        for k in pty_broker.keys():
            pty_broker.stop(k)


class TestSessions(TmuxCase):
    def test_names(self):
        self.assertEqual(pty_broker.session_name("demo.day", "on_bashy"), "demo-day__on_bashy")
        self.assertEqual(pty_broker.session_prefix("a:b_c"), "a-b-c__")

    def test_create_is_idempotent_and_env_is_session_scoped(self):
        d = tempfile.mkdtemp()
        out = os.path.join(d, "env.txt")
        cmd = ["/bin/sh", "-c", f"env > {out}; sleep 30"]
        with mock.patch.dict(os.environ, POLLUTED):
            self.assertTrue(pty_broker.ensure_session("t__a", cmd, d, env={"SECRET_A": "1"}))
        self.assertFalse(pty_broker.ensure_session("t__a", cmd, d, env={"SECRET_A": "2"}))
        self.assertTrue(pty_broker.session_alive("t__a"))
        self.assertTrue(wait_for(lambda: os.path.exists(out) and "SECRET_A=" in open(out).read()))
        text = open(out).read()
        self.assertIn("SECRET_A=1", text)
        self.assertIn(f"PWD={d}", text)
        for k in STRIPPED:
            self.assertNotIn(k + "=", text, k)
        # a second business's session does not see the first one's secret
        out2 = os.path.join(d, "env2.txt")
        pty_broker.ensure_session("u__b", ["/bin/sh", "-c", f"env > {out2}; sleep 30"], d, env={"SECRET_B": "1"})
        self.assertTrue(wait_for(lambda: os.path.exists(out2) and "SECRET_B=" in open(out2).read()))
        self.assertNotIn("SECRET_A=", open(out2).read())
        self.assertEqual(sorted(pty_broker.list_sessions()), ["t__a", "u__b"])
        self.assertEqual(pty_broker.list_sessions("t__"), ["t__a"])

    def test_options_applied(self):
        d = tempfile.mkdtemp()
        pty_broker.ensure_session("t__a", ["/bin/sh", "-c", "sleep 30"], d)
        pty_broker.ensure_session("u__b", ["/bin/sh", "-c", "sleep 30"], d)   # a 2nd session must not crash the server
        rc, out = pty_broker.tmux("show-options", "-g")
        self.assertIn("status off", out)
        self.assertIn("prefix None", out)
        self.assertIn("history-limit 50000", out)
        self.assertIn("mouse on", out)
        rc, out = pty_broker.tmux("show-options", "-sv", "default-terminal")
        self.assertEqual(out.strip(), "tmux-256color")
        rc, out = pty_broker.tmux("show-options", "-wv", "-t", "=t__a:", "window-size")
        self.assertEqual(out.strip(), "manual")
        rc, out = pty_broker.tmux("show-options", "-gw", "window-size")
        self.assertIn("latest", out)   # never global

    def test_session_outlives_mirror(self):
        d = tempfile.mkdtemp()
        pty_broker.ensure_session("t__a", ["/bin/sh", "-c", "sleep 30"], d)
        t = pty_broker.Terminal("k", pty_broker.attach_cmd("t__a"), d)
        self.assertTrue(wait_for(lambda: t.snapshot()[0] != b""))   # tmux drew something
        t.stop()   # the mirror dies (dashboard restart) ...
        self.assertTrue(wait_for(lambda: t.done))
        self.assertTrue(pty_broker.session_alive("t__a"))   # ... the shell lives
        t2 = pty_broker.Terminal("k", pty_broker.attach_cmd("t__a"), d)
        self.assertTrue(wait_for(lambda: t2.snapshot()[0] != b""))   # re-attach redraws
        self.assertTrue(pty_broker.kill_session("t__a"))
        self.assertTrue(wait_for(lambda: t2.done))   # killing the session ends the attach client
        self.assertFalse(pty_broker.session_alive("t__a"))
        self.assertFalse(pty_broker.kill_session("t__a"))

    def test_resize(self):
        d = tempfile.mkdtemp()
        pty_broker.ensure_session("t__a", ["/bin/sh", "-c", "sleep 30"], d, cols=100, rows=30)
        pty_broker.resize_session("t__a", 20, 80)
        rc, out = pty_broker.tmux("display-message", "-p", "-t", "=t__a:", "#{window_width}x#{window_height}")
        self.assertEqual(out.strip(), "80x20")


if __name__ == "__main__":
    unittest.main()
