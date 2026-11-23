"""The terminal routes and the owner's two runtime routes, through the real ``HTTPAPI`` dispatch with a stub runtime.

In managed mode every terminal route answers the product's normal ``terminal_mode_off`` error before any broker is touched (the
broker here fails the test when anything is asked of it). ``GET/POST /api/root/agents/runtime`` call the switch object and keep
its refusals, details included. The per-agent ``chat/runtime`` route stays gone. Needs Starlette; without it the module is skipped."""
from contextlib import contextmanager
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError
from qorqut.managed_runtime import mode

try:
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    SERVICE = True
except ImportError:
    SERVICE = False

OWNER = "owner-principal"
AGENT = "11111111-1111-4111-8111-111111111111"
JSON = {"Content-Type": "application/json"}
OFF = "Agents run as managed sessions on this installation."
VIEW = {"mode": "managed", "recommended": "managed", "switching": None}


class Tripwire:
    def __getattr__(self, name):
        raise AssertionError("the terminal broker was touched: " + name)


class FakeSwitch:
    def __init__(self):
        self.calls, self.error = [], None

    def view(self, principal_id):
        self.calls.append(("view", principal_id))
        if self.error:
            raise self.error
        return dict(VIEW)

    def start(self, principal_id, to, force=False, trust=False):
        self.calls.append(("start", principal_id, to, force) + (("trust",) if trust else ()))
        if self.error:
            raise self.error
        return {"accepted": True, **VIEW}


