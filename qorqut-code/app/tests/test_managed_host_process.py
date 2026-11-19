"""The second half of ``test_managed_host``: the socket path, the real detached host process (``SubprocessHostTest``), the launcher,
readiness, bounds, the lock probe and the stale-client checks. Split off so each module runs well under a minute. The fake driver
and the base fixtures are the first module's; the fake is still named in ``launch.json`` as ``test_managed_host:FakeSession``."""
import isolated_cli_environment

import collections
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from qorqut.managed_runtime import host as host_module, launcher, protocol, state as layout
from qorqut.managed_runtime.client import HostClient, HostUnavailable

from test_managed_host import (FakeSession, InProcessHost, ManagedTestCase, PROTECTED, SLEEPER, new_uuid, settle,  # noqa: F401
                               wait_for)


class SocketPathTest(ManagedTestCase):
    def deep_state(self):
        directory = self.root
        while len(os.fsencode(str(directory / self.agent / layout.SOCKET_FILE))) < layout.SOCKET_PATH_LIMIT + 10:
            directory = directory / ("nested-directory-%d" % len(directory.parts))
        state = directory / self.agent
        layout.private_directory(state)
        return state

    def test_socket_path_for_uses_the_state_directory_when_it_fits(self):
        path, fallback = layout.socket_path_for(self.state)
        self.assertEqual((path, fallback), (self.state / layout.SOCKET_FILE, False))

    def test_a_too_deep_state_directory_gets_a_short_private_socket_path(self):
        state = self.deep_state()
        path, fallback = layout.socket_path_for(state)
        self.addCleanup(lambda: path.parent.exists() and path.parent.rmdir())
        self.assertTrue(fallback)
        self.assertTrue(layout.socket_fits(path))
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path, layout.socket_path_for(state)[0])          # stable for the same directory

    def test_a_host_in_a_deep_directory_works_and_records_the_real_path(self):
        state = self.deep_state()
        running = InProcessHost(self, state=state)
        client = running.ready()
        record = layout.read_host(state)
        self.assertTrue(record["socket_fallback"])
        self.assertTrue(layout.socket_fits(record["socket_path"]))
        self.assertFalse((state / layout.SOCKET_FILE).exists())
        self.assertEqual(client.hello()["status"], "ok")
        client.close()
        running.join()
        self.assertFalse(Path(record["socket_path"]).exists())
        self.assertFalse(Path(record["socket_path"]).parent.exists())


# -- real processes --------------------------------------------------------------------------------------------------------

CLIENT_PROGRAM = """
import sys, uuid
from qorqut.managed_runtime.client import HostClient
client = HostClient(sys.argv[1])
client.send("SLOW work", sys.argv[2], confirm_seconds=0)
print("sent", flush=True)
import time
time.sleep(600)
"""


def host_cmdline(pid) -> bytes:
    """The host's argument vector, NUL separated, from /proc on Linux and from process_info (psutil) on macOS."""
    if sys.platform == "linux":
        return Path("/proc/%d/cmdline" % pid).read_bytes()
    from qorqut import process_info
    return b"\0".join(item.encode() for item in process_info.cmdline(pid)) + b"\0"


def host_cwd(pid) -> str:
    if sys.platform == "linux":
        return os.readlink("/proc/%d/cwd" % pid)
    from qorqut import process_info
    return str(process_info.cwd(pid))


