"""A managed agent from a closed session to a closed session, through every real layer that does not need a database service.

REAL here: a detached host process started through the launcher (``DetachedLauncher``, ``launch.json``, the Unix socket), the
host client, ``ManagedRuntime`` (mode file, host discovery and verification, ``ensure_open``, events), the ``managed`` adapter, the
session probe (its real process checks: the marker's process must exist with those start ticks and descend from the host), the
session registry and the binding transaction, the input arbiter and its durable inputs, the backend pass (``NativeBackend.observe``),
and the ``NativeChat`` facade (``send``, ``interrupt``, ``answer``, ``close``, ``session_view``, ``events``).

REPLACED, and nothing else:

* The Claude CLI. The host is told to load ``test_managed_host:FakeSession`` (``driver`` in ``launch.json``): a fake whose "claude"
  is a ``sleep`` child process, so the probe still verifies a real pid. What the message text says picks the behaviour (SLOW, PERM,
  ASK, BLOCK) as in ``test_managed_host``. No real ``claude``, model turn or sign-in is ever used.
* The session hooks. In the product the Claude hooks write the marker file (``.qorqut/terminal-chat.json`` /
  ``native-binding.json``) and bootstrap adopts the conversation into ``native_session_owners``, and they do so only after Claude
  has taken up a first message: before that nothing announces the session (a resumed session sends nothing by itself, a fresh one
  is sent ``/role`` by the host), and a prompt a hook blocked writes nothing at all. ``Hooks`` below does exactly those two things,
  from the real host's record, ``HOOK_DELAY`` after the fake CLI took up a message that was not blocked (marker after the owner
  row, as the hooks do); ``block_role`` makes the hooks refuse the ``/role`` prompt.
* The database: an in-memory SQLite with the real native Chat schema plus the one owner table (as ``test_managed_session_binding``),
  behind a lock; principal, protected-control and writer-admission checks are stubbed (they are the service's, not this layer's).
* The transcript archive: the indexer is a no-op (no transcript exists), so Chat has no conversation entries here.
"""
import isolated_cli_environment  # before any Qorqbot import: tests never touch the real user's CLI files

from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from qorqut_operations.contracts import DomainError

from qorqut.bootstrap.terminal_chat import process_fields
from qorqut.managed_runtime import launcher, mode as modes, state as layout
from qorqut.managed_runtime.client import HostClient, HostUnavailable
from qorqut.managed_runtime.manager import ManagedRuntime
from qorqut.native_chat import arbiter as arbiter_module, chat as chat_module, nudges, schema, store
from qorqut.native_chat.adapters import ManagedAdapter
from qorqut.native_chat.arbiter import AgentLocks, InputArbiter
from qorqut.native_chat.chat import NativeChat
from qorqut.native_chat.contracts import (InputRequest, RESULT_DEFERRED, STATE_SUBMITTED, STATE_WAITING, TYPE_NUDGE, TYPE_TELEGRAM)
from qorqut.native_chat.probe import SessionProbe
from qorqut.native_chat.service import NativeBackend
from qorqut.native_chat.sessions import SessionRegistry

from test_managed_host import wait_for

OWNER = "owner-1"
# The process checks read /proc on Linux and qorqut.process_info (psutil) on macOS; both are exercised.
LINUX = sys.platform.startswith(("linux", "darwin"))
WAIT = 20.0


class Database:
    """The native Chat tables (real schema) in one in-memory connection, serialized by a lock because the observer, the hooks and
    the test's own calls use it from different threads, as the service's database does with its own."""

    def __init__(self):
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        for statement in schema.SCHEMA:
            self.connection.execute(statement)
        self.connection.execute("CREATE TABLE native_session_owners(agent_id TEXT PRIMARY KEY, consumer_id TEXT NOT NULL, "
                                "binding_json TEXT NOT NULL, generation INTEGER NOT NULL, desired_state TEXT NOT NULL DEFAULT 'running')")

    @contextmanager
    def read(self):
        with self.lock:
            yield SimpleNamespace(connection=self.connection)

    @contextmanager
    def transaction(self, operation=None):
        with self.lock, self.connection:
            yield SimpleNamespace(connection=self.connection)


HOOK_DELAY = 0.4


