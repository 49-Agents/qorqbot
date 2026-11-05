#!/usr/bin/env python3
"""Terminal spawn: the PTY env has no Claude Code child-session markers (pty_broker.clean_env), and
the shared dashboard mirrors a plain shell. Registered terminal preparation and tmux survival
are tested in biz-core/tests/test_service_network.py."""

import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import pty_broker  # noqa: E402

POLLUTED = {
    "CLAUDECODE": "1", "CLAUDE_CODE_CHILD_SESSION": "1", "CLAUDE_CODE_SESSION_ID": "s1",
    "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/x.sock", "CLAUDE_CODE_MESSAGING_TOKEN": "t",
    "CLAUDE_CODE_BRIDGE_SESSION_ID": "b", "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000",
    "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_EXECPATH": "/x/claude",
    "CLAUDE_PID": "1", "CLAUDE_EFFORT": "low", "AI_AGENT": "claude-code",
    "CLAUDE_CONFIG_DIR": "/home/u/.claude", "ANTHROPIC_API_KEY": "sk", "PATH": "/usr/bin:/bin", "HOME": "/home/u",
}
STRIPPED = {k for k in POLLUTED if k == "CLAUDECODE" or k.startswith("CLAUDE_CODE_") or k in ("CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENT")}


class TestCleanEnv(unittest.TestCase):
    def test_strips_markers_keeps_config(self):
        out = pty_broker.clean_env(POLLUTED)
        for k in STRIPPED:
            self.assertNotIn(k, out, k)
        for k in ("CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "PATH", "HOME"):
            self.assertEqual(out[k], POLLUTED[k])

    def test_default_base_is_os_environ(self):
        with mock.patch.dict(os.environ, POLLUTED, clear=True):
            out = pty_broker.clean_env()
        self.assertNotIn("CLAUDE_CODE_CHILD_SESSION", out)
        self.assertEqual(out["HOME"], "/home/u")

    def test_spawned_process_sees_clean_env(self):
        """Real PTY: run `env` under a polluted os.environ and read what the child got."""
        with mock.patch.dict(os.environ, POLLUTED):
            t = pty_broker.Terminal("t", ["/usr/bin/env"], "/", env={"EXTRA": "yes"})
        for _ in range(100):
            if t.done:
                break
            time.sleep(0.05)
        blob, _ = t.snapshot()
        t.stop()
        text = blob.decode("utf-8", "replace")
        for k in STRIPPED:
            self.assertNotIn(k + "=", text, k)
        self.assertIn("CLAUDE_CONFIG_DIR=/home/u/.claude", text)
        self.assertIn("EXTRA=yes", text)
        self.assertIn("TERM=xterm-256color", text)


class TestTerminalShutdown(unittest.TestCase):
    def test_generated_tmux_config_uses_application_state_not_home(self):
        self.assertEqual(pty_broker.TMUX_CONFIG_DIR, os.path.join(os.path.dirname(pty_broker.__file__), ".state"))
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(pty_broker, "TMUX_CONFIG_DIR", directory), mock.patch.object(
                    pty_broker.os.path, "expanduser", side_effect=AssertionError("No personal configuration access")):
                path = pty_broker._conf_path()
                self.assertEqual(os.path.dirname(path), directory)
                with open(path, encoding="utf-8") as stream:
                    self.assertEqual(stream.read(), pty_broker.TMUX_CONF)
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_stop_waits_for_reader_before_releasing_descriptor(self):
        entered = threading.Event()
        release = threading.Event()

        def paused_select(readers, _writers, _errors, _timeout):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release the PTY reader")
            return readers, [], []

        with mock.patch.object(pty_broker.select, "select", side_effect=paused_select):
            terminal = pty_broker.Terminal("shutdown", ["/bin/sleep", "60"], "/")
            stopper = threading.Thread(target=terminal.stop)
            descriptor = terminal.master_fd
            try:
                self.assertTrue(entered.wait(2))
                stopper.start()
                self.assertTrue(terminal._stopping.wait(2))
                self.assertTrue(stopper.is_alive())
                os.fstat(descriptor)
            finally:
                release.set()
                if stopper.ident is not None:
                    stopper.join(5)
                terminal.stop()

        self.assertFalse(stopper.is_alive())
        self.assertFalse(terminal._reader.is_alive())
        self.assertEqual(terminal.master_fd, -1)
        self.assertTrue(terminal.done)
        with tempfile.TemporaryFile() as unrelated:
            unrelated.write(b"authority must remain intact")
            unrelated.seek(0)
            with mock.patch.object(pty_broker.os, "write") as write:
                terminal.write(b"stale terminal input")
                write.assert_not_called()
            self.assertEqual(unrelated.read(), b"authority must remain intact")


class TestWaitOutput(unittest.TestCase):
    """Mirrors idle on the reader's condition: wait_output wakes on output, on exit, or on timeout."""

    def test_wakes_promptly_when_output_arrives(self):
        t = pty_broker.Terminal("wait-output", ["/bin/cat"], "/")
        try:
            _, cursor = t.snapshot()
            threading.Timer(0.15, t.write, [b"ping\n"]).start()
            start = time.monotonic()
            t.wait_output(cursor, timeout=5)
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 2, "wait_output must wake on the echo, not on timeout")
            self.assertGreater(t.seq, cursor, "the wake must follow appended output")
        finally:
            t.stop()

    def test_returns_on_timeout_without_output(self):
        t = pty_broker.Terminal("wait-idle", ["/bin/sleep", "30"], "/")
        try:
            time.sleep(0.2)  # let the shell's startup output (if any) settle
            _, cursor = t.snapshot()
            start = time.monotonic()
            t.wait_output(cursor, timeout=0.2)
            self.assertLess(time.monotonic() - start, 2)
        finally:
            t.stop()

    def test_wakes_when_terminal_ends(self):
        t = pty_broker.Terminal("wait-done", ["/bin/true"], "/")
        for _ in range(100):
            if t.done:
                break
            time.sleep(0.05)
        self.assertTrue(t.done)
        _, cursor = t.snapshot()
        start = time.monotonic()
        t.wait_output(cursor, timeout=5)
        self.assertLess(time.monotonic() - start, 1, "a finished terminal must not block waiters")


if __name__ == "__main__":
    unittest.main()
