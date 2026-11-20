"""The managed Chat routes (``/api/root/agents/<uuid>/chat/...``), through the real ``HTTPAPI`` dispatch and the real
``native_chat.http.route`` with a recording stand-in for the Chat facade: request shapes, owner-only access, which fields are
required, what each route hands the facade, what comes back and how refusals are reported. The change stream (SSE) runs the real
``_events`` generator over the real facade. Nothing here starts a host or a CLI. Needs the service extras (Starlette, anyio); without
them the module is skipped."""
import isolated_cli_environment  # before any Qorqbot import

from contextlib import contextmanager
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError

try:
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    SERVICE = True
except ImportError:  # the service extras are not installed
    SERVICE = False

import test_managed_chat_send as base

OWNER = "owner-principal"
AGENT = "2f1c8a9e-5b1d-4a3e-9c1e-7d1f5a6b8c90"
PIN = "3f6c1d2e-8f40-4a53-b6a1-0d7a5f3c9e11"
CHAT = f"/api/root/agents/{AGENT}/chat"
JSON = {"Content-Type": "application/json"}


class FakeChat:
    """Records what a route hands the facade and answers with what the test sets."""

    def __init__(self):
        self.calls = []
        self.reply = {}
        self.error = None

    def _call(self, name, *args, **options):
        self.calls.append((name, args, options))
        if self.error is not None:
            raise self.error
        return self.reply.get(name, {"called": name})

    def send(self, *args, **options):
        return self._call("send", *args, **options)

    def interrupt(self, *args):
        return self._call("interrupt", *args)

    def answer(self, *args):
        return self._call("answer", *args)

    def close(self, *args):
        return self._call("close", *args)

    def check_events(self, principal_id, agent_id):
        self.calls.append(("check_events", (principal_id, agent_id), {}))
        if self.error is not None:
            raise self.error


