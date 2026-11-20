"""Codex slash commands in Chat: ``/compact`` and ``/model <name> [effort]`` run through the driver, every other built-in command
(a menu only a terminal shows) is refused before anything is recorded, and ordinary text goes to the thread as a message.

The arbiter runs on a real in-memory database holding the native Chat tables, with a fake probe, session registry and adapter; nothing
starts a CLI, reads a CLI configuration, or touches the network.
"""
import isolated_cli_environment  # before any Qorqbot import: tests never touch the real user's CLI files

from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut.native_chat import arbiter as arbiter_module, commands, schema, store
from qorqut.native_chat.arbiter import InputArbiter, MANAGED_COMMAND
from qorqut.native_chat.contracts import InputRequest, OwnerInputOutcome, RESULT_REJECTED, STATE_WAITING, TYPE_CHAT

AGENT, OWNER, SESSION = "agent-1", "owner-1", "session-codex-1"
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
    name = "managed"

    def __init__(self):
        self.typed = []

    def submit(self, profile, session, text, *, steer, echo, checkpoint, replace_composer=False):
        checkpoint()
        self.typed.append(text)
        return OwnerInputOutcome("submitted", "fake")


class Registry:
    session = None

    def reconcile(self, profile, observed):
        return SimpleNamespace(session=self.session, live=True)


class Probe:
    observation = None

    def observe(self, profile):
        return SimpleNamespace(reason="live", observation=self.observation)


def request(text, *, key="key-0001"):
    return InputRequest(AGENT, TYPE_CHAT, store.chat_source_key(OWNER, key), text, wait=True, principal_id=OWNER, expected_binding=SESSION,
                        steer=True)


class CodexCommandFunctions(unittest.TestCase):
    def test_compact_and_model_are_driver_commands(self):
        self.assertEqual(commands.codex_command("/compact"), ("compact", {}))
        self.assertEqual(commands.codex_command("  /COMPACT now"), ("compact", {}))
        self.assertEqual(commands.codex_command("/model gpt-x"), ("model", {"model": "gpt-x", "effort": None}))
        self.assertEqual(commands.codex_command("/model gpt-x high"), ("model", {"model": "gpt-x", "effort": "high"}))
        self.assertEqual(commands.codex_command("/model low"), ("model", {"model": None, "effort": "low"}))
        for text in ("/model", "/new", "hello /compact", "/tmp/foo", "/compactly", "//compact", ""):
            self.assertIsNone(commands.codex_command(text), text)

    def test_every_other_built_in_is_unrunnable_and_text_is_not(self):
        names = [name for name, _, _ in commands.BUILT_IN["codex"]]
        self.assertIn("/compact", names)
        for name in names:
            for text in (name, name.upper(), f"  {name}"):
                with self.subTest(text=text):
                    self.assertEqual(commands.unrunnable("codex", text), None if name in ("/compact", "/exit") else name)
        self.assertIsNone(commands.unrunnable("codex", "/model gpt-x"))
        for text in ("/tmp/foo", "/clear", "//compact", "hello /compact", "/ compact", "/unknown"):
            self.assertIsNone(commands.unrunnable("codex", text), text)

    def test_claude_menu_commands_stay_unrunnable(self):
        self.assertEqual(commands.unrunnable("claude", "/cost"), "/cost")
        self.assertEqual(commands.unrunnable("claude", "/model"), "/model")
        self.assertIsNone(commands.unrunnable("claude", "/model sonnet"))
        self.assertIsNone(commands.unrunnable("claude", "/compact"))


class ChatCodexCommandTest(unittest.TestCase):
    def setUp(self):
        isolated_cli_environment.isolate(self)
        self.db = Database()
        self.db.connection.execute(
            "INSERT INTO native_sessions(id,agent_id,provider,conversation_id,generation,transport,process_pid,process_started,"
            "owner,transcript_path,observed_at,verified_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (SESSION, AGENT, "codex", "conversation-1", 1, "managed", 4242, "1", "host:1:2", "/nonexistent/transcript.jsonl",
             "2026-11-04T10:00:00Z", "2026-11-04T10:00:00Z"))
        self.db.connection.commit()
        self.runtime = SimpleNamespace(database=self.db, paths=None, application=SimpleNamespace(ensure_writes_open=lambda uow: None))
        self.probe, self.registry, self.adapter = Probe(), Registry(), Adapter()
        self.arbiter = InputArbiter(self.runtime, self.probe, self.registry, self.adapter)
        self.forwarded = mock.Mock()
        for patcher in (mock.patch.object(InputArbiter, "_profile", lambda arbiter, agent_id: PROFILE),
                        mock.patch.object(arbiter_module, "forward_owner_message", self.forwarded),
                        mock.patch("qorqut.management.identity.configured_ordubashy", return_value=None),
                        mock.patch("qorqut.agents.attention.enrolled_board", return_value=False),
                        mock.patch("qorqut.management.identity.installation_owner", return_value=OWNER)):
            patcher.start()
            self.addCleanup(patcher.stop)
        row = store.get_session(self.db.connection, SESSION)
        self.registry.session = row
        self.probe.observation = SimpleNamespace(provider="codex", conversation_id=row["conversation_id"], pid=4242, started="1",
                                                 transport="managed", owner=row["owner"], capture_enabled=True)

    def test_a_menu_built_in_is_refused_before_anything_is_recorded_or_sent(self):
        for index, text in enumerate(("/new", "/init", "/review", "/model", "/NEW now")):
            with self.subTest(text=text):
                result = self.arbiter.submit(request(text, key=f"key-{index:04d}"))
                self.assertEqual((result.state, result.reason, result.input_id), (RESULT_REJECTED, MANAGED_COMMAND, None))
        self.assertEqual((self.db.count("native_inputs"), self.adapter.typed), (0, []))
        self.forwarded.assert_not_called()

    def test_the_driver_commands_and_ordinary_text_are_admitted_and_delivered(self):
        texts = ["/compact", "/model gpt-x high", "/tmp/foo", "/compact:more", "/clear", "/unknown", "//compact", "hello /compact", "/exit"]
        for index, text in enumerate(texts):
            with self.subTest(text=text):
                result = self.arbiter.submit(request(text, key=f"text-{index:04d}"))
                self.assertEqual((result.state, result.created), (STATE_WAITING, True))
        while self.arbiter.dispatch(AGENT):
            pass
        self.assertEqual(self.adapter.typed, texts)


if __name__ == "__main__":
    unittest.main()
