"""Hooks of extensions delivered at their four points (start, owner_message, after_tool, stop) for both CLIs, each
delivery recorded with its extension and entry, and every point unchanged when no extension is in effect."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest import mock
from uuid import uuid4

from qorqut import extension_hooks as hooks
from qorqut.bootstrap.claude_session import CLAUDE_HOOK_LIMIT, handle_hook
from qorqut.composition.extension_deliveries import SCHEMA
from qorqut.native_chat.arbiter import InputArbiter
from qorqut.native_chat.contracts import TYPE_DIRECT, TYPE_NUDGE, InputRequest
from qorqut.transport import http, mcp
from qorqut_agentic.behavior.seeds import runtime_defaults

from test_checkpoint_transaction_cost import CheckpointTests
from test_extension_hooks import FakeRegistry

FRAME = "From the {} extension:\n{}"
RUNTIMES = ("claude-code", "codex")


def plugin(*entries, texts=None):
    """A plugin part as a reader: ``hooks.yaml`` from ``entries`` and each text file."""
    files = {"hooks.yaml": "".join(entries).encode()}
    files.update({path: text.encode() for path, text in (texts or {}).items()})
    return files.get


def connection():
    database = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    database.row_factory = sqlite3.Row
    for statement in SCHEMA:
        database.execute(statement)
    return database


def rows(database):
    return [dict(row) for row in database.execute(
        "SELECT agent_id,extension_id,entry_id,point,place,session_id,turn_id,request_id,text_sha256 "
        "FROM extension_deliveries ORDER BY rowid")]


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class StartHook(unittest.TestCase):
    """The start hook (``claude_session.handle_hook``) with a bootstrap answer that carries or lacks ``extension_hooks``."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.session = str(uuid4())
        self.registration = {"registration_id": "synthetic", "workspace": str(self.root)}
        self.instructions = runtime_defaults()
        (self.root / "runtime-instructions.json").write_bytes(self.instructions.bytes())
        self.response = {"ready": True, "effective_config_id": "config-1",
                         "mandatory_context": {"rendered": "Mandatory instructions."}}
        for target, value in (("qorqut.bootstrap.claude_session.load_registration", self.registration),
                              ("qorqut.bootstrap.native_binding.attest", None),
                              ("qorqut.bootstrap.handoff.request", None),
                              ("qorqut.bootstrap.recovery.read", None)):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def cached(self):
        path = self.root / ".qorqut/delivery" / (hashlib.sha256(self.session.encode()).hexdigest() + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**self.registration, "effective_config_id": "config-1"}))

    def deliver(self, runtime, event="UserPromptSubmit", response=None, **fields):
        client = NS(post=lambda *_: response or self.response)
        output, receipt, client = handle_hook({"hook_event_name": event, "session_id": self.session, **fields},
                                              self.root / "registration.json", runtime=runtime,
                                              client_factory=lambda _: client)
        if receipt:
            receipt.pop("emitted_at")  # the time of the call, not part of what is delivered
        return output, receipt, getattr(client, "extension_deliveries", None)

    def hooked(self, point, *texts):
        return {**self.response, "extension_hooks": {point: [
            {"extension_id": "network", "entry_id": f"entry-{index}", "name": "Network", "text": text}
            for index, text in enumerate(texts)]}}

    def text(self, output):
        return output["hookSpecificOutput"]["additionalContext"]

    def test_with_no_extension_hooks_the_output_is_as_before_and_the_prompt_is_not_read(self):
        for runtime in RUNTIMES:
            for event, fields in (("SessionStart", {"source": "startup"}), ("UserPromptSubmit", {"prompt": "Hello."})):
                with self.subTest(runtime=runtime, event=event), \
                        mock.patch("qorqut.bootstrap.claude_session.message_turn", side_effect=AssertionError):
                    before = self.deliver(runtime, event, **fields)
                    self.assertIsNone(before[2])
                    self.assertEqual(self.deliver(runtime, event, {**self.response, "extension_hooks": {}}, **fields), before)
        # Owner-message texts change nothing on a start.
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                before = self.deliver(runtime, "SessionStart", source="startup")
                self.assertEqual(self.deliver(runtime, "SessionStart", self.hooked("owner_message", "Unused."),
                                              source="startup"), before)

    def test_start_texts_follow_the_start_context_on_both_clis(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                plain, receipt, _ = self.deliver(runtime, "SessionStart", source="startup")
                output, hooked_receipt, delivered = self.deliver(runtime, "SessionStart",
                                                                 self.hooked("start", "Mention people by name."),
                                                                 source="startup")
                self.assertEqual(self.text(output), self.text(plain) + "\n\n" + FRAME.format("Network", "Mention people by name."))
                self.assertEqual(hooked_receipt["bundle_hash"], receipt["bundle_hash"])
                self.assertEqual(delivered, [("start", [{"extension_id": "network", "entry_id": "entry-0", "place": "shown",
                                                         "text_sha256": digest("Mention people by name.")}])])

    def test_owner_message_texts_on_a_message_turn_of_both_clis(self):
        self.cached()
        self.response.pop("mandatory_context")
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                plain = self.text(self.deliver(runtime, prompt="Please update the CRM.")[0])
                output, _, delivered = self.deliver(runtime, response=self.hooked("owner_message", "Note the people."),
                                                    prompt="Please update the CRM.")
                self.assertEqual(self.text(output), plain + "\n\n" + FRAME.format("Network", "Note the people."))
                self.assertEqual(delivered[0][0], "owner_message")

    def test_owner_message_texts_are_not_given_on_a_turn_no_message_started(self):
        self.cached()
        self.response.pop("mandatory_context")
        continuation = "Finish the records before you stop."
        receipt = self.root / ".qorqut/activity" / (self.session + ".checkpoint.json")
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({"continuation": continuation}))
        receipt.chmod(0o600)
        nudge = InputArbiter._typed(InputRequest("worker", TYPE_NUDGE, "k", "x").origin, "f" * 32, TYPE_NUDGE,
                                    "You have new inbox events.")
        for runtime in RUNTIMES:
            for prompt in (continuation, nudge, "<task-notification>done</task-notification>", "/role"):
                with self.subTest(runtime=runtime, prompt=prompt[:20]):
                    plain = self.deliver(runtime, prompt=prompt)
                    output = self.deliver(runtime, response=self.hooked("owner_message", "Note the people."), prompt=prompt)
                    self.assertEqual(output[:2], plain[:2])
                    self.assertIsNone(output[2])

    def test_ordubashys_message_nudge_gets_no_owner_message_texts(self):
        from qorqut.native_chat.projection import marker_text
        self.cached()
        self.response.pop("mandatory_context")
        typed = marker_text("message-" + "a" * 32, TYPE_NUDGE, "You have new inbox events.")
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                plain = self.deliver(runtime, prompt=typed)
                output = self.deliver(runtime, response=self.hooked("owner_message", "Note the people."), prompt=typed)
                self.assertEqual(output[:2], plain[:2])
                self.assertIsNone(output[2])

    def test_a_terminal_control_direct_message_is_typed_plain_so_the_hook_takes_it_for_the_owners(self):
        # Ordubashy's terminal control types a direct message as it is (no system marker): the hook cannot tell it apart.
        self.cached()
        self.response.pop("mandatory_context")
        text = "Please send the summary to the owner."
        typed = InputArbiter._typed(InputRequest("worker", TYPE_DIRECT, "k", text).origin, None, TYPE_DIRECT, text)
        self.assertEqual(typed, text)
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                _, _, delivered = self.deliver(runtime, response=self.hooked("owner_message", "Note the people."), prompt=typed)
                self.assertEqual(delivered[0][1][0]["place"], "shown")

    def test_texts_that_do_not_fit_go_to_the_rest_file_and_the_pointer_is_shown(self):
        self.cached()
        self.response.pop("mandatory_context")
        long = "word " * (CLAUDE_HOOK_LIMIT // 5)
        output, _, delivered = self.deliver("claude-code", response=self.hooked("owner_message", "Short.", long.strip()),
                                            prompt="Hello.")
        text = self.text(output)
        rest = self.root / ".qorqut/extension-notes-owner_message.md"
        self.assertLessEqual(len(text), CLAUDE_HOOK_LIMIT)
        self.assertTrue(text.endswith(self.instructions.render("extension-hook-rest", FILE=".qorqut/extension-notes-owner_message.md")))
        self.assertIn(FRAME.format("Network", "Short."), text)
        self.assertEqual(rest.read_text(), FRAME.format("Network", long.strip()))
        self.assertEqual([item["place"] for item in delivered[0][1]], ["shown", "rest_file"])


class DeliveryRoute(unittest.TestCase):
    """What the server adds to the bootstrap answer and how it records the start hook's deliveries."""

    def setUp(self):
        self.database = connection()
        self.registry = FakeRegistry({"network": plugin(
            "- id: hello\n  at: start\n  text: instructions/a.md\n", "- id: people\n  at: owner_message\n  text: instructions/a.md\n",
            texts={"instructions/a.md": "Mention people by name.\n"})}, {"network": "Network"})

        @contextmanager
        def transaction(operation=None):
            yield NS(connection=self.database)
        self.api = object.__new__(http.HTTPAPI)
        self.opened = []
        self.api.runtime = NS(extension_registry=self.registry, database=NS(transaction=transaction),
                              application=NS(ensure_writes_open=self.opened.append),
                              require_authority=lambda: None, agent_context=lambda principal, **options: None)
        self.api.agent_routes = {}
        self.item = {"extension_id": "network", "entry_id": "hello", "place": "shown",
                     "text_sha256": digest("Mention people by name.")}

    def route(self, body, registration="reg-1"):
        """The route through ``agent_route``, as the agent's hook calls it, with the credential bound to ``reg-1``."""
        from starlette.requests import Request
        self.api.authentication = NS(agent=lambda header: NS(principal=NS(id="agent-1"), registration_id=registration))
        data = json.dumps(body).encode()
        scope = {"type": "http", "method": "POST", "path": "/api/agent/extension-deliveries", "query_string": b"",
                 "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode()),
                             (b"authorization", b"Bearer x")]}
        async def receive():
            return {"type": "http.request", "body": data, "more_body": False}
        async def call():
            return await self.api.agent_route(Request(scope, receive), ["agent", "extension-deliveries"])
        import asyncio
        return asyncio.run(call())

    def test_the_route_records_for_its_own_registration_only(self):
        body = {"registration_id": "reg-1", "session_id": "s-1", "point": "start", "deliveries": [self.item]}
        response = self.route(body)
        self.assertEqual(json.loads(response.body)["data"], {"recorded": 1})
        self.assertEqual(len(self.opened), 1)  # writes are checked open first
        from qorqut_operations.contracts import DomainError
        with self.assertRaises(DomainError) as refused:
            self.route({**body, "registration_id": "reg-2"})
        self.assertEqual(refused.exception.code, "forbidden")
        self.assertEqual(len(rows(self.database)), 1)

    def test_malformed_or_repeated_items_are_validation_errors(self):
        from qorqut_operations.contracts import DomainError
        actor = NS(principal=NS(id="agent-1"))
        base = {"session_id": "s-1", "point": "start", "deliveries": [self.item]}
        for body in ({**base, "deliveries": [{**self.item, "extension_id": ["network"]}]},
                     {**base, "deliveries": [{**self.item, "entry_id": {"a": 1}}]},
                     {**base, "session_id": ["s-1"]}, {**base, "session_id": {"a": 1}},
                     {**base, "deliveries": [self.item, self.item]}, {**base, "deliveries": []}):
            with self.subTest(body=body), self.assertRaises(DomainError) as error:
                self.api._extension_deliveries(actor, body)
            self.assertEqual(error.exception.code, "validation_error")
        self.assertEqual(rows(self.database), [])

    def test_no_registry_or_no_extension_adds_nothing(self):
        self.api.runtime.extension_registry = None
        self.assertEqual(self.api._extension_hook_entries("agent-1"), {})
        self.api.runtime.extension_registry = empty = FakeRegistry({})
        self.assertEqual(self.api._extension_hook_entries("agent-1"), {})
        self.assertEqual(empty.calls, [])  # none found at start: nothing is read

    def test_the_answer_carries_the_entries_of_both_points(self):
        value = self.api._extension_hook_entries("agent-1")
        self.assertEqual(value, {
            "start": [{"extension_id": "network", "entry_id": "hello", "name": "Network", "text": "Mention people by name."}],
            "owner_message": [{"extension_id": "network", "entry_id": "people", "name": "Network",
                               "text": "Mention people by name."}]})

    def test_only_current_entries_with_their_digest_are_recorded(self):
        actor = NS(principal=NS(id="agent-1"))
        item = {"extension_id": "network", "entry_id": "hello", "place": "shown", "text_sha256": digest("Mention people by name.")}
        stale = {**item, "text_sha256": digest("Old text.")}
        unknown = {**item, "entry_id": "people"}  # at owner_message, not start
        for other in (stale, unknown):  # neither is a current entry at start with its digest
            self.assertEqual(self.api._extension_deliveries(actor, {"session_id": "s-1", "point": "start",
                                                                    "deliveries": [other]}), {"recorded": 0})
        value = self.api._extension_deliveries(actor, {"session_id": "s-1", "point": "start", "deliveries": [item]})
        self.assertEqual(value, {"recorded": 1})
        self.assertEqual(rows(self.database), [{"agent_id": "agent-1", "extension_id": "network", "entry_id": "hello",
                                                "point": "start", "place": "shown", "session_id": "s-1", "turn_id": None,
                                                "request_id": None, "text_sha256": item["text_sha256"]}])
        with self.assertRaises(Exception):
            self.api._extension_deliveries(actor, {"session_id": "s-1", "point": "stop", "deliveries": [item]})