@unittest.skipUnless(SERVICE, "Starlette is not installed")
class RouteTest(unittest.TestCase):
    def setUp(self):
        from qorqut.transport.http import HTTPAPI
        self.kind, self.principal = "human", OWNER
        self.chat = FakeChat()

        @contextmanager
        def read():
            repository = SimpleNamespace(get=lambda name: {"owner_principal_id": OWNER})
            yield SimpleNamespace(repository=lambda name, **options: repository)

        api = HTTPAPI.__new__(HTTPAPI)
        api.runtime = SimpleNamespace(database=SimpleNamespace(read=read), require_authority=lambda: None, native_chat=self.chat,
                                      paths=SimpleNamespace(runtime="/nonexistent"))
        api.authentication = SimpleNamespace(installation_owner=lambda: (SimpleNamespace(id=OWNER, display_name="Owner"),))
        api.origins = SimpleNamespace(validate=lambda request, mutation=False: None)
        api.statuses, api.owner_routes = {}, {}
        api.route_counters = SimpleNamespace(record=lambda *args, **options: None)
        api._report_transport_success = lambda *args: None
        api._report_transport_failure = lambda *args: None
        api.context = lambda actor, slug=None, owner=False: (
            SimpleNamespace(principal_kind=self.kind, principal_id=self.principal), None)

        async def handler(request):
            return await api(request)
        self.client = TestClient(Starlette(routes=[Route("/api/{path:path}", handler, methods=["GET", "POST"])]))
        self.addCleanup(self.client.close)

    def post(self, tail, body, headers=None, agent=AGENT):
        return self.client.post(f"/api/root/agents/{agent}/chat/{tail}",
                                content=body if isinstance(body, str) else json.dumps(body), headers={**JSON, **(headers or {})})

    def names(self):
        return [name for name, _, _ in self.chat.calls]

    # ----- who may call ------------------------------------------------------------------------------------------------

    def test_every_managed_route_refuses_an_authorization_header_before_anything_runs(self):
        for tail, body in (("messages", {"text": "hi", "idempotency_key": "key-0000001"}), ("interrupt", {"expected_binding": PIN}),
                           ("answer", {"expected_binding": PIN, "request_id": "r1", "decision": "allow"}), ("close", {})):
            with self.subTest(route=tail):
                response = self.post(tail, body, headers={"Authorization": "Bearer agent-credential"})
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "forbidden"))
        self.assertEqual(self.chat.calls, [])

    def test_the_facade_is_handed_the_authenticated_principal_and_a_body_cannot_name_another(self):
        for tail, body in (("interrupt", {"expected_binding": PIN, "principal_id": "someone-else"}),
                           ("close", {"principal_id": "someone-else"}),
                           ("messages", {"text": "hi", "idempotency_key": "key-0000001", "principal_id": "someone-else"})):
            response = self.post(tail, body)
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (422, "validation_error"), tail)
        self.assertEqual(self.chat.calls, [])
        self.post("close", {})
        self.assertEqual(self.chat.calls[0][1][0], OWNER)

    def test_the_facade_repeats_the_owner_check_and_its_refusal_reaches_the_caller(self):
        self.chat.error = DomainError("forbidden", "Agent chat is for the installation owner")
        response = self.post("interrupt", {"expected_binding": PIN})
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "forbidden"))

    # ----- POST messages -----------------------------------------------------------------------------------------------

    def test_a_message_may_leave_the_pin_out_and_the_facade_decides_whether_that_is_allowed(self):
        self.chat.reply["send"] = {"input": {"id": "i1"}, "binding": {"binding_id": PIN}, "created": True}
        unpinned = self.post("messages", {"text": "hello", "idempotency_key": "key-0000001"})
        pinned = self.post("messages", {"text": "hello", "idempotency_key": "key-0000002", "expected_binding": PIN})
        self.assertEqual((unpinned.status_code, pinned.status_code), (200, 200))
        self.assertEqual(unpinned.json()["data"], self.chat.reply["send"])
        self.assertEqual(self.chat.calls, [("send", (OWNER, AGENT, "hello", "key-0000001", None), {"new_conversation": False}),
                                           ("send", (OWNER, AGENT, "hello", "key-0000002", PIN), {"new_conversation": False})])
        self.assertEqual(unpinned.headers["cache-control"], "private, no-store")

    def test_a_message_may_ask_for_a_new_conversation_and_the_facade_decides_whether_that_is_allowed(self):
        self.chat.reply["send"] = {"input": {"id": "i1"}, "binding": {"binding_id": PIN}, "created": True}
        response = self.post("messages", {"text": "hello", "idempotency_key": "key-0000003", "new_conversation": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.chat.calls, [("send", (OWNER, AGENT, "hello", "key-0000003", None), {"new_conversation": True})])

    def test_a_refused_open_reaches_the_page_with_its_code_and_whether_a_retry_is_worth_it(self):
        for code, retryable in (("resume_unavailable", False), ("open_busy", True), ("open_timeout", True)):
            with self.subTest(code=code):
                self.chat.error = DomainError(code, "plain words", retryable=retryable)
                response = self.post("messages", {"text": "hello", "idempotency_key": "key-0000004"})
                error = response.json()["error"]
                self.assertEqual((error["code"], error["message"], error["retryable"]), (code, "plain words", retryable))

    def test_a_message_without_text_or_key_or_with_another_field_is_a_validation_error(self):
        for body in ({"text": "hi"}, {"idempotency_key": "key-0000001"}, {"text": "hi", "idempotency_key": "key-0000001", "steer": True},
                     {"text": "hi", "idempotency_key": "key-0000001", "binding": PIN}):
            with self.subTest(body=body):
                response = self.post("messages", body)
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (422, "validation_error"))
        self.assertEqual(self.chat.calls, [])

    def test_a_body_that_is_not_a_json_object_is_refused_before_the_facade(self):
        self.assertEqual(self.post("messages", "not json").json()["error"]["code"], "invalid_json")
        self.assertEqual(self.post("messages", ["hi"]).json()["error"]["code"], "invalid_request")
        plain = self.client.post(f"{CHAT}/messages", content=json.dumps({"text": "hi", "idempotency_key": "key-0000001"}),
                                 headers={"Content-Type": "text/plain"})
        self.assertEqual((plain.status_code, plain.json()["error"]["code"]), (400, "invalid_request"))
        self.assertEqual(self.chat.calls, [])

    # ----- POST interrupt / answer / close / runtime ----------------------------------------------------------------------

    def test_interrupt_needs_the_pin_and_returns_what_the_facade_says(self):
        self.chat.reply["interrupt"] = {"stopped": True}
        response = self.post("interrupt", {"expected_binding": PIN})
        self.assertEqual((response.status_code, response.json()["data"]), (200, {"stopped": True}))
        self.assertEqual(self.chat.calls, [("interrupt", (OWNER, AGENT, PIN), {})])
        for body in ({}, {"expected_binding": PIN, "force": True}, {"binding": PIN}):
            self.assertEqual(self.post("interrupt", body).status_code, 422, body)
        self.assertEqual(len(self.chat.calls), 1)

    def test_answer_hands_over_the_pin_the_request_and_the_decision_as_given(self):
        self.chat.reply["answer"] = {"answered": True}
        for decision in ("allow", "deny", {"answers": {"Which?": "A"}}):
            response = self.post("answer", {"expected_binding": PIN, "request_id": "req-1", "decision": decision})
            self.assertEqual((response.status_code, response.json()["data"]), (200, {"answered": True}))
        self.assertEqual([call[1] for call in self.chat.calls],
                         [(OWNER, AGENT, PIN, "req-1", "allow"), (OWNER, AGENT, PIN, "req-1", "deny"),
                          (OWNER, AGENT, PIN, "req-1", {"answers": {"Which?": "A"}})])
        before = len(self.chat.calls)
        for body in ({"request_id": "r", "decision": "allow"}, {"expected_binding": PIN, "decision": "allow"},
                     {"expected_binding": PIN, "request_id": "r"}, {"expected_binding": PIN, "request_id": "r", "decision": "allow", "x": 1}):
            response = self.post("answer", body)
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (422, "validation_error"), body)
        self.assertEqual(len(self.chat.calls), before)

    def test_close_takes_an_optional_pin(self):
        self.chat.reply["close"] = {"closing": True}
        self.assertEqual(self.post("close", {}).json()["data"], {"closing": True})
        self.assertEqual(self.post("close", {"expected_binding": PIN}).json()["data"], {"closing": True})
        self.assertEqual([call[1] for call in self.chat.calls], [(OWNER, AGENT, None), (OWNER, AGENT, PIN)])
        self.assertEqual(self.post("close", {"expected_binding": PIN, "mode": "now"}).status_code, 422)
        self.assertEqual(len(self.chat.calls), 2)

    def test_the_runtime_switch_route_is_gone(self):
        self.assertEqual(self.post("runtime", {"mode": "managed"}).status_code, 404)
        self.assertEqual(self.chat.calls, [])

    def test_every_refusal_the_facade_can_give_keeps_its_code_and_a_retryable_one_says_so(self):
        refusals = {
            "session_closed": 422, "terminal_changed": 422, "binding_not_ready": 422, "managed_command": 422,
            "cli_unavailable": 422, "resume_failed": 422, "open_timeout": 422, "host_unverified": 422, "driver_unavailable": 422,
            "session_open": 422,
            "validation_error": 422, "not_found": 404, "forbidden": 403, "idempotency_conflict": 409, "operation_unavailable": 503}
        for code, status in refusals.items():
            for retryable in (False, True):
                with self.subTest(code=code, retryable=retryable):
                    self.chat.error = DomainError(code, "Plain words for the owner", retryable=retryable)
                    response = self.post("close", {})
                    self.assertEqual((response.status_code, response.json()["error"]["code"]), (status, code))
                    self.assertEqual(response.json()["error"]["message"], "Plain words for the owner")
                    self.assertEqual(response.headers.get("retry-after"), "2" if retryable else None)

    def test_an_unknown_post_a_get_on_a_post_route_or_a_bad_agent_id_is_not_found(self):
        self.assertEqual(self.post("explode", {}).status_code, 404)
        for tail in ("interrupt", "answer", "close", "messages"):
            self.assertEqual(self.client.get(f"{CHAT}/{tail}").status_code, 404, tail)
        self.assertEqual(self.post("close", {}, agent="not-a-uuid").status_code, 404)
        self.assertEqual(self.post("close", {}, agent=AGENT.upper()).status_code, 404)
        self.assertEqual(self.chat.calls, [])

    # ----- GET commands --------------------------------------------------------------------------------------------------

    def commands(self, client="claude"):
        with mock.patch("qorqut.composition.locations.agent_workspace", return_value=None), \
                mock.patch("qorqut.connectors._probe_environment", return_value={"HOME": None}):
            return self.client.get(f"{CHAT}/commands?client={client}")

    def test_the_slash_catalog_marks_the_menu_commands_unavailable(self):
        claude, codex = self.commands(), self.commands("codex")
        self.assertEqual((claude.status_code, codex.status_code), (200, 200))
        self.assertEqual(claude.headers["cache-control"], "private, no-store")
        claude_commands = {item["name"]: item["where"] for item in claude.json()["data"]["commands"]}
        codex_commands = {item["name"]: item["where"] for item in codex.json()["data"]["commands"]}
        self.assertEqual((claude_commands["/clear"], claude_commands["/model"], claude_commands["/compact"]),
                         ("unavailable", "unavailable", "chat"))
        self.assertEqual((codex_commands["/compact"], codex_commands["/model"], codex_commands["/new"]), ("chat", "chat", "unavailable"))
        self.assertEqual(self.names(), ["check_events", "check_events"])

    def test_the_catalog_is_owner_only_like_every_chat_read(self):
        self.chat.error = DomainError("forbidden", "Agent chat is for the installation owner")
        self.assertEqual(self.commands().status_code, 403)
        denied = self.client.get(f"{CHAT}/commands?client=claude", headers={"Authorization": "Bearer x"})
        self.assertEqual(denied.status_code, 403)