@unittest.skipUnless(SERVICE, "Starlette is not installed")
class RouteTest(unittest.TestCase):
    def setUp(self):
        from qorqut.transport.http import HTTPAPI
        from qorqut.transport.terminals import TerminalService
        self.kind, self.principal = "human", OWNER
        self.switch = FakeSwitch()
        self.chat_calls = []

        @contextmanager
        def read():
            repository = SimpleNamespace(get=lambda name: {"owner_principal_id": OWNER})
            yield SimpleNamespace(repository=lambda name, **options: repository)

        chat = SimpleNamespace(**{name: (lambda *a, **k: self.chat_calls.append(name)) for name in ("send", "close", "state")})
        api = HTTPAPI.__new__(HTTPAPI)
        api.runtime = SimpleNamespace(database=SimpleNamespace(read=read), require_authority=lambda: None, native_chat=chat,
                                      runtime_switch=self.switch, paths=SimpleNamespace(runtime="/nonexistent"),
                                      workspace=Path("/nonexistent"))
        api.terminals = TerminalService(api.runtime, Tripwire(), service_url="http://127.0.0.1:1/mcp")
        api.compatibility = None
        api.authentication = SimpleNamespace(installation_owner=lambda: (SimpleNamespace(id=OWNER, display_name="Owner"),))
        api.origins = SimpleNamespace(validate=lambda request, mutation=False: None)
        api.statuses, api.owner_routes = {}, {}
        api.route_counters = SimpleNamespace(record=lambda *args, **options: None)
        api._report_transport_success = lambda *args: None
        api._report_transport_failure = lambda *args: None
        api.context = lambda actor, slug=None, owner=False: (
            SimpleNamespace(principal_kind=self.kind, principal_id=self.principal, business_id=None), None)
        self.api = api

        async def handler(request):
            return await api(request)
        self.client = TestClient(Starlette(routes=[Route("/api/{path:path}", handler, methods=["GET", "POST"])]))
        self.addCleanup(self.client.close)

    def post(self, path, body=None, headers=None):
        return self.client.post(path, content=body if isinstance(body, str) else json.dumps(body if body is not None else {}),
                                headers={**JSON, **(headers or {})})

    def error(self, response):
        return response.json()["error"]

    # ----- terminal routes in managed mode ---------------------------------------------------------------------------------

    TERMINAL_ROUTES = (
        ("GET", "/api/root/terminals"),
        ("GET", f"/api/root/terminal/{AGENT}/stream"),
        ("GET", f"/api/root/terminal/{AGENT}/attachment"),
        ("POST", f"/api/root/terminal/{AGENT}/start"),
        ("POST", f"/api/root/terminal/{AGENT}/input"),
        ("POST", f"/api/root/terminal/{AGENT}/resize"),
        ("POST", f"/api/root/terminal/{AGENT}/stop"),
        ("POST", f"/api/root/terminal/{AGENT}/attach"),
        ("GET", "/api/root/workspace-terminal/stream"),
        ("POST", "/api/root/workspace-terminal/start"),
        ("POST", "/api/root/workspace-terminal/input"),
        ("POST", "/api/root/workspace-terminal/resize"),
        ("POST", "/api/root/workspace-terminal/stop"),
        ("POST", "/api/root/workspace-terminal/2/start"),
        ("GET", "/api/root/workspace-terminal/3/stream"),
        ("GET", "/api/root/agent-trust"),
        ("POST", "/api/root/agent-trust"),
        ("POST", "/api/root/operations/agent_terminal_start"),
    )

    def test_every_terminal_route_in_managed_mode_gives_the_terminal_mode_off_error_and_touches_no_broker(self):
        for method, path in self.TERMINAL_ROUTES:
            with self.subTest(path=path, method=method):
                response = self.client.get(path) if method == "GET" else self.post(path, {})
                self.assertEqual(response.status_code, 422, response.text)
                body = response.json()
                self.assertEqual(body["status"], "error")
                self.assertEqual(self.error(response)["code"], "terminal_mode_off")
                self.assertEqual(self.error(response)["message"], OFF)

    def test_a_switch_in_progress_is_the_retryable_error_not_a_crash(self):
        with mock.patch.object(mode, "switching", return_value=True):
            for method, path in self.TERMINAL_ROUTES:
                with self.subTest(path=path):
                    response = self.client.get(path) if method == "GET" else self.post(path, {})
                    self.assertEqual(self.error(response)["code"], "runtime_switching")
                    self.assertTrue(self.error(response)["retryable"])

    def test_in_terminals_mode_the_routes_reach_the_service_with_the_path_after_root(self):
        calls = []

        async def route(request, context, business, tail):
            calls.append((request.method, business, tail))
            from starlette.responses import JSONResponse
            return JSONResponse({"ok": True})
        self.api.terminals = SimpleNamespace(route=route, require_terminals=lambda: None)
        with mock.patch.object(mode, "current", return_value="terminal"):
            self.assertEqual(self.client.get("/api/root/terminals").status_code, 200)
            self.assertEqual(self.post(f"/api/root/terminal/{AGENT}/start", {"cols": 80, "rows": 24}).status_code, 200)
            self.assertEqual(self.post("/api/root/workspace-terminal/2/input", {"data": "x"}).status_code, 200)
        self.assertEqual(calls, [("GET", None, ["terminals"]), ("POST", None, ["terminal", AGENT, "start"]),
                                 ("POST", None, ["workspace-terminal", "2", "input"])])

    def test_without_a_terminal_service_the_routes_are_plain_not_found_in_managed_mode_never_a_500(self):
        self.api.terminals = None
        self.assertEqual(self.error(self.client.get("/api/root/terminals"))["code"], "not_found")
        self.assertEqual(self.error(self.client.get("/api/root/agent-trust"))["code"], "terminal_mode_off")

    def test_the_agent_trust_route_is_the_owners_alone(self):
        self.assertEqual(self.error(self.client.get("/api/root/agent-trust", headers={"Authorization": "Bearer x"}))["code"], "forbidden")
        self.kind = "agent"
        self.assertEqual(self.error(self.client.get("/api/root/agent-trust"))["code"], "forbidden")

    def test_the_stream_loop_ends_at_service_stop(self):
        from qorqut.transport import http, terminals
        self.assertIsInstance(http.STOPPING, threading.Event)
        self.assertIs(terminals.STOPPING, http.STOPPING)

    def test_the_per_agent_chat_runtime_route_stays_gone(self):
        response = self.post(f"/api/root/agents/{AGENT}/chat/runtime", {"mode": "terminal"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.switch.calls, [])
        self.assertEqual(self.chat_calls, [])

    # ----- the owner's runtime routes ---------------------------------------------------------------------------------

    def test_the_owner_reads_how_agents_run_uncached(self):
        response = self.client.get("/api/root/agents/runtime")
        self.assertEqual((response.status_code, response.json()["data"]), (200, VIEW))
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(self.switch.calls, [("view", OWNER)])

    def test_the_owner_asks_for_a_switch_and_force_defaults_to_false(self):
        response = self.post("/api/root/agents/runtime", {"mode": "terminal"})
        self.assertEqual((response.status_code, response.json()["data"]["accepted"]), (200, True))
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.post("/api/root/agents/runtime", {"mode": "managed", "force": True})
        self.post("/api/root/agents/runtime", {"mode": "terminal", "trust": True})
        self.assertEqual(self.switch.calls, [("start", OWNER, "terminal", False), ("start", OWNER, "managed", True),
                                             ("start", OWNER, "terminal", False, "trust")])

    def test_any_authorization_header_and_anyone_but_the_owner_are_refused_before_the_switch_is_asked(self):
        headers = {"Authorization": "Bearer x"}
        for response in (self.client.get("/api/root/agents/runtime", headers=headers),
                         self.post("/api/root/agents/runtime", {"mode": "terminal"}, headers=headers)):
            self.assertEqual((response.status_code, self.error(response)["code"]), (403, "forbidden"))
        for kind, principal in (("agent", OWNER), ("human", "someone-else")):
            self.kind, self.principal = kind, principal
            for response in (self.client.get("/api/root/agents/runtime"), self.post("/api/root/agents/runtime", {"mode": "terminal"})):
                self.assertEqual((response.status_code, self.error(response)["code"]), (403, "forbidden"), (kind, principal))
        self.assertEqual(self.switch.calls, [])

    def test_a_bad_body_is_a_validation_error_and_the_switch_is_not_asked(self):
        bad = [{}, {"mode": "both"}, {"mode": "terminal", "extra": 1}, {"mode": "terminal", "force": "yes"}, {"mode": "terminal", "trust": 1}, {"mode": ["terminal"]},
               {"force": True}]
        for body in bad:
            response = self.post("/api/root/agents/runtime", body)
            self.assertEqual((response.status_code, self.error(response)["code"]), (422, "validation_error"), body)
        for body, code in ((["terminal"], "invalid_request"), ("not json", "invalid_json")):
            response = self.post("/api/root/agents/runtime", body)
            self.assertEqual((response.status_code, self.error(response)["code"]), (400, code), body)
        self.assertEqual(self.switch.calls, [])

    def test_a_refusal_keeps_its_code_its_sentence_its_details_and_whether_a_retry_is_worth_it(self):
        agents = [{"agent_id": AGENT, "name": "Ordubashy"}]
        cases = (DomainError("turns_running", "Ordubashy is in the middle of a turn.", details={"agents": agents}),
                 DomainError("switch_running", "A switch is already running.", retryable=True),
                 DomainError("terminal_unavailable", "Terminals need tmux, which is not installed on this computer."),
                 DomainError("codex_agents", "Set them to Claude Code first."))
        for error in cases:
            self.switch.error = error
            for response in (self.client.get("/api/root/agents/runtime"), self.post("/api/root/agents/runtime", {"mode": "terminal"})):
                shown = self.error(response)
                self.assertEqual((shown["code"], shown["message"], shown["retryable"]), (error.code, error.message, error.retryable))
                if error.details:
                    self.assertEqual(shown["details"], {"agents": agents})
                self.assertNotEqual(response.status_code, 500)

    def test_without_a_switch_object_the_route_is_the_normal_unavailable_error(self):
        self.api.runtime.runtime_switch = None
        response = self.client.get("/api/root/agents/runtime")
        self.assertEqual((response.status_code, self.error(response)["code"]), (503, "operation_unavailable"))


if __name__ == "__main__":
    unittest.main()
