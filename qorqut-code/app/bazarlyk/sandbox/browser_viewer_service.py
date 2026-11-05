"""Private, owner-only navigation to a confined business Chrome instance.

One configured host port multiplexes business-specific viewer paths.  It never
proxies a browser endpoint: :class:`BrowserViewer` talks only through the
already-reviewed namespace socket connector and renders pixels/control UI.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
from time import monotonic
from urllib.parse import quote

from bazarlyk.browser.control import business_control
from bazarlyk.browser.socket import BrowserSocketConnector
from bazarlyk.browser.turns import BrowserTurnQueue
from bazarlyk.browser.viewer import BrowserViewer, BrowserViewerRoute
from qorqut_operations.contracts import DomainError, Principal
from bazarlyk.transport.auth import normalize_origin
from .config import business_identity
from .docker import Ownership
from .listener import ListenerServer, PrivatePeerBoundary


_PATH = re.compile(r"^/([0-9a-f-]{36})(/.*)?$")


class BusinessBrowserViewers:
    """A bounded private listener with one current viewer route per business."""

    def __init__(self, runtime, *, config_loader, dashboard_origins=(), server_factory=ListenerServer, interval=10):
        if not callable(config_loader) or type(interval) is not int or not 1 <= interval <= 60:
            raise ValueError("Browser viewer service configuration is invalid")
        self.runtime, self.config_loader, self.server_factory, self.interval = runtime, config_loader, server_factory, interval
        self.turns = getattr(runtime, "browser_turns", None) or BrowserTurnQueue()
        self.dashboard_origins = frozenset(normalize_origin(value) for value in dashboard_origins)
        self._routes, self._tokens = {}, {}
        self._guard, self._server, self._server_started = threading.RLock(), None, False
        self._lifecycle, self._server_key = threading.Lock(), None
        self._stop, self._thread = threading.Event(), None

    def _config(self):
        config = self.config_loader(self.runtime.paths)
        if config is None or config.viewer_port is None:
            return None
        return config

    def _owner(self, business_id):
        self.runtime.require_authority()
        with self.runtime.database.read() as uow:
            installation = uow.repository("installation").get("service", required=True)
            row = uow.repository("principals").get(installation["owner_principal_id"], required=True)
            membership = uow.repository("business_memberships", business_id=business_id).get(row["id"])
        if row["kind"] != "human" or not row["active"] or not membership or not membership["active"] or not membership["is_owner"]:
            raise DomainError("forbidden", "Private browser viewing requires the active registered project owner")
        return Principal(row["id"], "human", row["display_name"])

    @staticmethod
    def _peer(scope, config):
        client = scope.get("client")
        if not client or client[0] not in config.owner_addresses:
            raise DomainError("forbidden", "This peer cannot use the private browser viewer")

    def _binding(self, business_id):
        return self.runtime.sandbox.observed_browser_binding(business_id)

    def _target(self, business, _config):
        binding = self._binding(business["id"])
        return {"generation": binding.generation, "instance": binding.instance_id}

    def _control(self, scope, business_id, action, token, config, *, binding=None):
        with self.runtime.application.writer_admission.writer():
            self._peer(scope, config)
            owner = self._owner(business_id)
            turn = None
            if action == "acquire":
                with self.runtime.database.transaction(operation="browser.owner.acquire") as uow:
                    turn = self.turns.acquire(uow, browser_key="business:" + business_id, business_id=business_id,
                        parent_principal_id=owner.id, requester_kind="owner", requester_id=owner.id)
                    if turn["state"] != "owning":
                        raise DomainError("busy", "Another task owns this project browser; wait for its turn")
            elif action == "check":
                with self.runtime.database.read() as uow:
                    turn = uow.connection.execute(
                        "SELECT * FROM browser_turns WHERE browser_key=? AND requester_kind='owner' "
                        "AND requester_id=? AND state IN ('queued','owning') ORDER BY created_at DESC LIMIT 1",
                        ("business:" + business_id, owner.id)).fetchone()
                if turn is not None and turn["state"] != "owning":
                    raise DomainError("browser_wait", "The project browser is waiting for its exclusive turn", retryable=True)
            try:
                result = business_control(self.runtime.resolver, owner, action, token,
                    owner_business_id=business_id, sandbox_target=self._target)
            except Exception:
                if action == "acquire" and turn is not None:
                    with self.runtime.database.transaction(operation="browser.owner.recover") as uow:
                        self.turns.release(uow, turn["id"], parent_principal_id=owner.id,
                            requester_id=owner.id, reason="owner browser acquisition failed")
                raise
            if action == "release":
                with self.runtime.database.read() as uow:
                    turn = uow.connection.execute(
                        "SELECT * FROM browser_turns WHERE browser_key=? AND requester_kind='owner' "
                        "AND requester_id=? AND state IN ('queued','owning') ORDER BY created_at DESC LIMIT 1",
                        ("business:" + business_id, owner.id)).fetchone()
                if turn is not None:
                    with self.runtime.database.transaction(operation="browser.owner.release") as uow:
                        self.turns.release(uow, turn["id"], parent_principal_id=owner.id,
                            requester_id=owner.id, lease_id=turn["lease_id"], reason="owner released browser")
            if binding is not None and (result.get("generation") != binding.generation or result.get("instance") != binding.instance_id):
                raise DomainError("environment_conflict", "Private browser viewer binding is stale")
            return result

    def _viewer(self, business_id, config, binding):
        token = self._tokens.setdefault(business_id, secrets.token_hex(32))
        owner = Ownership(hashlib.sha256(str(self.runtime.root).encode()).hexdigest()[:20], business_id)
        connector = BrowserSocketConnector(self.runtime.sandbox.docker, owner,
            current_binding=lambda: self._binding(business_id))

        def authorize(scope, observed, action):
            self._peer(scope, config)
            self._owner(business_id)
            if action != "view":
                self._control(scope, business_id, "check", token, config, binding=observed)
            return True

        def acquire(scope, observed):
            self._control(scope, business_id, "acquire", token, config, binding=observed)
            return True

        def release(scope, observed):
            self._control(scope, business_id, "release", token, config)
            return True

        return BrowserViewer(BrowserViewerRoute(binding, config.viewer_origin()), connector=connector,
            current_binding=lambda: self._binding(business_id),
            authorize=authorize, acquire=acquire, release=release)

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "lifespan":
            # ListenerServer requires an explicit ASGI startup/shutdown
            # handshake. Routes have no independent startup side effects.
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif event["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope.get("type") == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        match = _PATH.fullmatch(scope.get("path", "")) if scope.get("type") == "http" else None
        if match is None:
            await send({"type": "http.response.start", "status": 404, "headers": [(b"cache-control", b"no-store")]})
            return await send({"type": "http.response.body", "body": b""})
        try:
            business_id = business_identity(match.group(1))
            with self._guard:
                viewer = self._routes.get(business_id)
            if viewer is None:
                raise DomainError("environment_unavailable", "Private browser viewer is unavailable", retryable=True)
            suffix = match.group(2) or "/"
            forwarded = {**scope, "path": suffix, "raw_path": quote(suffix, safe="/").encode("ascii")}
            return await viewer(forwarded, receive, send)
        except DomainError as exc:
            status = 403 if exc.code == "forbidden" else 409 if exc.code == "environment_conflict" else 503
            await send({"type": "http.response.start", "status": status, "headers": [(b"cache-control", b"no-store"),
                (b"content-security-policy", b"frame-ancestors 'none'")]})
            await send({"type": "http.response.body", "body": b'{"error":"browser_unavailable"}'})

    def link(self, business_id):
        business_identity(business_id)
        with self._guard:
            server, available, started = self._server, business_id in self._routes, self._server_started
        if server is None or not started or not server.running or not available:
            return None
        config = self._config()
        return config.viewer_origin() + "/" + business_id + "/" if config is not None else None

    def reconcile(self):
        with self.runtime.application.writer_admission.writer(), self._lifecycle:
            self.runtime.require_authority()
            config = self._config()
            if config is None:
                with self._guard:
                    old = self._server
                if old is not None:
                    old.stop(timeout=5)
                    with self._guard:
                        if self._server is old:
                            self._server, self._server_key, self._server_started = None, None, False
                with self._guard:
                    self._routes.clear()
                return
            if config.viewer_origin() in self.dashboard_origins:
                raise DomainError("invalid_origin", "Private browser viewer origin conflicts with the dashboard")
            wanted = {}
            with self.runtime.database.read() as uow:
                rows = [row[0] for row in uow.connection.execute(
                    "SELECT business_id FROM business_sandboxes WHERE state='ready' AND desired_state='running' "
                    "AND observed_generation=desired_generation")]
            for business_id in rows:
                try:
                    binding = self._binding(business_id)
                    wanted[business_id] = self._viewer(business_id, config, binding)
                except DomainError:
                    continue
            key = (config.bind_address, config.viewer_port, config.owner_addresses)
            with self._guard:
                old, old_key, old_started = self._server, self._server_key, self._server_started
            if old is not None and old_key == key and old_started and old.running:
                with self._guard:
                    self._routes = wanted
                return
            if old is not None:
                # Requests retain only a short route lookup lock.  A server's
                # bounded thread join must never wait while they hold it.
                old.stop(timeout=5)
                with self._guard:
                    if self._server is old:
                        self._server, self._server_key, self._server_started = None, None, False
            server = self.server_factory(PrivatePeerBoundary(self, config.owner_addresses), config.bind_address, config.viewer_port)
            try:
                server.start()
            except Exception:
                try:
                    server.stop(timeout=5)
                except (OSError, TimeoutError):
                    # A failed start may still have left a live writer.  Keep
                    # this identity visible until a later bounded stop proves
                    # it is gone; recovery must never lose that inventory.
                    with self._guard:
                        self._routes, self._server, self._server_key, self._server_started = wanted, server, key, False
                raise
            if not server.running:
                try:
                    server.stop(timeout=5)
                except (OSError, TimeoutError):
                    with self._guard:
                        self._routes, self._server, self._server_key, self._server_started = wanted, server, key, False
                raise DomainError("environment_unavailable", "Private browser viewer listener did not start", retryable=True)
            with self._guard:
                self._routes, self._server, self._server_key, self._server_started = wanted, server, key, True

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        def run():
            while not self._stop.is_set():
                try:
                    self.reconcile()
                except (DomainError, OSError, TimeoutError):
                    pass
                self._stop.wait(self.interval)
        self._thread = threading.Thread(target=run, name="qorqut-private-browser-viewer", daemon=True)
        self._thread.start()

    def stop(self, *, timeout=5):
        deadline = monotonic() + timeout
        self._stop.set()
        if self._thread:
            self._thread.join(max(0, deadline - monotonic()))
            if self._thread.is_alive():
                # The worker can be waiting to enter ``_lifecycle``.  Never
                # hold that lock while joining it, and retain its server map
                # unchanged when the bounded join expires.
                raise TimeoutError("Private browser viewer reconciliation is still draining")
        if not self._lifecycle.acquire(timeout=max(0, deadline - monotonic())):
            raise TimeoutError("Private browser viewer lifecycle is busy")
        try:
            failure = None
            with self._guard:
                server = self._server
            if server:
                try:
                    server.stop(timeout=max(0, deadline - monotonic()))
                    with self._guard:
                        if self._server is server:
                            self._server, self._server_key, self._server_started = None, None, False
                except TimeoutError as exc:
                    failure = failure or exc
            with self._guard:
                self._routes.clear()
            if failure:
                raise failure
        finally:
            self._lifecycle.release()
