"""Private, business-bound agent listeners managed by the trusted service.

Each socket binds only its own Docker bridge address. The listener has no owner
routes and requires both the expected network peer and a current registration in
that business. Forwarded headers never select a peer, business, or principal.
"""

from dataclasses import dataclass
from ipaddress import IPv4Address
import socket
import threading
import time
from urllib.parse import urlsplit

from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.service.web import create_app
from bazarlyk.transport.auth import Authentication
from bazarlyk.transport.results import failure


class BusinessAuthentication:
    def __init__(self, authentication, business_id):
        self.authentication, self.business_id = authentication, business_id

    def agent(self, authorization):
        actor = self.authentication.agent(authorization)
        with self.authentication.database.read() as uow:
            row = uow.connection.execute(
                "SELECT home_business_id, status FROM agent_profiles WHERE agent_id=?",
                (actor.principal.id,)).fetchone()
        if row is None or row["status"] != "active" or row["home_business_id"] != self.business_id:
            raise DomainError("forbidden", "Agent registration does not belong to this business listener")
        return actor


class PrivatePeerBoundary:
    def __init__(self, app, peers):
        self.app, self.peers = app, frozenset(str(IPv4Address(peer)) for peer in peers)

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"http", "websocket"}:
            client = scope.get("client")
            if not client or client[0] not in self.peers:
                if scope["type"] == "websocket":
                    return await send({"type": "websocket.close", "code": 1008})
                return await failure(DomainError("forbidden", "This peer cannot use the business listener"),
                                     new_id())(scope, receive, send)
        return await self.app(scope, receive, send)


@dataclass(frozen=True)
class ListenerBinding:
    business_id: str
    generation: int
    workstation_id: str
    gateway_id: str
    address: str
    port: int
    peers: tuple[str, ...]

    @classmethod
    def observed(cls, business, config, environment):
        endpoint = urlsplit(environment.agent_api_url)
        try:
            address = IPv4Address(endpoint.hostname)
            peers = tuple(str(IPv4Address(value)) for value in
                          (environment.agent_address, environment.agent_gateway))
            valid = (endpoint.scheme == "http" and endpoint.port == config.agent_api_port
                     and not endpoint.username and not endpoint.password
                     and not endpoint.path and not endpoint.query and not endpoint.fragment
                     and address.is_private and not address.is_loopback and not address.is_unspecified
                     and not address.is_multicast and business["id"] == environment.ownership.business_id)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise DomainError("environment_conflict", "Business listener has an invalid observed network binding")
        return cls(business["id"], environment.generation, environment.workstation_id,
                   environment.gateway_id, str(address), endpoint.port, peers)


class ListenerServer:
    """One bounded native ASGI server; construction opens no socket or thread."""

    def __init__(self, app, address, port):
        self.app, self.address, self.port = app, address, port
        self.server = self.thread = self.socket = None
        self._done = threading.Event()

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive() and self.server.started
                    and not self.server.should_exit)

    def start(self, *, timeout=10):
        if self.running:
            return
        if self.thread and self.thread.is_alive():
            raise DomainError("environment_unavailable", "Previous business listener is still stopping")
        import uvicorn
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.address, self.port))
            self.socket.listen(128)
            self.socket.setblocking(False)
            self.port = self.socket.getsockname()[1]
            config = uvicorn.Config(self.app, proxy_headers=False, server_header=False,
                access_log=False, timeout_graceful_shutdown=3, limit_concurrency=100,
                log_level="warning", lifespan="on")
            self.server = uvicorn.Server(config)
            self._done.clear()

            def run():
                try:
                    self.server.run(sockets=[self.socket])
                finally:
                    self._done.set()

            self.thread = threading.Thread(target=run, name="qorqut-business-agent-api", daemon=True)
            self.thread.start()
            deadline = time.monotonic() + timeout
            while not self.running:
                if self._done.wait(.01) or time.monotonic() >= deadline:
                    raise DomainError("environment_unavailable", "Business listener did not start")
        except BaseException:
            self.stop(timeout=5)
            raise

    def stop(self, *, timeout=5):
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout)
            if self.thread.is_alive():
                raise TimeoutError("Business agent listener is still stopping")
        if self.socket:
            self.socket.close()
            self.socket = None


class BusinessAgentListeners:
    def __init__(self, runtime, *, server_factory=ListenerServer, app_factory=create_app):
        self.runtime, self.server_factory, self.app_factory = runtime, server_factory, app_factory
        self._guard, self._servers = threading.RLock(), {}
        self._lifecycle = threading.Lock()

    def ensure(self, business, config, environment):
        binding = ListenerBinding.observed(business, config, environment)
        with self._lifecycle:
            with self._guard:
                old = self._servers.get(business["id"])
            if old and old[0] == binding and old[1].running:
                return
            if old:
                # In-flight authorizers read bindings while the server drains.
                # Never join its thread while holding the reader guard.
                old[1].stop(timeout=5)
                with self._guard:
                    del self._servers[business["id"]]
            authentication = BusinessAuthentication(
                Authentication(self.runtime.database, self.runtime.paths), business["id"])
            app = self.app_factory(self.runtime, origins=[environment.agent_api_url],
                authentication=authentication, agent_only=True, manage_lifecycle=False)
            server = self.server_factory(PrivatePeerBoundary(app, binding.peers), binding.address, binding.port)
            # Retain identity even if a failed start has an in-flight shutdown.
            with self._guard:
                self._servers[business["id"]] = binding, server
            server.start()

    def stop(self, *, timeout=5):
        failure = None
        with self._lifecycle:
            with self._guard:
                servers = list(self._servers.items())
            for business_id, (_, server) in servers:
                try:
                    server.stop(timeout=timeout)
                    with self._guard:
                        del self._servers[business_id]
                except TimeoutError as exc:
                    failure = failure or exc
        if failure:
            raise failure

    def stop_business(self, business_id, *, timeout=5):
        """Drain one exact listener without interrupting other businesses."""
        if not isinstance(business_id, str) or not business_id:
            raise ValueError("Business listener identity is required")
        if type(timeout) not in {int, float} or not 0 <= timeout <= 60:
            raise ValueError("Business listener shutdown timeout must be bounded")
        deadline = time.monotonic() + timeout
        if not self._lifecycle.acquire(timeout=timeout):
            raise TimeoutError("Business listener lifecycle is still in flight")
        try:
            with self._guard:
                current = self._servers.get(business_id)
            if current is None:
                return
            binding, server = current
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Business listener shutdown deadline elapsed")
            server.stop(timeout=remaining)
            with self._guard:
                # A lifecycle operation cannot replace this entry while we hold
                # _lifecycle, but retain the identity check for future callers.
                if self._servers.get(business_id) == (binding, server):
                    del self._servers[business_id]
        finally:
            self._lifecycle.release()