class SubprocessHostTest(ManagedTestCase):
    """A real detached host process with the fake driver, started through the launcher. Linux only here."""

    def setUp(self):
        super().setUp()
        self.launcher = launcher.DetachedLauncher()
        self.spawned = []
        self.addCleanup(self.cleanup_hosts)

    def cleanup_hosts(self):
        try:
            HostClient(self.state).close("now")
        except HostUnavailable:
            pass
        launcher.wait_until_gone(self.state, 15)
        for process in self.spawned:
            if process.poll() is None:
                process.kill()
            process.wait()

    def start(self, **changes):
        changes.setdefault("driver", "test_managed_host:FakeSession")
        changes.setdefault("idle_seconds", 0)
        changes.setdefault("env", {})["FAKE_SECRET"] = "SECRET-VALUE-0451"
        return launcher.start_host(self.state, self.spec(**changes), launcher=self.launcher, timeout=20)

    def test_a_host_survives_its_clients_death_and_a_fresh_client_finds_verifies_and_drives_it(self):
        verified = self.start()
        self.assertNotEqual(verified.host_pid, os.getpid())
        self.assertEqual(verified.owner, layout.host_owner(verified.host_pid, verified.host_identity))
        self.assertTrue(layout.descends_from(verified.claude_pid, verified.host_pid))
        self.assertEqual(self.logged("start")[0]["secret"], "SECRET-VALUE-0451")
        # the secret travelled in launch.json only: not in argv, not in the host's own environment, not on disk afterwards
        self.assertNotIn(b"SECRET-VALUE-0451", host_cmdline(verified.host_pid))
        if sys.platform == "linux":       # macOS shows a process's environment to nobody but the process itself
            self.assertNotIn(b"SECRET-VALUE-0451", Path("/proc/%d/environ" % verified.host_pid).read_bytes())
        self.assertFalse((self.state / layout.LAUNCH_FILE).exists())
        for path in self.state.iterdir():
            if path.is_file():
                self.assertNotIn(b"SECRET-VALUE-0451", path.read_bytes(), path.name)

        # a client process starts a long turn and is killed
        slow = new_uuid()
        environment = dict(os.environ, PYTHONPATH=os.pathsep.join(os.path.abspath(p) for p in sys.path if p))
        process = subprocess.Popen([sys.executable, "-c", CLIENT_PROGRAM, str(self.state), slow], stdout=subprocess.PIPE, env=environment)
        self.spawned.append(process)
        self.assertEqual(process.stdout.readline().strip(), b"sent")
        process.kill()
        process.wait()
        process.stdout.close()

        self.assertTrue(layout.process_alive(verified.host_pid, verified.host_identity))
        found = launcher.running_hosts(self.root / "managed-agents")
        self.assertEqual(list(found), [self.agent])
        fresh = found[self.agent]
        self.assertEqual((fresh.host_pid, fresh.instance, fresh.claude_pid), (verified.host_pid, verified.instance, verified.claude_pid))
        client = fresh.client()
        self.assertEqual(settle(client, "working")["inflight"], [slow])
        self.assertEqual(client.lookup(slow)["seen"], True)
        self.assertEqual(client.lookup(slow)["turn_started"], True)

        # send, lookup, interrupt, answer against the real process
        self.assertEqual(client.send("nope", new_uuid())["reason"], "turn_running")
        self.assertEqual(client.interrupt()["sent"], True)
        self.assertEqual(settle(client)["last_turn_status"], "interrupted")
        client.send("ASK go", new_uuid())
        pending = settle(client, "blocked")["pending"][0]
        self.assertEqual(client.answer(pending["request_id"], True)["delivered"], True)
        settle(client)
        self.assertEqual(client.events()["events"][-1]["type"], "turn_finished")

        # and it closes cleanly: exit 0, claude gone, files tidy
        self.assertEqual(client.close()["state"], "closing")
        self.assertTrue(launcher.wait_until_gone(self.state, 20))
        wait_for(lambda: self.launcher.exit_codes.get(str(self.state)) is not None, message="the host to be reaped")
        self.assertEqual(self.launcher.exit_codes[str(self.state)], layout.EXIT_STOPPED)
        self.assertFalse(layout.process_alive(verified.claude_pid, verified.claude_identity))
        self.assertEqual(layout.read_host(self.state)["exit"], {"code": 0, "reason": "requested"})
        self.assertFalse((self.state / layout.SOCKET_FILE).exists())
        self.assertEqual(launcher.inspect_host(self.state).status, "absent")

    def test_idle_close_in_a_real_process(self):
        verified = self.start(idle_seconds=1.0)
        client = verified.client()
        client.send("hello", new_uuid())
        self.assertTrue(launcher.wait_until_gone(self.state, 20))
        wait_for(lambda: self.launcher.exit_codes.get(str(self.state)) is not None, message="reaped")
        self.assertEqual(self.launcher.exit_codes[str(self.state)], 0)
        self.assertEqual(layout.read_host(self.state)["exit"]["reason"], "idle")
        self.assertFalse(layout.process_alive(verified.claude_pid, verified.claude_identity))

    def test_a_second_host_process_exits_75_and_a_second_start_reuses_the_first(self):
        verified = self.start()
        second = subprocess.run(launcher.host_argv(self.state), env=launcher.host_environment(), cwd=self.state,
                                capture_output=True, timeout=30)
        self.assertEqual(second.returncode, layout.EXIT_BUSY)
        self.assertEqual(verified.client().hello()["status"], "ok")
        again = launcher.start_host(self.state, self.spec(), launcher=self.launcher, timeout=5)
        self.assertEqual((again.host_pid, again.instance), (verified.host_pid, verified.instance))
        self.assertFalse((self.state / layout.LAUNCH_FILE).exists())          # nothing new was written for a reused host

    def test_a_directory_that_does_not_check_out_is_unverified_and_left_alone(self):
        verified = self.start()
        record = layout.read_host(self.state)
        layout.write_json_private(self.state / layout.HOST_FILE, {**record, "host_identity": "boot:1"})
        inspection = launcher.inspect_host(self.state)
        self.assertEqual((inspection.status, inspection.reason), ("unverified", "host_identity"))
        self.assertEqual(launcher.running_hosts(self.root / "managed-agents", clean=True), {})
        with self.assertRaises(launcher.LaunchError) as caught:
            launcher.start_host(self.state, self.spec(), launcher=self.launcher, timeout=2)
        self.assertEqual(caught.exception.reason, "host_unverified")
        self.assertTrue(layout.process_alive(verified.host_pid, verified.host_identity))     # nothing was killed
        self.assertTrue((self.state / layout.SOCKET_FILE).exists())                           # nothing was cleaned
        layout.write_json_private(self.state / layout.HOST_FILE, {**record, "host_command": 1})
        self.assertEqual(launcher.inspect_host(self.state).status, "running")

    def test_a_host_that_claims_another_agent_or_runs_other_code_is_not_verified(self):
        verified = self.start()
        with mock.patch.object(layout, "host_command_matches", return_value=False):
            self.assertEqual(launcher.inspect_host(self.state).reason, "host_command")
        with mock.patch.object(layout, "descends_from", return_value=False):
            self.assertEqual(launcher.inspect_host(self.state).reason, "claude_not_descendant")
        self.assertIsNotNone(launcher.verify_host(self.state))
        self.assertTrue(layout.host_command_matches(verified.host_pid, self.state))
        self.assertFalse(layout.host_command_matches(os.getpid(), self.state))

    def test_stale_files_of_a_dead_host_are_cleaned_and_the_next_start_works(self):
        verified = self.start()
        os.kill(verified.host_pid, signal.SIGKILL)
        os.killpg(verified.claude_pid, signal.SIGKILL)
        wait_for(lambda: not layout.lock_held(self.state / layout.LOCK_FILE), message="the lock to be released")
        self.assertEqual(launcher.inspect_host(self.state).status, "absent")
        self.assertTrue((self.state / layout.TOKEN_FILE).exists())
        launcher.running_hosts(self.root / "managed-agents", clean=True)
        self.assertFalse((self.state / layout.TOKEN_FILE).exists())
        self.assertEqual(layout.read_host(self.state)["state"], "failed")
        again = self.start()
        self.assertNotEqual(again.instance, verified.instance)
        self.assertEqual(again.client().hello()["status"], "ok")

    def test_a_host_that_cannot_start_the_cli_fails_start_with_the_reason(self):
        with self.assertRaises(launcher.LaunchError) as caught:
            self.start(env={"FAKE_START_FAIL": "1"})
        self.assertEqual(caught.exception.reason, "driver_unavailable")
        self.assertFalse((self.state / layout.LAUNCH_FILE).exists())

    def test_a_host_that_never_comes_up_does_not_leave_the_launch_file(self):
        class Dud:
            def start(self, state_dir):
                return True
        with self.assertRaises(launcher.LaunchError):
            launcher.start_host(self.state, self.spec(), launcher=Dud(), timeout=0.5)
        self.assertFalse((self.state / layout.LAUNCH_FILE).exists())