# ----- the change stream over the real facade -----------------------------------------------------------------------------------

class EventStreamTest(base.ChatCase):
    """The ``changed`` event the stream tails carries the managed summary, so Stop, the activity line and the request cards follow
    a turn without waiting for the page read."""

    def stream_first(self, cursor):
        from qorqut.native_chat import http

        async def run():
            state = {"disconnected": False}

            async def is_disconnected():
                return state["disconnected"]
            request = SimpleNamespace(headers={}, query_params={"cursor": str(cursor)}, is_disconnected=is_disconnected)
            response = await http._events(self.chat, request, base.OWNER, base.AGENT)
            iterator = response.body_iterator
            try:
                return await asyncio.wait_for(iterator.__anext__(), 10)
            finally:
                state["disconnected"] = True
                await iterator.aclose()
        return asyncio.run(run())

    def test_the_stream_delivers_the_managed_phase_and_pending_requests(self):
        with self.db.transaction() as uow:
            base.store.bump(uow.connection, base.AGENT)
        self.managed.view = {**base.OPEN_VIEW, "phase": "blocked", "can_stop": True, "pending": [
            {"request_id": "r1", "kind": "permission", "tool": "Write", "input_summary": "/tmp/x", "questions": []}]}
        chunk = self.stream_first(0).decode()
        lines = dict(line.split(": ", 1) for line in chunk.strip().splitlines())
        self.assertEqual(lines["event"], "chat")
        event = json.loads(lines["data"])
        self.assertEqual(event["kind"], "changed")
        self.assertEqual(event["data"]["managed"]["phase"], "blocked")
        self.assertEqual([item["request_id"] for item in event["data"]["managed"]["pending"]], ["r1"])
        self.assertEqual(lines["id"], str(event["cursor"]))


if __name__ == "__main__":
    unittest.main()