class Hooks(threading.Thread):
    """STUB of the Claude hooks. Once the fake CLI has taken up a message (and, with ``block_role``, one that is not the ``/role``
    prompt, which a hook refuses: that writes nothing), after ``delay`` adopt the host's conversation into the owner row (the pid and
    start ticks of the host's claude process, and the host that made it) and only then publish the marker. Before that there is no
    marker, however long the host has been ready: the transcript does not exist either."""

    def __init__(self, stack, delay=HOOK_DELAY, block_role=False):
        super().__init__(name="stub-hooks", daemon=True)
        self.stack = stack
        self.delay, self.block_role = delay, block_role
        self.marker = None
        self.seen = None
        self.written = 0
        self.waiting = None
        self.stop = threading.Event()

    def taken_up(self):
        """The messages the current session's fake CLI took up that a hook did not refuse (``[]`` until its start is logged)."""
        records = self.stack.logged()
        starts = [index for index, record in enumerate(records) if record["kind"] == "start"]
        if len(starts) <= self.written:
            return []
        sends = [record for record in records[starts[self.written] + 1:] if record["kind"] == "send"]
        return [record for record in sends if not (self.block_role and record["text"] == "/role")]

    def run(self):
        while not self.stop.wait(0.05):
            try:
                inspected = launcher.inspect_host(self.stack.state)
            except Exception:  # noqa: BLE001 - a host being born or dying is not a session yet
                continue
            host = inspected.host if inspected.status == "running" else None
            if host is None or not host.session_id or not host.claude_pid:
                continue
            key = (host.claude_pid, host.session_id)
            if self.seen == key:
                continue
            if not self.taken_up():
                self.waiting = None
                continue
            if self.waiting is None or self.waiting[0] != key:
                self.waiting = (key, time.monotonic())
            if time.monotonic() - self.waiting[1] < self.delay:
                continue
            try:
                started = process_fields(host.claude_pid)[19]
            except (OSError, IndexError):
                continue
            transcript = host.transcript_path or str(self.stack.workspace / ".fake-transcripts" / (host.session_id + ".jsonl"))
            binding = {"runtime": "claude-code", "pid": host.claude_pid, "started": started, "owner": f"{host.claude_pid}:{started}",
                       "managed_host": {"host_id": host.instance, "host_pid": host.host_pid,
                                        "host_ticks": host.host_identity.rsplit(":", 1)[1]}}
            with self.stack.db.transaction() as uow:
                generation = (uow.connection.execute("SELECT generation FROM native_session_owners WHERE agent_id=?",
                                                     (self.stack.agent,)).fetchone() or {"generation": 0})["generation"]
                uow.connection.execute("INSERT OR REPLACE INTO native_session_owners VALUES(?,?,?,?, 'running')",
                                       (self.stack.agent, host.session_id, json.dumps(binding), generation + 1))
            self.marker = {"agent_id": self.stack.agent, "session_id": host.session_id, "runtime": "claude-code",
                           "pid": host.claude_pid, "started": started, "transcript_path": transcript,
                           "capture": True, "capture_enabled": True}
            self.seen = (host.claude_pid, host.session_id)
            self.written += 1


class Observer(threading.Thread):
    """The backend's loop for this one agent: what ``NativeBackend.run_once`` does per agent, without the agent discovery that needs
    the service's profile tables. Anything the pass raises is kept and asserted empty at the end."""

    def __init__(self, stack):
        super().__init__(name="stub-observer", daemon=True)
        self.stack = stack
        self.errors = []
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                self.stack.backend.observe(self.stack.profile())
            except Exception as error:  # noqa: BLE001
                self.errors.append(error)
            self.stop.wait(0.1)


class DriverLauncher:
    """The real launcher functions, with the fake driver named in ``launch.json`` as the one change to the spec."""

    def __init__(self, log):
        self.detached, self.log = launcher.DetachedLauncher(), log

    def inspect_host(self, state_dir):
        return launcher.inspect_host(state_dir)

    def running_hosts(self, root):
        return launcher.running_hosts(root)

    def start_host(self, state_dir, spec, timeout=45):
        spec = {**spec, "driver": "test_managed_host:FakeSession", "env": {**spec["env"], "FAKE_LOG": str(self.log)},
                "close_seconds": 5, "ready_seconds": 5}
        return launcher.start_host(state_dir, spec, launcher=self.detached, timeout=timeout)