class LauncherTest(ManagedTestCase):
    def test_prepare_launch_writes_a_private_file_and_refuses_bad_specs(self):
        record = launcher.prepare_launch(self.state, self.spec())
        path = self.state / layout.LAUNCH_FILE
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text())["token"], record["token"])
        self.assertGreaterEqual(len(record["token"]), 32)
        for bad in ({"agent_id": new_uuid()}, {"folder": "relative"}, {"folder": str(self.root / "missing")}, {"args": [1]},
                    {"env": {"A": 1}}, {"executable": ""}):
            with self.assertRaises(launcher.LaunchError, msg=str(bad)) as caught:
                launcher.prepare_launch(self.state, self.spec(**bad))
            self.assertEqual(caught.exception.reason, "invalid_launch")

    def test_the_host_environment_is_an_allow_list_not_the_services_environment(self):
        with mock.patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "leak", "QORQUT_TOKEN": "leak", "HTTPS_PROXY": "http://proxy"}):
            environment = launcher.host_environment({"EXTRA": "1"})
        self.assertNotIn("leak", json.dumps(environment))
        self.assertEqual((environment["HTTPS_PROXY"], environment["EXTRA"], environment["HOME"]), ("http://proxy", "1", os.environ["HOME"]))
        self.assertTrue(all(os.path.isabs(item) for item in environment["PYTHONPATH"].split(os.pathsep)))

    def test_the_detached_launcher_reports_an_already_running_host_and_starts_nothing(self):
        InProcessHost(self).ready()
        with mock.patch.object(launcher.subprocess, "Popen") as popen:
            self.assertFalse(launcher.DetachedLauncher().start(self.state))
        popen.assert_not_called()

    def test_default_launcher_follows_the_browser_rule(self):
        with mock.patch.dict(os.environ, {"QORQUT_FOLDER_ROOT": str(self.root)}):
            self.assertIsInstance(launcher.default_launcher(), launcher.DetachedLauncher)
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("QORQUT_FOLDER_ROOT", None)
            os.environ.pop("QORQUT_NATIVE_ROOT", None)
            with mock.patch.object(sys, "platform", "linux"):
                self.assertIsInstance(launcher.default_launcher(), launcher.UserUnitLauncher)
            with mock.patch.object(sys, "platform", "darwin"):
                self.assertIsInstance(launcher.default_launcher(), launcher.DetachedLauncher)


@unittest.skipUnless(sys.platform == "linux", "systemd is Linux only")
class UserUnitLauncherTest(ManagedTestCase):
    def runner(self, active=False):
        calls = []

        def run(argv, **options):
            calls.append((argv, options))
            if "show" in argv:
                return subprocess.CompletedProcess(argv, 0, "LoadState=loaded\nActiveState=%s\n" % ("active" if active else "inactive"), "")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return run, calls

    def test_the_unit_is_derived_transient_and_carries_no_secret(self):
        run, calls = self.runner()
        record = launcher.prepare_launch(self.state, self.spec(env={"FAKE_SECRET": "SECRET-VALUE-0451"}))
        with mock.patch.dict(os.environ, {"QORQUT_TOKEN": "leak"}):
            self.assertTrue(launcher.UserUnitLauncher(runner=run, outside=lambda p: Path(p)).start(self.state))
        argv = calls[-1][0]
        unit = launcher.unit_name(self.state)
        self.assertRegex(unit, r"^qorqut-managed-[0-9a-f]{12}-%s\.service$" % self.agent)
        self.assertIn("--user", argv)
        self.assertIn("--unit=" + unit, argv)
        self.assertIn("--collect", argv)
        self.assertEqual(argv[argv.index("--") + 1:][1:], launcher.host_argv(self.state)[1:])
        self.assertIn("--working-directory=/", argv)
        joined = " ".join(argv)
        self.assertNotIn("SECRET-VALUE-0451", joined)
        self.assertNotIn(record["token"], joined)
        self.assertNotIn("leak", joined)
        self.assertIn("--property=StandardOutput=append:%s/host.log" % self.state, argv)

    def test_a_unit_that_is_already_running_is_reused_not_duplicated(self):
        run, calls = self.runner(active=True)
        self.assertFalse(launcher.UserUnitLauncher(runner=run, outside=lambda p: Path(p)).start(self.state))
        self.assertFalse([c for c in calls if "--unit=" + launcher.unit_name(self.state) in c[0]])

    def test_an_unreachable_user_manager_is_reported_precisely(self):
        def run(argv, **options):
            return subprocess.CompletedProcess(argv, 1, "", "Failed to connect to bus")
        with self.assertRaises(launcher.LaunchError) as caught:
            launcher.UserUnitLauncher(runner=run).start(self.state)
        self.assertEqual(caught.exception.reason, "systemd_unavailable")

    def test_unit_names_differ_per_installation_and_agent(self):
        other = self.root / "elsewhere" / self.agent
        self.assertNotEqual(launcher.unit_name(self.state), launcher.unit_name(other))
        self.assertTrue(launcher.UNIT.fullmatch(launcher.unit_name(self.state)))


