from contextlib import contextmanager
from dataclasses import replace
import sqlite3
from types import SimpleNamespace
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from qorqut_operations.contracts import DomainError, Principal
from bazarlyk.sandbox.docker import Ownership
from bazarlyk.sandbox.listener import (BusinessAgentListeners, BusinessAuthentication,
    ListenerBinding, ListenerServer, PrivatePeerBoundary)
from bazarlyk.sandbox.runtime import RunningEnvironment
from bazarlyk.transport.auth import AuthenticatedPrincipal

BUSINESS = "e4ad5a2d-5e38-4097-b9fb-590a8f1c496f"
FOREIGN = "142c4bbc-5f51-408b-940f-d9204f239c56"


def environment():
    return RunningEnvironment(Ownership("a" * 20, BUSINESS), 1, "a" * 64, "b" * 64, "c" * 64,
        "172.29.1.3", "172.29.2.3", "172.29.1.2", "172.29.2.2", "http://172.29.1.1:1148")


def test_business_listener_auth_rechecks_home_scope_and_active_profile():
    identities = {name: str(uuid4()) for name in ("own", "foreign", "global", "missing")}
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE agent_profiles(agent_id TEXT, home_business_id TEXT, status TEXT)")
    connection.executemany("INSERT INTO agent_profiles VALUES (?, ?, ?)",
        [(identities["own"], BUSINESS, "active"), (identities["foreign"], FOREIGN, "active"),
         (identities["global"], None, "active")])

    class Database:
        @contextmanager
        def read(self):
            yield SimpleNamespace(connection=connection)

    auth = SimpleNamespace(database=Database(),
        agent=lambda value: AuthenticatedPrincipal(Principal(identities[value], "agent")))
    restricted = BusinessAuthentication(auth, BUSINESS)
    assert restricted.agent("own").principal.id == identities["own"]
    for value in ("foreign", "global", "missing"):
        with pytest.raises(DomainError, match="does not belong"):
            restricted.agent(value)
    connection.execute("UPDATE agent_profiles SET status='inactive' WHERE agent_id=?", (identities["own"],))
    with pytest.raises(DomainError):
        restricted.agent("own")
    connection.close()


def test_network_peer_cannot_be_selected_with_forwarded_headers():
    async def endpoint(request):
        return JSONResponse({"ok": True})
    app = PrivatePeerBoundary(Starlette(routes=[Route("/mcp", endpoint)]), ["172.29.1.3"])
    with TestClient(app, client=("172.29.1.3", 1234)) as client:
        assert client.get("/mcp").status_code == 200
    with TestClient(app, client=("172.29.5.3", 1234)) as client:
        response = client.get("/mcp", headers={"X-Forwarded-For": "172.29.1.3", "Forwarded": "for=172.29.1.3"})
        assert response.status_code == 403


@pytest.mark.parametrize("endpoint", ["http://0.0.0.0:1148", "http://127.0.0.1:1148",
    "http://172.29.1.1:1049", "http://172.29.1.1:1148/mcp", "http://user@172.29.1.1:1148",
    "https://172.29.1.1:1148", "http://172.29.1.1:1148?business=foreign"])
def test_binding_rejects_owner_wildcard_and_caller_selectors(endpoint):
    with pytest.raises(DomainError):
        ListenerBinding.observed({"id": BUSINESS}, SimpleNamespace(agent_api_port=1148),
                                 replace(environment(), agent_api_url=endpoint))


def test_registry_reuses_listener_and_drains_before_rebinding():
    events, servers = [], []

    class Server:
        def __init__(self, app, address, port):
            self.running = False
            servers.append(self)
            assert address == "172.29.1.1" and port == 1148

        def start(self):
            events.append("start")
            self.running = True

        def stop(self, *, timeout):
            import threading
            completed = threading.Event()
            def authorize_pending_request():
                with registry._guard:
                    assert BUSINESS in registry._servers
                    completed.set()
            request = threading.Thread(target=authorize_pending_request, daemon=True)
            request.start()
            request.join(1)
            assert completed.is_set(), "shutdown must allow pending authorization to finish"
            events.append("stop")
            self.running = False

    def create(runtime, **kwargs):
        assert kwargs["agent_only"] and not kwargs["manage_lifecycle"]
        assert kwargs["origins"] == ["http://172.29.1.1:1148"]
        assert kwargs["authentication"].business_id == BUSINESS
        return object()

    registry = BusinessAgentListeners(SimpleNamespace(database=None, root="/unused"),
        server_factory=Server, app_factory=create)
    config, business, observed = SimpleNamespace(agent_api_port=1148), {"id": BUSINESS}, environment()
    registry.ensure(business, config, observed)
    registry.ensure(business, config, observed)
    assert events == ["start"]
    registry.ensure(business, config, replace(observed, generation=2, workstation_id="d" * 64))
    assert events == ["start", "stop", "start"]
    registry.stop()
    assert events[-1] == "stop" and not any(server.running for server in servers)


def test_registry_stops_only_selected_business_listener():
    stopped = []

    class Server:
        def __init__(self, name):
            self.name, self.running = name, True
        def stop(self, *, timeout):
            stopped.append((self.name, timeout))
            self.running = False

    registry = BusinessAgentListeners(SimpleNamespace(database=None, root="/unused"))
    first, second = Server("first"), Server("second")
    registry._servers = {BUSINESS: (object(), first), FOREIGN: (object(), second)}
    registry.stop_business(BUSINESS, timeout=3)
    assert stopped and stopped[0][0] == "first" and 0 < stopped[0][1] <= 3
    assert BUSINESS not in registry._servers and registry._servers[FOREIGN][1] is second and second.running


def test_native_listener_has_explicit_lifespan_and_releases_its_socket():
    events = []
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        events.append("start")
        yield
        events.append("stop")

    async def endpoint(request):
        return JSONResponse({"ok": True})

    server = ListenerServer(Starlette(routes=[Route("/probe", endpoint)], lifespan=lifespan), "127.0.0.1", 0)
    assert server.thread is None and server.socket is None
    try:
        server.start()
        with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{server.port}/probe", timeout=3) as response:
            assert response.status == 200
        assert events == ["start"] and server.running
    finally:
        server.stop()
    assert events == ["start", "stop"] and not server.running
    assert server.socket is None
