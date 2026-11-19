"""The managed agent host, its client and its launcher, against a fake driver. Nothing here starts a real ``claude``.

``FakeSession`` follows the real ``ClaudeSession`` (``managed_runtime/claude_driver.py``) as seen with a real Claude Code 2.1.327:
by default the session is announced (``session_started``) only AFTER the first message was taken up (``FAKE_INIT=late``; ``start``
announces at once, ``never`` not at all) and the transcript file does not exist until a message was processed; events are
delivered on a private dispatcher thread in order; one ``turn_finished`` covers every message folded into a run (and
``message_uuids`` is ``[]`` for a turn the host did not start); a hook-blocked prompt gives ``prompt_blocked`` then a "completed"
turn, and a steered message a hook blocked while a run holds a tool (STEERBLOCK) is finished by that run's ``turn_finished`` under
``blocked_message_uuids`` only (the run's ``message_uuids`` does not list it); ``interrupt`` returns False when idle; ``answer`` takes True/False, a string or a dict; ``close`` is safe to
call twice and ends with one ``exited`` event; the driver refuses the flags it owns. What the message text says picks the
behaviour: SLOW (runs until a message containing DONE arrives, or an interrupt), PERM, ASK, BLOCK, CRASH.

Most tests run the host in a thread of this process with the fake class injected; ``SubprocessHostTest`` starts a real detached
host process through the launcher, with the fake named in ``launch.json`` as ``test_managed_host:FakeSession``.
"""
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

PROTECTED = {"-p", "--print", "--input-format", "--output-format", "--permission-prompt-tool", "--resume", "-r", "--session-id"}
SLEEPER = "import time; time.sleep(600)"


