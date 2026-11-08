"""Owner Chat to a shared Codex server: a Codex built-in command is refused before anything is recorded or typed.

The arbiter runs on a real in-memory database holding the native Chat tables, with fake probe, session registry and input
adapters; nothing starts a CLI, reads a CLI configuration, or touches the network.
"""
import isolated_cli_environment  # before any Qorqbot import: tests never touch the real user's CLI files

from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError

from qorqut.native_chat import arbiter as arbiter_module, commands, schema, store
from qorqut.native_chat.arbiter import InputArbiter, TERMINAL_COMMAND
from qorqut.native_chat.chat import NativeChat
from qorqut.native_chat.contracts import (InputRequest, NativeChatError, RESULT_REJECTED, STATE_SUBMITTED, STATE_WAITING,
                                            TYPE_BROADCAST, TYPE_CHAT, TYPE_DIRECT, TYPE_NUDGE, TYPE_RECOVERY, TYPE_TELEGRAM)
from qorqut.transport.agent_terminal_control import OwnerInputOutcome

AGENT, OWNER = "agent-1", "owner-1"
SHARED, TERMINAL = "session-shared-1", "session-terminal-1"
PROFILE = {"agent_id": AGENT, "workspace": "/nonexistent/agent", "profile_json": "{}"}


class Database:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        for statement in schema.SCHEMA:
            self.connection.execute(statement)

    @contextmanager
    def read(self):
        yield SimpleNamespace(connection=self.connection)

    @contextmanager
    def transaction(self, operation=None):
        with self.connection:
            yield SimpleNamespace(connection=self.connection)

    def count(self, table):
        return self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class Adapter:
    """Records what it was asked to type; ``submit`` takes the claim checkpoint like the real adapters do."""

    def __init__(self, name):
        self.name, self.typed = name, []

    def submit(self, profile, session, text, *, steer, echo, checkpoint, replace_composer=False):
        checkpoint()
        self.typed.append(text)
        return OwnerInputOutcome("submitted", "fake")


class Registry:
    def __init__(self):
        self.session = None

    def reconcile(self, profile, observed):
        return SimpleNamespace(session=self.session, live=True)


class Probe:
    def __init__(self):
        self.observation = None

    def observe(self, profile):
        return SimpleNamespace(reason="live", observation=self.observation)


def request(text, *, key="key-0001", binding=None, input_type=TYPE_CHAT, wait=True):
    return InputRequest(AGENT, input_type, store.chat_source_key(OWNER, key) if input_type == TYPE_CHAT else key, text, wait=wait,
                        principal_id=OWNER if input_type == TYPE_CHAT else None, expected_binding=binding, steer=True)


