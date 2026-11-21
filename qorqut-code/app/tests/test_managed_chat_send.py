"""Native Chat's managed integration, at the facade: the ``send`` open flow, the managed ``session_view`` and change event, the
control operations (interrupt, answer, close), the runtime switch, the upload wording, the managed slash catalog, the attachment
view, the nudge outcome for a closed session and the recovery-window inventory of managed hosts.

Everything runs against a fake managed runtime (a duck-typed object, as ``test_managed_session_binding`` does) and a real in-memory
database with the native Chat tables; the real probe, registry and arbiter bind and admit. The real host process is
``test_managed_end_to_end``. Nothing starts a CLI.
"""
import isolated_cli_environment  # before any Qorqbot import: tests never touch the real user's CLI files

from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError
from qorqut_operations.inbox_nudges.contracts import Binding as NudgeBinding, TRANSPORTS as NUDGE_TRANSPORTS

from qorqut.bootstrap.terminal_chat import process_fields
from qorqut.composition import compactions
from qorqut.domains.compaction_handoff import CompactionHandoff
from qorqut.managed_runtime.manager import CLOSED_VIEW, ManagedError, ManagedRuntime
from qorqut.native_archive import CaptureError
from qorqut.native_chat import arbiter as arbiter_module, chat as chat_module, commands, nudges, schema, store
from qorqut.native_chat.arbiter import AgentLocks, InputArbiter
from qorqut.native_chat.chat import MANAGED_FIELDS, NativeChat
from qorqut.native_chat.contracts import InputResult, MANAGED_MENU_COMMANDS, RESULT_DEFERRED, STATE_WAITING, TRANSPORTS
from qorqut.native_chat.probe import SessionProbe
from qorqut.native_chat.service import NativeBackend
from qorqut.native_chat.sessions import SessionRegistry
from qorqut.storage.host_writers import ManagedHostWriters

AGENT, OWNER = "agent-1", "owner-1"
CONVERSATION = "11111111-1111-4111-8111-111111111111"
WORKER = {"runtime": "claude-code", "role": {"id": "global-worker"}}
OPEN_VIEW = {"open": True, "state": "ready", "phase": "idle", "activity": None, "turn_started_at": None, "can_stop": False,
             "pending": [], "last_turn_status": None, "session_id": CONVERSATION}


def ticks(pid):
    return process_fields(pid)[19]


class Database:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        for statement in schema.SCHEMA:
            self.connection.execute(statement)
        # What the opener reads to tell whether the product offers an agent (``SessionOpener.offered``).
        self.connection.execute("CREATE TABLE IF NOT EXISTS agent_profiles(agent_id TEXT PRIMARY KEY, profile_json TEXT NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS installation(id TEXT PRIMARY KEY, owner_principal_id TEXT NOT NULL)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS inbox_events(id INTEGER PRIMARY KEY, business_id TEXT, source_type TEXT, "
                                "source_id TEXT, actor_id TEXT, event_type TEXT, summary TEXT, created_at TEXT)")
        self.connection.execute("CREATE TABLE IF NOT EXISTS inbox_deliveries(agent_id TEXT, event_id INTEGER, acknowledged_at TEXT)")
        self.connection.execute("CREATE TABLE native_session_owners(agent_id TEXT PRIMARY KEY, consumer_id TEXT NOT NULL, "
                                "binding_json TEXT NOT NULL, generation INTEGER NOT NULL, desired_state TEXT NOT NULL DEFAULT 'running')")

    @contextmanager
    def read(self):
        yield SimpleNamespace(connection=self.connection)

    @contextmanager
    def transaction(self, operation=None):
        with self.connection:
            yield SimpleNamespace(connection=self.connection)

    def count(self, table):
        return self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def adopt(self, pid, started):
        binding = {"runtime": "claude-code", "pid": pid, "started": started, "owner": f"{pid}:{started}",
                   "managed_host": {"host_id": "h", "host_pid": 1, "host_ticks": "1"}}
        self.connection.execute("INSERT OR REPLACE INTO native_session_owners VALUES(?,?,?,?, 'running')",
                                (AGENT, CONVERSATION, json.dumps(binding), 1))
        self.connection.commit()


class FakeManaged:
    """The managed runtime as the facade, probe, backend and adapter use it."""

    def __init__(self, case):
        self.case = case
        self.view = dict(CLOSED_VIEW)
        self.running = False
        self.opened, self.on_open, self.open_error = [], None, None
        self.open_options, self.role_pending = [], False
        self.first, self.on_first, self.first_error = [], None, None
        self.holds, self.hold_error, self.live = [], None, None
        self.host, self.host_error = None, None
        self.reply, self.error = {"status": "ok"}, None
        self.calls = []
        self.owner = f"host:{os.getpid()}:{ticks(os.getpid())}"

    def verify(self, profile, marker):
        return {"transport": "managed", "owner": self.owner} if self.running else None

    def identity(self, agent_id):
        """The verified session's identity (what the probe holds the marker's process against); None while nothing runs."""
        if not self.running:
            return None
        client = self.case.client
        return {"agent_id": agent_id, "provider": "claude", "conversation_id": CONVERSATION, "pid": client.pid, "started": client.started,
                "owner": self.owner, "host_pid": os.getpid(), "host_started": ticks(os.getpid()), "instance": "instance-1",
                "state": "ready", "transcript_path": f"/nonexistent/{CONVERSATION}.jsonl"}

    def snapshot(self, agent_id):
        if isinstance(self.view, Exception):
            raise self.view
        return dict(self.view)

    def refresh(self, agent_id):
        return False

    def drain_events(self, agent_id):
        return []

    def ensure_open(self, profile, timeout=45, *, new_conversation=False):
        self.opened.append(profile["agent_id"])
        self.open_options.append({"timeout": timeout, "new_conversation": new_conversation})
        if self.open_error is not None:
            raise self.open_error
        if self.on_open is not None:
            self.on_open()
        return {"agent_id": profile["agent_id"], "state": "ready", "role_pending": self.role_pending}

    first_message_uuid = staticmethod(ManagedRuntime.first_message_uuid)

    def send_first(self, agent_id, text, message_uuid, deadline):
        self.first.append((agent_id, text, message_uuid))
        if self.first_error is not None:
            raise self.first_error
        if self.on_first is not None:
            self.on_first()

    def status(self, agent_id):
        """The session read from the host right now: ``live`` when a test makes it differ from the cached ``view``."""
        view = self.live if self.live is not None else self.view
        if isinstance(view, Exception):
            raise view
        return dict(view)

    @contextmanager
    def hold(self, agent_id, timeout=5.0):
        self.holds.append(agent_id)
        if self.hold_error is not None:
            raise self.hold_error
        yield

    def handle(self, agent_id):
        if self.host_error is not None:
            raise self.host_error
        return self.host

    def interrupt(self, agent_id):
        self.calls.append(("interrupt", agent_id))
        if self.error is not None:
            raise self.error
        return self.reply

    def answer(self, agent_id, request_id, decision):
        self.calls.append(("answer", agent_id, request_id, decision))
        if self.error is not None:
            raise self.error
        return self.reply

    def close(self, agent_id, mode="graceful"):
        self.calls.append(("close", agent_id, mode))
        if self.error is not None:
            raise self.error
        return self.reply


