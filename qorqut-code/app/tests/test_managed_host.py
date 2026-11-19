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

    def compact(self):
        self._log(kind="compact")
        return not self.busy

    def update_settings(self, model=None, effort=None):
        if effort is not None and effort not in ("minimal", "low", "medium", "high", "xhigh", "max"):
            raise ValueError("effort")
        self._log(kind="settings", model=model, effort=effort)
        return True

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
        # macOS's temporary folder is long (/private/var/folders/...): a Unix socket path must stay under 104 bytes, so use /tmp there.
        self.root = Path(tempfile.mkdtemp(prefix="mh-", dir="/tmp" if sys.platform == "darwin" else None)).resolve()
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


class CommandTest(ManagedTestCase):
    """``command``: the slash commands a client takes only through its own interface (Codex ``/compact``, ``/model``)."""

    def test_compact_reaches_the_driver_and_says_whether_it_was_accepted(self):
        client = InProcessHost(self).ready()
        reply = client.command("compact")
        self.assertEqual((reply["status"], reply["name"], reply["accepted"]), ("ok", "compact", True))
        self.assertEqual(len(self.logged("compact")), 1)

    def test_model_carries_the_model_and_the_effort_together(self):
        client = InProcessHost(self).ready()
        reply = client.command("model", model="gpt-x", effort="high")
        self.assertEqual((reply["status"], reply["accepted"]), ("ok", True))
        self.assertEqual([(r["model"], r["effort"]) for r in self.logged("settings")], [("gpt-x", "high")])

    def test_a_bad_value_is_a_bad_request_and_an_unknown_command_is_refused(self):
        client = InProcessHost(self).ready()
        for bad in ({"name": "model", "model": "gpt-x", "effort": "extreme"}, {"name": "rm-rf"}):
            with self.assertRaises(HostUnavailable):
                client.command(**bad)
        self.assertEqual(self.logged("settings"), [])


class InterruptAndAnswerTest(ManagedTestCase):
    def test_interrupt_stops_the_turn_and_is_a_no_op_when_idle(self):
        client = InProcessHost(self).ready()
        self.assertEqual(client.interrupt(), {"id": mock.ANY, "status": "ok", "sent": False})
        client.send("SLOW", new_uuid())
        settle(client, "working")
        self.assertEqual(client.interrupt()["sent"], True)
        self.assertEqual(settle(client)["last_turn_status"], "interrupted")
        self.assertEqual(len(self.logged("interrupt")), 1)

    def answers(self):
        return [record for record in self.logged("answer")]

    def test_a_permission_request_is_denied_by_the_host_and_never_pending_when_the_launch_says_nothing(self):
        client = InProcessHost(self).ready()
        client.send("PERM write", new_uuid())
        status = settle(client)
        self.assertEqual((status["pending"], status["last_turn_status"]), ([], "completed"))
        answered = self.answers()
        self.assertEqual(len(answered), 1)
        self.assertEqual(answered[0]["decision"]["behavior"], "deny")
        self.assertIn("Qorqbot", answered[0]["decision"]["message"])
        events = client.events()["events"]
        self.assertEqual([(e["type"], e.get("decision"), e.get("delivered")) for e in events if e["type"] == "permission_answered"],
                         [("permission_answered", "deny", True)])

    def test_bypass_allows_and_anything_else_denies(self):
        for mode, expected in (("bypass", True), ("bypassPermissions", {"behavior": "deny"}), (None, {"behavior": "deny"})):
            with self.subTest(mode=mode):
                self.log.unlink() if self.log.exists() else None
                state = self.root / "managed-agents" / str(uuid.uuid4())
                layout.private_directory(state)
                changes = {} if mode is None else {"permission_mode": mode}
                running = InProcessHost(self, state=state, agent_id=state.name, **changes)
                client = running.ready()
                client.send("PERM write", new_uuid())
                status = settle(client)
                self.assertEqual(status["pending"], [])
                decision = self.answers()[0]["decision"]
                if expected is True:
                    self.assertIs(decision, True)
                else:
                    self.assertEqual(decision["behavior"], "deny")
                client.close()
                running.join()

    def test_a_permission_the_driver_cannot_take_is_dropped_not_kept_for_the_owner(self):
        client = InProcessHost(self).ready()
        with mock.patch.object(FakeSession, "answer", side_effect=ValueError("bad")):
            client.send("PERM write", new_uuid())
            wait_for(lambda: [e for e in client.events()["events"] if e["type"] == "permission_answered"], message="the answer")
        self.assertEqual(client.status()["pending"], [])
        self.assertEqual([e["delivered"] for e in client.events()["events"] if e["type"] == "permission_answered"], [False])
        FakeSession.last.close()

    def test_a_question_is_still_pending_and_a_permission_in_the_same_turn_is_not(self):
        client = InProcessHost(self, permission_mode="bypass").ready()
        client.send("ASK me", new_uuid())
        pending = settle(client, "blocked")["pending"]
        self.assertEqual([(item["kind"], item["tool"]) for item in pending], [("question", "AskUserQuestion")])
        FakeSession.last._emit("permission_request", request_id="req-extra", tool="Write", input={"file_path": "/tmp/y"}, tool_use_id="t")
        wait_for(lambda: [e for e in client.events()["events"] if e["type"] == "permission_answered"], message="the answer")
        self.assertEqual([item["kind"] for item in client.status()["pending"]], ["question"])
        client.answer(pending[0]["request_id"], {"answers": {"Which?": "A"}})
        settle(client)

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
        client.send("ASK", new_uuid())
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
        client.send("ASK", new_uuid())
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

    def test_the_first_message_is_the_clients_role_command_and_the_client_is_published(self):
        for client_name, role in (("codex", "$role"), (None, "/role")):
            with self.subTest(client=client_name):
                self.log.unlink() if self.log.exists() else None
                state = self.root / "managed-agents" / str(uuid.uuid4())
                layout.private_directory(state)
                changes = {} if client_name is None else {"client": client_name}
                running = InProcessHost(self, state=state, agent_id=state.name, send_role=True, **changes)
                client = running.ready()
                settle(client)
                self.assertEqual(self.sent_texts(), [role])
                self.assertEqual(client.hello()["client"], client_name or "claude")
                self.assertEqual(layout.read_host(state)["client"], client_name or "claude")
                client.close()
                running.join()

    def test_an_unknown_client_is_an_invalid_launch(self):
        running = InProcessHost(self, client="gemini")
        self.assertEqual(running.join(), layout.EXIT_CONFIG)
        self.assertEqual(layout.read_host(self.state)["exit"]["reason"], "invalid_launch")

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


if __name__ == "__main__":
    unittest.main()