class Stack:
    """Everything one test needs, assembled the way ``NativeChat.build`` assembles the service's graph."""

    def __init__(self, case, idle_seconds=0, *, resume=None, block_role=False, hook_delay=HOOK_DELAY):
        self.case = case
        self.root = Path(tempfile.mkdtemp(prefix="me2e-")).resolve()
        case.addCleanup(shutil.rmtree, self.root, True)
        self.agent = str(uuid.uuid4())
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.paths = SimpleNamespace(runtime=self.root / "runtime")
        self.state = modes.agent_state(self.paths, self.agent)
        self.log = self.root / "fake.log"
        self.db = Database()
        self.runtime = SimpleNamespace(
            database=self.db, paths=self.paths, authority=None, root=self.root, terminals=None,
            application=SimpleNamespace(ensure_writes_open=lambda uow: None, ordubashy_attention=None,
                                        writer_admission=SimpleNamespace(writer=lambda: mock.MagicMock())))
        self.launcher = DriverLauncher(self.log)
        self.managed = ManagedRuntime(
            self.runtime, launcher=self.launcher, prepare=lambda agent_id: {"workspace": str(self.workspace), "env": {}},
            executable=sys.executable, idle_seconds=idle_seconds, ready=lambda client: True,
            resume=(lambda agent_id: resume) if resume is not None else None)
        self.hooks = Hooks(self, delay=hook_delay, block_role=block_role)
        self.probe = SessionProbe(marker_reader=lambda profile: dict(self.hooks.marker) if self.hooks.marker else None,
                                  managed=self.managed)
        self.registry = SessionRegistry(self.runtime)
        locks = AgentLocks()
        adapters = {"managed": ManagedAdapter(self.managed), "terminal": SimpleNamespace(), "codex_queue": SimpleNamespace()}
        self.arbiter = InputArbiter(self.runtime, self.probe, self.registry, adapters, locks=locks)
        indexer = SimpleNamespace(step=lambda profile, session, final=False: SimpleNamespace(more=False, finalized=True, error=None))
        self.backend = NativeBackend(self.runtime, self.probe, self.registry, indexer, self.arbiter, locks, managed=self.managed)
        self.chat = NativeChat(self.runtime, probe=self.probe, registry=self.registry, indexer=indexer, arbiter=self.arbiter,
                               backend=self.backend, managed=self.managed)
        self.observer = Observer(self)
        modes.set_mode(self.paths, self.agent, "managed")
        for patcher in (
                mock.patch.object(InputArbiter, "_profile", lambda arbiter, agent_id: self.profile()),
                mock.patch.object(NativeChat, "_target", lambda chat, uow, principal_id, agent_id: self.target(principal_id, agent_id)),
                mock.patch.object(chat_module, "require_protected_control", lambda uow, principal, kind, agents: None),
                mock.patch.object(chat_module, "installation_owner", lambda uow: OWNER),
                mock.patch.object(arbiter_module, "forward_owner_message", mock.Mock()),
                mock.patch("qorqut.management.identity.configured_ordubashy", return_value=None),
                mock.patch("qorqut.agents.attention.enrolled_board", return_value=False),
                mock.patch("qorqut.management.identity.installation_owner", return_value=OWNER)):
            patcher.start()
            case.addCleanup(patcher.stop)
        case.addCleanup(self.shutdown)
        self.hooks.start()
        self.observer.start()

    def profile(self):
        return {"agent_id": self.agent, "name": "Worker", "workspace": str(self.workspace),
                "profile_json": json.dumps({"runtime": "claude-code", "name": "Worker"}),
                "execution": modes.mode_of(self.paths, self.agent)}

    def target(self, principal_id, agent_id):
        if principal_id != OWNER:
            raise DomainError("forbidden", "Agent chat is for the installation owner")
        if agent_id != self.agent:
            raise DomainError("not_found", "Select an active registered agent")
        return self.profile()

    def shutdown(self):
        for thread, flag in ((self.observer, self.observer.stop), (self.hooks, self.hooks.stop)):
            flag.set()
            if thread.is_alive():
                thread.join(5)
        try:
            HostClient(self.state).close("now")
        except (HostUnavailable, OSError):
            pass
        launcher.wait_until_gone(self.state, 15)  # the fake's claude is a child of the host: it is gone with it

    # ----- what a test looks at ----------------------------------------------------------------------------------------

    def logged(self, kind=None):
        if not self.log.exists():
            return []
        records = [json.loads(line) for line in self.log.read_text().splitlines() if line]
        return [record for record in records if kind is None or record["kind"] == kind]

    def live(self):
        with self.db.read() as uow:
            return store.live_session(uow.connection, self.agent)

    def sessions(self):
        with self.db.read() as uow:
            return [dict(row) for row in uow.connection.execute(
                "SELECT * FROM native_sessions WHERE agent_id=? ORDER BY generation", (self.agent,))]

    def row(self, input_id):
        with self.db.read() as uow:
            return store.get_input(uow.connection, input_id)

    def view(self):
        return self.chat.session_view(self.profile(), self.live())

    def managed_view(self):
        return self.view()["managed"]

    def cursor(self):
        with self.db.read() as uow:
            return store.counters(uow.connection, self.agent)["revision"]

    def host_running(self):
        return launcher.inspect_host(self.state).status == "running"