class StateHelpersTest(ManagedTestCase):
    def test_host_owner_round_trip(self):
        self.assertEqual(layout.host_owner(42, "boot-id:98765"), "host:42:98765")
        self.assertEqual(layout.parse_host_owner("host:42:98765"), (42, "98765"))
        for bad in ("42:98765", "host:x:1", "host:1", None, "host:1:2:3"):
            self.assertIsNone(layout.parse_host_owner(bad))

    def test_private_files_are_refused_when_others_can_read_them(self):
        path = self.state / "x.json"
        path.write_text("{}")
        os.chmod(path, 0o644)
        self.assertIsNone(layout.read_json(path))
        os.chmod(path, 0o600)
        self.assertEqual(layout.read_json(path), {})
        link = self.state / "link.json"
        link.symlink_to(path)
        self.assertIsNone(layout.read_json(link))
        big = self.state / "big.json"
        layout.write_private(big, b"[" + b"1," * 100 + b"1]")
        self.assertIsNone(layout.read_json(big, limit=10))

    def test_descends_from_walks_parents_and_stops(self):
        self.assertTrue(layout.descends_from(os.getpid(), os.getpid()))
        self.assertTrue(layout.descends_from(os.getpid(), os.getppid()))
        self.assertFalse(layout.descends_from(os.getpid(), os.getpid() + 100000))
        child = subprocess.Popen([sys.executable, "-c", SLEEPER])
        self.addCleanup(lambda: (child.kill(), child.wait()))
        self.assertTrue(layout.descends_from(child.pid, os.getpid()))
        self.assertFalse(layout.descends_from(os.getpid(), child.pid))

    def test_lock_held_never_creates_the_file(self):
        path = self.state / "probe.lock"
        self.assertFalse(layout.lock_held(path))
        self.assertFalse(path.exists())


# -- readiness, ledger outcomes and bounds (reviews of the real run) ------------------------------------------------------

class ReadinessTest(ManagedTestCase):
    def test_a_cli_that_announces_its_session_only_after_the_first_message_is_ready_without_waiting_for_it(self):
        started = time.monotonic()
        running = InProcessHost(self)                               # the fake's default is the real CLI's: init only after a message
        client = running.ready()
        self.assertLess(time.monotonic() - started, 2.0, "ready must not wait for a fixed 3 s")
        self.assertEqual(running.host.launch["ready_seconds"], host_module.DEFAULT_READY_SECONDS)
        self.assertLess(host_module.DEFAULT_READY_SECONDS, 1.0)
        status = client.status()
        self.assertEqual((status["phase"], status["state"]), ("idle", "ready"))
        self.assertEqual(status["session_id"], FakeSession.last.session_id)          # the id the driver chose
        self.assertEqual(status["transcript_path"], FakeSession.last.transcript_path)
        self.assertFalse(status["transcript_exists"])                                 # nothing processed yet: absent is not failure
        self.assertFalse(client.hello()["transcript_exists"])
        self.assertEqual(client.events()["events"][-1]["type"], "host_state")
        self.assertNotIn("session_started", [e["type"] for e in client.events()["events"]])
        reply = client.send("hello", new_uuid())                                      # accepted at once, not session_starting
        self.assertEqual((reply["status"], reply["mode"]), ("ok", "started"))
        status = wait_for(lambda: (lambda s: s if s["phase"] == "idle" and s["last_turn_status"] else None)(client.status()),
                          message="the turn")
        self.assertTrue(status["transcript_exists"])
        self.assertEqual(layout.read_host(self.state)["session_id"], FakeSession.last.session_id)

    def test_a_claude_that_dies_right_away_never_becomes_ready(self):
        class Dies(FakeSession):
            def start(inner):
                super().start()
                inner.crash()
        launcher.prepare_launch(self.state, self.spec())
        self.assertEqual(host_module.ManagedHost(self.state, driver_class=Dies).run(threading.Event()), layout.EXIT_FAILED)
        self.assertEqual(layout.read_host(self.state)["exit"]["reason"], "claude_exited")
        self.assertNotIn("ready", [e["state"] for e in layout.read_events_file(self.state) if e["type"] == "host_state"])

    def test_a_resumed_session_whose_claude_exits_before_any_message_or_announcement_is_resume_failed(self):
        class DiesLater(FakeSession):
            def start(inner):
                super().start()
                threading.Timer(1.0, inner.crash).start()       # after the settle time: the host is ready by then
        launcher.prepare_launch(self.state, self.spec(resume_session_id=new_uuid(), ready_seconds=0.2))
        self.assertEqual(host_module.ManagedHost(self.state, driver_class=DiesLater).run(threading.Event()), layout.EXIT_FAILED)
        self.assertEqual(layout.read_host(self.state)["exit"]["reason"], "resume_failed")