class Caller(mcp.RegisteredAgentCaller):
    def __init__(self):
        pass


class AfterTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.database = connection()

        @contextmanager
        def transaction(operation=None):
            yield NS(connection=self.database)
        self.runtime = NS(extension_registry=None, database=NS(transaction=transaction), paths=None,
                          application=NS(ensure_writes_open=lambda uow: None),
                          resolver=NS(require_agentic_view=lambda: NS(runtime_instructions=runtime_defaults)))
        self.transport = mcp.MCPTransport(self.runtime, None, None, caller=Caller())
        self.session = str(uuid4())
        context = NS(request=NS(headers=NS(getlist=lambda name: [self.session])), meta=None)
        for patcher in (mock.patch.object(type(self.transport.server), "request_context",
                                          new_callable=mock.PropertyMock, return_value=context),
                        mock.patch("qorqut.composition.locations.agent_workspace_of", return_value=self.workspace)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.actor = NS(principal=NS(id="agent-1"))
        self.value = {"status": "ok", "data": {"entry": {"id": "e-1"}}, "request_id": "r-1"}

    def notes(self, name="crm_entries_update", value=None):
        return self.transport._extension_notes(name, name, value or self.value, self.actor, "request-1")

    async def test_with_no_extension_the_result_is_the_same_object(self):
        self.assertIs(await self.notes(), self.value)
        self.runtime.extension_registry = empty = FakeRegistry({})
        self.assertIs(await self.notes(), self.value)
        self.assertEqual(empty.calls, [])  # none found at start: nothing is read
        self.runtime.extension_registry = idle = FakeRegistry({"network": plugin("")})
        idle.refused.append("network")  # found, in effect for nobody: one registry read
        self.assertIs(await self.notes(), self.value)
        self.assertEqual(idle.calls, [("in_effect", "agent-1")])
        self.assertEqual(rows(self.database), [])

    async def test_the_note_sits_beside_the_unchanged_result_and_is_recorded(self):
        self.runtime.extension_registry = FakeRegistry({"network": plugin(
            "- id: after\n  at: after_tool\n  tools: [crm_entries_update]\n  text: instructions/a.md\n", texts={"instructions/a.md": "Check it.\n"})},
            {"network": "Network"})
        noted = await self.notes()
        self.assertEqual({key: noted[key] for key in self.value}, self.value)
        self.assertEqual(noted[hooks.NOTES_KEY], FRAME.format("Network", "Check it."))
        self.assertIs(await self.notes("tasks_get"), self.value)  # a tool the entry does not name
        self.assertEqual([(row["entry_id"], row["point"], row["request_id"], row["session_id"]) for row in rows(self.database)],
                         [("after", "after_tool", "request-1", self.session)])

    async def test_a_condition_is_judged_from_the_turns_used_tools(self):
        self.runtime.extension_registry = FakeRegistry({"network": plugin(
            "- id: after\n  at: after_tool\n  tools: [crm_entries_update]\n  unless_used: [network_follow_up_set]\n"
            "  text: instructions/a.md\n", texts={"instructions/a.md": "Check it.\n"})})
        for used, noted in ((["network_follow_up_set"], False), ([], True)):
            with self.subTest(used=used), mock.patch("qorqut.agents.checkpoints.native_checkpoint",
                                                     return_value={"used": used}):
                self.assertEqual(hooks.NOTES_KEY in await self.notes(), noted)

    async def test_the_call_itself_counts_as_used(self):
        self.runtime.extension_registry = FakeRegistry({"network": plugin(
            "- id: after\n  at: after_tool\n  tools: [crm_entries_update]\n  if_used: [crm_entries_update]\n"
            "  text: instructions/a.md\n", texts={"instructions/a.md": "Check it.\n"})})
        with mock.patch("qorqut.agents.checkpoints.native_checkpoint", return_value={"used": []}):
            self.assertIn(hooks.NOTES_KEY, await self.notes())

    async def test_a_note_that_does_not_fit_the_room_goes_to_the_rest_file(self):
        self.runtime.extension_registry = FakeRegistry({"network": plugin(
            "- id: after\n  at: after_tool\n  tools: [crm_entries_update]\n  text: instructions/a.md\n", texts={"instructions/a.md": "Check it.\n"})})
        big = {**self.value, "data": {"text": "x" * (mcp.mcp_presentation.RESULT_CHARACTER_BUDGET - 100)}}
        noted = await self.notes(value=big)
        self.assertEqual(noted["data"], big["data"])
        self.assertIn(".qorqut/extension-notes-after_tool.md", noted[hooks.NOTES_KEY])
        self.assertEqual((self.workspace / ".qorqut/extension-notes-after_tool.md").read_text(),
                         FRAME.format("Network", "Check it."))
        self.assertEqual([row["place"] for row in rows(self.database)], ["rest_file"])

    async def test_a_failed_result_gets_no_note(self):
        failed = {"status": "error", "error": {"code": "x"}}
        self.assertEqual(hooks.tool_notes(failed, [], None, runtime_defaults(), budget=100, size=len), (failed, None))


class StopPoint(CheckpointTests):
    """The completion checkpoint with the stop entries of an extension in effect."""

    def setUp(self):
        super().setUp()
        defaults = runtime_defaults()
        # Qorqbot's own messages render as their keys, as in the parent fixture; the extension templates are the real ones.
        self.instructions = NS(render=lambda key, **values: defaults.render(key, **values) if key.startswith("extension-")
                               else key)
        for statement in SCHEMA:
            self.connection.execute(statement)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        patcher = mock.patch("qorqut.agents.checkpoints.agent_workspace_of", return_value=self.workspace)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.checkpoints.runtime.paths = None
        self.native.update(owner_turn=True, used=[])

    def stop_entry(self, text="Check the follow ups.", condition="", count=1):
        self.checkpoints.runtime.extension_registry = FakeRegistry({"network": plugin(
            *(f"- id: check{index or ''}\n  at: stop\n{condition}  text: instructions/a.md\n" for index in range(count)),
            texts={"instructions/a.md": text + "\n"})}, {"network": "Network"})

    def claim(self):
        return self.check(allow_correction=True, claim_id=str(uuid4()))

    def test_no_extension_in_effect_gives_the_same_answer(self):
        before = self.claim()
        self.connection.execute("DELETE FROM agent_checkpoint_findings")
        self.checkpoints.runtime.extension_registry = empty = FakeRegistry({})
        self.assertEqual(self.claim(), before)
        self.assertEqual(empty.calls, [])
        self.assertEqual(rows(self.connection), [])

    def test_a_message_turn_gets_the_stop_text_after_qorqbots_once(self):
        self.stop_entry()
        answer = self.claim()
        self.assertEqual(answer["correction"], self.instructions.render("completion-records") + "\n\n"
                         + FRAME.format("Network", "Check the follow ups."))
        self.assertNotIn("notes_shown", answer)
        recorded = rows(self.connection)
        self.assertEqual([(r["entry_id"], r["point"], r["place"], r["turn_id"], r["session_id"]) for r in recorded],
                         [("check", "stop", "shown", self.turn, self.session)])
        self.assertIsNone(self.claim()["correction"])  # claimed once per root turn
        self.assertEqual(len(rows(self.connection)), 1)

    def test_no_stop_text_on_a_turn_no_message_started(self):
        self.stop_entry()
        self.native["owner_turn"] = False
        self.assertEqual(self.claim()["correction"], self.instructions.render("completion-records"))
        self.assertEqual(rows(self.connection), [])

    def test_conditions_are_judged_from_the_turns_used_tools(self):
        self.stop_entry(condition="  unless_used: [network_follow_up_set]\n")
        self.native["used"] = ["network_follow_up_set"]
        self.assertNotIn("Network", self.claim()["correction"])

    def test_a_stop_text_of_any_length_never_fails_the_claim_or_loses_qorqbots_finding(self):
        pointer = self.instructions.render("extension-hook-rest", FILE=".qorqut/extension-notes-stop.md")
        rest = self.workspace / ".qorqut/extension-notes-stop.md"
        for length in (3000, 30000):
            for own in (True, False):
                with self.subTest(length=length, own=own):
                    self.connection.execute("DELETE FROM agent_checkpoint_findings")
                    self.connection.execute("DELETE FROM extension_deliveries")
                    self.native["dirty_sequence"] = 1 if own else 0  # with or without Qorqbot's own finding
                    self.stop_entry("y" * length)
                    answer = self.claim()
                    expected = ("completion-records\n\n" if own else "") + pointer
                    self.assertEqual(answer["correction"], expected)
                    self.assertEqual(rest.read_text(), FRAME.format("Network", "y" * length))
                    stored = [row["message"] for row in self.connection.execute(
                        "SELECT message FROM agent_checkpoint_findings ORDER BY check_key")]
                    self.assertTrue(all(len(message) <= 1000 for message in stored))
                    self.assertIn(pointer, stored)
                    self.assertEqual(("completion-records" in stored), own)
                    self.assertEqual([r["place"] for r in rows(self.connection)], ["rest_file"])

    def test_a_long_text_with_a_short_one_keeps_the_short_one_shown(self):
        self.checkpoints.runtime.extension_registry = FakeRegistry({"network": plugin(
            "- id: long\n  at: stop\n  text: instructions/a.md\n", "- id: short\n  at: stop\n  text: instructions/b.md\n",
            texts={"instructions/a.md": "y" * 3000, "instructions/b.md": "Check it."})}, {"network": "Network"})
        answer = self.claim()
        pointer = self.instructions.render("extension-hook-rest", FILE=".qorqut/extension-notes-stop.md")
        self.assertEqual(answer["correction"], "completion-records\n\n" + FRAME.format("Network", "Check it.") + "\n\n" + pointer)
        self.assertEqual(sorted((r["entry_id"], r["place"]) for r in rows(self.connection)),
                         [("long", "rest_file"), ("short", "shown")])

    def test_a_stop_text_that_does_not_fit_goes_to_the_rest_file(self):
        self.stop_entry("y" * 900, count=3)
        answer = self.claim()
        self.assertNotIn("correction_rest", answer)
        self.assertIn(".qorqut/extension-notes-stop.md", answer["correction"])
        self.assertEqual((self.workspace / ".qorqut/extension-notes-stop.md").read_text(), FRAME.format("Network", "y" * 900))
        self.assertEqual([r["place"] for r in rows(self.connection)], ["shown", "shown", "rest_file"])


class OrdubashysMessage(unittest.TestCase):
    """A worker's turn started by Ordubashy's ``agents_message_send`` is a message turn: the nudge that carries it is typed
    behind a marker the delivery sets. Reminders, other inbox events and timed nudges are not."""

    def test_the_marker_the_delivery_sets(self):
        from qorqut.bootstrap.activity_hook import owner_prompt
        from qorqut.native_chat.arbiter import system_marker
        from qorqut.native_chat.projection import marker_text, leading_marker
        message = system_marker(InputRequest("worker", TYPE_NUDGE, "k", "x", metadata={"message_turn": True}))
        plain = system_marker(InputRequest("worker", TYPE_NUDGE, "k", "x", metadata={"nudge_kind": "initial"}))
        self.assertTrue(message.startswith("message-"))
        self.assertFalse(plain.startswith("message-"))
        self.assertIsNone(system_marker(InputRequest("worker", TYPE_DIRECT, "k", "x", metadata={"message_turn": True})))
        for marker, expected in ((message, True), (plain, False)):
            typed = marker_text(marker, TYPE_NUDGE, "You have new inbox events.")
            self.assertEqual(leading_marker(typed)[0], marker)  # Chat still reads it as a system input
            for runtime in RUNTIMES:
                with self.subTest(marker=marker[:8], runtime=runtime):
                    self.assertIs(owner_prompt(typed, {}, runtime=runtime, ordubashy=True), expected)  # stop
                    self.assertIs(owner_prompt(typed, {}, runtime=runtime), False)  # owner_message: the owner only

    def test_only_a_notice_covering_ordubashys_message_to_a_worker_carries_the_mark(self):
        from qorqut.native_chat.nudges import NudgeBackend
        database = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        database.row_factory = sqlite3.Row
        database.execute("CREATE TABLE inbox_events(id INTEGER PRIMARY KEY, source_type TEXT, actor_id TEXT)")
        database.executemany("INSERT INTO inbox_events VALUES(?,?,?)", [
            (1, "agent_messages", "ordubashy"), (2, "reminders", "ordubashy"), (3, "agent_messages", "worker-2")])

        @contextmanager
        def read():
            yield NS(connection=database)
        backend = object.__new__(NudgeBackend)
        backend.runtime = NS(database=NS(read=read))
        with mock.patch("qorqut.management.identity.configured_ordubashy", return_value="ordubashy"):
            for agent, events, expected in (("worker", (1,), True), ("worker", (2, 1), True), ("worker", (2,), False),
                                             ("worker", (3,), False), ("worker", (), False), ("ordubashy", (1,), False)):
                with self.subTest(agent=agent, events=events):
                    self.assertIs(backend._carries_message(NS(agent_id=agent, event_ids=events)), expected)


class Timed(unittest.TestCase):
    """``timed`` entries from the nudge pass: anchored when first seen in effect, due one ``every`` later, one input for
    every due entry, waking an asleep agent, nothing for an agent that is off, and again from when it came back."""

    def setUp(self):
        from datetime import datetime, timedelta, timezone
        self.timedelta = timedelta
        self.now = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = connection()
        self.database.execute("CREATE TABLE native_session_owners(agent_id TEXT, desired_state TEXT)")
        self.database.execute("CREATE TABLE native_inputs(id TEXT, agent_id TEXT, source_key TEXT, attempt INTEGER, "
                              "state TEXT, session_id TEXT, settled_at TEXT, updated_at TEXT, sequence INTEGER)")
        self.answers = []  # states the fake arbiter answers in turn; then ``submitted``

        @contextmanager
        def unit(operation=None):
            yield NS(connection=self.database)
        self.registry = FakeRegistry({"network": plugin(
            "- id: weekly\n  at: timed\n  every: 2m\n  text: instructions/a.md\n",
            "- id: daily\n  at: timed\n  every: 4m\n  text: instructions/b.md\n",
            texts={"instructions/a.md": "Look through the Connections.\n", "instructions/b.md": "Plan the day.\n"})},
            {"network": "Network"})
        self.registry.subscribed = []
        self.registry.subscribe = self.registry.subscribed.append
        self.runtime = NS(extension_registry=self.registry, database=NS(read=unit, transaction=unit), paths=None,
                          application=NS(ensure_writes_open=lambda uow: None),
                          resolver=NS(require_agentic_view=lambda: NS(runtime_instructions=runtime_defaults)))
        self.sent, self.woken, self.live = [], [], True
        self.arbiter = NS(admit_nudge=self.admit)
        self.chat = NS(probe=NS(observe=lambda profile: "observed"),
                       registry=NS(reconcile=lambda profile, observed: NS(
                           live=self.live, session={"id": "binding-1", "conversation_id": "c-1"} if self.live else None)),
                       arbiter=self.arbiter, opener=NS(wake_quietly=lambda profile, source: self.woken.append(source)))
        self.timed = hooks.TimedNudges(self.runtime, self.chat, clock=lambda: self.now)
        self.profile = {"agent_id": "agent-1"}
        for patcher in (mock.patch("qorqut.native_chat.store.waiting_inputs", return_value=[]),
                        mock.patch("qorqut.composition.locations.agent_workspace_of", return_value=Path(temporary.name))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def admit(self, agent_id, key, text, **fields):
        """As the arbiter: a repeated key gives back its stored input; a new key stores one."""
        prior = self.database.execute("SELECT * FROM native_inputs WHERE agent_id=? AND source_key=?", (agent_id, key)).fetchone()
        if prior is not None:
            return NS(state=prior["state"], input_id=prior["id"])
        self.sent.append((agent_id, key, text, fields))
        state = self.answers.pop(0) if self.answers else "submitted"
        input_id = f"input-{len(self.sent)}"
        moment = self.now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.database.execute("INSERT INTO native_inputs VALUES(?,?,?,1,?,?,?,?,?)",
                              (input_id, agent_id, key, state, fields["expected_binding"], moment, moment, len(self.sent)))
        return NS(state=state, input_id=input_id)

    def later(self, **delta):
        self.now += self.timedelta(**delta)
        return self.timed.send_due(self.profile)

    def places(self):
        return [(row["entry_id"], row["place"]) for row in rows(self.database)]

    def test_the_first_nudge_comes_one_every_after_it_came_into_effect(self):
        self.assertEqual(self.timed.send_due(self.profile), "not_due")
        self.assertEqual(self.places(), [("weekly", "in_effect"), ("daily", "in_effect")])
        self.assertEqual(self.later(seconds=119), "not_due")
        self.assertEqual(self.later(seconds=1), "submitted")
        self.assertEqual(len(self.sent), 1)
        agent, key, text, fields = self.sent[0]
        self.assertEqual(text, FRAME.format("Network", "Look through the Connections."))
        self.assertEqual(fields["expected_binding"], "binding-1")
        self.assertNotIn("message_turn", fields["metadata"])  # a timed nudge's turn is not a message turn
        self.assertEqual(self.places()[-1], ("weekly", "shown"))
        self.assertEqual(rows(self.database)[-1]["session_id"], "c-1")

    def test_all_due_entries_go_as_one_input_and_at_most_once_per_every(self):
        self.timed.send_due(self.profile)
        self.later(minutes=4)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][2], FRAME.format("Network", "Look through the Connections.") + "\n\n"
                         + FRAME.format("Network", "Plan the day."))
        self.assertEqual(self.later(seconds=30), "not_due")
        # A new process reads the anchor from the table.
        self.timed = hooks.TimedNudges(self.runtime, self.chat, clock=lambda: self.now)
        self.assertEqual(self.later(seconds=30), "not_due")
        self.assertEqual(self.later(minutes=1), "submitted")
        self.assertEqual(len(self.sent), 2)
        self.assertNotEqual(self.sent[0][1], self.sent[1][1])

    def test_an_asleep_agent_is_woken_and_an_agent_that_is_off_gets_nothing(self):
        self.timed.send_due(self.profile)
        self.live = False
        self.assertEqual(self.later(minutes=2), "waking")
        self.assertEqual((self.woken, self.sent), (["extension"], []))
        self.live = True
        self.assertEqual(self.later(seconds=3), "submitted")
        for state in ("paused", "stopped"):
            with self.subTest(state=state):
                self.database.execute("DELETE FROM native_session_owners")
                self.database.execute("INSERT INTO native_session_owners VALUES('agent-1',?)", (state,))
                self.assertEqual(self.later(minutes=5), "desired_" + state)
        self.assertEqual(len(self.sent), 1)

    def test_a_send_that_did_not_reach_the_agent_is_sent_again_and_a_delivered_one_never(self):
        for answer in ("failed", "undelivered"):
            with self.subTest(answer=answer):
                self.database.execute("DELETE FROM native_inputs")
                self.database.execute("DELETE FROM extension_deliveries")
                self.sent.clear()
                self.timed.send_due(self.profile)
                self.answers = [answer]
                self.assertEqual(self.later(minutes=2), answer)
                self.assertEqual(self.later(seconds=3), "retry_later")  # paced as an untyped inbox nudge
                self.assertEqual(self.later(seconds=27), "submitted")
                self.assertEqual(len(self.sent), 2)
                self.assertNotEqual(self.sent[0][1], self.sent[1][1])
                self.assertEqual([p for _, p in self.places()].count("shown"), 1)
                self.assertEqual(self.later(seconds=3), "not_due")
                self.assertEqual(len(self.sent), 2)

    def test_failing_sends_back_off_as_inbox_nudges_do(self):
        from qorqut_operations.inbox_nudges.state import retry_delay
        self.timed.send_due(self.profile)
        self.answers = ["failed"] * 1000
        self.later(minutes=2)
        for _ in range(200):  # ten minutes of 3-second passes
            self.later(seconds=3)
        waits = [retry_delay("failed", streak) for streak in range(1, 8)]
        self.assertEqual(waits[:5], [30, 60, 120, 240, 300])
        # Tried at 0, 30, 90, 210 and 450 s: the wait runs on across due slots, as it runs on across inbox notices.
        self.assertEqual(len(self.sent), 5)
        self.assertEqual(self.later(minutes=1), "retry_later")  # the daily entry's own slot waits on the same streak

    def test_an_input_still_in_flight_is_waited_for(self):
        self.timed.send_due(self.profile)
        self.answers = ["submitting"]
        self.assertEqual(self.later(minutes=2), "submitting")
        self.assertEqual(self.later(seconds=3), "in_flight")
        self.assertEqual(len(self.sent), 1)
        self.database.execute("UPDATE native_inputs SET state='submitted'")
        self.assertEqual(self.later(seconds=3), "submitted")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual([p for _, p in self.places()].count("shown"), 1)

    def test_a_changed_session_gets_the_send_again_under_a_new_key(self):
        self.timed.send_due(self.profile)
        self.answers = ["undelivered"]
        self.later(minutes=2)
        self.chat.registry = NS(reconcile=lambda profile, observed: NS(
            live=True, session={"id": "binding-2", "conversation_id": "c-2"}))
        self.assertEqual(self.later(seconds=3), "submitted")
        self.assertEqual([fields["expected_binding"] for *_, fields in self.sent], ["binding-1", "binding-2"])
        self.assertEqual(rows(self.database)[-1]["session_id"], "c-2")

    def test_a_submitted_input_whose_record_was_lost_is_recorded_and_not_sent_again(self):
        self.timed.send_due(self.profile)
        with mock.patch.object(hooks.TimedNudges, "_delivered", side_effect=OSError("stopped")):
            with self.assertRaises(OSError):
                self.later(minutes=2)
        self.assertEqual(self.later(seconds=3), "submitted")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual([p for _, p in self.places()].count("shown"), 1)

    def test_an_update_does_not_move_the_clock(self):
        from qorqut.extension_registry import Change
        self.timed.send_due(self.profile)
        self.now += self.timedelta(seconds=100)
        self.registry.subscribed[0]((Change("network", "plugin"), Change("network", "updated", None, ("agent-1",)),
                                     Change("network", "status", None, ())))
        self.assertEqual(self.later(seconds=20), "submitted")  # two minutes after it came into effect

    def test_going_out_sends_nothing_and_coming_back_restarts_for_each_kind_of_flip(self):
        from qorqut.extension_registry import Change
        for kind in ("status", "switch", "plugin"):
            with self.subTest(kind=kind):
                self.database.execute("DELETE FROM extension_deliveries")
                self.timed._back.clear()
                self.timed.send_due(self.profile)  # anchored now
                self.registry.refused.append("network")
                self.registry.subscribed[0]((Change("network", kind, None, ("agent-1",)),))
                self.assertEqual(self.later(minutes=5), "none")  # out of effect: nothing, however long
                self.registry.refused.remove("network")
                self.registry.subscribed[0]((Change("network", kind, None, ("agent-1",)),))
                sent = len(self.sent)
                self.assertEqual(self.later(seconds=119), "not_due")  # long past the old anchor, not past the return
                self.assertEqual(self.later(seconds=1), "submitted")
                self.assertEqual(len(self.sent), sent + 1)

    def test_back_in_effect_starts_again_one_every_later(self):
        self.timed.send_due(self.profile)
        self.later(minutes=2)
        self.assertEqual(len(self.sent), 1)
        from qorqut.extension_registry import Change
        self.now += self.timedelta(seconds=90)
        self.registry.subscribed[0]((Change("network", "switch", "agent-1", ("agent-1",)),))
        self.assertEqual(self.later(seconds=40), "not_due")  # two minutes after the first nudge, but only 40 s after return
        self.assertEqual(self.later(seconds=80), "submitted")

    def test_with_no_extension_found_the_pass_has_no_timed_part(self):
        from qorqut.native_chat.nudges import NudgeBackend
        empty = FakeRegistry({})
        backend = NudgeBackend(NS(extension_registry=empty), None, None, None, None, None)
        self.assertIsNone(backend.timed)
        self.assertEqual(empty.calls, [])
        self.assertIsNone(NudgeBackend(NS(), None, None, None, None, None).timed)


