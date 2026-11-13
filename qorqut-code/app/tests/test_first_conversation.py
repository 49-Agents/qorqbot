"""Ordubashy is told at startup when the owner has never written to him, so a new owner's first hello opens set-up."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_agentic.behavior.instructions import from_catalog
from qorqut.bootstrap.claude_session import handle_hook
from qorqut.management import service as service_module
from qorqut.management.service import FIRST_CONVERSATION, ManagementService

ORDUBASHY = "agent-ordubashy"


class Principal:
    def __init__(self, identity):
        self.id = identity


class FakeApp:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript("""
            CREATE TABLE native_inputs (agent_id TEXT, origin TEXT, input_type TEXT);
            CREATE TABLE global_chat_messages (agent_id TEXT, role TEXT);
            CREATE TABLE owner_conversation_messages (author_role TEXT);
        """)
        self.database = self

    @contextmanager
    def read(self):
        yield self


class FirstConversationTests(unittest.TestCase):
    def setUp(self):
        self.app = FakeApp()
        self.service = ManagementService(self.app, clock=lambda: None)
        configured = mock.patch.object(service_module, "configured_ordubashy", return_value=ORDUBASHY)
        configured.start()
        self.addCleanup(configured.stop)

    def line(self, identity=ORDUBASHY):
        return self.service.first_conversation(Principal(identity))

    def test_new_installation_tells_ordubashy(self):
        self.assertEqual(self.line(), FIRST_CONVERSATION)

    def test_only_ordubashy_is_told(self):
        self.assertEqual(self.line("agent-learning-applier"), "")

    def test_system_inputs_are_not_the_owner_writing(self):
        self.app.connection.execute("INSERT INTO native_inputs VALUES (?, 'system', 'inbox_nudge')", (ORDUBASHY,))
        self.assertEqual(self.line(), FIRST_CONVERSATION)

    def test_any_owner_message_ends_it(self):
        writes = {
            "chat": ("INSERT INTO native_inputs VALUES (?, 'owner', 'chat')", (ORDUBASHY,)),
            "telegram": ("INSERT INTO native_inputs VALUES (?, 'owner', 'telegram')", (ORDUBASHY,)),
            "earlier chat": ("INSERT INTO global_chat_messages VALUES (?, 'owner')", (ORDUBASHY,)),
            "private thread": ("INSERT INTO owner_conversation_messages VALUES ('owner')", ()),
        }
        for name, (sql, values) in writes.items():
            with self.subTest(name):
                self.setUp()
                self.app.connection.execute(sql, values)
                self.assertEqual(self.line(), "")

    def test_an_owner_message_to_another_agent_does_not_end_it(self):
        self.app.connection.execute("INSERT INTO native_inputs VALUES ('agent-other', 'owner', 'chat')")
        self.assertEqual(self.line(), FIRST_CONVERSATION)

    def test_no_configured_ordubashy_tells_no_one(self):
        with mock.patch.object(service_module, "configured_ordubashy", return_value=None):
            self.assertEqual(self.line(), "")


class FirstConversationHookTests(unittest.TestCase):
    """The session hook puts the line where the model reads it: first at startup, and with each prompt."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.session = "00000000-0000-4000-8000-000000000001"
        self.registration = {"registration_id": "synthetic", "workspace": str(self.root)}
        instruction_root = (Path(__file__).resolve().parents[3]
                            / "qorqut-agentic/src/qorqut_agentic/resources/defaults/operations/runtime-instructions")
        instructions = from_catalog(lambda path: (instruction_root / path).read_text())
        (self.root / "runtime-instructions.json").write_bytes(instructions.bytes())
        self.response = {"ready": True, "effective_config_id": "config-1", "first_conversation": FIRST_CONVERSATION,
                         "mandatory_context": {"rendered": "Mandatory instructions."}}
        for target, value in (("qorqut.bootstrap.claude_session.load_registration", self.registration),
                              ("qorqut.bootstrap.native_binding.attest", None),
                              ("qorqut.bootstrap.handoff.request", None),
                              ("qorqut.bootstrap.recovery.read", None)):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def deliver(self, runtime, event, **fields):
        client = SimpleNamespace(post=lambda *_args: self.response)
        output, _, _ = handle_hook({"hook_event_name": event, "session_id": self.session, **fields},
                                   self.root / "registration.json", runtime=runtime, client_factory=lambda _: client)
        return output.get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_startup_context_opens_with_the_line(self):
        for runtime in ("claude-code", "codex"):
            with self.subTest(runtime=runtime):
                self.assertTrue(self.deliver(runtime, "SessionStart", source="startup").startswith(FIRST_CONVERSATION))

    def test_each_prompt_carries_the_line(self):
        receipt = self.root / ".qorqut/delivery" / (hashlib.sha256(self.session.encode()).hexdigest() + ".json")
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({**self.registration, "effective_config_id": "config-1"}))
        self.response.pop("mandatory_context")
        for runtime in ("claude-code", "codex"):
            with self.subTest(runtime=runtime):
                self.assertTrue(self.deliver(runtime, "UserPromptSubmit").startswith(FIRST_CONVERSATION))

    def test_without_the_line_nothing_is_added(self):
        self.response.pop("first_conversation")
        self.assertNotIn("FIRST CONVERSATION", self.deliver("claude-code", "SessionStart", source="startup"))


if __name__ == "__main__":
    unittest.main()