class BlockedOutcomeTest(ManagedTestCase):
    def test_a_steered_message_a_hook_blocked_is_finished_with_outcome_blocked_everywhere(self):
        client = InProcessHost(self).ready()
        first, second = new_uuid(), new_uuid()
        client.send("SLOW hold the tool", first)
        settle(client, "working")
        self.assertEqual(client.send("STEERBLOCK DONE", second, steer=True)["mode"], "queued")
        status = settle(client)
        # the driver's turn_finished lists the run's message under message_uuids and the blocked one only under blocked_message_uuids
        raw = [e for e in client.events()["events"] if e["type"] == "turn_finished"][-1]
        self.assertEqual((raw["message_uuids"], raw["blocked_message_uuids"]), ([first], [second]))
        last = status["last_turn"]
        self.assertEqual(last["message_uuids"], [first, second])                 # a waiter on message_uuids sees both
        self.assertEqual(last["outcomes"], {first: "completed", second: "blocked"})
        self.assertEqual(last["blocked_message_uuids"], [second])
        self.assertEqual(status["inflight"], [])
        found = client.lookup(second)
        self.assertEqual((found["seen"], found["finished"], found["outcome"]), (True, True, "blocked"))
        self.assertEqual((client.lookup(first)["finished"], client.lookup(first)["outcome"]), (True, "completed"))
        self.assertEqual(client.lookup(new_uuid())["seen"], False)

    def test_a_plain_hook_blocked_message_has_outcome_blocked_and_turn_started_keeps_its_meaning(self):
        client = InProcessHost(self).ready()
        value = new_uuid()
        reply = client.send("BLOCK me", value, confirm_seconds=3)
        self.assertEqual((reply["status"], reply["turn_started"]), ("ok", True))     # "the CLI took it up", even for a blocked prompt
        status = settle(client)
        self.assertEqual(status["last_turn"]["outcomes"], {value: "blocked"})
        self.assertEqual(client.lookup(value)["outcome"], "blocked")


class IdleCloseRaceTest(ManagedTestCase):
    def test_the_idle_decision_and_the_closing_state_are_one_critical_section(self):
        seen = {}
        original = host_module.ManagedHost._close_session

        def watching(host, mode, reason):
            # a send that arrives now must already be turned away: the host decided to close in the same lock hold that saw it idle
            seen["phase"] = host.phase_state
            seen["reply"] = host._op_send("r", {"text": "late", "message_uuid": new_uuid(), "steer": False, "confirm_seconds": 0})
            return original(host, mode, reason)

        with mock.patch.object(host_module.ManagedHost, "_close_session", watching):
            running = InProcessHost(self, idle_seconds=0.3)
            running.ready()
            running.join()
        self.assertEqual(seen["phase"], "closing")
        self.assertEqual((seen["reply"]["status"], seen["reply"]["reason"]), ("rejected", "session_closing"))
        self.assertEqual(self.logged("send"), [])
        self.assertEqual(layout.read_host(self.state)["exit"]["reason"], "idle")

    def test_a_message_accepted_just_before_the_idle_deadline_is_never_interrupted(self):
        running = InProcessHost(self, idle_seconds=0.25)
        client = running.ready()
        accepted = []
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and running.thread.is_alive():
            try:
                reply = client.send("quick", new_uuid(), confirm_seconds=0)
            except HostUnavailable:
                break
            if reply["status"] == "ok":
                accepted.append(reply["message_uuid"])
            elif reply["reason"] not in ("turn_running", "host_busy"):
                break
            time.sleep(0.01)
        running.join()
        outcomes = [e for e in layout.read_events_file(self.state) if e["type"] == "turn_finished"]
        self.assertTrue(accepted)
        self.assertNotIn("interrupted", [e["status"] for e in outcomes])


class AnswerRestrictionTest(ManagedTestCase):
    def test_only_allow_deny_and_answers_reach_the_driver(self):
        client = InProcessHost(self).ready()
        client.send("ASK write", new_uuid())
        pending = settle(client, "blocked")["pending"][0]
        reply = client.answer(pending["request_id"], {"behavior": "allow", "updated_input": {"file_path": "/etc/passwd"},
                                                      "updated_permissions": [{"type": "addRules"}], "message": "ok", "extra": 1})
        self.assertEqual(reply["delivered"], True)
        self.assertEqual(self.logged("answer")[0]["decision"], {"behavior": "allow", "message": "ok"})

    def test_a_decision_the_driver_refuses_is_a_bad_request_and_the_request_stays_pending(self):
        client = InProcessHost(self).ready()
        client.send("ASK write", new_uuid())
        pending = settle(client, "blocked")["pending"][0]
        with mock.patch.object(FakeSession, "answer", side_effect=ValueError("an explicit allow or deny is required")):
            with self.assertRaises(HostUnavailable) as caught:
                client.answer(pending["request_id"], {"answers": {"x": "y"}})
        self.assertEqual(caught.exception.reason, "protocol")
        self.assertEqual([item["request_id"] for item in client.status()["pending"]], [pending["request_id"]])
        self.assertEqual(client.answer(pending["request_id"], True)["delivered"], True)