class InboxNoticeMark(unittest.TestCase):
    """The inbox nudge pass puts ``message_turn`` into the metadata only when an extension was found at start."""

    def send(self, found, carries=True):
        from qorqut.native_chat import nudges
        from qorqut_operations.inbox_nudges import Reservation
        backend = object.__new__(nudges.NudgeBackend)
        backend.extensions_found = found
        backend.runtime = NS(resolver=NS(require_agentic_view=lambda: NS(runtime_instructions=runtime_defaults)))
        backend.engine = NS(validate=lambda *a: True, cancel=lambda *a: None)
        sent = []
        backend.arbiter = NS(admit_nudge=lambda *args, **fields: sent.append(fields) or NS())
        reservation = Reservation("attempt-1", "worker", 1, 1, "initial", None, (1,))
        with mock.patch.object(nudges.NudgeBackend, "_carries_message", return_value=carries) as carried, \
                mock.patch.object(nudges, "outcome_of", return_value="stale"):
            backend._send({"id": "binding-1"}, reservation)
        return sent[0]["metadata"], carried.call_count

    def test_the_mark_only_with_an_extension_found(self):
        self.assertEqual(self.send(True), ({"nudge_attempt": "attempt-1", "nudge_kind": "initial", "message_turn": True}, 1))
        self.assertEqual(self.send(True, carries=False)[0], {"nudge_attempt": "attempt-1", "nudge_kind": "initial"})
        self.assertEqual(self.send(False), ({"nudge_attempt": "attempt-1", "nudge_kind": "initial"}, 0))  # no store read


