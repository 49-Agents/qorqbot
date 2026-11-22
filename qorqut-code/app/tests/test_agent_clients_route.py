"""The owner-only ``/api/root/agent-clients`` route, through the real ``HTTPAPI`` dispatch with a stub runtime and the
agent client functions replaced: no Claude Code, Codex, network or sign-in is touched. Needs the service extras (Starlette,
anyio); without them the module is skipped."""
from contextlib import contextmanager
import json
import time
from types import SimpleNamespace
import unittest
from unittest import mock

try:
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    SERVICE = True
except ImportError:                                           # the service extras are not installed
    SERVICE = False

OWNER = "owner-principal"
VIEW = {"claude": {"installed": True}, "codex": {"installed": False}}


@unittest.skipUnless(SERVICE, "Starlette is not installed")
class RouteTest(unittest.TestCase):
    def setUp(self):
        from qorqut.transport.http import HTTPAPI
        self.kind, self.principal = "human", OWNER

        @contextmanager
        def read():
            repository = SimpleNamespace(get=lambda name: {"owner_principal_id": OWNER})
            yield SimpleNamespace(repository=lambda name, **options: repository)

        api = HTTPAPI.__new__(HTTPAPI)
        api.runtime = SimpleNamespace(database=SimpleNamespace(read=read), require_authority=lambda: None,
                                      paths=SimpleNamespace(runtime=None))
        api.authentication = SimpleNamespace(installation_owner=lambda: (SimpleNamespace(id=OWNER, display_name="Owner"),))
        api.origins = SimpleNamespace(validate=lambda request, mutation=False: None)
        api.statuses, api.owner_routes = {}, {}
        api.route_counters = SimpleNamespace(record=lambda *args, **options: None)
        api.context = lambda actor, slug=None, owner=False: (
            SimpleNamespace(principal_kind=self.kind, principal_id=self.principal), None)
        async def handler(request):                          # as service/web.py mounts it: a request in, a response out
            return await api(request)
        self.client = TestClient(Starlette(routes=[Route("/api/{path:path}", handler, methods=["GET", "POST"])]))
        self.addCleanup(self.client.close)

    def post(self, body, headers=None):
        return self.client.post("/api/root/agent-clients", content=body if isinstance(body, str) else json.dumps(body),
                                headers={"Content-Type": "application/json", **(headers or {})})

    def test_the_owner_reads_the_view_uncached(self):
        with mock.patch("qorqut.owner_settings.agent_clients_view", return_value=VIEW) as view:
            response = self.client.get("/api/root/agent-clients")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], VIEW)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        view.assert_called_once_with()

    def test_any_authorization_header_is_refused_before_anything_runs(self):
        with mock.patch("qorqut.owner_settings.agent_clients_view") as view, \
                mock.patch("qorqut.owner_settings.agent_clients_action") as action:
            get = self.client.get("/api/root/agent-clients", headers={"Authorization": "Bearer x"})
            post = self.post({"provider": "claude", "action": "connect"}, headers={"Authorization": "Bearer x"})
        for response in (get, post):
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["error"]["code"], "forbidden")
        view.assert_not_called()
        action.assert_not_called()

    def test_anyone_but_the_installation_owner_is_refused(self):
        for kind, principal in (("agent", OWNER), ("human", "someone-else")):
            self.kind, self.principal = kind, principal
            with mock.patch("qorqut.owner_settings.agent_clients_view") as view, \
                    mock.patch("qorqut.owner_settings.agent_clients_action") as action:
                responses = [self.client.get("/api/root/agent-clients"), self.post({"provider": "claude", "action": "connect"})]
            for response in responses:
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "forbidden"), (kind, principal))
            view.assert_not_called()
            action.assert_not_called()

    def test_a_bad_provider_action_field_or_body_is_a_validation_error_and_runs_nothing(self):
        bad = [{"provider": "gemini", "action": "connect"}, {"provider": "claude", "action": "explode"},
               {"provider": "claude", "action": "connect", "extra": 1}, {"provider": "claude"}, {"action": "connect"},
               {"provider": ["claude"], "action": "connect"}]
        with mock.patch("qorqut.owner_settings.agent_clients_action") as action:
            for body in bad:
                response = self.post(body)
                self.assertEqual(response.status_code, 422, body)
                self.assertEqual(response.json()["error"]["code"], "validation_error", body)
            for body, code in ((["claude", "connect"], "invalid_request"), ("not json", "invalid_json")):
                response = self.post(body)
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (400, code), body)
        action.assert_not_called()

    def test_each_action_calls_the_matching_function_and_returns_its_view(self):
        for provider in ("claude", "codex"):
            for name in ("download", "cancel_download", "connect", "cancel_connect"):
                with mock.patch("qorqut.owner_settings.agent_clients_action", return_value=VIEW) as action:
                    response = self.post({"provider": provider, "action": name})
                self.assertEqual((response.status_code, response.json()["data"]), (200, VIEW), (provider, name))
                action.assert_called_once_with(provider, name)
                self.assertEqual(response.headers["cache-control"], "private, no-store")

    def test_connect_and_download_reach_the_sign_in_and_install_modules(self):
        from qorqut import agent_cli_sign_in
        install = SimpleNamespace(start_download=mock.Mock(), cancel_download=mock.Mock())
        with mock.patch.object(agent_cli_sign_in, "_install", return_value=install), \
                mock.patch.object(agent_cli_sign_in, "start_sign_in") as start, \
                mock.patch.object(agent_cli_sign_in, "cancel_sign_in") as cancel, \
                mock.patch("qorqut.owner_settings.agent_clients_view", return_value=VIEW):
            self.assertEqual(self.post({"provider": "codex", "action": "download"}).status_code, 200)
            self.assertEqual(self.post({"provider": "claude", "action": "connect"}).status_code, 200)
            self.assertEqual(self.post({"provider": "claude", "action": "cancel_connect"}).status_code, 200)
        install.start_download.assert_called_once_with("codex")
        self.assertEqual(start.call_args.args, ("claude",))
        cancel.assert_called_once_with("claude")

    def test_other_methods_and_the_about_route_are_unchanged(self):
        with mock.patch("qorqut.owner_settings.about_view", return_value={"version": "1"}):
            self.assertEqual(self.client.get("/api/root/about").json()["data"], {"version": "1"})
            self.assertEqual(self.client.get("/api/root/about", headers={"Authorization": "x"}).status_code, 403)