class Untouchable:
    """A managed runtime a terminal agent must never reach."""

    def __getattr__(self, name):
        raise AssertionError(f"a terminal agent touched the managed runtime ({name})")


class Sleeper:
    def __init__(self, case):
        self.process = subprocess.Popen(["sleep", "60"])
        case.addCleanup(self.kill)
        self.pid, self.started = self.process.pid, ticks(self.process.pid)

    def kill(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()


class ChatCase(unittest.TestCase):
    """A ``NativeChat`` over the real probe, registry and arbiter, with a fake managed runtime."""

    runtime_extra = WORKER

    def setUp(self):
        isolated_cli_environment.isolate(self)
        self.root = Path(tempfile.mkdtemp(prefix="mcs-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.client = Sleeper(self)
        self.db = Database()
        self.paths = SimpleNamespace(runtime=self.root / "runtime")
        self.runtime = SimpleNamespace(
            database=self.db, paths=self.paths, authority=None, root=self.root,
            application=SimpleNamespace(ensure_writes_open=lambda uow: None, writer_admission=SimpleNamespace(writer=nullcontext)))
        self.managed = FakeManaged(self)
        self.marker = None
        self.probe = SessionProbe(marker_reader=lambda profile: dict(self.marker) if self.marker else None, managed=self.managed)
        self.registry = SessionRegistry(self.runtime)
        self.adapter = SimpleNamespace()
        self.locks = AgentLocks()
        self.arbiter = InputArbiter(self.runtime, self.probe, self.registry, self.adapter, locks=self.locks)
        indexer = SimpleNamespace(step=mock.Mock())
        self.backend = NativeBackend(self.runtime, self.probe, self.registry, indexer, self.arbiter, self.locks, managed=self.managed)
        self.chat = NativeChat(self.runtime, probe=self.probe, registry=self.registry, indexer=indexer, arbiter=self.arbiter,
                               backend=self.backend, managed=self.managed)
        self.chat._target = self.target
        for patcher in (mock.patch.object(chat_module, "require_protected_control", lambda uow, principal, kind, agents: None),
                        mock.patch.object(chat_module, "installation_owner", lambda uow: OWNER),
                        mock.patch.object(arbiter_module, "forward_owner_message", mock.Mock()),
                        mock.patch("qorqut.management.identity.configured_ordubashy", return_value=None),
                        mock.patch("qorqut.agents.attention.enrolled_board", return_value=False),
                        mock.patch("qorqut.management.identity.installation_owner", return_value=OWNER),
                        mock.patch.object(chat_module, "OPEN_POLL_SECONDS", 0.01)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.patch_profile()

    def patch_profile(self):
        patcher = mock.patch.object(InputArbiter, "_profile", lambda arbiter, agent_id: self.profile())
        patcher.start()
        self.addCleanup(patcher.stop)

    def profile(self, runtime=None):
        return {"agent_id": AGENT, "name": "Worker", "workspace": str(self.root / "workspace"),
                "profile_json": json.dumps(runtime if runtime is not None else self.runtime_extra)}

    def target(self, uow, principal_id, agent_id):
        if principal_id != OWNER:
            raise DomainError("forbidden", "Agent chat is for the installation owner")
        return self.profile()

    # ----- a host that "starts" when asked ----------------------------------------------------------------------------

    def bring_up(self):
        """The session comes up: the owner row is adopted for the client process, the marker appears and the managed runtime
        verifies the host (what the real hooks and host do once Claude has taken up a first message)."""
        self.db.adopt(self.client.pid, self.client.started)
        self.marker = {"agent_id": AGENT, "session_id": CONVERSATION, "runtime": "claude-code", "pid": self.client.pid,
                       "started": self.client.started, "transcript_path": f"/nonexistent/{CONVERSATION}.jsonl",
                       "capture": True, "capture_enabled": True}
        self.managed.running = True
        self.managed.view = dict(OPEN_VIEW)

    def host_starts(self):
        """``ensure_open`` brings up a session."""
        self.managed.on_open = self.bring_up

    def open(self):
        """Open the session through ``send`` and return its pin."""
        self.host_starts()
        first = self.chat.send(OWNER, AGENT, "open it", "key-open-0001")
        self.managed.opened.clear()
        return first["binding"]["binding_id"]

    def inputs(self):
        return self.db.count("native_inputs")


# ===== the open flow =============================================================================================================

class SendOpenTests(ChatCase):
    def test_a_message_to_a_closed_managed_agent_opens_a_session_then_admits_for_its_binding(self):
        self.host_starts()
        result = self.chat.send(OWNER, AGENT, "hello", "key-0000001")
        self.assertEqual(self.managed.opened, [AGENT])
        self.assertEqual((result["created"], result["binding"]["transport"], result["input"]["state"]), (True, "managed", STATE_WAITING))
        live = store.live_session(self.db.connection, AGENT)
        self.assertEqual((live["id"], live["transport"], live["conversation_id"]), (result["binding"]["binding_id"], "managed", CONVERSATION))
        self.assertEqual((result["input"]["session_id"], self.inputs()), (live["id"], 1))

    def test_the_open_waits_for_the_transcript_the_message_is_admitted_against(self):
        self.host_starts()
        inner = self.managed.on_open
        reads = []

        def late_transcript():
            inner()
            self.marker = {**self.marker, "transcript_path": None, "capture": False}  # capture is on but not published yet
            original = self.probe._marker_reader

            def marker_reader(profile):
                reads.append(1)
                if len(reads) > 3:
                    self.marker = {**self.marker, "transcript_path": f"/nonexistent/{CONVERSATION}.jsonl", "capture": True}
                return original(profile)
            self.probe._marker_reader = marker_reader
        self.managed.on_open = late_transcript
        result = self.chat.send(OWNER, AGENT, "hello", "key-0000002")
        self.assertGreater(len(reads), 3)
        self.assertEqual(result["input"]["state"], STATE_WAITING)

    def test_a_session_that_never_binds_is_open_timeout_and_admits_nothing(self):
        self.managed.on_open = lambda: None  # the host "starts" but its hooks never publish a session
        with mock.patch.object(chat_module, "OPEN_TOTAL_SECONDS", 0.2):
            with self.assertRaises(DomainError) as refused:
                self.chat.send(OWNER, AGENT, "hello", "key-0000003")
        self.assertEqual((refused.exception.code, refused.exception.retryable), ("open_timeout", True))
        self.assertEqual((self.inputs(), store.live_session(self.db.connection, AGENT)), (0, None))

    def test_the_agent_lock_is_not_held_while_the_host_starts(self):
        """The backend observer needs the agent's lock to adopt the new conversation; holding it across the open would deadlock."""
        self.host_starts()
        inner, taken = self.managed.on_open, []

        def start_and_observe():
            def other_thread():
                with self.locks(AGENT):
                    taken.append(True)
            thread = threading.Thread(target=other_thread)
            thread.start()
            thread.join(3)
            taken.append(not thread.is_alive())
            inner()
        self.managed.on_open = start_and_observe
        self.chat.send(OWNER, AGENT, "hello", "key-0000004")
        self.assertEqual(taken, [True, True])

    def test_an_exact_repeat_without_a_pin_is_the_first_input_and_opens_nothing(self):
        self.host_starts()
        first = self.chat.send(OWNER, AGENT, "hello", "key-0000005")
        self.managed.opened.clear()
        again = self.chat.send(OWNER, AGENT, "hello", "key-0000005")
        self.assertEqual((again["input"]["id"], again["created"], again["binding"]["binding_id"]),
                         (first["input"]["id"], False, first["binding"]["binding_id"]))
        self.assertEqual((self.managed.opened, self.inputs()), ([], 1))
        with self.assertRaises(DomainError) as conflict:
            self.chat.send(OWNER, AGENT, "other text", "key-0000005")
        self.assertEqual(conflict.exception.code, "idempotency_conflict")

    def test_a_live_session_needs_its_pin_and_a_stale_pin_is_terminal_changed(self):
        pin = self.open()
        with self.assertRaises(DomainError) as unpinned:
            self.chat.send(OWNER, AGENT, "second", "key-0000006")
        self.assertEqual(unpinned.exception.code, "validation_error")
        with self.assertRaises(DomainError) as stale:
            self.chat.send(OWNER, AGENT, "second", "key-0000007", "00000000-not-the-live-one")
        self.assertEqual(stale.exception.code, "terminal_changed")
        ok = self.chat.send(OWNER, AGENT, "second", "key-0000008", pin)
        self.assertEqual((ok["created"], ok["binding"]["binding_id"], self.managed.opened), (True, pin, []))
        self.assertEqual(self.inputs(), 2)

    def test_what_cannot_be_admitted_is_refused_before_anything_starts(self):
        self.host_starts()
        cases = [("/clear", "key-0000010", "managed_command"), ("/model", "key-0000011", "managed_command"),
                 ("/resume", "key-0000012", "managed_command"), ("/exit", "key-0000013", "session_closed"),
                 ("", "key-0000014", "validation_error"), ("hello", "short", "validation_error"),
                 ("x" * 70000, "key-0000015", "validation_error")]
        for text, key, code in cases:
            with self.subTest(text=text[:12], key=key):
                with self.assertRaises(DomainError) as refused:
                    self.chat.send(OWNER, AGENT, text, key)
                self.assertEqual(refused.exception.code, code)
        self.assertEqual((self.managed.opened, self.inputs()), ([], 0))
        # a command that runs headless is not a menu command: it opens and is admitted
        self.chat.send(OWNER, AGENT, "/compact", "key-0000016")
        self.assertEqual(self.managed.opened, [AGENT])

    def test_the_menu_command_message_names_the_managed_agent_plainly(self):
        with self.assertRaises(DomainError) as refused:
            self.chat.send(OWNER, AGENT, "/clear", "key-0000017")
        self.assertIn("isn't available when Qorqbot runs the agent", refused.exception.message)
        self.assertNotIn("Terminal", refused.exception.message)

    def test_a_menu_command_to_an_open_session_is_refused_by_the_arbiter_with_the_same_code(self):
        pin = self.open()
        with self.assertRaises(DomainError) as refused:
            self.chat.send(OWNER, AGENT, "/cost", "key-0000018", pin)
        self.assertEqual(refused.exception.code, "managed_command")
        self.assertEqual(self.inputs(), 1)  # only the message that opened the session

    def test_every_open_failure_is_a_refusal_with_a_stable_code_and_stores_nothing(self):
        cases = [("resume_failed", "resume_failed", False), ("open_busy", "open_busy", True),
                 ("session_opening", "session_opening", True),
                 ("open_timeout", "open_timeout", True), ("host_unverified", "host_unverified", True),
                 ("session_closed", "session_closed", False),
                 ("session_starting", "binding_not_ready", True), ("host_failed", "driver_unavailable", False),
                 ("host_start_failed", "driver_unavailable", False), ("managed_unavailable", "driver_unavailable", False)]
        for raised, shown, retryable in cases:
            with self.subTest(raised=raised):
                self.managed.open_error = ManagedError(raised, "internal detail with /private/path", retryable=retryable)
                with self.assertRaises(DomainError) as refused:
                    self.chat.send(OWNER, AGENT, "hello", "key-0000019")
                self.assertEqual((refused.exception.code, bool(refused.exception.retryable)), (shown, retryable))
                self.assertNotIn("/private/path", refused.exception.message)
        self.assertEqual((self.inputs(), store.live_session(self.db.connection, AGENT)), (0, None))

    def test_an_unexpected_open_failure_is_not_swallowed_as_a_refusal(self):
        self.managed.open_error = RuntimeError("a bug")
        with self.assertRaises(RuntimeError):
            self.chat.send(OWNER, AGENT, "hello", "key-0000020")

    def test_without_a_managed_runtime_a_managed_agent_cannot_be_opened(self):
        self.chat.managed = None
        with self.assertRaises(DomainError) as refused:
            self.chat.send(OWNER, AGENT, "hello", "key-0000021")
        self.assertEqual(refused.exception.code, "driver_unavailable")

    def test_only_the_installation_owner_may_send(self):
        with self.assertRaises(DomainError) as refused:
            self.chat.send("someone-else", AGENT, "hello", "key-0000022")
        self.assertEqual(refused.exception.code, "forbidden")
        self.assertEqual(self.managed.opened, [])


# ===== the open flow's races, the resume refusal and the first message ===========================================================

class OpenFlowTests(ChatCase):
    def refusal(self, *args, **options):
        with self.assertRaises(DomainError) as refused:
            self.chat.send(OWNER, AGENT, *args, **options)
        return refused.exception

    def test_a_conversation_that_cannot_be_resumed_is_refused_in_plain_words_and_nothing_is_stored_or_sent(self):
        words = "The earlier conversation can't be continued because its file is missing. Start a new conversation to carry on without it."
        self.managed.open_error = ManagedError("resume_unavailable", words)
        refused = self.refusal("hello", "key-0000030")
        self.assertEqual((refused.code, refused.message, bool(refused.retryable)), ("resume_unavailable", words, False))
        self.assertEqual((self.inputs(), self.managed.first, store.live_session(self.db.connection, AGENT)), (0, [], None))

    def test_a_missing_claude_names_where_it_looked_and_a_launcher_detail_is_still_not_shown(self):
        self.managed.open_error = ManagedError("cli_unavailable", "Claude Code could not be found. Qorqbot looked in /opt/bin. Install it.")
        refused = self.refusal("hello", "key-0000031")
        self.assertEqual(refused.code, "cli_unavailable")
        self.assertIn("looked in /opt/bin", refused.message)
        self.managed.open_error = ManagedError("host_failed", "failed in /private/path", details={"reason": "x"})
        refused = self.refusal("hello", "key-0000032")
        self.assertEqual(refused.code, "driver_unavailable")
        self.assertNotIn("/private/path", refused.message)

    def test_a_new_conversation_is_only_started_when_the_owner_asks_for_it(self):
        self.host_starts()
        self.chat.send(OWNER, AGENT, "hello", "key-0000033")
        self.assertEqual(self.managed.open_options[-1]["new_conversation"], False)
        self.managed.running, self.managed.view, self.marker = False, dict(CLOSED_VIEW), None
        self.db.connection.execute("UPDATE native_sessions SET ended_at='then', end_reason='process_gone'")
        self.chat.send(OWNER, AGENT, "again", "key-0000034", new_conversation=True)
        self.assertEqual(self.managed.open_options[-1]["new_conversation"], True)

    def test_a_new_conversation_is_refused_with_a_pin_for_a_live_session_and_when_not_a_boolean(self):
        pin = self.open()
        self.assertEqual(self.refusal("hello", "key-0000035", pin, new_conversation=True).code, "validation_error")
        self.assertEqual(self.refusal("hello", "key-0000036", new_conversation=True).code, "validation_error")
        for bad in ("yes", 1, None):
            self.assertEqual(self.refusal("hello", "key-0000037", new_conversation=bad).code, "validation_error")
        self.assertEqual(self.managed.opened, [])

    def test_the_new_conversation_is_followed_while_the_open_waits_so_that_it_can_bind(self):
        self.managed.on_open = None
        followed = []

        def follow(profile):
            followed.append(profile["agent_id"])
            self.bring_up()  # the observer pass adopts the conversation the session announced
        with mock.patch.object(self.chat, "adopt_session", follow):
            result = self.chat.send(OWNER, AGENT, "hello", "key-0000039", new_conversation=True)
        self.assertEqual((followed, result["created"]), ([AGENT], True))

    def test_one_budget_covers_the_host_and_the_binding(self):
        def slow_start():
            time.sleep(0.2)
        self.managed.on_open = slow_start
        started = time.monotonic()
        with mock.patch.object(chat_module, "OPEN_TOTAL_SECONDS", 0.4):
            refused = self.refusal("hello", "key-0000040")
        self.assertEqual((refused.code, refused.retryable), ("open_timeout", True))
        self.assertEqual(self.managed.open_options, [{"timeout": 0.4, "new_conversation": False}])
        self.assertLess(time.monotonic() - started, 0.4 + 0.5)

    # ----- the first message of a session --------------------------------------------------------------------------------

    def test_a_resumed_session_binds_only_once_the_owners_message_is_delivered_to_the_host(self):
        """No ``/role`` is sent on a resume and nothing announces the session until a message arrives: the owner's message is
        handed to the host with a uuid derived from the request, and the session binds after it."""
        self.managed.on_open = None
        self.managed.on_first = self.bring_up
        result = self.chat.send(OWNER, AGENT, "carry on", "key-0000041")
        self.assertEqual(self.managed.first, [(AGENT, "carry on", ManagedRuntime.first_message_uuid(AGENT, "key-0000041", "carry on"))])
        self.assertEqual((result["created"], result["input"]["state"], self.inputs()), (True, STATE_WAITING, 1))

    def test_a_fresh_session_that_binds_from_its_role_prompt_is_not_sent_the_message_twice(self):
        self.managed.role_pending = True
        self.managed.on_open = None
        reads = []
        original = self.probe._marker_reader

        def marker_reader(profile):
            reads.append(1)
            if len(reads) == 3:
                self.bring_up()  # the role prompt was taken up: Claude announced its session
            return original(profile)
        self.probe._marker_reader = marker_reader
        with mock.patch.object(chat_module, "ROLE_GRACE_SECONDS", 5.0):
            self.chat.send(OWNER, AGENT, "hello", "key-0000042")
        self.assertEqual(self.managed.first, [])

    def test_a_fresh_session_whose_role_prompt_was_blocked_is_given_the_message_after_the_grace_instead_of_waiting_forever(self):
        """A hook-blocked prompt writes no transcript record and announces nothing: the binding would never come. After
        ``ROLE_GRACE_SECONDS`` the owner's message itself is delivered."""
        self.managed.role_pending = True
        self.managed.on_open = None
        self.managed.on_first = self.bring_up
        started = time.monotonic()
        with mock.patch.object(chat_module, "ROLE_GRACE_SECONDS", 0.15):
            result = self.chat.send(OWNER, AGENT, "hello", "key-0000043")
        self.assertGreaterEqual(time.monotonic() - started, 0.15)
        self.assertEqual(len(self.managed.first), 1)
        self.assertEqual((result["created"], self.inputs()), (True, 1))

    def test_a_host_that_cannot_take_the_first_message_is_a_refusal_not_a_hang(self):
        self.managed.on_open = None
        for raised, shown, retryable in (("session_closed", "session_closed", False), ("open_timeout", "open_timeout", True),
                                         ("host_unverified", "host_unverified", True)):
            with self.subTest(raised=raised):
                self.managed.first_error = ManagedError(raised, "detail in /private/path", retryable=retryable)
                refused = self.refusal("hello", "key-0000044")
                self.assertEqual((refused.code, bool(refused.retryable)), (shown, retryable))
                self.assertNotIn("/private/path", refused.message)
        self.assertEqual((self.inputs(), store.live_session(self.db.connection, AGENT)), (0, None))

    def test_a_retry_with_the_same_key_after_a_timeout_neither_opens_twice_nor_sends_twice_and_uses_the_host_that_came_up(self):
        self.managed.on_open = None
        with mock.patch.object(chat_module, "OPEN_TOTAL_SECONDS", 0.2):
            self.assertEqual(self.refusal("hello", "key-0000045").code, "open_timeout")
        self.assertEqual((len(self.managed.opened), len(self.managed.first)), (1, 1))
        self.bring_up()  # the host that was still starting came up after the call gave up
        result = self.chat.send(OWNER, AGENT, "hello", "key-0000045")
        self.assertEqual((len(self.managed.opened), len(self.managed.first)), (1, 1))
        self.assertEqual((result["created"], self.inputs(), result["binding"]["transport"]), (True, 1, "managed"))
        again = self.chat.send(OWNER, AGENT, "hello", "key-0000045")
        self.assertEqual((again["created"], again["input"]["id"], self.inputs()), (False, result["input"]["id"], 1))

    def test_another_message_cannot_ride_on_the_retry_record_of_a_different_key(self):
        self.managed.on_open = None
        with mock.patch.object(chat_module, "OPEN_TOTAL_SECONDS", 0.1):
            self.refusal("hello", "key-0000046")
        self.bring_up()
        self.assertEqual(self.refusal("other", "key-0000047").code, "validation_error")
        self.assertEqual(self.inputs(), 0)


# ===== the view and the change event =============================================================================================

class SessionViewTests(ChatCase):
    def view(self):
        return self.chat.session_view(self.profile(), store.live_session(self.db.connection, AGENT))

    def test_a_closed_managed_agent_can_send_without_a_binding_and_says_how_it_starts(self):
        view = self.view()
        self.assertEqual((view["state"], view["reason"], view["can_send"], view["binding"], view["working"]),
                         ("offline", "session_closed", True, None, False))
        self.assertNotIn("execution", view)
        self.assertEqual(view["detail"], "Sending a message starts a session")
        self.assertEqual(set(view["managed"]), set(MANAGED_FIELDS))
        self.assertEqual((view["managed"]["open"], view["managed"]["pending"]), (False, []))

    def test_a_host_that_runs_before_its_session_is_bound_is_connecting_not_closed(self):
        self.managed.view = {**OPEN_VIEW, "state": "starting", "phase": "starting"}
        view = self.view()
        self.assertEqual((view["state"], view["reason"], view["can_send"], view["managed"]["open"]), ("live", "binding_not_ready", False, True))

    def test_a_bound_session_is_live_and_carries_the_hosts_account_of_it(self):
        pin = self.open()
        self.managed.view = {**OPEN_VIEW, "phase": "blocked", "can_stop": True, "activity": "Waiting",
                             "pending": [{"request_id": "r1", "kind": "permission", "tool": "Write", "input_summary": "/tmp/x",
                                          "questions": []}]}
        view = self.view()
        self.assertEqual((view["state"], view["transport"], view["can_send"], view["binding"]["binding_id"]), ("live", "managed", True, pin))
        self.assertEqual(view["detail"], "Working…")  # a blocked session is mid-turn
        self.assertEqual((view["managed"]["phase"], view["managed"]["can_stop"], view["managed"]["pending"][0]["request_id"]),
                         ("blocked", True, "r1"))

    def test_a_runtime_that_cannot_be_read_is_a_closed_session(self):
        self.managed.view = OSError("host unreadable")
        self.assertEqual(self.view()["managed"]["open"], False)
        self.chat.managed = None
        self.assertEqual(self.view()["managed"]["open"], False)
        self.assertEqual(self.view()["can_send"], True)

    def test_the_change_event_carries_the_managed_summary(self):
        with self.db.transaction() as uow:
            store.bump(uow.connection, AGENT)
        self.managed.view = {**OPEN_VIEW, "phase": "working", "can_stop": True, "activity": "Reading a file"}
        events, latest = self.chat.events(OWNER, AGENT, 0)
        self.assertEqual([event["kind"] for event in events], ["changed"])
        data = events[0]["data"]
        self.assertEqual(set(data), {"revision", "newest_sequence", "waiting", "managed"})
        self.assertEqual((data["managed"]["phase"], data["managed"]["activity"], data["managed"]["can_stop"]),
                         ("working", "Reading a file", True))
        self.assertEqual(self.chat.events(OWNER, AGENT, latest), ([], latest))

# ===== the conversation before a compaction ======================================================================================

class ContinuesTests(ChatCase):
    """``conversation.continues`` of the state: the conversation a compaction closed to open the one shown. The log of
    compactions is the real table and the real lookup of the handoff domain; the captures are rows with nothing archived."""

    OPENED_AT = "2026-11-20T10:00:00.000000Z"

    def setUp(self):
        super().setUp()
        for statement in compactions.SCHEMA:
            self.db.connection.execute(statement)
        self.runtime.compaction_handoff = CompactionHandoff(self.runtime)

    def capture(self, conversation, updated, provider="claude", receipt=None):
        self.db.connection.execute(
            "INSERT INTO native_captures(agent_id,provider,conversation_id,transcript_path,receipt_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)", (AGENT, provider, conversation, f"/nonexistent/{conversation}.jsonl", receipt, updated, updated))

    def compacted(self, closed, session, agent=AGENT, at=OPENED_AT):
        """One opened row of the log, as ``CompactionHandoff.opened`` leaves it."""
        self.db.connection.execute(
            "INSERT INTO compactions(id,agent_id,reason,requested_at,request_json,listed_from,listed_until,opened_at,opened_json) "
            "VALUES(?,?,'context',?,'{}',?,?,?,?)", (f"{agent}:{closed}:{session}:{at}", agent, at, at, at, at, json.dumps(
                {"unanswered": [], "closed": closed, "session": session})))

    def shown(self, **selection):
        return self.chat.state(OWNER, AGENT, **selection)["conversation"]

    def test_a_conversation_a_compaction_opened_points_back_to_the_captured_one_it_replaced(self):
        self.capture("old", "2026-11-20T09:00:00Z")
        self.capture("new", "2026-11-20T11:00:00Z")
        self.compacted("old", "new")
        earlier = {"conversation_id": "old", "provider": "claude", "at": self.OPENED_AT}
        shown = self.shown()                               # nothing of the fresh conversation is archived yet
        self.assertEqual((shown["conversation_id"], shown["continues"]), ("new", earlier))
        # The one it points to is an ordinary conversation, and can be asked for by exactly that pair.
        before = self.shown(conversation="old", provider="claude")
        self.assertEqual((before["conversation_id"], before["continues"]), ("old", None))
        # It is part of the conversation's info whether the page is read, cannot be read, or has nothing yet.
        self.db.connection.execute("UPDATE native_captures SET receipt_id='r1' WHERE conversation_id='new'")
        receipt = SimpleNamespace(archive_id="archive", source_path="/nonexistent/new.jsonl")
        self.chat.indexer.archive = lambda profile: SimpleNamespace(load_receipt=lambda identity: receipt)
        self.chat.reader = SimpleNamespace(window=lambda *args, **options: SimpleNamespace(entries=[], older=None))
        self.assertEqual(self.shown()["continues"], earlier)

        def unreadable(*args, **options):
            raise CaptureError("archive_unavailable", "The archive cannot be read now")
        self.chat.reader = SimpleNamespace(window=unreadable)
        shown = self.shown()
        self.assertEqual((shown["error"], shown["continues"]), ("archive_unavailable", earlier))

    def test_an_ordinary_conversation_continues_nothing(self):
        self.capture("old", "2026-11-20T09:00:00Z")
        self.capture("new", "2026-11-20T11:00:00Z")
        self.assertIsNone(self.shown()["continues"])
        self.compacted("old", "new", agent="agent-2")      # another agent's compaction says nothing about this one
        self.assertEqual((self.shown()["conversation_id"], self.shown()["continues"]), ("new", None))
        self.assertIsNone(self.shown(conversation="new", provider="claude")["continues"])

    def test_a_conversation_whose_earlier_one_was_not_captured_under_the_same_provider_continues_nothing(self):
        self.capture("new", "2026-11-20T11:00:00Z")
        self.compacted("old", "new")
        self.assertEqual((self.shown()["conversation_id"], self.shown()["continues"]), ("new", None))      # never captured
        self.capture("old", "2026-11-20T09:00:00Z", provider="codex")
        self.assertIsNone(self.shown()["continues"])       # captured, but as another client's conversation of that id
        self.capture("old", "2026-11-20T09:00:00Z")
        self.assertEqual(self.shown()["continues"]["conversation_id"], "old")

    def test_without_the_handoff_domain_the_key_is_there_and_empty(self):
        self.capture("old", "2026-11-20T09:00:00Z")
        self.capture("new", "2026-11-20T11:00:00Z")
        self.compacted("old", "new")
        del self.runtime.compaction_handoff
        self.assertEqual((self.shown()["conversation_id"], self.shown()["continues"]), ("new", None))


# ===== uploads ===================================================================================================================

class UploadTests(ChatCase):
    def upload(self, pin=None):
        return self.chat.upload(OWNER, AGENT, "upload-key-0001", pin, "notes.txt", b"hello")

    def test_a_file_for_an_asleep_managed_agent_opens_its_session_and_is_attached_to_the_binding_that_comes_up(self):
        """Was ``session_closed``: a file is input like a message now, and wakes the agent (stage 1 of the cutover)."""
        self.host_starts()
        (self.root / "workspace").mkdir()
        with mock.patch("qorqut.native_chat.uploads.save", return_value={"saved": True}) as save:
            result = self.upload("a-pin-from-a-closed-session")
        live = store.live_session(self.db.connection, AGENT)
        self.assertEqual(result, {"saved": True, "binding_id": live["id"]})
        self.assertEqual((self.managed.opened, save.call_args.args[3]), ([AGENT], live["id"]))

    def test_a_file_for_an_agent_that_cannot_be_opened_is_refused_and_not_saved(self):
        self.managed.on_open = lambda: None
        with mock.patch.object(chat_module, "OPEN_TOTAL_SECONDS", 0.2), \
                mock.patch("qorqut.native_chat.uploads.save") as save:
            with self.assertRaises(DomainError) as refused:
                self.upload("pin-not-used")
        self.assertEqual((refused.exception.code, save.called), ("open_timeout", False))

    def test_a_session_still_starting_is_binding_not_ready(self):
        self.managed.view = {**OPEN_VIEW, "state": "starting", "phase": "starting"}
        with self.assertRaises(DomainError) as refused:
            self.upload("pin-not-used")
        self.assertEqual(refused.exception.code, "binding_not_ready")

    def test_an_open_session_takes_the_file_for_exactly_its_binding(self):
        pin = self.open()
        (self.root / "workspace").mkdir()
        with self.assertRaises(DomainError) as stale:
            self.upload("another-binding-id")
        self.assertEqual(stale.exception.code, "terminal_changed")
        with mock.patch("qorqut.native_chat.uploads.save", return_value={"saved": True}) as save:
            self.assertEqual(self.upload(pin), {"saved": True, "binding_id": pin})
        self.assertEqual(save.call_args.args[3], pin)

    def go_to_sleep(self, desired="running"):
        self.managed.running = False
        self.managed.view = dict(CLOSED_VIEW)
        self.marker = None
        self.db.connection.execute("UPDATE native_session_owners SET desired_state=?", (desired,))
        self.db.connection.commit()

    def desired(self):
        return self.db.connection.execute("SELECT desired_state FROM native_session_owners").fetchone()[0]

    def test_upload_to_a_sleeping_agent_then_a_send_with_the_returned_binding_is_one_open_and_one_input(self):
        self.open()
        self.go_to_sleep()
        self.host_starts()
        (self.root / "workspace").mkdir()
        saved = self.upload(None)
        live = store.live_session(self.db.connection, AGENT)
        self.assertEqual(saved["binding_id"], live["id"])
        reference = saved["reference"]
        sent = self.chat.send(OWNER, AGENT, "what is in the file? " + reference, "key-after-up-1", saved["binding_id"])
        self.assertEqual(self.managed.opened, [AGENT])  # the upload opened it; the message did not open a second one
        self.assertEqual((sent["binding"]["binding_id"], sent["created"]), (live["id"], True))
        repeat = self.chat.send(OWNER, AGENT, "what is in the file? " + reference, "key-after-up-1", saved["binding_id"])
        self.assertEqual((repeat["created"], repeat["input"]["id"]), (False, sent["input"]["id"]))
        self.assertEqual(self.db.count("native_inputs"), 2)  # the first open's message and this one, never a copy of the file

    def test_an_upload_to_an_off_agent_turns_it_on_like_a_message_and_the_send_keeps_it_on(self):
        self.open()
        self.go_to_sleep("stopped")
        self.host_starts()
        (self.root / "workspace").mkdir()
        self.assertEqual(self.desired(), "stopped")
        saved = self.upload(None)
        self.assertEqual(self.desired(), "running")
        self.assertEqual(saved["binding_id"], store.live_session(self.db.connection, AGENT)["id"])
        self.chat.send(OWNER, AGENT, "what is in the file?", "key-after-up-2", saved["binding_id"])
        self.assertEqual((self.desired(), self.managed.opened), ("running", [AGENT]))


# ===== the controls ==============================================================================================================

class ControlTests(ChatCase):
    def test_only_the_installation_owner_may_control(self):
        pin = self.open()
        for call in (lambda: self.chat.interrupt("someone-else", AGENT, pin), lambda: self.chat.close("someone-else", AGENT),
                     lambda: self.chat.answer("someone-else", AGENT, pin, "r1", "allow")):
            with self.assertRaises(DomainError) as refused:
                call()
            self.assertEqual(refused.exception.code, "forbidden")
        self.assertEqual(self.managed.calls, [])

    def test_interrupt_reports_whether_anything_was_running(self):
        pin = self.open()
        self.managed.reply = {"status": "ok", "sent": True}
        self.assertEqual(self.chat.interrupt(OWNER, AGENT, pin), {"stopped": True})
        self.managed.reply = {"status": "ok", "sent": False}
        self.assertEqual(self.chat.interrupt(OWNER, AGENT, pin), {"stopped": False})
        self.assertEqual(self.managed.calls, [("interrupt", AGENT)] * 2)

    def test_the_pin_must_name_the_live_session_for_interrupt_and_answer(self):
        self.open()
        for call in (lambda: self.chat.interrupt(OWNER, AGENT, "0000-another-session"),
                     lambda: self.chat.answer(OWNER, AGENT, "0000-another-session", "r1", "allow")):
            with self.assertRaises(DomainError) as stale:
                call()
            self.assertEqual(stale.exception.code, "terminal_changed")
        for call in (lambda: self.chat.interrupt(OWNER, AGENT, None), lambda: self.chat.interrupt(OWNER, AGENT, "short"),
                     lambda: self.chat.answer(OWNER, AGENT, None, "r1", "allow")):
            with self.assertRaises(DomainError) as invalid:
                call()
            self.assertEqual(invalid.exception.code, "validation_error")
        self.assertEqual(self.managed.calls, [])

    def test_an_interrupt_with_no_session_is_terminal_changed_not_a_start(self):
        with self.assertRaises(DomainError) as refused:
            self.chat.interrupt(OWNER, AGENT, "pin-0000001")
        self.assertEqual(refused.exception.code, "terminal_changed")
        self.assertEqual(self.managed.opened, [])

    def test_a_failure_to_reach_the_host_is_a_plain_refusal_with_no_detail(self):
        pin = self.open()
        for raised, shown, retryable in (("session_closed", "session_closed", False), ("host_unverified", "host_unverified", True),
                                         ("session_starting", "binding_not_ready", True), ("host_busy", "operation_unavailable", True)):
            with self.subTest(raised=raised):
                self.managed.error = ManagedError(raised, "socket /run/private/host.sock refused", retryable=retryable)
                with self.assertRaises(DomainError) as refused:
                    self.chat.interrupt(OWNER, AGENT, pin)
                self.assertEqual((refused.exception.code, bool(refused.exception.retryable)), (shown, retryable))
                self.assertNotIn("/run/private", refused.exception.message)

    def test_a_host_that_rejects_the_control_is_session_closed(self):
        pin = self.open()
        self.pending({"request_id": "r1", "kind": "permission", "tool": "Bash", "input_summary": "", "questions": []})
        self.managed.reply = {"status": "rejected", "reason": "session_closed"}
        for call in (lambda: self.chat.interrupt(OWNER, AGENT, pin), lambda: self.chat.answer(OWNER, AGENT, pin, "r1", "allow")):
            with self.assertRaises(DomainError) as refused:
                call()
            self.assertEqual(refused.exception.code, "session_closed")

    def test_answer_validates_before_it_reaches_the_host(self):
        pin = self.open()
        for request_id, decision in ((None, "allow"), ("", "allow"), ("r" * 201, "allow"), ("r1", "maybe"), ("r1", True), ("r1", None),
                                     ("r1", {}), ("r1", {"answers": {}}), ("r1", {"answers": {"q": 1}}), ("r1", {"answers": {"q": "a"}, "x": 1}),
                                     ("r1", {"answers": {"": "a"}}), ("r1", {"answers": {f"q{n}": "a" for n in range(21)}})):
            with self.subTest(request_id=request_id, decision=decision):
                with self.assertRaises(DomainError) as invalid:
                    self.chat.answer(OWNER, AGENT, pin, request_id, decision)
                self.assertEqual(invalid.exception.code, "validation_error")
        self.assertEqual(self.managed.calls, [])

    def test_a_permission_is_answered_allow_or_deny_and_a_question_with_its_answers(self):
        pin = self.open()
        self.managed.view = {**OPEN_VIEW, "phase": "blocked", "pending": [
            {"request_id": "perm-1", "kind": "permission", "tool": "Write", "input_summary": "", "questions": []},
            {"request_id": "ask-1", "kind": "question", "tool": None, "input_summary": "", "questions": [{"question": "Which?"}]}]}
        self.managed.reply = {"status": "ok", "delivered": True}
        self.assertEqual(self.chat.answer(OWNER, AGENT, pin, "perm-1", "deny"), {"answered": True})
        self.assertEqual(self.chat.answer(OWNER, AGENT, pin, "ask-1", {"answers": {"Which?": "A"}}), {"answered": True})
        self.assertEqual(self.managed.calls, [("answer", AGENT, "perm-1", "deny"), ("answer", AGENT, "ask-1", {"answers": {"Which?": "A"}})])
        for request_id, decision in (("perm-1", {"answers": {"Which?": "A"}}), ("ask-1", "allow")):
            with self.assertRaises(DomainError) as mismatch:
                self.chat.answer(OWNER, AGENT, pin, request_id, decision)
            self.assertEqual(mismatch.exception.code, "validation_error")
        self.assertEqual(len(self.managed.calls), 2)

    def test_a_request_that_stops_waiting_while_it_is_answered_is_not_found(self):
        pin = self.open()
        self.pending({"request_id": "r-gone", "kind": "permission", "tool": "Bash", "input_summary": "", "questions": []})
        self.managed.reply = {"status": "ok", "delivered": False}
        with self.assertRaises(DomainError) as gone:
            self.chat.answer(OWNER, AGENT, pin, "r-gone", "allow")
        self.assertEqual(gone.exception.code, "not_found")

    def pending(self, *items):
        self.managed.view = {**OPEN_VIEW, "phase": "blocked", "pending": list(items)}
        self.managed.reply = {"status": "ok", "delivered": True}

    QUESTION = {"request_id": "ask-1", "kind": "question", "tool": "AskUserQuestion", "input_summary": "", "questions": [
        {"question": "Which database?", "header": "DB", "multiSelect": False,
         "options": [{"label": "Postgres", "description": "x"}, {"label": "SQLite", "description": "y"}]},
        {"question": "Which checks?", "header": "Checks", "multiSelect": True,
         "options": [{"label": "Lint", "description": ""}, {"label": "Types", "description": ""}]}]}

    def test_the_request_is_read_live_so_one_that_is_not_waiting_any_more_never_reaches_the_host(self):
        pin = self.open()
        self.pending({"request_id": "perm-1", "kind": "permission", "tool": "Bash", "input_summary": "", "questions": []})
        self.managed.live = {**OPEN_VIEW, "pending": []}  # the page's cached view is stale: it was answered elsewhere
        with self.assertRaises(DomainError) as gone:
            self.chat.answer(OWNER, AGENT, pin, "perm-1", "allow")
        self.assertEqual(gone.exception.code, "not_found")
        self.assertEqual(self.managed.calls, [])

    def test_a_request_that_was_never_pending_is_not_found_before_the_host(self):
        pin = self.open()
        with self.assertRaises(DomainError) as gone:
            self.chat.answer(OWNER, AGENT, pin, "made-up", "allow")
        self.assertEqual(gone.exception.code, "not_found")
        self.assertEqual(self.managed.calls, [])

    def test_a_session_that_cannot_be_read_is_a_refusal_and_nothing_is_answered(self):
        pin = self.open()
        self.managed.live = ManagedError("host_unverified", "no reply", retryable=True)
        with self.assertRaises(DomainError) as unreachable:
            self.chat.answer(OWNER, AGENT, pin, "perm-1", "allow")
        self.assertEqual((unreachable.exception.code, unreachable.exception.retryable), ("host_unverified", True))
        self.assertEqual(self.managed.calls, [])

    def test_a_question_is_answered_for_each_question_asked_with_a_label_or_the_owners_own_words(self):
        pin = self.open()
        self.pending(self.QUESTION)
        good = {"answers": {"Which database?": "Postgres", "Which checks?": "Lint, Types"}}
        self.assertEqual(self.chat.answer(OWNER, AGENT, pin, "ask-1", good), {"answered": True})
        other = {"answers": {"Which database?": "Something else entirely", "Which checks?": "Lint"}}
        self.assertEqual(self.chat.answer(OWNER, AGENT, pin, "ask-1", other), {"answered": True})
        self.assertEqual([call[3] for call in self.managed.calls], [good, other])
        before = len(self.managed.calls)
        for bad in ({"answers": {"Which database?": "Postgres"}},                                   # a question left unanswered
                    {"answers": {"Which database?": "Postgres", "Which checks?": "Lint", "Extra?": "x"}},   # a question that was not asked
                    {"answers": {"Which database?": "Postgres", "Which checks?": "  "}},            # nothing chosen
                    {"answers": {"Which database": "Postgres", "Which checks?": "Lint"}},           # not the question's exact text
                    "allow", "deny"):
            with self.subTest(bad=bad):
                with self.assertRaises(DomainError) as invalid:
                    self.chat.answer(OWNER, AGENT, pin, "ask-1", bad)
                self.assertEqual(invalid.exception.code, "validation_error")
        self.assertEqual(len(self.managed.calls), before)

    def test_a_permission_request_takes_allow_or_deny_and_never_answers(self):
        pin = self.open()
        self.pending({"request_id": "perm-1", "kind": "permission", "tool": "Bash", "input_summary": "", "questions": []})
        for decision in ({"answers": {"x": "y"}}, "maybe"):
            with self.assertRaises(DomainError) as invalid:
                self.chat.answer(OWNER, AGENT, pin, "perm-1", decision)
            self.assertEqual(invalid.exception.code, "validation_error")
        self.assertEqual(self.chat.answer(OWNER, AGENT, pin, "perm-1", "allow"), {"answered": True})
        self.assertEqual(self.managed.calls, [("answer", AGENT, "perm-1", "allow")])

    def test_close_reports_whether_a_session_was_open_and_the_pin_is_optional(self):
        pin = self.open()
        self.managed.reply = True
        self.assertEqual(self.chat.close(OWNER, AGENT), {"closing": True})
        self.assertEqual(self.chat.close(OWNER, AGENT, pin), {"closing": True})
        self.managed.reply = False
        self.assertEqual(self.chat.close(OWNER, AGENT), {"closing": False})
        self.assertEqual(self.managed.calls, [("close", AGENT, "graceful")] * 3)
        with self.assertRaises(DomainError) as stale:
            self.chat.close(OWNER, AGENT, "0000-another-session")
        self.assertEqual(stale.exception.code, "terminal_changed")
        with self.assertRaises(DomainError) as invalid:
            self.chat.close(OWNER, AGENT, "short")
        self.assertEqual(invalid.exception.code, "validation_error")

    def test_close_of_a_closed_agent_is_a_plain_no(self):
        self.managed.reply = False
        self.assertEqual(self.chat.close(OWNER, AGENT), {"closing": False})
        self.assertEqual(self.managed.opened, [])


# ===== the runtime switch ========================================================================================================

class CatalogTests(unittest.TestCase):
    def by_name(self, **options):
        return {item["name"]: item for item in commands.catalog("claude", **options)["commands"]}

    def test_the_menu_only_commands_are_unavailable_and_the_rest_run_in_chat(self):
        managed = self.by_name()
        menu = {name for name in managed if name in MANAGED_MENU_COMMANDS}
        self.assertTrue({"/clear", "/resume", "/cost", "/status", "/model", "/help", "/context"} <= menu)
        for name in menu:
            self.assertEqual(managed[name]["where"], "unavailable", name)
        self.assertEqual(managed["/exit"]["where"], "chat")
        self.assertEqual(managed["/compact"]["where"], "chat")

    def test_the_owners_own_commands_stay_chat_commands_and_codex_has_no_managed_catalog(self):
        with tempfile.TemporaryDirectory() as home:
            folder = Path(home) / ".claude" / "commands"
            folder.mkdir(parents=True)
            (folder / "deploy.md").write_text("---\ndescription: Ship it\n---\nShip.\n")
            catalog = {item["name"]: item for item in commands.catalog("claude", home=home)["commands"]}
        self.assertEqual((catalog["/deploy"]["where"], catalog["/deploy"]["source"]), ("chat", "yours"))
        codex = {item["name"]: item for item in commands.catalog("codex")["commands"]}
        self.assertEqual((codex["/compact"]["where"], codex["/model"]["where"], codex["/exit"]["where"]), ("chat", "chat", "chat"))
        self.assertEqual(codex["/new"]["where"], "unavailable")
        self.assertEqual(commands.catalog("other"), {"client": None, "commands": []})


# ===== the attachment view =======================================================================================================

# ===== the nudge outcome =========================================================================================================

class NudgeOutcomeTests(unittest.TestCase):
    BINDING = NudgeBinding("b1", AGENT, "claude", CONVERSATION, 1, "managed", 4242, "99")

    def result(self, state, reason):
        return InputResult(state=state, reason=reason, origin="system", input_type="inbox_nudge")

    def test_a_managed_binding_is_a_valid_nudge_transport(self):
        self.assertIn("managed", NUDGE_TRANSPORTS)
        self.assertEqual(tuple(NUDGE_TRANSPORTS), tuple(TRANSPORTS))
        self.assertEqual(self.BINDING.public()["transport"], "managed")

    def test_a_closed_session_is_the_reason_class_not_a_failed_delivery(self):
        for reason in ("session_closed", "not_typed:session_closed"):
            self.assertEqual(nudges.outcome_of(self.result(RESULT_DEFERRED, reason), self.BINDING), "session_closed")
        self.assertIn("session_closed", nudges.CLOSED_REASONS)

    def test_a_session_that_is_starting_closing_or_busy_is_still_a_plain_deferral(self):
        for reason in ("session_starting", "session_closing", "turn_running", "host_unverified", "host_never_received"):
            with self.subTest(reason=reason):
                outcome = nudges.outcome_of(self.result(RESULT_DEFERRED, reason), self.BINDING)
                self.assertEqual((outcome.state, outcome.reason), ("deferred", reason))
        for reason in ("stale_session", "agent_unavailable"):
            self.assertEqual(nudges.outcome_of(self.result(RESULT_DEFERRED, reason), self.BINDING), reason)
        self.assertEqual(nudges.outcome_of(self.result(RESULT_DEFERRED, "terminal_offline"), self.BINDING).state, "deferred")


# ===== the recovery window =======================================================================================================

class ManagedHostWritersTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mhw-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.states = {}

    def inspect(self, directory):
        found = self.states[directory.name]
        if isinstance(found, Exception):
            raise found
        return SimpleNamespace(status=found)

    def writers(self, root=None):
        return ManagedHostWriters(root if root is not None else (lambda: self.root), inspect=self.inspect)

    def folder(self, name, status):
        (self.root / name).mkdir()
        self.states[name] = status

    def test_no_managed_agent_ever_switched_on_is_no_writer(self):
        self.assertEqual(self.writers(self.root / "missing").running(), [])
        self.assertEqual(self.writers().inventory_and_freeze(timeout=5).state, "frozen")

    def test_a_live_starting_or_unverified_host_refuses_the_window_and_nothing_is_closed(self):
        self.folder("idle-agent", "absent")
        for name, status in (("running-agent", "running"), ("starting-agent", "starting"), ("odd-agent", "unverified")):
            self.folder(name, status)
        writers = self.writers()
        self.assertEqual(writers.running(), ["odd-agent", "running-agent", "starting-agent"])
        with self.assertRaises(DomainError) as refused:
            writers.inventory_and_freeze(timeout=5)
        self.assertEqual((refused.exception.code, refused.exception.retryable), ("recovery_not_quiescent", True))
        self.assertIn("explicit close", refused.exception.message)

    def test_a_folder_that_cannot_be_inspected_is_not_proved_free(self):
        self.folder("broken-agent", OSError("unreadable"))
        with self.assertRaises(DomainError) as refused:
            self.writers().running()
        self.assertEqual(refused.exception.code, "recovery_not_quiescent")

    def test_links_and_loose_files_are_skipped_and_thaw_does_nothing(self):
        (self.root / "file.txt").write_text("x")
        os.symlink(self.root, self.root / "link-agent")
        self.states["link-agent"] = "running"
        self.assertEqual(self.writers().running(), [])
        self.assertIsNone(self.writers().thaw())

    def test_the_timeout_must_be_bounded(self):
        for bad in (0, 601, "5"):
            with self.assertRaises(ValueError):
                self.writers().inventory_and_freeze(timeout=bad)


if __name__ == "__main__":
    unittest.main()
