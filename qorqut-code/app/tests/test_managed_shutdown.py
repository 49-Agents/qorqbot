"""``python -m qorqut.managed_runtime.shutdown``: closes every managed host of one installation for the updater.

Real detached host processes (the real host with the fake driver of ``test_managed_host``) are started through the launcher; nothing
here starts a real ``claude``. Linux only for the process tests.
"""
import isolated_cli_environment

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from qorqut.managed_runtime import launcher, shutdown, state as layout
from qorqut.managed_runtime.client import HostClient, HostUnavailable
from test_managed_host import wait_for


class ShutdownTestCase(unittest.TestCase):
    def setUp(self):
        self.environment = isolated_cli_environment.isolate(self)
        self.base = Path(tempfile.mkdtemp(prefix="sd-", dir="/tmp" if sys.platform == "darwin" else None)).resolve()
        self.addCleanup(self._remove)
        self.instance = self.base / "instance"
        self.states = self.instance / "operations" / ".runtime" / "managed-agents"
        self.launcher = launcher.DetachedLauncher()
        self.started = []
        self.addCleanup(self._cleanup_hosts)

    def _remove(self):
        import shutil
        shutil.rmtree(self.base, ignore_errors=True)

    def _cleanup_hosts(self):
        for state in self.started:
            try:
                HostClient(state).close("now")
            except HostUnavailable:
                pass
            launcher.wait_until_gone(state, 10)
            record = layout.read_json(state / layout.HOST_FILE) or {}
            for pid_key, identity_key in (("host_pid", "host_identity"), ("claude_pid", "claude_identity")):
                layout.terminate_process(record.get(pid_key), record.get(identity_key), 1.0, 1.0)

    def start_host(self, **changes):
        agent = str(uuid.uuid4())
        state = self.states / agent
        layout.private_directory(state)
        folder = self.base / ("workspace-" + agent[:8])
        folder.mkdir()
        spec = {"agent_id": agent, "folder": str(folder), "executable": "/usr/bin/claude-fake", "args": [],
                "env": {"FAKE_LOG": str(self.base / "fake.log")}, "resume_session_id": None, "send_role": False,
                "idle_seconds": 0, "close_seconds": 5, "driver": "test_managed_host:FakeSession"}
        spec.update(changes)
        self.started.append(state)
        return launcher.start_host(state, spec, launcher=self.launcher, timeout=20), state

    def entries(self, report):
        return {entry["agent_id"]: entry for entry in report["agents"]}


class NothingToCloseTest(ShutdownTestCase):
    def test_no_state_folder_at_all_is_fine(self):
        report = shutdown.shutdown(self.instance, 1)
        self.assertEqual((report["ok"], report["agents"], report["remaining"]), (True, [], []))

    def test_an_empty_state_folder_and_stray_files_are_fine(self):
        self.states.mkdir(parents=True)
        (self.states / "note.txt").write_text("x")
        outside = self.base / "elsewhere"
        outside.mkdir()
        (self.states / "link").symlink_to(outside)
        report = shutdown.shutdown(self.instance, 1)
        self.assertTrue(report["ok"])
        self.assertEqual(report["agents"], [])

    def test_the_command_prints_json_and_exits_zero(self):
        environment = dict(os.environ, PYTHONPATH=os.pathsep.join(os.path.abspath(p) for p in sys.path if p))
        done = subprocess.run([sys.executable, "-m", "qorqut.managed_runtime.shutdown", "--instance-root", str(self.instance),
                               "--timeout", "1"], capture_output=True, text=True, env=environment, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["ok"], True)

    def test_a_bad_timeout_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as raised:
            shutdown.main(["--instance-root", str(self.instance), "--timeout", "-1"])
        self.assertEqual(raised.exception.code, 2)


class StaleFilesTest(ShutdownTestCase):
    def stale_state(self):
        agent = str(uuid.uuid4())
        state = self.states / agent
        layout.private_directory(state)
        (state / layout.LOCK_FILE).write_text("")
        layout.write_private(state / layout.TOKEN_FILE, b"token")
        layout.write_json_private(state / layout.HOST_FILE, {
            "schema_version": layout.SCHEMA_VERSION, "agent_id": agent, "instance": "i", "host_pid": 2 ** 22 + 12345,
            "host_identity": "boot:1", "protocol": layout.PROTOCOL, "state": "ready", "started_at": layout.now_iso(),
            "claude_pid": 2 ** 22 + 12346, "claude_identity": "boot:2", "socket_path": str(state / layout.SOCKET_FILE),
            "socket_fallback": False})
        short = Path(tempfile.mkdtemp(prefix="s-", dir="/tmp"))
        self.addCleanup(__import__("shutil").rmtree, short, True)
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(short / layout.SOCKET_FILE))
        listener.close()                           # a socket file nobody listens on, at the fallback place the record names
        record = layout.read_json(state / layout.HOST_FILE)
        record.update(socket_path=str(short / layout.SOCKET_FILE), socket_fallback=True)
        layout.write_json_private(state / layout.HOST_FILE, record)
        self.socket_file = short / layout.SOCKET_FILE
        return agent, state

    def test_stale_files_are_cleaned_and_a_second_run_finds_nothing(self):
        agent, state = self.stale_state()
        first = shutdown.shutdown(self.instance, 1)
        self.assertTrue(first["ok"])
        self.assertEqual(self.entries(first)[agent]["result"], "cleaned")
        self.assertFalse((state / layout.TOKEN_FILE).exists())
        self.assertFalse(self.socket_file.exists())
        self.assertEqual(layout.read_host(state)["state"], "failed")
        second = shutdown.shutdown(self.instance, 1)
        self.assertTrue(second["ok"])
        self.assertEqual(self.entries(second)[agent]["result"], "absent")

    def test_a_directory_without_any_record_is_absent(self):
        (self.states / "empty").mkdir(parents=True)
        report = shutdown.shutdown(self.instance, 1)
        self.assertEqual([(e["agent_id"], e["result"]) for e in report["agents"]], [("empty", "absent")])