class BoundsTest(ManagedTestCase):
    """The hardening around what the owner is shown applies to questions now: ``pending`` never holds anything else."""

    def emit_question(self, request_id, **fields):
        questions = fields.pop("questions", [{"question": "Which?", "options": [{"label": "A"}]}])
        base = {"request_id": request_id, "questions": questions, "input": {"questions": questions}, "input_truncated": False,
                "tool_use_id": "tu"}
        FakeSession.last._emit("question", **{**base, **fields})

    def test_pending_items_keep_the_whole_question_up_to_a_bound(self):
        client = InProcessHost(self).ready()
        questions = [{"question": "Which? " + "x" * 12000, "options": [{"label": "A"}]}]
        self.emit_question("req-full", questions=questions)
        item = wait_for(lambda: client.status()["pending"] or None, message="the request")[0]
        self.assertEqual((item["kind"], item["tool"], item["questions"], item["input_truncated"]),
                         ("question", "AskUserQuestion", questions, False))

    def test_an_oversized_question_cannot_make_status_unreadable_and_its_id_survives_in_the_log(self):
        client = InProcessHost(self).ready()
        self.emit_question("req-huge", suggestions=[{"type": "addRules", "rules": ["y" * 200000]}] * 30,
                           questions=[{"question": "q" * 300000}], title="t" * 100000)
        item = wait_for(lambda: client.status()["pending"] or None, message="the request")[0]
        self.assertEqual(item["request_id"], "req-huge")
        self.assertLessEqual(len(json.dumps(item)), host_module.MAX_PENDING_ITEM_BYTES)
        self.assertTrue(item["input_truncated"])
        self.assertLess(len(protocol.encode(client.status())), protocol.MAX_LINE)
        self.assertEqual(client.answer("req-huge", False)["delivered"], False)           # the fake does not know it; the host copes
        events = [e for e in client.events()["events"] if e["type"] == "question"]
        self.assertEqual(events[-1]["request_id"], "req-huge")
        logged = [e for e in layout.read_events_file(self.state) if e["type"] == "question"]
        self.assertEqual(logged[-1]["request_id"], "req-huge")
        self.assertLess((self.state / layout.EVENTS_FILE).stat().st_size, 256 * 1024)

    def test_the_event_log_keeps_ids_when_it_has_to_cut_an_event(self):
        log = host_module.EventLog(self.state / "e.jsonl", 10)
        entry = log.append("turn_finished", {"status": "error", "message_uuids": [new_uuid()], "error": "e" * 100,
                                             "request_id": "req-1", "big": ["x" * 1000] * 200})
        self.assertTrue(entry["truncated"])
        stored = json.loads((self.state / "e.jsonl").read_text().splitlines()[-1])
        self.assertEqual((stored["type"], stored["request_id"], stored["status"], stored["truncated"]), ("turn_finished", "req-1", "error", True))
        self.assertEqual(len(stored["message_uuids"]), 1)
        self.assertNotIn("big", stored)
        log.close()

    def test_many_oversized_questions_still_fit_in_a_status_reply(self):
        client = InProcessHost(self).ready()
        for index in range(host_module.MAX_PENDING):
            self.emit_question("req-%d" % index, questions=[{"question": "c" * 15000}], title="t" * 400)
        status = wait_for(lambda: (lambda s: s if len(s["pending"]) == host_module.MAX_PENDING else None)(client.status()),
                          message="all requests")
        self.assertLess(len(protocol.encode(status)), protocol.MAX_LINE)
        self.assertEqual([item["request_id"] for item in status["pending"]], ["req-%d" % i for i in range(host_module.MAX_PENDING)])
        self.assertTrue(status.get("status_truncated"))
        self.assertTrue(all(item["input_truncated"] for item in status["pending"]))

    def test_the_event_log_keeps_neither_result_text_nor_tool_input_but_status_has_what_the_ui_needs(self):
        client = InProcessHost(self).ready()
        client.send("ASK write", new_uuid())
        pending = settle(client, "blocked")["pending"][0]
        self.assertEqual(pending["questions"], [{"question": "Which?"}])
        client.answer(pending["request_id"], True)
        settle(client)
        client.send("PERM write", new_uuid())
        settle(client)
        for text in (json.dumps(client.events()["events"]), (self.state / layout.EVENTS_FILE).read_text()):
            self.assertNotIn("RESULT-TEXT-SECRET", text)
            self.assertNotIn("/tmp/x", text)
        events = client.events()["events"]
        self.assertEqual([e["request_id"] for e in events if e["type"] == "question"], [pending["request_id"]])
        self.assertEqual(len([e for e in events if e["type"] == "permission_request"]), 1)
        self.assertEqual(len([e for e in events if e["type"] == "permission_answered"]), 1)