class ManagedEndToEnd(unittest.TestCase):
    def setUp(self):
        isolated_cli_environment.isolate(self)

    def stack(self, **options):
        return Stack(self, **options)

    def settle(self, stack, phase, timeout=WAIT):
        return wait_for(lambda: (lambda view: view if view["phase"] == phase else None)(stack.managed_view()), timeout,
                        message="the session to be " + phase)

    def say(self, stack, text, pin, key=None):
        result = stack.chat.send(OWNER, stack.agent, text, key or "key-" + uuid.uuid4().hex[:12], pin)
        return result

    def submitted(self, stack, result):
        return wait_for(lambda: (lambda row: row if row["state"] == STATE_SUBMITTED else None)(stack.row(result["input"]["id"])),
                        WAIT, message="the input to be submitted")

    # ===== the main scenario ========================================================================================

    @unittest.skipUnless(LINUX, "the host's process checks need Linux or macOS")
    def test_a_managed_agent_from_closed_to_closed(self):
        stack = self.stack()
        chat, agent = stack.chat, stack.agent

        # ----- 1. closed is the steady state: nothing runs, and the composer says a message starts a session -----------
        closed = stack.view()
        self.assertEqual((closed["execution"], closed["state"], closed["reason"], closed["can_send"], closed["binding"]),
                         ("managed", "offline", "session_closed", True, None))
        self.assertEqual((closed["managed"]["open"], closed["managed"]["phase"], closed["managed"]["can_stop"]), (False, None, False))
        self.assertEqual(closed["launch_commands"], [])
        self.assertFalse(stack.host_running())
        self.assertEqual(stack.logged(), [])

        # ----- 2. the owner's first message starts the host, binds the session as "managed" and is admitted for it -------
        first = self.say(stack, "hello there", None, key="key-first-0001")
        self.assertTrue(first["created"])
        self.assertEqual(first["binding"]["transport"], "managed")
        pin = first["binding"]["binding_id"]
        self.assertTrue(stack.host_running())
        live = stack.live()
        self.assertEqual((live["transport"], live["id"]), ("managed", pin))
        verified = launcher.inspect_host(stack.state).host
        self.assertEqual(live["owner"], f"host:{verified.host_pid}:{verified.host_identity.rsplit(':', 1)[1]}")
        self.assertEqual((live["process_pid"], live["conversation_id"]), (verified.claude_pid, verified.session_id))
        self.assertEqual(live["owner"], layout.host_owner(verified.host_pid, verified.host_identity))
        # the session was opened for this message: Qorqbot started it fresh, so the host sent /role first
        self.assertEqual([entry["kind"] for entry in stack.logged("start")], ["start"])
        self.assertIsNone(stack.logged("start")[0]["resume"])

        # ----- 3. the input is delivered by the managed adapter and reported submitted -----------------------------------
        delivered = self.submitted(stack, first)
        self.assertEqual(delivered["reason"] in ("processing_confirmed", "queued_mid_turn", "processing_not_confirmed"), True)
        self.assertEqual(json.loads(delivered["evidence_json"])["adapter"], "managed")
        texts = [entry["text"] for entry in stack.logged("send")]
        self.assertEqual(texts, ["/role", "hello there"])
        view = stack.view()
        self.assertEqual((view["state"], view["transport"], view["can_send"], view["binding"]["binding_id"]), ("live", "managed", True, pin))
        self.assertEqual(view["managed"]["open"], True)
        self.settle(stack, "idle")

        # the exact repeat (the first response may have been lost) is the same input: nothing is typed twice
        again = chat.send(OWNER, agent, "hello there", "key-first-0001")
        self.assertEqual((again["input"]["id"], again["created"], again["binding"]["binding_id"]), (first["input"]["id"], False, pin))
        self.assertEqual(len(stack.logged("send")), 2)
        # while a session is open the pin is required, and the same key with another text conflicts
        with self.assertRaises(DomainError) as refused:
            chat.send(OWNER, agent, "another", "key-another-001")
        self.assertEqual(refused.exception.code, "validation_error")
        with self.assertRaises(DomainError) as refused:
            chat.send(OWNER, agent, "changed text", "key-first-0001", pin)
        self.assertEqual(refused.exception.code, "idempotency_conflict")

        # ----- 4. a turn runs; a second message arrives mid-turn and is queued into it; the change reaches Chat ----------
        cursor = stack.cursor()
        slow = self.say(stack, "SLOW first part", pin)
        self.submitted(stack, slow)
        running = self.settle(stack, "working")
        self.assertEqual((running["can_stop"], running["open"]), (True, True))
        events, _ = chat.events(OWNER, agent, cursor)
        self.assertEqual(events[0]["kind"], "changed")
        wait_for(lambda: chat.events(OWNER, agent, cursor)[0][0]["data"]["managed"]["phase"] == "working",
                 message="the change event to carry the working phase")
        self.assertEqual(stack.view()["working"], True)
        second = self.say(stack, "second part DONE", pin)
        self.assertEqual(self.submitted(stack, second)["reason"], "queued_mid_turn",
                         "the host's own log of this scenario: %r; its state: %r" % (stack.logged()[-6:], stack.managed_view()))
        self.settle(stack, "idle")
        self.assertEqual(stack.managed_view()["last_turn_status"], "completed")
        self.assertEqual([entry["text"] for entry in stack.logged("send")][-2:], ["SLOW first part", "second part DONE"])

        # ----- 5. interrupt: stops the running turn once; idle it reports nothing was running ----------------------------
        stuck = self.say(stack, "SLOW stuck", pin)
        self.submitted(stack, stuck)
        self.settle(stack, "working")
        with self.assertRaises(DomainError) as stale:
            chat.interrupt(OWNER, agent, "not-the-live-binding")
        self.assertEqual(stale.exception.code, "terminal_changed")
        self.assertEqual(chat.interrupt(OWNER, agent, pin), {"stopped": True})
        self.settle(stack, "idle")
        self.assertEqual(stack.managed_view()["last_turn_status"], "interrupted")
        self.assertEqual(chat.interrupt(OWNER, agent, pin), {"stopped": False})
        self.assertEqual(len(stack.logged("interrupt")), 1)
        with self.assertRaises(DomainError) as nobody:
            chat.interrupt("someone-else", agent, pin)
        self.assertEqual(nobody.exception.code, "forbidden")

        # ----- 6. a permission request is answered by the host itself and never shown; a question is answered with its own text
        ask = self.say(stack, "PERM write the file", pin)
        self.submitted(stack, ask)
        wait_for(lambda: stack.logged("answer"), WAIT, message="the host's own answer to the permission request")
        self.settle(stack, "idle")
        self.assertEqual(stack.logged("answer")[-1]["decision"]["behavior"], "deny", "the prepared settings do not say bypass")
        self.assertEqual(stack.managed_view()["pending"], [])

        question = self.say(stack, "ASK which one", pin)
        self.submitted(stack, question)
        asked = self.settle(stack, "blocked")["pending"][0]
        self.assertEqual((asked["kind"], asked["questions"][0]["question"]), ("question", "Which?"))
        with self.assertRaises(DomainError) as wrong_kind:
            chat.answer(OWNER, agent, pin, asked["request_id"], "allow")
        self.assertEqual(wrong_kind.exception.code, "validation_error")
        self.assertEqual(chat.answer(OWNER, agent, pin, asked["request_id"], {"answers": {"Which?": "A"}}), {"answered": True})
        self.settle(stack, "idle")
        self.assertEqual(stack.logged("answer")[-1]["decision"], {"answers": {"Which?": "A"}})
        self.assertEqual(len(stack.logged("answer")), 2, "one answer from the host, one from the owner")

        # ----- 7. a prompt a hook blocked: the input stays submitted and says it was not processed --------------------
        blocked_input = self.say(stack, "BLOCK this one", pin)
        row = wait_for(lambda: (lambda current: current if current["reason"] == "prompt_blocked" else None)(
            stack.row(blocked_input["input"]["id"])), WAIT, message="the blocked prompt to be recorded on its input")
        self.assertEqual(row["state"], STATE_SUBMITTED)
        self.assertEqual(json.loads(row["evidence_json"])["prompt_blocked"], {"reason": "a hook refused it"})
        self.settle(stack, "idle")
        self.assertEqual(stack.observer.errors, [])

        # ----- 8. the owner ends the session with /exit: the host closes, the binding ends -----------------------------
        before = stack.cursor()
        farewell = self.say(stack, "/exit", pin)
        self.assertEqual(self.submitted(stack, farewell)["reason"], "session_closing")
        self.assertTrue(launcher.wait_until_gone(stack.state, WAIT))
        wait_for(lambda: stack.live() is None, WAIT, message="the binding to end")
        ended = stack.sessions()[-1]
        self.assertEqual((ended["id"], ended["end_reason"]), (pin, "process_gone"))
        self.assertGreater(stack.cursor(), before)
        # the host summary is cached for up to 5 seconds (ManagedRuntime.snapshot): wait for the closed view to show
        wait_for(lambda: stack.view()["reason"] == "session_closed", 12, message="the view to show the closed session")
        shut = stack.view()
        self.assertEqual((shut["state"], shut["reason"], shut["can_send"], shut["binding"], shut["managed"]["open"]),
                         ("offline", "session_closed", True, None, False))
        self.assertEqual(stack.logged("close")[-1]["kind"], "close")

        # ----- 9. a nudge-style input finds the session closed and is not a failure; the arbiter does not wake for it ----
        nudge = InputRequest(agent, TYPE_NUDGE, "nudge-after-close-1", "You have a task waiting", wait=False)
        result = stack.arbiter.submit(nudge)
        self.assertEqual((result.state, result.reason, result.input_id), (RESULT_DEFERRED, "session_closed", None))
        self.assertEqual(nudges.outcome_of(result, store.binding_of(ended)), "session_closed")
        self.assertFalse(stack.host_running())
        self.assertEqual(stack.logged("start").__len__(), 1)

        # ----- 10. Telegram owner input wakes the asleep agent: the attempt starts the open and is told so, the retry delivers --
        telegram_request = InputRequest(agent, TYPE_TELEGRAM, "tg-after-close-01", "hi", wait=False)
        telegram = stack.arbiter.submit(telegram_request)
        self.assertEqual((telegram.state, telegram.reason, telegram.input_id), (RESULT_DEFERRED, "session_starting", None))
        wait_for(lambda: stack.live() is not None, WAIT, message="the wake to bind a session")
        self.assertEqual(stack.logged("start").__len__(), 2)

        def retried():
            again = stack.arbiter.submit(telegram_request)
            return again if again.state == STATE_SUBMITTED else None
        delivered = wait_for(retried, WAIT, message="the retry to deliver the Telegram input")
        self.assertEqual([entry["text"] for entry in stack.logged("send")].count("hi"), 1, "typed once, by its own claim")
        self.assertEqual(delivered.input_id, stack.arbiter.submit(telegram_request).input_id)
        self.settle(stack, "idle")

        # ----- 11. End session closes it and turns the agent off; a session that is not open is not an error ----------------
        self.assertEqual(chat.close(OWNER, agent), {"closing": True})
        self.assertTrue(launcher.wait_until_gone(stack.state, WAIT))
        wait_for(lambda: stack.live() is None, WAIT, message="the binding to end")
        self.assertEqual(chat.close(OWNER, agent), {"closing": False})
        self.assertEqual(stack.observer.errors, [])

    # ===== what the real CLI does about announcing a session =======================================================

    @unittest.skipUnless(LINUX, "the host's process checks need Linux or macOS")
    def test_a_fresh_session_binds_only_once_claude_has_taken_up_the_role_prompt(self):
        """The host is ready at once but nothing is announced (no marker, no owner row) until Claude took up a first message: the
        open waits for the binding the role prompt makes, it does not need the transcript file and does not send the owner's
        message ahead of it."""
        stack = self.stack(hook_delay=1.0)
        started = time.monotonic()
        first = self.say(stack, "hello there", None, key="key-fresh-00001")
        self.assertGreaterEqual(time.monotonic() - started, 1.0)
        self.assertEqual(first["binding"]["transport"], "managed")
        delivered = self.submitted(stack, first)
        self.assertNotEqual(delivered["reason"], "accepted_by_host", "the owner's message went through the input, not ahead of it")
        self.assertEqual([entry["text"] for entry in stack.logged("send")], ["/role", "hello there"])
        self.assertEqual(stack.observer.errors, [])

    @unittest.skipUnless(LINUX, "the host's process checks need Linux or macOS")
    def test_a_resumed_session_is_announced_by_the_owners_own_message_which_is_typed_once(self):
        """A resumed conversation is sent no ``/role`` and nothing announces it until a message arrives: so the owner's message is
        delivered to the host before there is a binding, and the input admitted for the binding it makes is recognised by the host
        as the same message."""
        resumed = str(uuid.uuid4())
        stack = self.stack(resume=resumed)
        first = self.say(stack, "carry on please", None, key="key-resume-0001")
        self.assertEqual(stack.logged("start")[0]["resume"], resumed)
        live = stack.live()
        self.assertEqual((live["transport"], live["conversation_id"], live["id"]), ("managed", resumed, first["binding"]["binding_id"]))
        delivered = self.submitted(stack, first)
        self.assertEqual(delivered["reason"], "accepted_by_host")
        sent = stack.logged("send")
        self.assertEqual([entry["text"] for entry in sent], ["carry on please"], "no role prompt, and the message is typed once")
        self.assertEqual(sent[0]["uuid"], ManagedRuntime.first_message_uuid(stack.agent, "key-resume-0001", "carry on please"))
        # the retry of the same request neither opens a second host nor types the message again
        again = stack.chat.send(OWNER, stack.agent, "carry on please", "key-resume-0001")
        self.assertEqual((again["created"], again["input"]["id"]), (False, first["input"]["id"]))
        self.assertEqual((len(stack.logged("start")), len(stack.logged("send"))), (1, 1))
        self.assertEqual(stack.observer.errors, [])

    @unittest.skipUnless(LINUX, "the host's process checks need Linux or macOS")
    def test_a_role_prompt_that_a_hook_blocked_does_not_leave_the_open_waiting_for_a_session_that_never_announces(self):
        """A hook-blocked prompt writes no transcript record and no marker. The role prompt is blocked here, so after the grace the
        owner's message is delivered; Claude takes it up and announces the session."""
        stack = self.stack(block_role=True)
        with mock.patch.object(chat_module, "ROLE_GRACE_SECONDS", 0.8):
            started = time.monotonic()
            first = self.say(stack, "hello after a blocked role", None, key="key-blocked-0001")
        self.assertGreaterEqual(time.monotonic() - started, 0.8)
        delivered = self.submitted(stack, first)
        self.assertEqual(delivered["reason"], "accepted_by_host")
        self.assertEqual([entry["text"] for entry in stack.logged("send")], ["/role", "hello after a blocked role"])
        self.assertEqual(stack.live()["id"], first["binding"]["binding_id"])
        self.assertEqual(stack.observer.errors, [])

    @unittest.skipUnless(LINUX, "the host's process checks need Linux or macOS")
    def test_an_open_that_gave_up_is_finished_by_the_retry_with_the_same_key_on_the_same_host(self):
        """The open ran out of time while the host was still coming up (the message may or may not have reached it yet). The host
        keeps starting; the retry (same key, no pin) finds that host, starts nothing, and the message is typed once."""
        stack = self.stack(resume=str(uuid.uuid4()), hook_delay=1.0)
        with mock.patch.object(chat_module, "OPEN_TOTAL_SECONDS", 0.3):
            with self.assertRaises(DomainError) as gave_up:
                stack.chat.send(OWNER, stack.agent, "slow to announce", "key-retry-00001")
        self.assertEqual((gave_up.exception.code, gave_up.exception.retryable), ("open_timeout", True))
        with stack.db.read() as uow:
            self.assertEqual(uow.connection.execute("SELECT COUNT(*) FROM native_inputs").fetchone()[0], 0)
        wait_for(stack.host_running, WAIT, message="the host that was still starting to come up")
        retried = stack.chat.send(OWNER, stack.agent, "slow to announce", "key-retry-00001")
        self.assertTrue(retried["created"])
        self.assertEqual(self.submitted(stack, retried)["reason"], "accepted_by_host")
        self.assertEqual((len(stack.logged("start")), [entry["text"] for entry in stack.logged("send")]), (1, ["slow to announce"]))
        again = stack.chat.send(OWNER, stack.agent, "slow to announce", "key-retry-00001")
        self.assertEqual((again["created"], again["input"]["id"]), (False, retried["input"]["id"]))
        self.assertEqual(stack.observer.errors, [])

    # ===== close, reopen, the old pin ===============================================================================

    @unittest.skipUnless(LINUX, "the host's process checks need Linux or macOS")
    def test_the_close_route_ends_the_session_and_the_next_message_resumes_the_same_conversation(self):
        stack = self.stack()
        chat, agent = stack.chat, stack.agent
        first = self.say(stack, "first session", None, key="key-session-one")
        old_pin = first["binding"]["binding_id"]
        self.submitted(stack, first)
        first_host = launcher.inspect_host(stack.state).host
        with self.assertRaises(DomainError) as stale:
            chat.close(OWNER, agent, "not-the-live-binding")
        self.assertEqual(stale.exception.code, "terminal_changed")
        self.assertEqual(chat.close(OWNER, agent, old_pin), {"closing": True})
        self.assertTrue(launcher.wait_until_gone(stack.state, WAIT))
        wait_for(lambda: stack.live() is None, WAIT, message="the binding to end")
        self.assertFalse(layout.process_alive(first_host.claude_pid, first_host.claude_identity))
        # the earlier input keeps its own binding after its session ended; the repeat of it is still the same input
        repeat = chat.send(OWNER, agent, "first session", "key-session-one")
        self.assertEqual((repeat["created"], repeat["binding"]["binding_id"]), (False, old_pin))
        self.assertFalse(stack.host_running())

        second = self.say(stack, "second session", None, key="key-session-two")
        new_pin = second["binding"]["binding_id"]
        self.assertNotEqual(new_pin, old_pin)
        self.submitted(stack, second)
        sessions = stack.sessions()
        self.assertEqual([(row["generation"], row["transport"]) for row in sessions], [(1, "managed"), (2, "managed")])
        # the conversation is the same one: its transcript exists, so the next message resumes it (and /role is not sent again)
        self.assertEqual(sessions[0]["conversation_id"], sessions[1]["conversation_id"])
        self.assertNotEqual(launcher.inspect_host(stack.state).host.host_pid, first_host.host_pid)
        self.assertEqual([entry["resume"] for entry in stack.logged("start")], [None, sessions[0]["conversation_id"]])
        self.assertEqual([entry["text"] for entry in stack.logged("send")], ["/role", "first session", "second session"])
        # a message pinned to the session that ended is refused: it is never typed into the new one
        with self.assertRaises(DomainError) as old:
            chat.send(OWNER, agent, "to the old one", "key-to-old-0001", old_pin)
        self.assertEqual(old.exception.code, "terminal_changed")
        self.assertNotIn("to the old one", [entry["text"] for entry in stack.logged("send")])
        self.assertEqual(stack.observer.errors, [])

    # ===== the host closes itself when idle =========================================================================

    @unittest.skipUnless(LINUX, "the host's process checks need Linux or macOS")
    def test_an_idle_host_closes_itself_and_the_binding_ends_with_it(self):
        stack = self.stack(idle_seconds=4)
        agent = stack.agent
        first = self.say(stack, "one short turn", None, key="key-idle-0001")
        self.submitted(stack, first)
        self.assertTrue(stack.host_running())
        self.assertTrue(launcher.wait_until_gone(stack.state, WAIT))
        self.assertEqual(layout.read_host(stack.state)["exit"]["reason"], "idle")
        wait_for(lambda: stack.live() is None, WAIT, message="the binding to end")
        self.assertEqual(stack.sessions()[-1]["end_reason"], "process_gone")
        # The host's summary is cached for up to 5 seconds (ManagedRuntime.snapshot), so for that long after the host left Chat
        # still says "starting the session" (binding_not_ready, cannot send). It settles to session_closed within the cache's age.
        wait_for(lambda: stack.view()["reason"] == "session_closed", 12, message="the view to show the closed session")
        view = stack.view()
        self.assertEqual((view["reason"], view["can_send"], view["managed"]["open"]), ("session_closed", True, False))
        result = stack.arbiter.submit(InputRequest(agent, TYPE_NUDGE, "nudge-after-idle-1", "inbox", wait=False))
        self.assertEqual((result.state, result.reason), (RESULT_DEFERRED, "session_closed"))
        self.assertEqual(stack.observer.errors, [])

    # ===== refusals before anything starts ==============================================================================

    @unittest.skipUnless(LINUX, "the host's process checks need Linux or macOS")
    def test_a_menu_command_a_bad_key_and_a_closing_exit_start_nothing(self):
        stack = self.stack()
        chat, agent = stack.chat, stack.agent
        for text, key, code in (("/clear", "key-menu-00001", "managed_command"), ("/model", "key-menu-00002", "managed_command"),
                                ("/exit", "key-exit-00001", "session_closed"), ("hello", "short", "validation_error"),
                                ("   ", "key-blank-0001", "validation_error")):
            with self.subTest(text=text):
                with self.assertRaises(DomainError) as refused:
                    chat.send(OWNER, agent, text, key)
                self.assertEqual(refused.exception.code, code)
        self.assertFalse(stack.host_running())
        self.assertEqual(stack.logged(), [])
        with stack.db.read() as uow:
            self.assertEqual(uow.connection.execute("SELECT COUNT(*) FROM native_inputs").fetchone()[0], 0)
        # a terminal agent is untouched by all of this: it still needs its pin and starts nothing
        modes.set_mode(stack.paths, agent, "terminal")
        with self.assertRaises(DomainError) as terminal:
            chat.send(OWNER, agent, "hello", "key-terminal-001")
        self.assertEqual(terminal.exception.code, "validation_error")
        self.assertFalse(stack.host_running())
        self.assertEqual(stack.observer.errors, [])

    @unittest.skipUnless(LINUX, "the host's process checks need Linux or macOS")
    def test_a_start_that_fails_is_refused_with_its_code_and_stores_nothing(self):
        stack = self.stack()
        with mock.patch.object(DriverLauncher, "start_host", side_effect=launcher.LaunchError("not_ready", "slow")):
            with self.assertRaises(DomainError) as refused:
                stack.chat.send(OWNER, stack.agent, "hello", "key-fail-000001")
        self.assertEqual((refused.exception.code, refused.exception.retryable), ("open_timeout", True))
        self.assertEqual(stack.live(), None)
        with stack.db.read() as uow:
            self.assertEqual(uow.connection.execute("SELECT COUNT(*) FROM native_inputs").fetchone()[0], 0)
        self.assertFalse(stack.host_running())


if __name__ == "__main__":
    unittest.main()