class FakeSession:
    last = None

    def __init__(self, folder, executable, env=None, args=(), resume_session_id=None, on_event=None):
        for argument in args:
            if str(argument).split("=", 1)[0] in PROTECTED:
                raise ValueError("%s is set by the driver and cannot be passed in args" % argument)
        if resume_session_id is not None:
            uuid.UUID(str(resume_session_id))
        self.folder, self.executable, self.args = folder, executable, tuple(args)
        self.env = dict(env or {})
        self.resume = resume_session_id
        self.session_id = resume_session_id or str(uuid.uuid4())
        self._on_event = on_event
        self._lock = threading.RLock()
        self._events = queue.Queue()
        self._inflight = collections.OrderedDict()
        self._texts = {}
        self._pending = {}
        self._wake = threading.Event()
        self._state = "new"
        self._interrupted = False
        self._child = None
        self._runner = None
        self._announced = False
        self._exited = False
        self._dispatcher = None
        FakeSession.last = self

    # -- the driver's surface -------------------------------------------------------------------------------------------
    @property
    def pid(self):
        return self._child.pid if self._child is not None else None

    @property
    def busy(self):
        with self._lock:
            return bool(self._inflight)

    @property
    def transcript_path(self):
        return os.path.join(self.folder, ".fake-transcripts", self.session_id + ".jsonl")

    def start(self):
        if self.env.get("FAKE_START_FAIL"):
            raise RuntimeError("could not start the fake CLI")
        self._child = subprocess.Popen([sys.executable, "-c", SLEEPER], start_new_session=True,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._dispatcher = threading.Thread(target=self._dispatch, daemon=True)
        self._dispatcher.start()
        self._state = "running"
        self._log(kind="start", resume=self.resume, args=list(self.args), secret=self.env.get("FAKE_SECRET"))
        if self.env.get("FAKE_INIT") == "start":
            self._announce()

    def send(self, text, message_uuid):
        canonical = str(uuid.UUID(str(message_uuid)))
        with self._lock:
            if self._state != "running":
                raise RuntimeError("the Claude session is not running")
            if canonical in self._inflight:
                raise ValueError("message_uuid is already in use by an unfinished message")
            self._inflight[canonical] = "queued"
            self._texts[canonical] = text
            self._log(kind="send", text=text, uuid=canonical)
            if "DONE" in text:
                self._wake.set()
            if self._runner is None or not self._runner.is_alive():
                self._runner = threading.Thread(target=self._run, daemon=True)
                self._runner.start()

    def interrupt(self):
        with self._lock:
            if self._state != "running" or not (self._inflight or self._pending):
                return False
            self._interrupted = True
            self._log(kind="interrupt")
            self._wake.set()
            return True

    def answer(self, request_id, decision):
        with self._lock:
            if self._state != "running" or request_id not in self._pending:
                return False
            if not isinstance(decision, (bool, str, dict)):
                raise ValueError("bad decision")
            del self._pending[request_id]
            self._log(kind="answer", request_id=request_id, decision=decision)
            self._wake.set()
            return True

    def close(self, timeout=None):
        with self._lock:
            if self._state == "closed":
                return
            first = self._state == "running"
            self._state = "closing"
            if first:
                self._log(kind="close", timeout=timeout)
            self._interrupted = bool(self._inflight)
            self._wake.set()
        if self._runner is not None:
            self._runner.join(3)
        self._kill_child()
        self._exit(0, False)
        if self._dispatcher is not None and self._dispatcher is not threading.current_thread():
            self._dispatcher.join(3)

    # -- test helpers -----------------------------------------------------------------------------------------------------
    def emit_autonomous_turn(self):
        """A turn the host did not start (a background-task wake-up)."""
        self._emit("turn_started", message_uuid=None)
        self._emit("turn_finished", status="completed", message_uuids=[], blocked_message_uuids=[], session_id=self.session_id)

    def crash(self):
        self._kill_child()
        self._exit(-9, True)

    # -- internals --------------------------------------------------------------------------------------------------------
    def _log(self, **record):
        path = self.env.get("FAKE_LOG")
        if path:
            with open(path, "a") as stream:
                stream.write(json.dumps(record) + "\n")

    def _emit(self, kind, **fields):
        self._events.put({"type": kind, **fields})

    def _dispatch(self):
        while True:
            event = self._events.get()
            if event is None:
                return
            try:
                self._on_event(event)
            except Exception:
                pass

    def _announce(self):
        if not self._announced:
            self._announced = True
            self._emit("session_started", session_id=self.session_id, transcript_path=self.transcript_path)

    def _kill_child(self):
        child = self._child
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                pass
            child.wait()

    def _exit(self, code, unexpected):
        with self._lock:
            if self._exited:
                return
            self._exited = True
            inflight = list(self._inflight)
            self._inflight.clear()
            self._state = "closed"
        if inflight:
            self._emit("turn_finished", status="error" if unexpected else "interrupted", message_uuids=inflight,
                       blocked_message_uuids=[], session_id=self.session_id)
        self._emit("exited", code=code, unexpected=unexpected)
        self._events.put(None)

    def _wait_for(self, condition):
        while not condition() and self._state == "running" and not self._interrupted:
            self._wake.wait(0.02)

    def _run(self):
        while True:
            with self._lock:
                if not self._inflight or self._state != "running":
                    return
                current = next(iter(self._inflight))
                self._inflight[current] = "started"
                text = self._texts[current]
            self._emit("turn_started", message_uuid=current)
            os.makedirs(os.path.dirname(self.transcript_path), exist_ok=True)
            open(self.transcript_path, "a").close()    # the transcript appears once a message is processed
            if self.env.get("FAKE_INIT", "late") == "late":
                self._announce()                       # the CLI announces the session after the first message starts
            self._emit("activity", text="Working")
            blocked = []
            if "CRASH" in text:
                self.crash()
                return
            if "BLOCK" in text:
                blocked = [current]
                self._emit("prompt_blocked", message_uuid=current, reason="a hook refused it")
            if "PERM" in text or "ASK" in text:
                request_id = "req-" + uuid.uuid4().hex[:8]
                tool_input = {"file_path": "/tmp/x"} if "PERM" in text else {"questions": [{"question": "Which?"}]}
                with self._lock:
                    self._pending[request_id] = tool_input
                if "PERM" in text:
                    self._emit("permission_request", request_id=request_id, tool="Write", input=tool_input, tool_use_id="tu1")
                else:
                    self._emit("question", request_id=request_id, questions=tool_input["questions"], input=tool_input,
                               tool_use_id="tu2")
                self._wait_for(lambda: request_id not in self._pending)
            elif "SLOW" in text:
                self._wait_for(lambda: any("DONE" in value for value in self._texts.values()))
            else:
                time.sleep(0.05)
            with self._lock:
                finished = list(self._inflight)
                steer_blocked = [value for value in finished if value != current and "STEERBLOCK" in self._texts.get(value, "")]
                finished = [value for value in finished if value not in steer_blocked]
                blocked = blocked + steer_blocked
                self._inflight.clear()
                cancelled = list(self._pending)
                self._pending.clear()
                status = "interrupted" if self._interrupted else "completed"
                self._interrupted = False
                for value in finished:
                    self._texts.pop(value, None)
                self._wake.clear()
            for request_id in cancelled:
                self._emit("request_cancelled", request_id=request_id, reason="turn_ended")
            self._emit("turn_finished", status=status, message_uuids=finished, blocked_message_uuids=blocked,
                       session_id=self.session_id, result_text="RESULT-TEXT-SECRET")


# -- fixtures --------------------------------------------------------------------------------------------------------------

def wait_for(condition, timeout=8.0, interval=0.02, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("timed out waiting for " + message)


class ManagedTestCase(unittest.TestCase):
    def setUp(self):
        self.environment = isolated_cli_environment.isolate(self)
        self.root = Path(tempfile.mkdtemp(prefix="mh-")).resolve()
        self.addCleanup(self._remove_root)
        self.agent = str(uuid.uuid4())
        self.state = self.root / "managed-agents" / self.agent
        layout.private_directory(self.state)
        self.folder = self.root / "workspace"
        self.folder.mkdir()
        self.log = self.root / "fake.log"
        self.hosts = []

    def _remove_root(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def spec(self, **changes):
        spec = {"agent_id": self.agent, "folder": str(self.folder), "executable": "/usr/bin/claude-fake", "args": [],
                "env": {"FAKE_LOG": str(self.log)}, "resume_session_id": None, "send_role": False, "idle_seconds": 0,
                "close_seconds": 5}
        env = changes.pop("env", {})
        spec["env"].update(env)
        spec.update(changes)
        return spec

    def logged(self, kind=None):
        if not self.log.exists():
            return []
        records = [json.loads(line) for line in self.log.read_text().splitlines() if line]
        return [r for r in records if kind is None or r["kind"] == kind]


class InProcessHost:
    """A host running in a thread of this process, with the fake class injected."""

    def __init__(self, case, state=None, **spec):
        self.case = case
        self.state = state or case.state
        self.launch = launcher.prepare_launch(self.state, case.spec(**spec))
        self.stop = threading.Event()
        self.host = host_module.ManagedHost(self.state, driver_class=FakeSession)
        self.code = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        case.addCleanup(self.shutdown)
        case.hosts.append(self)

    def _run(self):
        self.code = self.host.run(self.stop)

    def ready(self, timeout=8.0):
        wait_for(lambda: (layout.read_host(self.state) or {}).get("state") == "ready", timeout, message="the host to be ready")
        return self.client()

    def client(self, **options):
        return HostClient(self.state, **options)

    def join(self, timeout=15.0):
        self.thread.join(timeout)
        self.case.assertFalse(self.thread.is_alive(), "the host did not exit")
        return self.code

    def shutdown(self):
        self.stop.set()
        self.thread.join(15)


def new_uuid():
    return str(uuid.uuid4())


def settle(client, phase="idle"):
    return wait_for(lambda: (lambda s: s if s["phase"] == phase else None)(client.status()), message="phase " + phase)


# -- the host --------------------------------------------------------------------------------------------------------------

class HostStateTest(ManagedTestCase):
    def test_launch_is_read_and_unlinked_and_everything_private(self):
        running = InProcessHost(self)
        running.ready()
        self.assertFalse((self.state / layout.LAUNCH_FILE).exists())
        for name in (layout.TOKEN_FILE, layout.HOST_FILE, layout.EVENTS_FILE, layout.SENT_FILE, layout.SOCKET_FILE, layout.LOCK_FILE):
            mode = (self.state / name).lstat().st_mode & 0o777
            self.assertEqual(mode & 0o077, 0, "%s is not private: %o" % (name, mode))
        record = layout.read_host(self.state)
        self.assertEqual((record["agent_id"], record["state"], record["host_pid"]), (self.agent, "ready", os.getpid()))
        self.assertNotIn(running.launch["token"], json.dumps(record))
        self.assertEqual(layout.read_token(self.state), running.launch["token"])

    def test_second_host_exits_busy_and_does_not_disturb_the_first(self):
        first = InProcessHost(self)
        client = first.ready()
        started = layout.read_host(self.state)["instance"]
        self.assertEqual(host_module.ManagedHost(self.state, driver_class=FakeSession).run(threading.Event()), layout.EXIT_BUSY)
        self.assertEqual(client.hello()["instance"], started)
        self.assertEqual(client.status()["phase"], "idle")

    def test_hello_and_status(self):
        client = InProcessHost(self).ready()
        hello = client.hello()
        self.assertEqual(hello["status"], "ok")
        self.assertEqual((hello["agent_id"], hello["host_pid"], hello["state"]), (self.agent, os.getpid(), "ready"))
        self.assertEqual(hello["claude_pid"], FakeSession.last.pid)
        self.assertEqual(hello["claude_identity"], layout.process_identity(FakeSession.last.pid))
        self.assertEqual(hello["session_id"], FakeSession.last.session_id)
        status = client.status()
        for key in ("phase", "activity", "turn_started_at", "pending", "last_turn_status", "seq", "session_id"):
            self.assertIn(key, status)
        self.assertEqual((status["phase"], status["pending"], status["last_turn_status"]), ("idle", [], None))

    def test_managed_host_file_for_the_hook_side_exists_before_the_driver_starts_and_goes_after(self):
        marker_path = self.folder / ".qorqut" / "managed-host.json"
        seen = {}

        class Watching(FakeSession):
            def start(inner):
                seen["marker"] = layout.read_json(marker_path)       # the driver has not started claude yet
                super().start()

        launch = launcher.prepare_launch(self.state, self.spec())
        stop = threading.Event()
        host = host_module.ManagedHost(self.state, driver_class=Watching)
        thread = threading.Thread(target=lambda: host.run(stop), daemon=True)
        thread.start()
        self.addCleanup(lambda: (stop.set(), thread.join(15)))
        client = HostClient(self.state)
        wait_for(lambda: (layout.read_host(self.state) or {}).get("state") == "ready", message="ready")
        ticks = layout.process_ticks(layout.process_identity(os.getpid()))
        for marker in (seen["marker"], layout.read_json(marker_path)):
            self.assertEqual(marker, {"schema_version": 1, "host_id": launch["instance"], "host_pid": os.getpid(),
                                      "host_ticks": ticks, "agent_id": self.agent, "instance": launch["instance"]})
        self.assertEqual(marker_path.stat().st_mode & 0o777, 0o600)
        self.assertLess(marker_path.stat().st_size, 4096)
        self.assertIsInstance(seen["marker"]["host_ticks"], str)
        # hello and status expose the same ticks the probe-side owner string is built from, for host and claude
        for reply in (client.hello(), client.status()):
            self.assertEqual((reply["host_pid"], reply["host_ticks"]), (os.getpid(), ticks))
            self.assertEqual((reply["claude_pid"], reply["claude_ticks"]),
                             (FakeSession.last.pid, layout.process_ticks(layout.process_identity(FakeSession.last.pid))))
        self.assertEqual(layout.host_owner(os.getpid(), layout.process_identity(os.getpid())), "host:%d:%s" % (os.getpid(), ticks))
        client.close()
        thread.join(15)
        self.assertFalse(marker_path.exists())

    def test_the_hosts_command_line_has_dash_m_directly_before_the_module_in_both_launch_forms(self):
        def consecutive(argv):
            return any(a == "-m" and b == "qorqut.managed_runtime.host" for a, b in zip(argv, argv[1:]))
        self.assertTrue(consecutive(launcher.host_argv(self.state)))
        calls = []

        def run(argv, **options):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "LoadState=not-found\nActiveState=inactive\n" if "show" in argv else "", "")
        if sys.platform == "linux":
            launcher.UserUnitLauncher(runner=run, outside=lambda p: Path(p)).start(self.state)
            self.assertTrue(consecutive(calls[-1][calls[-1].index("--") + 1:]))

    def test_driver_args_and_environment_reach_the_driver(self):
        InProcessHost(self, args=["--settings", "/ws/s.json"], env={"FAKE_SECRET": "s3cret", "REMOVED": None}).ready()
        self.assertEqual(FakeSession.last.args, ("--settings", "/ws/s.json"))
        self.assertEqual(FakeSession.last.executable, "/usr/bin/claude-fake")
        self.assertEqual(self.logged("start")[0]["secret"], "s3cret")
        self.assertNotIn("s3cret", (self.state / layout.HOST_FILE).read_text())


class SendTest(ManagedTestCase):
    def test_send_starts_a_turn_and_the_driver_gets_the_text_once(self):
        client = InProcessHost(self).ready()
        value = new_uuid()
        reply = client.send("hello there", value)
        self.assertEqual((reply["status"], reply["mode"], reply["turn_started"], reply["duplicate"]), ("ok", "started", True, False))
        status = settle(client)
        self.assertEqual(status["last_turn_status"], "completed")
        self.assertIsNone(status["activity"])
        self.assertEqual([r["text"] for r in self.logged("send")], ["hello there"])

    def test_the_same_uuid_again_is_acknowledged_and_not_sent_twice(self):
        client = InProcessHost(self).ready()
        value = new_uuid()
        first = client.send("once", value)
        again = client.send("once", value)
        self.assertEqual((again["status"], again["duplicate"], again["mode"]), ("ok", True, first["mode"]))
        settle(client)
        self.assertEqual(len(self.logged("send")), 1)

    def test_lookup_reports_seen_and_unseen_and_fence_makes_unseen_final(self):
        client = InProcessHost(self).ready()
        sent, other = new_uuid(), new_uuid()
        client.send("x", sent)
        seen = client.lookup(sent)
        self.assertEqual((seen["seen"], seen["fenced"]), (True, False))
        self.assertEqual({k: client.lookup(other)[k] for k in ("seen", "fenced")}, {"seen": False, "fenced": False})
        fenced = client.lookup(other, fence=True)
        self.assertEqual((fenced["seen"], fenced["fenced"]), (False, True))
        late = client.send("late arrival", other)
        self.assertEqual((late["status"], late["reason"]), ("rejected", "fenced"))
        settle(client)
        self.assertEqual([r["text"] for r in self.logged("send")], ["x"])
        # a fence on an uuid that was accepted does not undo it
        self.assertEqual(client.lookup(sent, fence=True)["seen"], True)

    def test_without_steer_a_running_turn_rejects_and_with_steer_it_queues(self):
        client = InProcessHost(self).ready()
        slow = new_uuid()
        self.assertEqual(client.send("SLOW please", slow)["mode"], "started")
        self.assertEqual(settle(client, "working")["inflight"], [slow])
        refused = client.send("not now", new_uuid(), steer=False)
        self.assertEqual((refused["status"], refused["reason"]), ("rejected", "turn_running"))
        middle, done = new_uuid(), new_uuid()
        queued = client.send("a thought", middle, steer=True)
        self.assertEqual((queued["status"], queued["mode"], queued["turn_started"]), ("ok", "queued", False))
        client.send("DONE", done, steer=True)
        status = settle(client)
        self.assertEqual(status["last_turn"]["message_uuids"], [slow, middle, done])    # folded into one run
        self.assertEqual(status["last_turn_status"], "completed")
        self.assertEqual(client.lookup(middle)["finished"], True)

    def test_phase_follows_the_turn_and_a_turn_the_host_did_not_start_changes_nothing(self):
        client = InProcessHost(self).ready()
        client.send("SLOW", new_uuid())
        status = wait_for(lambda: (lambda s: s if s["phase"] == "working" and s["activity"] else None)(client.status()),
                          message="the activity")      # the driver reports it just after turn_started
        self.assertIsNotNone(status["turn_started_at"])
        self.assertEqual(status["activity"], "Working")
        client.send("DONE", new_uuid(), steer=True)
        settle(client)
        FakeSession.last.emit_autonomous_turn()
        events = wait_for(lambda: [e for e in client.events()["events"] if e["type"] == "turn_finished"][1:], message="wake-up turn")
        self.assertEqual(events[0]["message_uuids"], [])
        self.assertEqual(client.status()["phase"], "idle")

    def test_a_hook_blocked_prompt_is_an_event_and_the_turn_still_completes(self):
        client = InProcessHost(self).ready()
        value = new_uuid()
        client.send("BLOCK this", value)
        status = settle(client)
        self.assertEqual(status["last_turn_status"], "completed")
        self.assertEqual(status["last_turn"]["blocked_message_uuids"], [value])
        blocked = [e for e in client.events()["events"] if e["type"] == "prompt_blocked"]
        self.assertEqual((blocked[0]["message_uuid"], blocked[0]["reason"]), (value, "a hook refused it"))

    def test_input_before_the_session_is_ready_and_after_close_is_rejected(self):
        running = InProcessHost(self, env={"FAKE_INIT": "late"}, ready_seconds=0)
        client = wait_for(lambda: layout.read_token(self.state) and running.client(), message="the socket")
        wait_for(lambda: (layout.read_host(self.state) or {}).get("state") == "starting", message="starting")
        reply = client.send("too early", new_uuid())
        self.assertEqual((reply["status"], reply["reason"]), ("rejected", "session_starting"))
        self.assertEqual(client.status()["phase"], "starting")

    def test_a_send_during_close_is_rejected_as_closing(self):
        running = InProcessHost(self)
        client = running.ready()
        client.send("SLOW", new_uuid())
        settle(client, "working")
        self.assertEqual(client.close()["status"], "ok")
        # either the host still answers (closing) or it already left; a send is never accepted
        try:
            reply = client.send("late", new_uuid())
            self.assertEqual(reply["status"], "rejected")
            self.assertIn(reply["reason"], ("session_closing", "session_closed"))
        except HostUnavailable:
            pass
        running.join()


class InterruptAndAnswerTest(ManagedTestCase):
    def test_interrupt_stops_the_turn_and_is_a_no_op_when_idle(self):
        client = InProcessHost(self).ready()
        self.assertEqual(client.interrupt(), {"id": mock.ANY, "status": "ok", "sent": False})
        client.send("SLOW", new_uuid())
        settle(client, "working")
        self.assertEqual(client.interrupt()["sent"], True)
        self.assertEqual(settle(client)["last_turn_status"], "interrupted")
        self.assertEqual(len(self.logged("interrupt")), 1)

    def test_permission_request_is_state_and_answer_resumes_the_turn(self):
        client = InProcessHost(self).ready()
        client.send("PERM write", new_uuid())
        status = settle(client, "blocked")
        pending = status["pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual((pending[0]["kind"], pending[0]["tool"], pending[0]["input"]), ("permission", "Write", {"file_path": "/tmp/x"}))
        # a second, fresh client (a restarted service) reads the same pending state: it is state, not an event
        self.assertEqual(HostClient(self.state).status()["pending"], pending)
        reply = client.answer(pending[0]["request_id"], "allow")
        self.assertEqual((reply["status"], reply["delivered"]), ("ok", True))
        self.assertEqual(settle(client)["pending"], [])
        self.assertEqual(self.logged("answer")[0]["decision"], "allow")

    def test_question_is_answered_with_an_object_and_an_unknown_request_is_not_delivered(self):
        client = InProcessHost(self).ready()
        client.send("ASK me", new_uuid())
        pending = settle(client, "blocked")["pending"][0]
        self.assertEqual((pending["kind"], pending["questions"]), ("question", [{"question": "Which?"}]))
        self.assertEqual(client.answer("nope", True)["delivered"], False)
        self.assertEqual(client.answer(pending["request_id"], {"answers": {"Which?": "A"}})["delivered"], True)
        settle(client)

    def test_an_interrupt_withdraws_the_pending_request(self):
        client = InProcessHost(self).ready()
        client.send("PERM", new_uuid())
        settle(client, "blocked")
        client.interrupt()
        status = settle(client)
        self.assertEqual((status["pending"], status["last_turn_status"]), ([], "interrupted"))
        self.assertTrue([e for e in client.events()["events"] if e["type"] == "request_cancelled"])


class EventLogTest(ManagedTestCase):
    def test_sequence_is_monotonic_and_the_cursor_returns_only_newer_events(self):
        client = InProcessHost(self).ready()
        client.send("one", new_uuid())
        settle(client)
        first = client.events()
        seqs = [e["seq"] for e in first["events"]]
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertEqual(seqs[0], 1)
        self.assertEqual((first["next"], first["gap"]), (seqs[-1], False))
        self.assertEqual(client.events(since=first["next"])["events"], [])
        client.send("two", new_uuid())
        settle(client)
        more = client.events(since=first["next"])
        self.assertTrue(more["events"])
        self.assertTrue(all(e["seq"] > first["next"] for e in more["events"]))
        self.assertEqual(client.events(since=0, limit=2)["events"][-1]["seq"], 2)
        self.assertEqual({"host_state", "session_started", "turn_started", "turn_finished"} - {e["type"] for e in first["events"]}, set())

    def test_the_log_is_bounded_in_memory_and_on_disk_and_a_stale_cursor_sees_a_gap(self):
        client = InProcessHost(self, event_limit=6).ready()
        for number in range(5):
            client.send("n%d" % number, new_uuid())
            settle(client)
        reply = client.events(since=0)
        self.assertTrue(reply["gap"])
        self.assertLessEqual(len(reply["events"]), 6)
        self.assertGreater(reply["oldest"], 1)
        self.assertEqual(reply["events"][-1]["seq"], reply["seq"])
        lines = (self.state / layout.EVENTS_FILE).read_text().splitlines()
        self.assertLessEqual(len(lines), 2 * 6 + 1)
        self.assertEqual(json.loads(lines[-1])["seq"], reply["seq"])
        self.assertFalse(client.events(since=reply["seq"])["gap"])

    def test_the_file_remains_readable_after_the_host_is_gone(self):
        running = InProcessHost(self)
        client = running.ready()
        client.send("hi", new_uuid())
        settle(client)
        client.close()
        running.join()
        kinds = [e["type"] for e in layout.read_events_file(self.state)]
        self.assertIn("turn_finished", kinds)
        self.assertEqual(kinds[-1], "host_state")


class RefusalTest(ManagedTestCase):
    def test_a_wrong_token_is_refused_and_a_missing_one_gets_no_service(self):
        running = InProcessHost(self)
        client = running.ready()
        with mock.patch.object(layout, "read_token", return_value="x" * 43):
            with self.assertRaises(HostUnavailable) as caught:
                running.client().status()
        self.assertEqual(caught.exception.reason, "refused")
        raw = socket.socket(socket.AF_UNIX)
        self.addCleanup(raw.close)
        raw.connect(str(self.state / layout.SOCKET_FILE))
        raw.sendall(protocol.encode({"id": "a", "op": "status"}))
        reply = protocol.decode(protocol.LineReader(raw).readline())
        self.assertEqual((reply["status"], reply["error"]), ("error", "unauthorized"))
        self.assertEqual(client.status()["phase"], "idle")            # and the host is unharmed
        self.assertEqual(self.logged("send"), [])

    def test_a_peer_that_is_not_this_user_gets_no_answer(self):
        other = lambda connection: (os.getpid(), os.geteuid() + 1)
        running = InProcessHost.__new__(InProcessHost)
        running.case, running.state = self, self.state
        running.launch = launcher.prepare_launch(self.state, self.spec())
        running.stop = threading.Event()
        running.host = host_module.ManagedHost(self.state, driver_class=FakeSession, peer_credentials=other)
        running.code = None
        running.thread = threading.Thread(target=running._run, daemon=True)
        running.thread.start()
        self.addCleanup(running.shutdown)
        wait_for(lambda: (layout.read_host(self.state) or {}).get("state") == "ready", message="ready")
        with self.assertRaises(HostUnavailable) as caught:
            running.client().status()
        self.assertEqual(caught.exception.reason, "connection_lost")

    def test_a_socket_whose_peer_pid_is_not_the_recorded_host_is_not_trusted(self):
        running = InProcessHost(self)
        client = running.ready()
        with mock.patch.object(layout, "peer_credentials", return_value=(os.getpid() + 1, os.geteuid())):
            with self.assertRaises(HostUnavailable) as caught:
                client.hello()
        self.assertEqual((caught.exception.reason, caught.exception.sent), ("unverified", False))
        with mock.patch.object(layout, "peer_credentials", return_value=(None, None)):
            with self.assertRaises(HostUnavailable):
                client.hello()                                         # fails closed when the platform cannot say
        self.assertEqual(client.hello()["status"], "ok")

    def test_bad_requests_are_errors_not_crashes(self):
        running = InProcessHost(self)
        running.ready()
        token = layout.read_token(self.state)
        raw = socket.socket(socket.AF_UNIX)
        self.addCleanup(raw.close)
        raw.connect(str(self.state / layout.SOCKET_FILE))
        reader = protocol.LineReader(raw)
        for message, code in (({"id": "1", "op": "send", "token": token, "text": "x", "message_uuid": "bad"}, "bad_request"),
                              ({"id": "2", "op": "explode", "token": token}, "unknown_op")):
            raw.sendall(protocol.encode(message))
            self.assertEqual(protocol.decode(reader.readline())["error"], code)
        raw.sendall(b"not json\n")
        self.assertEqual(protocol.decode(reader.readline())["error"], "bad_request")
        self.assertEqual(running.client().status()["phase"], "idle")


class ClientTest(ManagedTestCase):
    def test_no_host_and_not_running(self):
        with self.assertRaises(HostUnavailable) as caught:
            HostClient(self.state).status()
        self.assertEqual(caught.exception.reason, "no_host")
        running = InProcessHost(self)
        client = running.ready()
        client.close()
        running.join()
        with self.assertRaises(HostUnavailable) as caught:
            HostClient(self.state).status()
        self.assertEqual(caught.exception.reason, "not_running")

    def test_a_client_is_pinned_to_the_instance_it_first_talked_to(self):
        first = InProcessHost(self)
        client = first.ready()
        client.status()
        client.close()
        first.join()
        InProcessHost(self).ready()
        with self.assertRaises(HostUnavailable) as caught:
            client.status()
        self.assertEqual(caught.exception.reason, "instance_changed")

    def test_a_silent_host_times_out_and_the_error_says_the_request_was_written(self):
        # a stand-in that accepts and never answers; the recorded pid is this process, which holds the listener
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(self.state / layout.SOCKET_FILE))
        listener.listen(1)
        self.addCleanup(listener.close)
        accepted = []
        threading.Thread(target=lambda: accepted.append(listener.accept()), daemon=True).start()
        self.addCleanup(lambda: [pair[0].close() for pair in accepted])
        layout.write_private(self.state / layout.TOKEN_FILE, b"t" * 32)
        layout.write_json_private(self.state / layout.HOST_FILE, {
            "schema_version": 1, "agent_id": self.agent, "instance": "i", "host_pid": os.getpid(), "host_identity": "x",
            "protocol": 1, "state": "ready", "socket_path": None, "socket_fallback": False})
        started = time.monotonic()
        with self.assertRaises(HostUnavailable) as caught:
            HostClient(self.state).send("hi", new_uuid(), timeout=0.4)
        self.assertEqual((caught.exception.reason, caught.exception.sent), ("timeout", True))
        self.assertLess(time.monotonic() - started, 3)

    def test_connection_refused_is_unreachable_and_nothing_was_sent(self):
        layout.write_private(self.state / layout.TOKEN_FILE, b"t" * 32)
        layout.write_json_private(self.state / layout.HOST_FILE, {
            "schema_version": 1, "agent_id": self.agent, "instance": "i", "host_pid": os.getpid(), "host_identity": "x",
            "protocol": 1, "state": "ready", "socket_path": None, "socket_fallback": False})
        with self.assertRaises(HostUnavailable) as caught:
            HostClient(self.state).send("hi", new_uuid())
        self.assertEqual((caught.exception.reason, caught.exception.sent), ("unreachable", False))


class LifecycleTest(ManagedTestCase):
    def test_idle_session_is_closed_and_the_host_exits_cleanly(self):
        running = InProcessHost(self, idle_seconds=0.6)
        client = running.ready()
        client.send("hi", new_uuid())
        self.assertEqual(running.join(), layout.EXIT_STOPPED)
        record = layout.read_host(self.state)
        self.assertEqual((record["state"], record["exit"]), ("closed", {"code": 0, "reason": "idle"}))
        self.assertTrue(self.logged("close"))
        self.assertFalse((self.state / layout.SOCKET_FILE).exists())
        self.assertFalse((self.state / layout.TOKEN_FILE).exists())
        self.assertFalse(layout.lock_held(self.state / layout.LOCK_FILE))
        self.assertTrue(FakeSession.last._child.poll() is not None)

    def test_a_running_turn_or_a_pending_request_is_never_idle(self):
        running = InProcessHost(self, idle_seconds=0.4)
        client = running.ready()
        client.send("PERM", new_uuid())
        settle(client, "blocked")
        time.sleep(1.2)
        self.assertEqual(client.status()["phase"], "blocked")
        client.answer(client.status()["pending"][0]["request_id"], True)
        self.assertEqual(running.join(), 0)

    def test_status_polling_does_not_keep_a_session_alive(self):
        running = InProcessHost(self, idle_seconds=0.8)
        client = running.ready()
        deadline = time.monotonic() + 10
        while running.thread.is_alive() and time.monotonic() < deadline:
            try:
                client.status()
            except HostUnavailable:
                break
            time.sleep(0.05)
        self.assertEqual(running.join(), 0)

    def test_close_request_stops_the_session_and_reports_why(self):
        running = InProcessHost(self)
        client = running.ready()
        self.assertEqual(client.close()["status"], "ok")
        self.assertEqual(running.join(), 0)
        self.assertEqual(layout.read_host(self.state)["exit"], {"code": 0, "reason": "requested"})

    def test_close_now_passes_a_zero_grace_to_the_driver(self):
        running = InProcessHost(self)
        running.ready().close("now")
        running.join()
        self.assertEqual(self.logged("close")[0]["timeout"], 0.0)

    def test_a_stop_signal_closes_gracefully(self):
        running = InProcessHost(self)
        running.ready()
        running.stop.set()
        self.assertEqual(running.join(), 0)
        self.assertEqual(layout.read_host(self.state)["exit"]["reason"], "signal")

    def test_a_claude_process_that_dies_on_its_own_is_a_failed_host(self):
        running = InProcessHost(self)
        client = running.ready()
        client.send("CRASH now", new_uuid())
        self.assertEqual(running.join(), layout.EXIT_FAILED)
        record = layout.read_host(self.state)
        self.assertEqual((record["state"], record["exit"]["reason"]), ("failed", "claude_exited"))

    def test_a_resumed_session_whose_claude_dies_before_it_is_ready_is_reported_as_resume_failed(self):
        class Dies(FakeSession):
            def start(inner):
                super().start()
                inner.crash()
        launcher.prepare_launch(self.state, self.spec(resume_session_id=new_uuid(), env={"FAKE_INIT": "never"}))
        self.assertEqual(host_module.ManagedHost(self.state, driver_class=Dies).run(threading.Event()), layout.EXIT_FAILED)
        self.assertEqual(layout.read_host(self.state)["exit"]["reason"], "resume_failed")

    def test_a_driver_that_cannot_start_is_reported_as_unavailable(self):
        running = InProcessHost(self, env={"FAKE_START_FAIL": "1"})
        self.assertEqual(running.join(), layout.EXIT_FAILED)
        record = layout.read_host(self.state)
        self.assertEqual((record["state"], record["exit"]["reason"]), ("failed", "driver_unavailable"))

    def test_an_unimportable_driver_named_in_launch_json_is_unavailable(self):
        launcher.prepare_launch(self.state, self.spec(driver="no_such_module_here:Nothing"))
        self.assertEqual(host_module.ManagedHost(self.state).run(threading.Event()), layout.EXIT_FAILED)
        self.assertEqual(layout.read_host(self.state)["exit"]["reason"], "driver_unavailable")

    def test_arguments_the_driver_owns_are_a_configuration_error(self):
        running = InProcessHost(self, args=["--resume=abc"])
        self.assertEqual(running.join(), layout.EXIT_CONFIG)
        self.assertEqual(layout.read_host(self.state)["exit"]["reason"], "invalid_launch")

    def test_missing_or_invalid_launch_files_are_a_configuration_error_and_leave_no_secret_behind(self):
        self.assertEqual(host_module.ManagedHost(self.state, driver_class=FakeSession).run(threading.Event()), layout.EXIT_CONFIG)
        layout.write_private(self.state / layout.LAUNCH_FILE, b'{"schema_version": 1, "token": "short"}')
        self.assertEqual(host_module.ManagedHost(self.state, driver_class=FakeSession).run(threading.Event()), layout.EXIT_CONFIG)
        self.assertFalse((self.state / layout.LAUNCH_FILE).exists())
        self.assertEqual(layout.read_host(self.state)["state"], "failed")
        wrong_agent = launcher.prepare_launch(self.state, self.spec())
        wrong_agent["agent_id"] = new_uuid()
        layout.write_json_private(self.state / layout.LAUNCH_FILE, wrong_agent)
        self.assertEqual(host_module.ManagedHost(self.state, driver_class=FakeSession).run(threading.Event()), layout.EXIT_CONFIG)

    def test_a_state_directory_that_is_not_private_is_refused(self):
        os.chmod(self.state, 0o755)
        self.assertEqual(host_module.ManagedHost(self.state, driver_class=FakeSession).run(threading.Event()), layout.EXIT_CONFIG)


class RoleTest(ManagedTestCase):
    def sent_texts(self):
        return [r["text"] for r in self.logged("send")]

    def test_a_fresh_session_gets_role_first_through_the_hosts_own_send(self):
        running = InProcessHost(self, send_role=True)
        client = running.ready()
        settle(client)
        self.assertEqual(self.sent_texts(), ["/role"])
        self.assertTrue([e for e in client.events()["events"] if e["type"] == "role_sent"])
        client.send("hello", new_uuid())
        settle(client)
        self.assertEqual(self.sent_texts(), ["/role", "hello"])

    def test_the_role_turn_makes_the_agent_busy_so_an_ordinary_send_waits(self):
        running = InProcessHost(self, send_role=True, env={"FAKE_INIT": "start"})
        client = running.ready()
        state = client.status()
        # right after ready the role turn is in flight or just finished; a non-steer send never overtakes a running turn
        if state["phase"] == "working":
            self.assertEqual(client.send("hi", new_uuid())["reason"], "turn_running")

    def test_a_custom_role_text(self):
        running = InProcessHost(self, send_role="$role")
        settle(running.ready())
        self.assertEqual(self.sent_texts(), ["$role"])

    def test_a_resumed_session_and_a_session_without_send_role_get_no_role(self):
        running = InProcessHost(self, send_role=True, resume_session_id=new_uuid())
        settle(running.ready())
        self.assertEqual(self.sent_texts(), [])
        self.assertEqual(self.logged("start")[0]["resume"], FakeSession.last.session_id)
        running.client().close()
        running.join()
        other = Path(tempfile.mkdtemp(prefix="mh-", dir=self.root)) / self.agent
        layout.private_directory(other)
        quiet = InProcessHost(self, state=other, send_role=False)
        settle(quiet.ready().__class__(other))
        self.assertEqual(self.sent_texts(), [])


class StartupOrderTest(ManagedTestCase):
    def test_a_cli_that_announces_its_session_only_after_the_first_message_becomes_ready_by_fallback(self):
        running = InProcessHost(self, env={"FAKE_INIT": "late"}, ready_seconds=0.3, send_role=True)
        client = running.ready()
        # ready came from the fallback; the role message started the turn and the session was announced after turn_started
        status = wait_for(lambda: (lambda s: s if s["session_id"] and s["phase"] == "idle" else None)(client.status()),
                          message="the session announcement")
        self.assertEqual(status["session_id"], FakeSession.last.session_id)
        kinds = [e["type"] for e in client.events()["events"]]
        self.assertLess(kinds.index("turn_started"), kinds.index("session_started"))
        self.assertEqual(layout.read_host(self.state)["session_id"], FakeSession.last.session_id)

    def test_with_the_fallback_off_the_host_waits_for_the_announcement(self):
        running = InProcessHost(self, env={"FAKE_INIT": "late"}, ready_seconds=0)
        wait_for(lambda: layout.read_host(self.state), message="record")
        time.sleep(0.6)
        self.assertEqual(layout.read_host(self.state)["state"], "starting")
        running.client().close()
        running.join()


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
        client.send("PERM go", new_uuid())
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
        client.send("PERM write", new_uuid())
        pending = settle(client, "blocked")["pending"][0]
        reply = client.answer(pending["request_id"], {"behavior": "allow", "updated_input": {"file_path": "/etc/passwd"},
                                                      "updated_permissions": [{"type": "addRules"}], "message": "ok", "extra": 1})
        self.assertEqual(reply["delivered"], True)
        self.assertEqual(self.logged("answer")[0]["decision"], {"behavior": "allow", "message": "ok"})

    def test_a_decision_the_driver_refuses_is_a_bad_request_and_the_request_stays_pending(self):
        client = InProcessHost(self).ready()
        client.send("PERM write", new_uuid())
        pending = settle(client, "blocked")["pending"][0]
        with mock.patch.object(FakeSession, "answer", side_effect=ValueError("an explicit allow or deny is required")):
            with self.assertRaises(HostUnavailable) as caught:
                client.answer(pending["request_id"], {"answers": {"x": "y"}})
        self.assertEqual(caught.exception.reason, "protocol")
        self.assertEqual([item["request_id"] for item in client.status()["pending"]], [pending["request_id"]])
        self.assertEqual(client.answer(pending["request_id"], True)["delivered"], True)


class BoundsTest(ManagedTestCase):
    def emit_permission(self, request_id, **fields):
        base = {"request_id": request_id, "tool": "Bash", "input": {"command": "ls"}, "input_truncated": False, "tool_use_id": "tu"}
        FakeSession.last._emit("permission_request", **{**base, **fields})

    def test_pending_items_keep_the_full_input_up_to_a_bound_and_the_reason_and_path(self):
        client = InProcessHost(self).ready()
        command = "echo " + "x" * 12000
        self.emit_permission("req-full", input={"command": command}, decision_reason="Command contains a redirect",
                             blocked_path="/work/dir/file.txt")
        item = wait_for(lambda: client.status()["pending"] or None, message="the request")[0]
        self.assertEqual(item["input"], {"command": command})
        self.assertEqual((item["input_truncated"], item["decision_reason"], item["blocked_path"]),
                         (False, "Command contains a redirect", "/work/dir/file.txt"))
        self.assertEqual(item["tool"], "Bash")

    def test_an_oversized_request_cannot_make_status_unreadable_and_its_id_survives_in_the_log(self):
        client = InProcessHost(self).ready()
        self.emit_permission("req-huge", suggestions=[{"type": "addRules", "rules": ["y" * 200000]}] * 30,
                             input={"command": "z" * 300000}, title="t" * 100000)
        item = wait_for(lambda: client.status()["pending"] or None, message="the request")[0]
        self.assertEqual(item["request_id"], "req-huge")
        self.assertLessEqual(len(json.dumps(item)), host_module.MAX_PENDING_ITEM_BYTES)
        self.assertTrue(item["input_truncated"] or item["input"] is None or len(json.dumps(item["input"])) < 300000)
        self.assertLess(len(protocol.encode(client.status())), protocol.MAX_LINE)
        self.assertEqual(client.answer("req-huge", False)["delivered"], False)           # the fake does not know it; the host copes
        events = [e for e in client.events()["events"] if e["type"] == "permission_request"]
        self.assertEqual(events[-1]["request_id"], "req-huge")
        logged = [e for e in layout.read_events_file(self.state) if e["type"] == "permission_request"]
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

    def test_many_oversized_requests_still_fit_in_a_status_reply(self):
        client = InProcessHost(self).ready()
        for index in range(host_module.MAX_PENDING):
            self.emit_permission("req-%d" % index, input={"command": "c" * 15000}, title="t" * 400, decision_reason="r" * 400,
                                 suggestions=[{"type": "addRules", "rules": ["s" * 1900]}] * 4)
        status = wait_for(lambda: (lambda s: s if len(s["pending"]) == host_module.MAX_PENDING else None)(client.status()),
                          message="all requests")
        self.assertLess(len(protocol.encode(status)), protocol.MAX_LINE)
        self.assertEqual([item["request_id"] for item in status["pending"]], ["req-%d" % i for i in range(host_module.MAX_PENDING)])
        self.assertTrue(status.get("status_truncated"))
        self.assertTrue(all(item["input_truncated"] for item in status["pending"]))

    def test_the_event_log_keeps_neither_result_text_nor_tool_input_but_status_has_what_the_ui_needs(self):
        client = InProcessHost(self).ready()
        client.send("PERM write", new_uuid())
        pending = settle(client, "blocked")["pending"][0]
        self.assertEqual(pending["input"], {"file_path": "/tmp/x"})
        client.answer(pending["request_id"], True)
        settle(client)
        for text in (json.dumps(client.events()["events"]), (self.state / layout.EVENTS_FILE).read_text()):
            self.assertNotIn("RESULT-TEXT-SECRET", text)
            self.assertNotIn("/tmp/x", text)
        types = [e["type"] for e in client.events()["events"]]
        self.assertIn("permission_request", types)
        self.assertEqual([e for e in client.events()["events"] if e["type"] == "permission_request"][0]["request_id"], pending["request_id"])


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