class LockHeldByStrangerTest(ShutdownTestCase):
    def test_a_lock_nobody_can_show_to_be_a_host_is_reported_and_left_alone(self):
        state = self.states / "stranger"
        layout.private_directory(state)
        import fcntl
        descriptor = os.open(state / layout.LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        report = shutdown.shutdown(self.instance, 0.3, close_timeout=0.3)
        self.assertFalse(report["ok"])
        self.assertEqual(report["remaining"], ["stranger"])
        self.assertEqual(self.entries(report)["stranger"]["reason"], "host_unverified")
        self.assertEqual(shutdown.main(["--instance-root", str(self.instance), "--timeout", "0.2"]), 1)


@unittest.skipUnless(sys.platform == "linux", "process tests run on Linux here")
class LiveHostTest(ShutdownTestCase):
    def test_a_live_host_is_closed_gracefully_and_a_second_run_finds_nothing(self):
        first_host, first_state = self.start_host()
        second_host, second_state = self.start_host()
        report = shutdown.shutdown(self.instance, 20)
        self.assertTrue(report["ok"], report)
        entries = self.entries(report)
        self.assertEqual({entries[first_state.name]["result"], entries[second_state.name]["result"]}, {"closed"})
        for verified in (first_host, second_host):
            wait_for(lambda: not layout.process_alive(verified.host_pid, verified.host_identity), 10, message="host gone")
            self.assertFalse(layout.process_alive(verified.claude_pid, verified.claude_identity))
        again = shutdown.shutdown(self.instance, 5)
        self.assertTrue(again["ok"])
        self.assertEqual({entry["result"] for entry in again["agents"]}, {"absent"})

    def test_a_host_that_does_not_answer_is_ended_by_pid_with_its_client(self):
        verified, state = self.start_host()
        self.assertTrue(layout.descends_from(verified.claude_pid, verified.host_pid))
        os.kill(verified.host_pid, signal.SIGSTOP)           # holds the lock, answers nothing
        report = shutdown.shutdown(self.instance, 0.5, close_timeout=0.5)
        self.assertTrue(report["ok"], report)
        entry = self.entries(report)[state.name]
        self.assertEqual(entry["result"], "ended")
        self.assertTrue(entry["verified_host"])
        self.assertFalse(layout.process_alive(verified.host_pid, verified.host_identity))
        self.assertFalse(layout.process_alive(verified.claude_pid, verified.claude_identity))
        self.assertFalse(layout.lock_held(state / layout.LOCK_FILE))
        self.assertFalse((state / layout.SOCKET_FILE).exists())
        self.assertEqual(shutdown.shutdown(self.instance, 1)["ok"], True)

    def test_a_killed_host_leaves_a_client_that_is_ended(self):
        verified, state = self.start_host()
        os.kill(verified.host_pid, signal.SIGKILL)
        wait_for(lambda: not layout.process_alive(verified.host_pid, verified.host_identity), 10, message="host gone")
        report = shutdown.shutdown(self.instance, 1)
        self.assertTrue(report["ok"], report)
        self.assertIn(self.entries(report)[state.name]["result"], ("cleaned", "ended"))
        wait_for(lambda: not layout.process_alive(verified.claude_pid, verified.claude_identity), 10, message="client gone")

    def test_a_reused_pid_is_never_touched(self):
        """A record that names a live process with another start time (a reused pid) must not be signalled."""
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: (bystander.kill(), bystander.wait()))
        state = self.states / str(uuid.uuid4())
        layout.private_directory(state)
        layout.write_json_private(state / layout.HOST_FILE, {
            "schema_version": layout.SCHEMA_VERSION, "agent_id": state.name, "instance": "i", "host_pid": bystander.pid,
            "host_identity": "wrong-boot:1", "protocol": layout.PROTOCOL, "state": "ready", "started_at": layout.now_iso(),
            "claude_pid": bystander.pid, "claude_identity": "wrong-boot:1", "socket_path": str(state / layout.SOCKET_FILE),
            "socket_fallback": False})
        report = shutdown.shutdown(self.instance, 0.5)
        self.assertTrue(report["ok"])
        time.sleep(0.2)
        self.assertIsNone(bystander.poll())


if __name__ == "__main__":
    unittest.main()