class HostEnvironmentFileTest(ManagedTestCase):
    def test_credential_bearing_host_environment_comes_from_launch_json_not_argv(self):
        proxy = "http://user:PROXYPASSWORD@proxy.example:3128"
        seen = {}

        class Watching(FakeSession):
            def start(inner):
                seen["proxy"] = os.environ.get("HTTPS_PROXY")
                super().start()
        launch = launcher.prepare_launch(self.state, self.spec())
        launcher._add_host_environment(self.state, {"HTTPS_PROXY": proxy})
        self.assertEqual(json.loads((self.state / layout.LAUNCH_FILE).read_text())["host_env"], {"HTTPS_PROXY": proxy})
        self.assertEqual((self.state / layout.LAUNCH_FILE).stat().st_mode & 0o777, 0o600)
        stop = threading.Event()
        host = host_module.ManagedHost(self.state, driver_class=Watching)
        with mock.patch.dict(os.environ):
            os.environ.pop("HTTPS_PROXY", None)
            thread = threading.Thread(target=lambda: host.run(stop), daemon=True)
            thread.start()
            wait_for(lambda: (layout.read_host(self.state) or {}).get("state") == "ready", message="ready")
            stop.set()
            thread.join(15)
        self.assertEqual(seen["proxy"], proxy)
        self.assertEqual(launch["token"], host._token)

    def test_a_bad_host_env_is_an_invalid_launch(self):
        launcher.prepare_launch(self.state, self.spec())
        launcher._add_host_environment(self.state, {"A": 1})
        self.assertEqual(host_module.ManagedHost(self.state, driver_class=FakeSession).run(threading.Event()), layout.EXIT_CONFIG)

    @unittest.skipUnless(sys.platform == "linux", "UserUnitLauncher starts systemd units, which exist only on Linux")
    def test_only_variables_that_cannot_hold_credentials_travel_in_the_systemd_command_line(self):
        calls = []

        def run(argv, **options):
            calls.append(argv)
            if "show" in argv:
                return subprocess.CompletedProcess(argv, 0, "LoadState=loaded\nActiveState=inactive\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")
        proxy = "http://user:PROXYPASSWORD@proxy.example:3128"
        launcher.prepare_launch(self.state, self.spec())
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": proxy, "http_proxy": proxy, "ALL_PROXY": proxy}):
            started = launcher.UserUnitLauncher(runner=run, outside=lambda p: Path(p),
                                                extra_environment={"EXTRA_SECRET_NAME": "EXTRASECRET"}).start(self.state)
        self.assertTrue(started)
        argv = " ".join(calls[-1])
        for secret in ("PROXYPASSWORD", "EXTRASECRET", "proxy.example"):
            self.assertNotIn(secret, argv)
        self.assertIn("--setenv=PYTHONPATH=", argv)
        stored = json.loads((self.state / layout.LAUNCH_FILE).read_text())["host_env"]
        self.assertEqual((stored["HTTPS_PROXY"], stored["http_proxy"], stored["EXTRA_SECRET_NAME"]), (proxy, proxy, "EXTRASECRET"))
        self.assertNotIn("PATH", stored)


class LockProbeTest(ManagedTestCase):
    def test_probes_do_not_make_a_starting_host_see_the_lock_busy(self):
        import fcntl
        path = self.state / "probe.lock"
        path.touch()
        stop = threading.Event()

        def poll():
            while not stop.is_set():
                layout.lock_held(path)
        pollers = [threading.Thread(target=poll) for _ in range(2)]
        for poller in pollers:
            poller.start()
        try:
            failures = 0
            for _ in range(1500):                       # what a starting host does, while the service polls at full speed
                descriptor = os.open(path, os.O_RDWR)
                try:
                    if not layout.acquire_lock(descriptor, attempts=20, delay=0.001):
                        failures += 1
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
        finally:
            stop.set()
            for poller in pollers:
                poller.join()
        self.assertEqual(failures, 0)

    def test_concurrent_probes_never_see_each_other_as_the_holder(self):
        path = self.state / "probe.lock"
        path.touch()
        results = []

        def poll():
            results.extend(layout.lock_held(path) for _ in range(3000))
        pollers = [threading.Thread(target=poll) for _ in range(4)]
        for poller in pollers:
            poller.start()
        for poller in pollers:
            poller.join()
        self.assertEqual(len(results), 12000)
        self.assertFalse(any(results), "a free lock was reported held")

    def test_a_held_lock_is_seen_and_a_real_second_acquire_gives_up(self):
        import fcntl
        path = self.state / "probe.lock"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, descriptor)
        self.assertTrue(layout.acquire_lock(descriptor))
        self.assertTrue(layout.lock_held(path))
        other = os.open(path, os.O_RDWR)
        self.addCleanup(os.close, other)
        started = time.monotonic()
        self.assertFalse(layout.acquire_lock(other, attempts=3, delay=0.01))
        self.assertLess(time.monotonic() - started, 1.0)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        self.assertFalse(layout.lock_held(path))

    def test_lock_held_in_another_process(self):
        path = self.state / "probe.lock"
        holder = subprocess.Popen([sys.executable, "-c", "import fcntl,os,sys,time\nfd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600)\n"
                                   "fcntl.flock(fd,fcntl.LOCK_EX)\nprint('held',flush=True)\ntime.sleep(60)", str(path)],
                                  stdout=subprocess.PIPE)
        self.addCleanup(lambda: (holder.kill(), holder.wait(), holder.stdout.close()))
        self.assertEqual(holder.stdout.readline().strip(), b"held")
        self.assertTrue(layout.lock_held(path))
        holder.kill()
        holder.wait()
        self.assertFalse(layout.lock_held(path))


