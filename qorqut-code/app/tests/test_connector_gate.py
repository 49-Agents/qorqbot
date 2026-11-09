"""A Claude connector call that changes anything needs the owner's exact, single-use approval."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut import connector_gate as gate
from qorqut.bootstrap import activity_hook as hook

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
TOOL = "mcp__claude_ai_Gmail__create_draft"


def answered(identifier="a", selected=("approve",), by="owner", at=NOW - timedelta(minutes=5)):
    return {"id": identifier, "state": "answered",
            "decision_json": {"kind": "respond", "by": by, "at": at.isoformat(), "selected_choices": list(selected)}}


class ClassifyTest(unittest.TestCase):
    def test_plain_reads_pass(self):
        for name in ("get_message", "listLabels", "search_threads", "read_file", "get_draft", "list_drafts"):
            with self.subTest(name=name):
                self.assertTrue(hook.connector_is_read("mcp__claude_ai_Gmail__" + name))

    def test_changes_and_unknown_names_are_gated(self):
        for name in ("send_email", "create_draft", "get_or_create_label", "listAndDelete", "post_message", "do_thing",
                     "authenticate", ""):
            with self.subTest(name=name):
                self.assertFalse(hook.connector_is_read("mcp__claude_ai_Gmail__" + name))


class HookTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.registration = Path(folder.name) / "registration.json"
        self.registration.write_text(json.dumps({"registration_id": "reg"}))
        self.event = {"tool_name": TOOL, "tool_input": {"to": "a"}, "session_id": "s"}

    def decision(self, output):
        return output["hookSpecificOutput"]["permissionDecision"], output["hookSpecificOutput"]["permissionDecisionReason"]

    def test_reads_never_reach_the_service(self):
        post = mock.Mock()
        self.assertIsNone(hook.connector_gate({**self.event, "tool_name": "mcp__claude_ai_Gmail__get_message"}, self.registration, post))
        post.assert_not_called()

    def test_other_tools_are_ignored(self):
        post = mock.Mock()
        self.assertIsNone(hook.connector_gate({**self.event, "tool_name": "Bash"}, self.registration, post))
        post.assert_not_called()

    def test_allow_continues_and_denial_passes_the_reason(self):
        self.assertIsNone(hook.connector_gate(self.event, self.registration, lambda *_a, **_k: {"decision": "allow"}))
        output = hook.connector_gate(self.event, self.registration, lambda *_a, **_k: {"decision": "deny", "reason": "Needs approval"})
        self.assertEqual(self.decision(output), ("deny", "Needs approval"))

    def test_every_failure_refuses_the_call(self):
        def broken(*_a, **_k):
            raise OSError("down")
        for post, event, path in ((broken, self.event, self.registration),
                                  (lambda *_a, **_k: {}, self.event, self.registration),
                                  (mock.Mock(), {"tool_name": TOOL}, self.registration),
                                  (mock.Mock(), self.event, self.registration.parent / "missing.json")):
            with self.subTest(post=post, event=event):
                self.assertEqual(self.decision(hook.connector_gate(event, path, post))[0], "deny")

    def test_the_real_post_reaches_the_gate_route(self):
        workspace = self.registration.parent.resolve()
        (workspace / ".qorqut").mkdir()
        credential = workspace / ".qorqut/credential"
        credential.write_text("t" * 40)
        credential.chmod(0o600)
        self.registration.write_text(json.dumps({"registration_id": "reg", "service_url": "http://127.0.0.1:1050/mcp",
                                                 "workspace": str(workspace)}))
        reply = mock.MagicMock()
        reply.__enter__.return_value.read.return_value = json.dumps({"status": "ok", "data": {"decision": "allow"}}).encode()
        opener = mock.Mock()
        opener.open.return_value = reply
        with mock.patch("urllib.request.build_opener", return_value=opener):
            self.assertIsNone(hook.connector_gate(self.event, self.registration))
        self.assertEqual(opener.open.call_args.args[0].full_url, "http://127.0.0.1:1050/api/agent/connector-gate")

    def test_the_gate_answers_before_the_hook_times_out(self):
        source = (Path(hook.__file__).parent / "preparation.py").read_text()
        self.assertIn("'matcher': 'mcp__claude_ai_.*'", source)
        self.assertIn("--connector-gate', 'timeout': 30", source)
        self.assertLess(hook.CONNECTOR_TIMEOUT, 30)


class DecideTest(unittest.TestCase):
    def outcome(self, rows, used=()):
        return gate.decide(rows, "owner", set(used), NOW)[0]

    def test_no_request_yet(self):
        self.assertEqual(self.outcome([]), "none")

    def test_pending_waits(self):
        self.assertEqual(self.outcome([{"id": "a", "state": "pending", "decision_json": None}]), "pending")

    def test_only_an_owner_approve_opens_the_gate(self):
        self.assertEqual(self.outcome([answered()]), "approved")
        self.assertEqual(self.outcome([answered(selected=("decline",))]), "declined")
        self.assertEqual(self.outcome([answered(selected=())]), "declined")
        self.assertEqual(self.outcome([answered(by="agent")]), "none")

    def test_an_approval_opens_once(self):
        self.assertEqual(self.outcome([answered()], used=["a"]), "none")

    def test_an_approval_expires(self):
        self.assertEqual(self.outcome([answered(at=NOW - gate.APPROVAL_LIFETIME - timedelta(seconds=1))]), "none")

    def test_cancelled_requests_do_not_count(self):
        self.assertEqual(self.outcome([{"id": "a", "state": "cancelled", "decision_json": None}]), "none")

    def test_the_newest_unused_answer_wins(self):
        self.assertEqual(self.outcome([answered("new", selected=("decline",)), answered("old")]), "declined")


class ReadableTest(unittest.TestCase):
    def test_labels_put_the_message_fields_first(self):
        self.assertEqual(gate.readable({"body": "hi", "extra_flag": True, "to": ["a@b.c", "d@e.f"], "subject": "S"}),
                         "To: a@b.c, d@e.f\nSubject: S\nBody: hi\nExtra flag: true")

    def test_a_value_cannot_pretend_to_be_another_field(self):
        self.assertEqual(gate.readable({"body": "hi\nTo: fake@x.y", "to": "real@x.y"}), "To: real@x.y\nBody: hi\n    To: fake@x.y")

    def test_a_comma_in_a_name_keeps_the_list_unambiguous(self):
        self.assertEqual(gate.readable({"to": ["Doe, Jo", "x@y.z"]}), 'To: ["Doe, Jo", "x@y.z"]')

    def test_empty_input(self):
        self.assertEqual(gate.readable({}), "(no details)")


class CheckTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript("""CREATE TABLE installation (id TEXT, owner_principal_id TEXT);
            INSERT INTO installation VALUES ('service', 'owner');
            CREATE TABLE signoffs (id TEXT, state TEXT, proposer_principal_id TEXT, request_key TEXT, category TEXT,
                superseded_by_id TEXT, created_at TEXT, decision_json TEXT);""")
        self.addCleanup(connection.close)
        self.connection = connection
        class Read:
            def __enter__(self):
                return SimpleNamespace(connection=connection)
            def __exit__(self, *_):
                return False
        self.created = []
        def invoke(_context, name, args, _options):
            self.created.append(args)
            key = args["request_key"]
            connection.execute("INSERT INTO signoffs VALUES (?,?,?,?,?,?,?,?)",
                               (f"s{len(self.created)}", "pending", "agent", key, "input_request", None, str(len(self.created)), None))
            return SimpleNamespace(status="ok", data={"signoff": {"id": f"s{len(self.created)}"}})
        self.runtime = SimpleNamespace(database=SimpleNamespace(read=Read), paths=SimpleNamespace(runtime=folder.name),
                                       application=SimpleNamespace(invoke=invoke))
        self.context = SimpleNamespace(principal_id="agent")

    def check(self, tool_input=None):
        return gate.check(self.runtime, self.context, TOOL, {"to": "a@b.c", "body": "hello"} if tool_input is None else tool_input)

    def answer(self, identifier, selected):
        decision = json.dumps({"kind": "respond", "by": "owner", "at": datetime.now(timezone.utc).isoformat(), "selected_choices": selected})
        self.connection.execute("UPDATE signoffs SET state='answered', decision_json=? WHERE id=?", (decision, identifier))

    def test_full_cycle_opens_exactly_once(self):
        self.assertEqual(self.check()["decision"], "deny")
        self.assertEqual(len(self.created), 1)
        form = self.created[0]["form"]
        self.assertEqual(form["response_policy"], "owner_only")
        self.assertIn("a@b.c", form["explanation"])
        self.assertIn("hello", form["explanation"])
        self.assertEqual(self.check()["decision"], "deny")
        self.assertEqual(len(self.created), 1, "a repeat while waiting must not ask twice")
        self.answer("s1", ["approve"])
        self.assertEqual(self.check(), {"decision": "allow"})
        self.assertEqual(self.check()["decision"], "deny")
        self.assertEqual(len(self.created), 2, "sending the same thing again needs a new approval")

    def test_an_approval_does_not_cover_changed_content(self):
        self.check()
        self.answer("s1", ["approve"])
        self.assertEqual(self.check({"to": "someone@else.c", "body": "hello"})["decision"], "deny")
        self.assertEqual(len(self.created), 2)

    def test_an_approval_does_not_cover_another_agent(self):
        self.check()
        self.answer("s1", ["approve"])
        self.context.principal_id = "other"
        self.assertEqual(self.check()["decision"], "deny")

    def test_a_decline_blocks_without_asking_again(self):
        self.check()
        self.answer("s1", ["decline"])
        result = self.check()
        self.assertIn("did not approve", result["reason"])
        self.assertEqual(len(self.created), 1)

    def test_content_too_long_to_review_is_blocked(self):
        result = self.check({"body": "x" * (gate.MAX_SHOWN + 1)})
        self.assertIn("too long", result["reason"])
        self.assertEqual(self.created, [])

    def test_a_failed_request_is_still_a_denial(self):
        self.runtime.application.invoke = lambda *_a: SimpleNamespace(status="error", data=None)
        self.assertEqual(self.check()["decision"], "deny")

    def test_only_claude_connector_calls_are_accepted(self):
        for tool in ("Bash", "mcp__qorqut__tasks_list", None):
            with self.subTest(tool=tool), self.assertRaises(gate.DomainError):
                gate.check(self.runtime, self.context, tool, {})