class HookPosting(unittest.TestCase):
    """The start hook posts its deliveries after writing the output, or queues them for a prepared handoff."""

    def run_hook(self, prepared):
        from qorqut.bootstrap import claude_session
        import io
        posted, queued = [], []
        client = NS(post=lambda route, body: posted.append((route, body)), native=None, handoff=prepared,
                    extension_deliveries=[("start", [{"extension_id": "network", "entry_id": "hello", "place": "shown",
                                                      "text_sha256": "a" * 64}])])
        stream = io.StringIO()
        with mock.patch.object(claude_session, "load_registration", return_value={"registration_id": "reg-1", "workspace": "/w"}), \
                mock.patch.object(claude_session, "handle_hook", return_value=({"hookSpecificOutput": {}}, None, client)), \
                mock.patch.object(claude_session, "snapshot_transcript"), \
                mock.patch("qorqut.bootstrap.recovery.read", return_value=None), \
                mock.patch("qorqut.bootstrap.handoff.consume"), \
                mock.patch("qorqut.installation.installation_config", return_value={"root": "/r"}), \
                mock.patch("qorqut.bootstrap.handoff.queue", side_effect=lambda *args: queued.append(args)):
            claude_session.run({"hook_event_name": "SessionStart", "session_id": "s-1"}, Path("/w/registration.json"),
                               stream=stream, diagnostics=io.StringIO())
        self.assertTrue(stream.getvalue())  # the output came first
        return posted, queued

    def test_posted_without_a_handoff(self):
        posted, queued = self.run_hook(None)
        self.assertEqual(queued, [])
        self.assertEqual(posted, [("/api/agent/extension-deliveries", {
            "registration_id": "reg-1", "session_id": "s-1", "point": "start",
            "deliveries": [{"extension_id": "network", "entry_id": "hello", "place": "shown", "text_sha256": "a" * 64}]})])

    def test_queued_with_a_prepared_handoff(self):
        posted, queued = self.run_hook({"session_id": "s-1"})
        self.assertEqual(posted, [])
        self.assertEqual([(args[2], args[3]["point"]) for args in queued], [("/api/agent/extension-deliveries", "start")])


del CheckpointTests  # its own tests run from their own file

if __name__ == "__main__":
    unittest.main()