class NegativeCacheTest(ManagedTestCase):
    def test_a_host_that_holds_the_lock_but_never_answers_costs_one_timeout_not_one_per_call(self):
        import fcntl
        runtime = launcher.HostClient
        descriptor = os.open(self.state / layout.LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        identity = layout.process_identity(os.getpid())
        layout.write_json_private(self.state / layout.HOST_FILE, {
            "schema_version": 1, "agent_id": self.agent, "instance": "i-1", "host_pid": os.getpid(), "host_identity": identity,
            "protocol": 1, "state": "ready", "socket_path": None, "socket_fallback": False})
        layout.write_private(self.state / layout.TOKEN_FILE, b"t" * 32)
        now = [1000.0]
        calls = []

        def hello(client, timeout=None):
            calls.append(client.instance)
            raise HostUnavailable("timeout", "no answer", sent=True)
        with mock.patch.object(layout, "host_command_matches", return_value=True), \
                mock.patch.object(runtime, "hello", hello):
            first = launcher.inspect_host(self.state, clock=lambda: now[0])
            second = launcher.inspect_host(self.state, clock=lambda: now[0] + 1)
            self.assertEqual((first.status, first.reason), ("unverified", "hello_timeout"))
            self.assertEqual((second.status, second.reason), ("unverified", "hello_timeout"))
            self.assertEqual(len(calls), 1)
            launcher.inspect_host(self.state, clock=lambda: now[0] + launcher.NEGATIVE_CACHE_SECONDS + 1)
            self.assertEqual(len(calls), 2)                                           # the entry expired
            # a different host in the same directory (another instance) is not covered by the entry
            layout.write_json_private(self.state / layout.HOST_FILE, {
                "schema_version": 1, "agent_id": self.agent, "instance": "i-2", "host_pid": os.getpid(), "host_identity": identity,
                "protocol": 1, "state": "ready", "socket_path": None, "socket_fallback": False})
            launcher.inspect_host(self.state, clock=lambda: now[0] + launcher.NEGATIVE_CACHE_SECONDS + 2)
            self.assertEqual(calls, ["i-1", "i-1", "i-2"])
            # other failures (the socket is gone, a refusal) are not cached
            calls.clear()
            with mock.patch.object(runtime, "hello", side_effect=HostUnavailable("unreachable")) as unreachable:
                launcher.inspect_host(self.state, clock=lambda: 5000.0)
                launcher.inspect_host(self.state, clock=lambda: 5000.1)
                self.assertEqual(unreachable.call_count, 2)


class StaleClaudeTest(SubprocessHostTest):
    """A host killed outright cannot stop its Claude. The next start must stop that exact process, and only that."""

    def orphan(self):
        verified = self.start()
        return verified, verified.claude_pid, verified.claude_identity

    def test_a_new_host_does_not_start_beside_the_orphaned_claude_of_a_killed_host(self):
        verified, claude, identity = self.orphan()
        os.kill(verified.host_pid, signal.SIGKILL)                    # SIGKILL / OOM: no chance to close anything
        wait_for(lambda: not layout.lock_held(self.state / layout.LOCK_FILE), message="the lock to be released")
        self.assertTrue(layout.process_alive(claude, identity), "the fake driver has no death signal: the orphan is still there")
        again = self.start()
        self.assertFalse(layout.process_alive(claude, identity))      # the previous claude was stopped before the new host started
        self.assertNotEqual(again.claude_pid, claude)
        self.assertEqual(again.client().hello()["status"], "ok")
        record = json.loads((self.state / layout.HOST_FILE).read_text())
        self.assertEqual(record["state"], "ready")

    def test_clean_stale_records_that_it_stopped_the_orphan(self):
        verified, claude, identity = self.orphan()
        os.kill(verified.host_pid, signal.SIGKILL)
        wait_for(lambda: not layout.lock_held(self.state / layout.LOCK_FILE), message="the lock to be released")
        self.assertTrue(layout.clean_stale(self.state))
        self.assertFalse(layout.process_alive(claude, identity))
        self.assertEqual(layout.read_host(self.state)["exit"], {"code": None, "reason": "host_gone", "claude_terminated": True})
        self.assertFalse(layout.clean_stale(self.state))              # nothing left to stop

    def test_a_reused_pid_or_a_live_host_is_never_signalled(self):
        bystander = subprocess.Popen([sys.executable, "-c", SLEEPER], start_new_session=True)
        self.addCleanup(lambda: (bystander.kill(), bystander.wait()))
        identity = layout.process_identity(bystander.pid)
        layout.write_json_private(self.state / layout.HOST_FILE, {
            "schema_version": 1, "agent_id": self.agent, "instance": "i", "host_pid": 1, "host_identity": "x", "protocol": 1,
            "state": "ready", "claude_pid": bystander.pid, "claude_identity": "boot:1",
            "socket_path": None, "socket_fallback": False})
        self.assertFalse(layout.clean_stale(self.state))              # same pid, different start time: someone else now
        self.assertIsNone(bystander.poll())
        # the lock is held (a host is running): not even the recorded process is touched
        layout.write_json_private(self.state / layout.HOST_FILE, {
            "schema_version": 1, "agent_id": self.agent, "instance": "i", "host_pid": 1, "host_identity": "x", "protocol": 1,
            "state": "ready", "claude_pid": bystander.pid, "claude_identity": identity, "socket_path": None, "socket_fallback": False})
        import fcntl
        descriptor = os.open(self.state / layout.LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        self.assertFalse(layout.clean_stale(self.state))
        self.assertIsNone(bystander.poll())
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        self.assertTrue(layout.clean_stale(self.state))               # lock free and the identity matches: now it is stopped
        bystander.wait(5)
        self.assertIsNotNone(bystander.poll())

    def test_the_host_process_runs_from_a_neutral_directory_and_ignores_files_planted_in_the_state_directory(self):
        marker = self.root / "planted-ran"
        (self.state / "json.py").write_text("open(%r, 'w').write('x')\nraise ImportError('planted')\n" % str(marker))
        (self.state / "qorqut_planted.py").write_text("")
        verified = self.start()
        self.assertFalse(marker.exists(), "a module in the state directory was imported by the host")
        self.assertEqual(host_cwd(verified.host_pid), "/")
        argv = host_cmdline(verified.host_pid).split(b"\0")
        if sys.version_info >= (3, 11):
            self.assertIn(b"-P", argv)
        self.assertEqual(verified.client().hello()["status"], "ok")


if __name__ == "__main__":
    unittest.main()