@unittest.skipUnless(SERVICE, "Starlette is not installed")
class ShutdownTest(unittest.TestCase):
    """The service stops an open sign-in on the way out, in bounded time, and always stops the runtime afterwards."""

    def run_lifespan(self, stop_all):
        import anyio
        import tempfile
        from contextlib import asynccontextmanager
        from qorqut import agent_cli_sign_in
        from qorqut.service import web

        @asynccontextmanager
        async def running():
            yield
        transport = SimpleNamespace(manager=SimpleNamespace(run=running))
        runtime = mock.MagicMock()
        order = []
        runtime.stop.side_effect = lambda: order.append("runtime.stop")

        def stopper(*args):
            order.append("stop_all")
            return stop_all()

        async def go():
            with tempfile.TemporaryDirectory() as root, \
                    mock.patch.object(web, "HTTPAPI"), mock.patch.object(web, "MCPTransport", return_value=transport), \
                    mock.patch.object(web, "OriginPolicy"), mock.patch("qorqut.browser.setup.ensure_all"), \
                    mock.patch.object(agent_cli_sign_in, "stop_all", side_effect=stopper):
                app = web.create_app(runtime, origins=[], dashboard_root=root, authentication=mock.Mock(), agent_only=True)
                async with app.router.lifespan_context(app):
                    pass
        started = time.monotonic()
        anyio.run(go)
        return order, time.monotonic() - started

    def test_the_sign_in_is_stopped_before_the_runtime(self):
        order, _ = self.run_lifespan(lambda: None)
        self.assertEqual(order, ["stop_all", "runtime.stop"])

    def test_a_failing_stop_does_not_skip_the_runtime_stop(self):
        def boom():
            raise RuntimeError("stop failed")
        with self.assertLogs("qorqut.service.web", "ERROR"):
            order, _ = self.run_lifespan(boom)
        self.assertEqual(order, ["stop_all", "runtime.stop"])

    def test_a_stop_that_hangs_is_abandoned_and_the_runtime_is_still_stopped(self):
        import threading
        from qorqut import agent_cli_sign_in
        release = threading.Event()
        self.addCleanup(release.set)
        with mock.patch.object(agent_cli_sign_in, "STOP_ALL_BUDGET", -3.7):          # the wait is budget + 4 seconds: 0.3 s here
            order, elapsed = self.run_lifespan(lambda: release.wait(10))
        self.assertEqual(order, ["stop_all", "runtime.stop"])
        self.assertLess(elapsed, 3)


if __name__ == "__main__":
    unittest.main()
