"""The ``ManagedRuntime`` (``managed_runtime/manager.py``): finding hosts, the session view, opening, control.

Hosts, the launcher and the host client are stand-ins that record what they are asked; nothing starts a process, a socket or a
real ``claude``. The only real processes are the test's own parent chain and one short ``sleep`` (for the ancestry check).
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut.managed_runtime import manager as hosts
from qorqut.managed_runtime import state as layout
from qorqut.managed_runtime.client import HostUnavailable

AGENT = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
SESSION = "33333333-3333-4333-8333-333333333333"
PROFILE = {"agent_id": AGENT, "workspace": "/w"}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="managed mode ")).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.paths = SimpleNamespace(runtime=self.base / "runtime")
        self.paths.runtime.mkdir()


class FakeClient:
    def __init__(self, status=None, events=None):
        self.status_reply = status or {}
        self.event_pages = list(events or [])
        self.calls = []
        self.fail = None

    def _do(self, name, *args):
        self.calls.append((name, *args))
        if self.fail is not None:
            raise self.fail
        return {"status": "ok"}

    def status(self):
        self.calls.append(("status",))
        if self.fail is not None:
            raise self.fail
        return self.status_reply

    def events(self, since=0):
        self.calls.append(("events", since))
        if self.fail is not None:
            raise self.fail
        page = self.event_pages.pop(0) if self.event_pages else {"events": [], "next": since, "seq": since}
        return page

    def interrupt(self):
        return self._do("interrupt")

    def answer(self, request_id, decision):
        return self._do("answer", request_id, decision)

    def close(self, mode="graceful"):
        return self._do("close", mode)


def verified(agent=AGENT, *, state="ready", host_pid=4242, host_identity="boot:555", claude_pid=4343, claude_identity="boot:777",
             session=SESSION, client=None, transcript="/t/session.jsonl", instance="inst-1"):
    return SimpleNamespace(agent_id=agent, instance=instance, host_pid=host_pid, host_identity=host_identity, claude_pid=claude_pid,
                           claude_identity=claude_identity, session_id=session, transcript_path=transcript, state=state,
                           client=lambda: client if client is not None else FakeClient())


class FakeLauncher:
    """The module functions of ``launcher.py``, over a dict of inspections keyed by agent folder name."""

    def __init__(self):
        self.inspections = {}
        self.started = []
        self.start_result = None
        self.start_error = None

    def inspect_host(self, state_dir):
        return self.inspections.get(Path(state_dir).name, SimpleNamespace(status="absent", reason="lock_free", host=None))

    def start_host(self, state_dir, spec, timeout=45.0):
        self.started.append((Path(state_dir), spec, timeout))
        if self.start_error is not None:
            raise self.start_error
        return self.start_result or verified(spec["agent_id"])

    def running_hosts(self, root):
        return {name: item.host for name, item in self.inspections.items() if item.status == "running"}


def running(host):
    return SimpleNamespace(status="running", reason="", host=host)


class LaunchError(RuntimeError):
    def __init__(self, reason, detail=""):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


class ManagerFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.launcher = FakeLauncher()
        for agent in (AGENT,):      # an agent that was ever opened has a state folder; one that never was costs a ``stat``
            layout.agent_state(self.paths, agent).mkdir(parents=True)
        self.now = [0.0]
        self.prepared = []
        self.workspace = self.base / "workspace"
        (self.workspace / ".qorqut").mkdir(parents=True)
        (self.workspace / ".qorqut" / "claude-launch-settings.json").write_text("{}")
        self.cli = self.base / "claude"
        self.cli.write_text("#!/bin/sh\n")
        self.cli.chmod(0o755)

        def prepare(agent_id):
            self.prepared.append(agent_id)
            return {"workspace": str(self.workspace), "env": {"PATH": "/bin", "SECRET_ROLE_KEY": "s3cret-value"}}

        self.runtime = SimpleNamespace(paths=self.paths)
        self.resume = None
        self.manager = hosts.ManagedRuntime(self.runtime, launcher=self.launcher, prepare=prepare, resume=lambda agent: self.resume,
                                            executable=str(self.cli), clock=lambda: self.now[0], sleep=self.advance, ready=lambda client: True)

    def advance(self, seconds):
        self.now[0] += seconds

class ManagerStartStop(ManagerFixture):
    def test_start_spawns_nothing_and_stop_closes_nothing(self):
        client = FakeClient()
        self.launcher.inspections[AGENT] = running(verified(client=client))
        self.manager.start()
        self.assertEqual((self.launcher.started, client.calls), ([], []))
        self.manager.snapshot(AGENT)
        self.manager.stop()
        self.assertEqual(self.launcher.started, [])
        self.assertNotIn(("close", "graceful"), client.calls)
        self.assertNotIn(("close", "now"), client.calls)


class Verify(ManagerFixture):
    def host(self, **fields):
        self.launcher.inspections[AGENT] = running(verified(host_pid=os.getppid(), **fields))

    def marker(self, **fields):
        return {"pid": os.getpid(), "started": "1", "session_id": SESSION, "runtime": "claude-code", **fields}

    def test_a_managed_claude_below_its_verified_host_gets_the_host_as_owner(self):
        self.host()
        self.assertEqual(self.manager.verify(PROFILE, self.marker()), {"transport": "managed", "owner": f"host:{os.getppid()}:555"})

    def test_nothing_is_verified_without_a_running_matching_host(self):
        self.assertIsNone(self.manager.verify(PROFILE, self.marker()), "no host")
        self.launcher.inspections[AGENT] = SimpleNamespace(status="unverified", reason="host_command", host=None)
        self.assertIsNone(self.manager.verify(PROFILE, self.marker()), "a host that cannot be proven ours")
        self.launcher.inspections[AGENT] = SimpleNamespace(status="starting", reason="starting", host=None)
        self.assertIsNone(self.manager.verify(PROFILE, self.marker()), "a host that is still starting has no session")
        self.host()
        self.assertIsNone(self.manager.verify(PROFILE, self.marker(session_id="other")), "another conversation")
        self.assertIsNone(self.manager.verify(PROFILE, {"pid": "x"}))
        self.assertIsNone(self.manager.verify({}, self.marker()))

    def test_a_claude_that_does_not_descend_from_the_host_is_not_its_session(self):
        process = subprocess.Popen(["sleep", "30"])
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        self.launcher.inspections[AGENT] = running(verified(host_pid=process.pid))
        self.assertIsNone(self.manager.verify(PROFILE, self.marker()))
        self.assertIsNone(self.manager.verify(PROFILE, self.marker(pid=process.pid)), "the host itself is not its own claude")

    def test_identity_is_the_verified_sessions(self):
        self.assertIsNone(self.manager.identity(AGENT))
        self.launcher.inspections[AGENT] = running(verified())
        self.assertEqual(self.manager.identity(AGENT), {
            "agent_id": AGENT, "provider": "claude", "conversation_id": SESSION, "pid": 4343, "started": "777",
            "owner": "host:4242:555", "host_pid": 4242, "host_started": "555", "instance": "inst-1", "state": "ready",
            "transcript_path": "/t/session.jsonl"})
        self.launcher.inspections[AGENT] = SimpleNamespace(status="unverified", reason="x", host=None)
        self.assertIsNone(self.manager.identity(AGENT))
        with self.assertRaises(hosts.HostUnverified):
            self.manager.handle(AGENT)


class Snapshot(ManagerFixture):
    STATUS = {"status": "ok", "state": "ready", "phase": "working", "activity": "Reading app.py", "turn_started_at": "t0",
              "pending": [{"request_id": "r1", "kind": "permission", "tool": "Bash", "input": {"command": "ls  -la\n/tmp"}},
                          {"request_id": "r2", "kind": "question", "tool": "AskUserQuestion", "questions": [{"question": "Sure?"}]},
                          {"kind": "permission"}], "last_turn_status": "completed", "session_id": SESSION}

    def open(self, **status):
        self.client = FakeClient({**self.STATUS, **status})
        self.launcher.inspections[AGENT] = running(verified(client=self.client))

    def test_a_closed_agent_snapshots_as_closed_and_costs_one_stat(self):
        self.assertEqual(self.manager.snapshot(OTHER), hosts.CLOSED_VIEW)
        self.assertFalse(self.manager.refresh(OTHER))
        view = self.manager.snapshot(AGENT)
        self.assertEqual((view["open"], view["phase"], view["can_stop"], view["pending"]), (False, None, False, []))

    def test_an_open_session_shows_phase_activity_and_requests(self):
        self.open()
        view = self.manager.snapshot(AGENT)
        self.assertEqual((view["open"], view["phase"], view["activity"], view["can_stop"], view["session_id"]),
                         (True, "working", "Reading app.py", True, SESSION))
        self.assertEqual([item["request_id"] for item in view["pending"]], ["r1", "r2"])
        self.assertEqual(view["pending"][0]["input_summary"], "ls -la /tmp")
        self.assertEqual(view["pending"][1]["questions"], [{"question": "Sure?"}])
        self.assertEqual(view["last_turn_status"], "completed")

    def test_refresh_reports_a_change_once(self):
        self.open()
        self.assertTrue(self.manager.refresh(AGENT), "the session appeared")
        self.assertFalse(self.manager.refresh(AGENT))
        self.client.status_reply = {**self.STATUS, "phase": "idle", "pending": []}
        self.assertTrue(self.manager.refresh(AGENT))
        self.client.status_reply = {**self.STATUS, "phase": "idle", "pending": [{"request_id": "r9", "kind": "permission"}]}
        self.assertTrue(self.manager.refresh(AGENT), "a new request")
        self.launcher.inspections.clear()
        self.assertTrue(self.manager.refresh(AGENT), "the session closed")
        self.assertFalse(self.manager.snapshot(AGENT)["open"])

    def test_activity_only_changes_are_shown_at_most_once_a_second(self):
        self.open()
        self.manager.refresh(AGENT)
        self.client.status_reply = {**self.STATUS, "activity": "Reading b.py"}
        self.advance(0.4)
        self.assertFalse(self.manager.refresh(AGENT))
        self.assertEqual(self.manager.snapshot(AGENT)["activity"], "Reading app.py")
        self.advance(0.7)
        self.assertTrue(self.manager.refresh(AGENT))
        self.assertEqual(self.manager.snapshot(AGENT)["activity"], "Reading b.py")

    def test_a_host_that_does_not_answer_keeps_the_last_state(self):
        self.open()
        self.manager.refresh(AGENT)
        self.client.fail = HostUnavailable("timeout", sent=True)
        self.assertFalse(self.manager.refresh(AGENT))
        self.assertTrue(self.manager.snapshot(AGENT)["open"])

    def test_a_host_that_is_still_starting_is_open_and_starting(self):
        self.launcher.inspections[AGENT] = SimpleNamespace(status="starting", reason="starting", host=None)
        view = self.manager.snapshot(AGENT)
        self.assertEqual((view["open"], view["phase"], view["can_stop"]), (True, "starting", False))

    def test_events_are_read_from_the_cursor_and_drained_once(self):
        self.open()
        self.client.event_pages = [
            {"events": [{"seq": 1, "type": "turn_started"}, {"seq": 2, "type": "prompt_blocked", "message_uuid": "u1", "reason": "hook"}],
             "next": 2, "seq": 2},
            {"events": [{"seq": 3, "type": "turn_finished"}], "next": 3, "seq": 3}]
        self.manager.refresh(AGENT)
        self.assertEqual([event["seq"] for event in self.manager.drain_events(AGENT)], [1, 2])
        self.assertEqual(self.manager.drain_events(AGENT), [])
        self.manager.refresh(AGENT)
        self.assertEqual([event["seq"] for event in self.manager.drain_events(AGENT)], [3])
        self.assertIn(("events", 2), self.client.calls)

    def test_a_long_backlog_is_read_in_pages(self):
        self.open()
        self.client.event_pages = [{"events": [{"seq": 1, "type": "a"}], "next": 1, "seq": 3},
                                   {"events": [{"seq": 2, "type": "a"}, {"seq": 3, "type": "a"}], "next": 3, "seq": 3}]
        self.manager.refresh(AGENT)
        self.assertEqual([event["seq"] for event in self.manager.drain_events(AGENT)], [1, 2, 3])

    def test_a_new_host_starts_its_event_log_from_the_beginning(self):
        self.open()
        self.client.event_pages = [{"events": [{"seq": 5, "type": "a"}], "next": 5, "seq": 5}]
        self.manager.refresh(AGENT)
        self.manager.drain_events(AGENT)
        self.client = FakeClient(self.STATUS, [{"events": [{"seq": 1, "type": "b"}], "next": 1, "seq": 1}])
        self.launcher.inspections[AGENT] = running(verified(host_pid=9999, host_identity="boot:1", client=self.client))
        self.manager.refresh(AGENT)
        self.assertIn(("events", 0), self.client.calls)
        self.assertEqual([event["type"] for event in self.manager.drain_events(AGENT)], ["b"])


class Control(ManagerFixture):
    def test_interrupt_answer_and_close_reach_the_verified_host(self):
        client = FakeClient()
        self.launcher.inspections[AGENT] = running(verified(client=client))
        self.manager.interrupt(AGENT)
        self.manager.answer(AGENT, "r1", {"behavior": "allow"})
        self.assertTrue(self.manager.close(AGENT, "now"))
        self.assertTrue(self.manager.close(AGENT))
        self.assertEqual(client.calls, [("interrupt",), ("answer", "r1", {"behavior": "allow"}), ("close", "now"), ("close", "graceful")])

    def test_without_a_host_there_is_nothing_to_control(self):
        self.assertFalse(self.manager.close(AGENT))
        with self.assertRaises(hosts.ManagedError) as raised:
            self.manager.interrupt(AGENT)
        self.assertEqual(raised.exception.code, "session_closed")
        self.launcher.inspections[AGENT] = SimpleNamespace(status="unverified", reason="x", host=None)
        self.assertFalse(self.manager.close(AGENT))
        with self.assertRaises(hosts.ManagedError) as raised:
            self.manager.answer(AGENT, "r1", True)
        self.assertEqual(raised.exception.code, "host_unverified")

    def test_a_host_that_does_not_answer_is_reported_not_swallowed(self):
        client = FakeClient()
        client.fail = HostUnavailable("timeout", sent=True)
        self.launcher.inspections[AGENT] = running(verified(client=client))
        with self.assertRaises(hosts.ManagedError) as raised:
            self.manager.interrupt(AGENT)
        self.assertEqual((raised.exception.code, raised.exception.retryable), ("host_unverified", True))

    def test_close_all_asks_every_verified_host_and_nothing_else(self):
        first, second = FakeClient(), FakeClient()
        self.launcher.inspections[AGENT] = running(verified(client=first))
        self.launcher.inspections[OTHER] = running(verified(OTHER, host_pid=5000, client=second))
        self.launcher.inspections["33333333-3333-4333-8333-000000000000"] = SimpleNamespace(status="unverified", reason="x", host=None)
        self.assertEqual(sorted(host["agent_id"] for host in self.manager.running_hosts()), sorted([AGENT, OTHER]))
        self.assertEqual(sorted(self.manager.close_all("update")), sorted([AGENT, OTHER]))
        self.assertEqual((first.calls, second.calls), ([("close", "graceful")], [("close", "graceful")]))

    def test_running_hosts_lists_state_and_identity(self):
        self.launcher.inspections[AGENT] = running(verified())
        self.assertEqual(self.manager.running_hosts(), [{"agent_id": AGENT, "state": "ready", "host_pid": 4242, "session_id": SESSION,
                                                         "instance": "inst-1"}])
        self.assertEqual(hosts.ManagedRuntime(SimpleNamespace(paths=None), launcher=self.launcher).running_hosts(), [])


class EnsureOpen(ManagerFixture):
    def test_a_ready_host_is_used_as_it_is(self):
        self.launcher.inspections[AGENT] = running(verified())
        opened = self.manager.ensure_open(PROFILE, 5)
        self.assertEqual((opened["state"], opened["session_id"], opened["started"], opened["host_pid"]), ("ready", SESSION, False, 4242))
        self.assertEqual((self.launcher.started, self.prepared), ([], []), "nothing is prepared or spawned for a running host")

    def test_a_missing_host_is_started_from_a_prepared_workspace(self):
        opened = self.manager.ensure_open(PROFILE, 30)
        self.assertTrue(opened["started"])
        self.assertEqual(self.prepared, [AGENT])
        (state_dir, spec, timeout), = self.launcher.started
        self.assertEqual(state_dir, self.paths.runtime / "managed-agents" / AGENT)
        self.assertEqual((spec["agent_id"], spec["folder"], spec["executable"]), (AGENT, str(self.workspace), str(self.cli)))
        self.assertEqual(spec["args"], ["--settings", str(self.workspace / ".qorqut/claude-launch-settings.json")])
        self.assertEqual((spec["resume_session_id"], spec["send_role"], spec["idle_seconds"]), (None, "/role", 900))
        self.assertEqual(spec["env"]["SECRET_ROLE_KEY"], "s3cret-value", "the environment goes to the launcher's private launch file")
        self.assertNotIn("s3cret-value", json.dumps(spec["args"]), "and never into an argument")
        self.assertLessEqual(timeout, 30)

    def test_the_clients_own_compaction_is_switched_off_only_for_an_agent_whose_conversation_qorqbot_compacts(self):
        self.manager.ensure_open(PROFILE, 30)
        self.assertNotIn("DISABLE_AUTO_COMPACT", self.launcher.started[-1][1]["env"])        # nothing composed: as before
        asked = []
        self.runtime.compaction = SimpleNamespace(replaces_client=lambda agent_id: asked.append(agent_id) or self.own)
        for self.own, expected in ((False, None), (True, "1"), ("yes", None)):
            self.launcher.inspections.pop(AGENT, None)
            self.manager.ensure_open(PROFILE, 30)
            self.assertEqual(self.launcher.started[-1][1]["env"].get("DISABLE_AUTO_COMPACT"), expected)
        self.assertEqual(asked, [AGENT] * 3)

    def test_the_resumed_conversation_is_not_sent_the_role_again(self):
        self.resume = SESSION
        opened = self.manager.ensure_open(PROFILE, 30)
        spec = self.launcher.started[0][1]
        self.assertEqual((spec["resume_session_id"], spec["send_role"]), (SESSION, False))
        self.assertEqual(opened["session_id"], SESSION)

    def test_a_conversation_that_did_not_resume_is_never_replaced_by_a_fresh_one(self):
        self.resume = "44444444-4444-4444-8444-444444444444"
        client = FakeClient()
        self.launcher.start_result = verified(client=client)
        self.launcher.inspections[AGENT] = SimpleNamespace(status="absent", reason="", host=None)

        def start(state_dir, spec, timeout=45.0):
            self.launcher.inspections[AGENT] = running(verified(client=client))
            return self.launcher.start_result
        self.launcher.start_host = start
        with self.assertRaises(hosts.ManagedError) as raised:
            self.manager.ensure_open(PROFILE, 30)
        self.assertEqual(raised.exception.code, "resume_failed")
        self.assertEqual(client.calls, [("close", "now")], "the session that is not the selected one is closed")

    def test_a_host_still_starting_is_waited_for(self):
        starting = SimpleNamespace(status="starting", reason="starting", host=None)
        self.launcher.inspections[AGENT] = starting
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            self.advance(seconds)
            if len(sleeps) == 3:
                self.launcher.inspections[AGENT] = running(verified())
        self.manager._sleep = sleep
        opened = self.manager.ensure_open(PROFILE, 30)
        self.assertEqual((opened["started"], len(sleeps)), (False, 3))
        self.assertEqual(self.launcher.started, [])

    def test_a_host_that_never_becomes_ready_times_out_retryably(self):
        self.launcher.inspections[AGENT] = SimpleNamespace(status="starting", reason="starting", host=None)
        with self.assertRaises(hosts.ManagedError) as raised:
            self.manager.ensure_open(PROFILE, 2)
        self.assertEqual((raised.exception.code, raised.exception.retryable), ("open_timeout", True))

    def test_a_host_that_cannot_be_verified_is_not_replaced(self):
        self.launcher.inspections[AGENT] = SimpleNamespace(status="unverified", reason="host_command", host=None)
        with self.assertRaises(hosts.ManagedError) as raised:
            self.manager.ensure_open(PROFILE, 5)
        self.assertEqual(raised.exception.code, "host_unverified")
        self.assertEqual(self.launcher.started, [])

    def test_launch_failures_have_stable_codes(self):
        for reason, code in (("resume_failed", "resume_failed"), ("host_unverified", "host_unverified"), ("not_ready", "open_timeout"),
                             ("invalid_launch", "host_start_failed"), ("driver_unavailable", "host_failed"), ("", "host_failed")):
            with self.subTest(reason=reason):
                self.launcher.start_error = LaunchError(reason, "the folder /secret/place is bad")
                with self.assertRaises(hosts.ManagedError) as raised:
                    self.manager.ensure_open(PROFILE, 5)
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn("/secret/place", raised.exception.message)

    def test_a_missing_claude_is_reported_before_anything_starts(self):
        self.manager._executable = str(self.base / "no such claude")
        with self.assertRaises(hosts.ManagedError) as raised:
            self.manager.ensure_open(PROFILE, 5)
        self.assertEqual(raised.exception.code, "cli_unavailable")
        self.assertEqual(self.launcher.started, [])

    def test_the_launcher_is_looked_up_lazily(self):
        manager = hosts.ManagedRuntime(self.runtime)
        self.assertEqual(manager.snapshot(AGENT), hosts.CLOSED_VIEW)

    def test_one_start_at_a_time_per_agent(self):
        with self.manager._lifecycle(AGENT) as first:
            with self.manager._lifecycle(AGENT) as second:
                self.assertEqual((first, second), (True, False))
        with self.manager._lifecycle(AGENT) as again:
            self.assertTrue(again)

    def test_the_service_runtime_finds_the_manager_in_one_place(self):
        manager = object()
        self.assertIs(hosts.managed_of(SimpleNamespace(managed_runtime=manager)), manager)
        self.assertIs(hosts.managed_of(SimpleNamespace(native_chat=SimpleNamespace(managed=manager))), manager)
        self.assertIsNone(hosts.managed_of(SimpleNamespace(native_chat=None)))


# ----- finding: a conversation that cannot be resumed is never silently replaced --------------------------------------------------

class Resume(ManagerFixture):
    """The real ``_resume_id`` (no override) over a stand-in for the selection and the capture row."""

    def manager_for(self, chosen, capture):
        database = SimpleNamespace(read=lambda: _reading())
        runtime = SimpleNamespace(paths=self.paths, database=database)
        manager = hosts.ManagedRuntime(runtime, launcher=self.launcher, prepare=self.manager._prepare_override, executable=str(self.cli),
                                       clock=lambda: self.now[0], sleep=self.advance, ready=lambda client: True)
        for target, value in (("qorqut.native_chat.selection.selected", chosen), ("qorqut.native_chat.store.get_capture", capture)):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return manager

    def chosen(self, provider="claude"):
        return SimpleNamespace(provider=provider, conversation_id=SESSION)

    def open(self, manager, **options):
        return manager.ensure_open(PROFILE, 30, **options)

    def test_a_conversation_whose_transcript_exists_is_resumed(self):
        transcript = self.base / "t.jsonl"
        transcript.write_text("{}\n")
        manager = self.manager_for(self.chosen(), {"transcript_path": str(transcript), "receipt_id": "r", "captured_length": 5})
        opened = self.open(manager)
        spec = self.launcher.started[0][1]
        self.assertEqual((spec["resume_session_id"], spec["send_role"], opened["role_pending"]), (SESSION, False, False))

    def test_a_missing_transcript_that_had_content_is_refused_and_nothing_prepared_or_started(self):
        manager = self.manager_for(self.chosen(), {"transcript_path": str(self.base / "gone.jsonl"), "receipt_id": "r", "captured_length": 5})
        with self.assertRaises(hosts.ManagedError) as refused:
            self.open(manager)
        self.assertEqual((refused.exception.code, refused.exception.retryable), ("resume_unavailable", False))
        self.assertIn("Start a new conversation", refused.exception.message)
        self.assertNotIn(str(self.base), refused.exception.message)
        self.assertEqual((self.launcher.started, self.prepared), ([], []))

    def test_the_owner_may_choose_a_new_conversation_instead(self):
        manager = self.manager_for(self.chosen(), {"transcript_path": str(self.base / "gone.jsonl"), "receipt_id": "r", "captured_length": 5})
        opened = self.open(manager, new_conversation=True)
        spec = self.launcher.started[0][1]
        self.assertEqual((spec["resume_session_id"], spec["send_role"], opened["role_pending"]), (None, "/role", True))

    def test_a_selection_with_nothing_recorded_a_foreign_provider_or_no_selection_starts_fresh_without_a_refusal(self):
        for chosen, capture in ((self.chosen(), {"transcript_path": None, "receipt_id": None, "captured_length": 0}),
                                (self.chosen(), None), (self.chosen("codex"), {"transcript_path": "/x", "receipt_id": "r"}),
                                (None, None)):
            with self.subTest(chosen=chosen, capture=capture):
                self.launcher.started.clear()
                self.launcher.inspections.clear()
                manager = self.manager_for(chosen, capture)
                self.open(manager)
                self.assertEqual(self.launcher.started[0][1]["resume_session_id"], None)


@contextmanager
def _reading():
    yield SimpleNamespace(connection=None)


# ----- finding: one blocking open per agent, bounded across agents ----------------------------------------------------------------

class OpenRaces(ManagerFixture):
    def setUp(self):
        super().setUp()
        # Real time: these tests run threads against each other.
        self.manager = hosts.ManagedRuntime(self.runtime, launcher=self.launcher, prepare=self.manager._prepare_override, resume=lambda agent: None,
                                            executable=str(self.cli), max_opens=1, ready=lambda client: True)
        self.release = threading.Event()
        self.entered = threading.Event()

    def slow_start(self, agent):
        def start(state_dir, spec, timeout=45.0):
            self.launcher.started.append((Path(state_dir), spec, timeout))
            self.entered.set()
            self.release.wait(10)
            self.launcher.inspections[spec["agent_id"]] = running(verified(spec["agent_id"]))
            return verified(spec["agent_id"])
        self.launcher.start_host = start

    def test_a_second_concurrent_open_of_the_agent_waits_for_the_first_and_neither_prepares_nor_starts_again(self):
        self.slow_start(AGENT)
        results, failures = [], []

        def call():
            try:
                results.append(self.manager.ensure_open(PROFILE, 10))
            except Exception as error:  # noqa: BLE001
                failures.append(error)
        first = threading.Thread(target=call)
        first.start()
        self.assertTrue(self.entered.wait(5))
        second = threading.Thread(target=call)
        second.start()
        time.sleep(0.4)
        self.assertEqual((len(self.launcher.started), len(self.prepared), results), (1, 1, []), "the second caller is still waiting")
        self.release.set()
        first.join(10)
        second.join(10)
        self.assertEqual(failures, [])
        self.assertEqual(sorted(item["started"] for item in results), [False, True], "the second one used the first one's host")
        self.assertEqual((len(self.launcher.started), len(self.prepared)), (1, 1))

    def test_a_waiting_open_gives_up_at_its_own_deadline_with_a_retryable_code_and_the_host_is_still_reused_afterwards(self):
        self.slow_start(AGENT)
        first = threading.Thread(target=lambda: self.manager.ensure_open(PROFILE, 10))
        first.start()
        self.assertTrue(self.entered.wait(5))
        started = time.monotonic()
        with self.assertRaises(hosts.ManagedError) as late:
            self.manager.ensure_open(PROFILE, 0.3)
        self.assertEqual((late.exception.code, late.exception.retryable), ("open_timeout", True))
        self.assertLess(time.monotonic() - started, 2)
        self.release.set()
        first.join(10)
        again = self.manager.ensure_open(PROFILE, 5)
        self.assertEqual((again["started"], len(self.launcher.started), len(self.prepared)), (False, 1, 1))

    def test_the_host_that_appears_while_the_lifecycle_lock_is_taken_is_used_not_prepared_for(self):
        real = self.manager._lifecycle

        @contextmanager
        def lifecycle(agent_id):
            with real(agent_id) as held:
                self.launcher.inspections[agent_id] = running(verified(agent_id))  # another process started it meanwhile
                yield held
        self.manager._lifecycle = lifecycle
        # The first look (before the lock) finds nothing: the answer only changes once the lock is taken.
        looks = []
        inspect = self.launcher.inspect_host

        def first_look_empty(state_dir):
            looks.append(1)
            return SimpleNamespace(status="absent", reason="", host=None) if len(looks) == 1 else inspect(state_dir)
        self.launcher.inspect_host = first_look_empty
        opened = self.manager.ensure_open(PROFILE, 5)
        self.assertEqual((opened["started"], self.prepared, self.launcher.started), (False, [], []))

    def test_across_agents_only_so_many_opens_run_at_once_the_rest_are_busy_and_retryable(self):
        self.slow_start(AGENT)
        first = threading.Thread(target=lambda: self.manager.ensure_open(PROFILE, 10))
        first.start()
        self.assertTrue(self.entered.wait(5))
        with self.assertRaises(hosts.ManagedError) as busy:
            self.manager.ensure_open({"agent_id": OTHER, "workspace": "/w"}, 5)
        self.assertEqual((busy.exception.code, busy.exception.retryable), ("open_busy", True))
        self.assertEqual(self.prepared, [AGENT], "the busy agent's workspace was not prepared")
        self.release.set()
        first.join(10)
        self.manager.ensure_open({"agent_id": OTHER, "workspace": "/w"}, 5)
        self.assertEqual(self.prepared, [AGENT, OTHER])

    def test_hold_keeps_opens_out_and_waits_for_one_in_progress(self):
        self.slow_start(AGENT)
        first = threading.Thread(target=lambda: self.manager.ensure_open(PROFILE, 10))
        first.start()
        self.assertTrue(self.entered.wait(5))
        with self.assertRaises(hosts.ManagedError) as opening:
            with self.manager.hold(AGENT, timeout=0.3):
                self.fail("the setting must not change while a session is being opened")
        self.assertEqual((opening.exception.code, opening.exception.retryable), ("session_opening", True))
        self.release.set()
        first.join(10)
        with self.manager.hold(AGENT, timeout=2):
            with self.assertRaises(hosts.ManagedError) as late:
                self.manager.ensure_open(PROFILE, 0.3)
            self.assertEqual(late.exception.code, "open_timeout")
        self.assertEqual(self.manager.ensure_open(PROFILE, 2)["started"], False)



# ----- finding: Claude Code is found where the owner's terminals find it --------------------------------------------------------

class ClaudeLookup(ManagerFixture):
    def setUp(self):
        super().setUp()
        self.manager._executable = None
        self.bin = self.base / "owner-bin"
        self.bin.mkdir()
        self.owner_claude = self.bin / "claude"
        self.owner_claude.write_text("#!/bin/sh\n")
        self.owner_claude.chmod(0o755)
        self.own_copy = self.base / "managed-claude"
        self.own_copy.write_text("#!/bin/sh\n")
        self.own_copy.chmod(0o755)

    def patched(self, *, own=None, installed=None, path=None):
        for target, replacement in (
                ("qorqut.agent_cli_launch.executable", lambda provider, default=None, environ=None: own or default),
                ("qorqut.agent_cli_install.resolve", lambda provider, environ=None, **options: {"path": str(installed), "source": "system"} if installed else None),
                ("qorqut.agent_cli_launch.search_environment", lambda env=None: {"PATH": path or ""})):
            patcher = mock.patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_managed_copy_is_used_first(self):
        self.patched(own=str(self.own_copy), installed=self.owner_claude, path=str(self.bin))
        self.manager.ensure_open(PROFILE, 5)
        self.assertEqual(self.launcher.started[0][1]["executable"], str(self.own_copy))

    def test_the_owners_own_claude_on_their_path_is_used_when_there_is_no_managed_copy(self):
        self.patched(installed=self.owner_claude, path=str(self.bin))
        self.manager.ensure_open(PROFILE, 5)
        self.assertEqual(self.launcher.started[0][1]["executable"], str(self.owner_claude))

    def test_a_claude_the_installer_does_not_accept_is_not_started_by_a_search_of_the_path(self):
        # What the setup screen does not call Ready (too old, silent about its version) is not what a session runs either.
        self.patched(path=str(self.bin))
        with self.assertRaises(hosts.ManagedError) as refused:
            self.manager.ensure_open(PROFILE, 5)
        self.assertEqual(refused.exception.code, "cli_unavailable")
        self.assertEqual(self.launcher.started, [])

    def test_when_it_is_nowhere_the_error_is_one_plain_sentence_about_the_agents_client(self):
        empty = self.base / "empty"
        empty.mkdir()
        self.patched(path=os.pathsep.join([str(empty), "/usr/nowhere/bin"]))
        with self.assertRaises(hosts.ManagedError) as refused:
            self.manager.ensure_open(PROFILE, 5)
        self.assertEqual(refused.exception.code, "cli_unavailable")
        # One plain sentence naming the agent's client and what to do; no folders (those are the service's project, not the owner's).
        self.assertEqual(refused.exception.message, "This agent is set to use Claude Code, and Claude Code couldn't be found. "
                                                    "Download it under Settings > Agents, then send again.")
        self.assertEqual(self.launcher.started, [])


# ----- finding: what a permission card shows, and why a session ended ------------------------------------------------------------

class CardsAndEnds(ManagerFixture):
    def host(self, **status):
        self.client = FakeClient({"status": "ok", "state": "ready", "phase": "blocked", "session_id": SESSION, **status})
        self.launcher.inspections[AGENT] = running(verified(client=self.client))

    def test_a_permission_card_carries_the_whole_bounded_input_the_reason_and_the_path(self):
        command = "git clone https://example.com/a.git && " + "echo 1; " * 40 + "rm -rf ./build"
        self.host(pending=[{"request_id": "r1", "kind": "permission", "tool": "Bash", "input": {"command": command, "description": "d"},
                            "decision_reason": "  Bash(rm *) needs approval ", "blocked_path": "/home/me/project/build"}])
        card = self.manager.snapshot(AGENT)["pending"][0]
        self.assertEqual((card["input_text"], card["input_truncated"]), (command, False))
        self.assertEqual((card["decision_reason"], card["blocked_path"]), ("Bash(rm *) needs approval", "/home/me/project/build"))
        self.assertEqual(card["input"]["command"], command)
        self.assertLessEqual(len(card["input_summary"]), 240)

    def test_an_input_that_was_cut_says_so_and_is_never_shown_as_whole(self):
        self.host(pending=[{"request_id": "r1", "kind": "permission", "tool": "Bash", "input": {"command": "x" * 100},
                            "input_truncated": True}])
        card = self.manager.snapshot(AGENT)["pending"][0]
        self.assertTrue(card["input_truncated"])
        huge = {"command": "y" * (hosts.INPUT_TEXT_LIMIT + 500)}
        self.host(pending=[{"request_id": "r2", "kind": "permission", "tool": "Bash", "input": huge}])
        self.manager.refresh(AGENT)
        card = self.manager.snapshot(AGENT)["pending"][0]
        self.assertEqual((card["input_truncated"], len(card["input_text"]), card["input"]), (True, hosts.INPUT_TEXT_LIMIT, None))

    def test_a_question_keeps_its_options_and_multiselect_as_asked(self):
        asked = [{"question": "Which?", "header": "H", "multiSelect": True, "options": [{"label": "A", "description": "a"}]}]
        self.host(pending=[{"request_id": "q1", "kind": "question", "tool": "AskUserQuestion", "questions": asked}])
        self.assertEqual(self.manager.snapshot(AGENT)["pending"][0]["questions"], asked)

    def test_status_reads_the_host_now_not_the_cached_view(self):
        self.host(pending=[{"request_id": "r1", "kind": "permission", "tool": "Bash", "input": {"command": "ls"}}])
        self.assertEqual([item["request_id"] for item in self.manager.snapshot(AGENT)["pending"]], ["r1"])
        self.client.status_reply = {**self.client.status_reply, "pending": []}
        self.assertEqual(self.manager.snapshot(AGENT)["pending"][0]["request_id"], "r1", "the cached view is still the old one")
        self.assertEqual(self.manager.status(AGENT)["pending"], [])

    def test_status_of_a_closed_or_unreachable_session_is_a_refusal(self):
        with self.assertRaises(hosts.ManagedError) as closed:
            self.manager.status(AGENT)
        self.assertEqual(closed.exception.code, "session_closed")
        self.host()
        self.client.fail = HostUnavailable("timeout", "no answer")
        with self.assertRaises(hosts.ManagedError) as silent:
            self.manager.status(AGENT)
        self.assertEqual((silent.exception.code, silent.exception.retryable), ("host_unverified", True))

    def record(self, **fields):
        folder = self.paths.runtime / "managed-agents" / AGENT
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "host.json").write_text(json.dumps({"schema_version": layout.SCHEMA_VERSION, "agent_id": AGENT, **fields}))
        os.chmod(folder / "host.json", 0o600)

    def test_a_host_that_crashed_says_why_in_plain_words_and_one_that_was_closed_says_nothing(self):
        for state, reason, shown in (("failed", "claude_exited", "Claude Code stopped unexpectedly"),
                                     ("closed", "host_error", "internal error"), ("ready", None, "stopped unexpectedly")):
            with self.subTest(state=state, reason=reason):
                self.record(state=state, exit={"code": 1, "reason": reason, "detail": "/secret/place"} if reason else None)
                view = self.manager._ended(AGENT)
                self.assertIn(shown, view["closed_text"])
                self.assertNotIn("/secret/place", json.dumps(view))
                self.assertEqual(view["closed_reason"], reason or "host_gone")
        for state, reason in (("closed", "requested"), ("closed", "idle"), ("closed", None)):
            with self.subTest(state=state, reason=reason):
                self.record(state=state, exit={"code": 0, "reason": reason} if reason else None)
                self.assertEqual(self.manager._ended(AGENT), {})

    def test_the_closed_view_of_an_agent_carries_the_reason(self):
        self.record(state="failed", exit={"code": 1, "reason": "claude_exited"})
        view = self.manager.snapshot(AGENT)
        self.assertEqual((view["open"], view["closed_reason"]), (False, "claude_exited"))
        self.assertIn("unexpectedly", view["closed_text"])


# ----- finding: the owner's first message, and what a host that is gone has recorded -------------------------------------------------

class FirstMessage(ManagerFixture):
    def host(self, client=None):
        self.client = client or FakeClient()
        self.launcher.inspections[AGENT] = running(verified(client=self.client))

    def test_the_uuid_is_the_request_s_so_a_repeat_is_the_same_message(self):
        one = self.manager.first_message_uuid(AGENT, "key-1", "hello")
        self.assertEqual(one, self.manager.first_message_uuid(AGENT, "key-1", "hello"))
        self.assertNotEqual(one, self.manager.first_message_uuid(AGENT, "key-2", "hello"))
        self.assertNotEqual(one, self.manager.first_message_uuid(AGENT, "key-1", "hello!"))
        self.assertNotEqual(one, self.manager.first_message_uuid(OTHER, "key-1", "hello"))
        self.assertEqual(str(__import__("uuid").UUID(one)), one)

    def test_the_message_is_handed_to_the_host_remembered_and_forgotten_once_it_is_an_input(self):
        self.host()
        sent = []
        self.client.send = lambda text, uid, steer=False: sent.append((text, uid, steer)) or {"status": "ok"}
        uid = self.manager.first_message_uuid(AGENT, "key-1", "hello")
        self.manager.send_first(AGENT, "hello", uid, self.now[0] + 5)
        self.assertEqual(sent, [("hello", uid, True)])
        self.assertEqual(self.manager.staged_uuid(AGENT, "hello"), uid)
        self.assertIsNone(self.manager.staged_uuid(AGENT, "something else"))
        self.manager.unstage(AGENT, "hello")
        self.assertIsNone(self.manager.staged_uuid(AGENT, "hello"))

    def test_a_staged_message_is_forgotten_after_a_while(self):
        self.host()
        self.client.send = lambda text, uid, steer=False: {"status": "ok"}
        self.manager.send_first(AGENT, "hello", "u-1", self.now[0] + 5)
        self.now[0] += hosts.STAGED_SECONDS + 1
        self.assertIsNone(self.manager.staged_uuid(AGENT, "hello"))

    def test_a_host_that_does_not_answer_is_asked_again_with_the_same_uuid_until_the_deadline(self):
        self.host()
        attempts = []

        def send(text, uid, steer=False):
            attempts.append(uid)
            if len(attempts) < 3:
                raise HostUnavailable("timeout", "no answer")
            return {"status": "ok"}
        self.client.send = send
        self.manager.send_first(AGENT, "hello", "u-1", self.now[0] + 30)
        self.assertEqual(attempts, ["u-1"] * 3)
        self.client.send = lambda text, uid, steer=False: (_ for _ in ()).throw(HostUnavailable("timeout", "no answer"))
        with self.assertRaises(hosts.ManagedError) as late:
            self.manager.send_first(AGENT, "hello", "u-1", self.now[0] + 2)
        self.assertEqual((late.exception.code, late.exception.retryable), ("open_timeout", True))

    def test_a_closing_or_gone_session_is_not_a_hang(self):
        self.host()
        self.client.send = lambda text, uid, steer=False: {"status": "rejected", "reason": "session_closing"}
        with self.assertRaises(hosts.ManagedError) as closing:
            self.manager.send_first(AGENT, "hello", "u-1", self.now[0] + 5)
        self.assertEqual(closing.exception.code, "session_closed")
        self.launcher.inspections.clear()
        with self.assertRaises(hosts.ManagedError) as gone:
            self.manager.send_first(AGENT, "hello", "u-1", self.now[0] + 5)
        self.assertEqual(gone.exception.code, "session_closed")

    def test_after_the_host_exited_its_ledger_says_whether_the_message_left(self):
        folder = self.paths.runtime / "managed-agents" / AGENT
        folder.mkdir(parents=True, exist_ok=True)
        self.assertEqual(self.manager.sent_before_exit(AGENT, "u-1"), "absent" if (folder / "sent.jsonl").exists() else None)
        ledger = folder / "sent.jsonl"
        ledger.write_text(json.dumps({"uuid": "u-1", "state": "sent"}) + "\n" + json.dumps({"uuid": "u-2", "state": "fenced"}) + "\n")
        os.chmod(ledger, 0o600)
        self.assertEqual([self.manager.sent_before_exit(AGENT, uid) for uid in ("u-1", "u-2", "u-3")], ["sent", "fenced", "absent"])


if __name__ == "__main__":
    unittest.main()