class ChatCodexCommandTest(unittest.TestCase):
    def setUp(self):
        isolated_cli_environment.isolate(self)
        self.db = Database()
        for session_id, generation, transport in ((SHARED, 1, "codex_queue"), (TERMINAL, 2, "terminal")):
            self.db.connection.execute(
                "INSERT INTO native_sessions(id,agent_id,provider,conversation_id,generation,transport,process_pid,process_started,"
                "owner,transcript_path,observed_at,verified_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, AGENT, "codex", "conversation-" + session_id, generation, transport, 4242, "1", "owner-lock",
                 "/nonexistent/transcript.jsonl", "2026-11-04T10:00:00Z", "2026-11-04T10:00:00Z"))
        self.db.connection.commit()
        self.runtime = SimpleNamespace(database=self.db, paths=None, application=SimpleNamespace(
            ensure_writes_open=lambda uow: None))
        self.probe, self.registry = Probe(), Registry()
        self.adapters = {"codex_queue": Adapter("codex_queue"), "terminal": Adapter("terminal")}
        self.arbiter = InputArbiter(self.runtime, self.probe, self.registry, self.adapters)
        self.forwarded = mock.Mock()
        for patcher in (mock.patch.object(InputArbiter, "_profile", lambda arbiter, agent_id: PROFILE),
                        mock.patch.object(arbiter_module, "forward_owner_message", self.forwarded),
                        mock.patch("qorqut.management.identity.configured_ordubashy", return_value=None),
                        # This fixture has no Attention board, so no owner-message watermark is recorded.
                        mock.patch("qorqut.agents.attention.enrolled_board", return_value=False),
                        mock.patch("qorqut.management.identity.installation_owner", return_value=OWNER)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.use(SHARED)

    def use(self, session_id):
        """Make ``session_id`` the live, verified session of the agent."""
        row = store.get_session(self.db.connection, session_id)
        self.registry.session = row
        self.probe.observation = SimpleNamespace(provider=row["provider"], conversation_id=row["conversation_id"], pid=row["process_pid"],
                                                 started=row["process_started"], transport=row["transport"], owner=row["owner"],
                                                 capture_enabled=True)

    def delivered(self, adapter):
        """Dispatch whatever waits for the live session, then what each adapter was asked to type."""
        while self.arbiter.dispatch(AGENT):
            pass
        return list(self.adapters[adapter].typed)

    def nothing_stored(self):
        self.assertEqual((self.db.count("native_inputs"), self.db.count("native_input_events")), (0, 0))
        self.assertEqual(self.db.count("native_agent_counters"), 0)
        self.assertEqual((self.adapters["codex_queue"].typed, self.adapters["terminal"].typed), ([], []))
        self.forwarded.assert_not_called()

    # ----- the shared Codex server ------------------------------------------------------------------------------------

    def test_a_recognised_command_is_refused_before_anything_is_recorded_or_typed(self):
        result = self.arbiter.submit(request("/compact"))
        self.assertEqual((result.state, result.reason, result.input_id), (RESULT_REJECTED, TERMINAL_COMMAND, None))
        self.assertEqual(result.binding.id, SHARED)
        self.nothing_stored()
        self.assertEqual(self.delivered("codex_queue"), [])
        self.nothing_stored()

    def test_every_built_in_command_is_recognised_by_its_first_word_whatever_follows(self):
        names = [name for name, _, _ in commands.BUILT_IN["codex"]]
        self.assertIn("/compact", names)
        for index, name in enumerate(names):
            for text in (name, f"{name} now", f"  {name}\nkeep decisions", f"{name}\tx", name.upper()):
                with self.subTest(text=text):
                    result = self.arbiter.submit(request(text, key=f"key-{index:04d}"))
                    self.assertEqual((result.state, result.reason), (RESULT_REJECTED, TERMINAL_COMMAND))
        self.nothing_stored()

    def test_ordinary_text_and_other_slashes_are_admitted_and_delivered(self):
        texts = ["/tmp/foo", "/tmp/foo is missing", "/model/x", "/compact:more", "/clear", "/unknown", "//compact", "hello /compact",
                 "please run /new", "see /usr/bin/env", "/ compact", "/compactly"]
        for index, text in enumerate(texts):
            with self.subTest(text=text):
                result = self.arbiter.submit(request(text, key=f"text-{index:04d}"))
                self.assertEqual((result.state, result.created), (STATE_WAITING, True))
        self.assertEqual(self.delivered("codex_queue"), texts)
        self.assertEqual(self.db.count("native_inputs"), len(texts))

    def test_a_repeat_of_an_input_already_recorded_is_replayed_not_refused(self):
        # Recorded before the refusal existed (or under a session that has since ended): the original input comes back.
        first = request("/compact", key="key-0001", binding=SHARED)
        with self.db.transaction() as uow:
            row = store.insert_input(uow.connection, store.get_session(self.db.connection, SHARED), first, store.request_hash(first),
                                     state=STATE_WAITING)
        repeat = self.arbiter.submit(request("/compact", key="key-0001", binding=SHARED))
        self.assertEqual((repeat.input_id, repeat.state, repeat.created, repeat.reason), (row["id"], STATE_WAITING, False, None))
        self.assertEqual(self.db.count("native_inputs"), 1)
        self.use(TERMINAL)
        moved = self.arbiter.submit(request("/compact", key="key-0001", binding=SHARED))
        self.assertEqual((moved.input_id, moved.created, moved.binding.id), (row["id"], False, SHARED))
        with self.assertRaises(NativeChatError) as conflict:
            self.arbiter.submit(request("/compact other", key="key-0001", binding=SHARED))
        self.assertEqual(conflict.exception.code, "idempotency_conflict")
        self.assertEqual(self.db.count("native_inputs"), 1)

    def test_the_binding_pin_is_checked_first(self):
        result = self.arbiter.submit(request("/compact", binding=TERMINAL))
        self.assertEqual((result.state, result.reason), (RESULT_REJECTED, "stale_session"))
        self.nothing_stored()
        accepted = self.arbiter.submit(request("/tmp/foo", binding=SHARED))
        self.assertEqual(accepted.state, STATE_WAITING)
        refused = self.arbiter.submit(request("/compact", key="key-0002", binding=SHARED))
        self.assertEqual((refused.state, refused.reason), (RESULT_REJECTED, TERMINAL_COMMAND))

    def test_only_owner_chat_is_refused(self):
        for input_type in (TYPE_TELEGRAM, TYPE_BROADCAST, TYPE_DIRECT, TYPE_NUDGE, TYPE_RECOVERY):
            with self.subTest(input_type=input_type):
                result = self.arbiter.submit(request("/compact", key=f"other-{input_type}", input_type=input_type, wait=False,
                                                     binding=SHARED))
                self.assertNotEqual(result.reason, TERMINAL_COMMAND)
                self.assertEqual(result.state, STATE_SUBMITTED)
        self.assertEqual(len(self.adapters["codex_queue"].typed), 5)
        # A nudge or a recovery notice is typed behind its marker, so it is never a bare command.
        self.assertEqual(sum(1 for text in self.adapters["codex_queue"].typed if text == "/compact"), 3)

    def test_a_waiting_nudge_is_admitted_for_the_shared_server(self):
        result = self.arbiter.admit_nudge(AGENT, "nudge-0001", "/compact", wait=True, expected_binding=SHARED)
        self.assertEqual((result.state, result.reason), (STATE_WAITING, None))

    # ----- a terminal ---------------------------------------------------------------------------------------------------

    def test_a_terminal_session_still_runs_the_command(self):
        self.use(TERMINAL)
        for index, text in enumerate(("/compact", "/model", "/new keep this", "/review", "/tmp/foo")):
            result = self.arbiter.submit(request(text, key=f"term-{index:04d}", binding=TERMINAL))
            self.assertEqual((result.state, result.created), (STATE_WAITING, True), text)
        self.assertEqual(self.delivered("terminal"), ["/compact", "/model", "/new keep this", "/review", "/tmp/foo"])
        self.assertEqual(self.adapters["codex_queue"].typed, [])

    # ----- the Chat facade ----------------------------------------------------------------------------------------------

    def facade(self):
        chat = object.__new__(NativeChat)
        chat.runtime, chat.arbiter = self.runtime, self.arbiter
        chat._target = lambda uow, principal_id, agent_id: PROFILE
        return chat

    def test_send_tells_the_owner_where_the_command_runs(self):
        with self.assertRaises(DomainError) as refusal:
            self.facade().send(OWNER, AGENT, "/compact", "key-0001", SHARED)
        self.assertEqual(refusal.exception.code, "terminal_required")
        self.assertIn("Terminal", refusal.exception.message)
        self.nothing_stored()

    def test_send_still_accepts_text_and_a_terminal_command_and_replays_a_repeat(self):
        chat = self.facade()
        plain = chat.send(OWNER, AGENT, "/tmp/foo", "key-0001", SHARED)
        self.assertEqual((plain["input"]["state"], plain["created"], plain["binding"]["binding_id"]), (STATE_WAITING, True, SHARED))
        again = chat.send(OWNER, AGENT, "/tmp/foo", "key-0001", SHARED)
        self.assertEqual((again["input"]["id"], again["created"]), (plain["input"]["id"], False))
        self.use(TERMINAL)
        command = chat.send(OWNER, AGENT, "/compact", "key-0002", TERMINAL)
        self.assertEqual((command["input"]["state"], command["created"], command["binding"]["transport"]),
                         (STATE_WAITING, True, "terminal"))
        self.assertEqual(self.db.count("native_inputs"), 2)


if __name__ == "__main__":
    unittest.main()
