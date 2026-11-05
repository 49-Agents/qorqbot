"""Explicit private owner access to registered business development ports.

The host chooses a private bind address, exact permitted owner peer addresses,
and a reserved listener port range. Neither an agent nor an HTTP caller can
choose an upstream address or change that access boundary. No listener is
opened until explicit service startup and a persisted preview registration.
"""

from dataclasses import dataclass
import hashlib
import ipaddress
import json
import secrets
import threading
from time import monotonic

from qorqut_operations.contracts import DomainError, new_id, utc_now
from qorqut_operations.storage import safe_io
from bazarlyk.transport.auth import normalize_origin
from .config import BusinessPaths, HostConfig, business_identity
from .docker import Ownership
from .listener import ListenerServer
from .preview import DockerPreviewObserver, PreviewBinding, PreviewProxy


def _private_ip(value):
    try:
        address = ipaddress.IPv4Address(value)
        # Tailnet addresses use shared CGN space, which is not is_private.
        allowed = any(address in ipaddress.ip_network(network) for network in
                      ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "100.64.0.0/10"))
        if not allowed or address.is_unspecified or address.is_multicast or address.is_reserved:
            raise ValueError
        return str(address)
    except (ValueError, TypeError):
        raise DomainError("invalid_configuration", "Preview access requires explicit private IPv4 addresses") from None


@dataclass(frozen=True)
class PreviewHostConfig:
    bind_address: str
    owner_addresses: tuple[str, ...]
    first_port: int
    last_port: int
    viewer_port: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "bind_address", _private_ip(self.bind_address))
        if not isinstance(self.owner_addresses, tuple) or not 1 <= len(self.owner_addresses) <= 32:
            raise DomainError("invalid_configuration", "Preview access requires explicit owner peers")
        object.__setattr__(self, "owner_addresses", tuple(_private_ip(value) for value in self.owner_addresses))
        if (type(self.first_port) is not int or type(self.last_port) is not int
                or not 1024 <= self.first_port <= self.last_port <= 65535
                or self.last_port - self.first_port >= 256):
            raise DomainError("invalid_configuration", "Preview listener ports require a bounded reserved range")
        if self.viewer_port is not None and (type(self.viewer_port) is not int or not 1024 <= self.viewer_port <= 65535
                                             or self.first_port <= self.viewer_port <= self.last_port):
            raise DomainError("invalid_configuration", "Private browser viewer port must be outside the preview range")

    @classmethod
    def read(cls, paths):
        try:
            value = json.loads(safe_io.read_bytes(paths.runtime / "preview-host.json", private=True, limit=8192))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, DomainError):
            raise DomainError("invalid_configuration", "Private preview configuration cannot be read") from None
        try:
            if not isinstance(value, dict) or value.pop("schema_version", None) != 1:
                raise ValueError
            value["owner_addresses"] = tuple(value["owner_addresses"])
            return cls(**value)
        except (ValueError, TypeError, KeyError):
            raise DomainError("invalid_configuration", "Private preview configuration is invalid") from None

    def origin(self, port):
        if not self.first_port <= port <= self.last_port:
            raise DomainError("invalid_configuration", "Preview registration is outside the configured listener range")
        return f"http://{self.bind_address}:{port}"

    def viewer_origin(self):
        if self.viewer_port is None:
            raise DomainError("configuration_missing", "Private browser viewing is not configured")
        return f"http://{self.bind_address}:{self.viewer_port}"


class PreviewRegistry:
    """Local administration of ports; disabling never frees an origin for reuse."""

    def __init__(self, runtime, dashboard_origins, *, config_loader=PreviewHostConfig.read):
        self.runtime, self.config_loader = runtime, config_loader
        self.dashboard_origins = frozenset(normalize_origin(value) for value in dashboard_origins)
        if not self.dashboard_origins:
            raise ValueError("Preview registration requires known dashboard origins")

    def list(self, business_id=None, *, active_only=True):
        conditions, parameters = [], []
        if active_only:
            conditions.append("active=1")
        if business_id is not None:
            parameters.append(business_identity(business_id))
            conditions.append("business_id=?")
        with self.runtime.database.read() as uow:
            return [dict(row) for row in uow.connection.execute(
                "SELECT * FROM business_previews" + (" WHERE " + " AND ".join(conditions) if conditions else "")
                + " ORDER BY public_port", parameters)]

    def register(self, business_id, port):
        business_identity(business_id)
        if type(port) is not int or not 1024 <= port <= 65535:
            raise DomainError("validation_error", "Preview requires an unprivileged development port")
        with self.runtime.application.writer_admission.writer():
            self.runtime.require_authority()
            config = self.config_loader(self.runtime.paths)
            if not isinstance(config, PreviewHostConfig):
                raise DomainError("configuration_missing", "Private preview access is not configured")
            with self.runtime.database.transaction() as uow:
                uow.repository("businesses").get(business_id, required=True)
                uow.repository("business_sandboxes").get(business_id, required=True)
                current = uow.connection.execute("SELECT * FROM business_previews WHERE business_id=? AND workload_port=?",
                                                 (business_id, port)).fetchone()
                if current:
                    if current["origin"] != config.origin(current["public_port"]):
                        raise DomainError("invalid_configuration", "Preview origin cannot be changed in place")
                    if current["origin"] in self.dashboard_origins:
                        raise DomainError("invalid_origin", "Preview origin conflicts with the dashboard")
                    if current["active"]:
                        return dict(current)
                    uow.connection.execute("UPDATE business_previews SET active=1, revision=revision+1, updated_at=? WHERE id=?",
                                           (utc_now(), current["id"]))
                    return {**dict(current), "active": 1, "revision": current["revision"] + 1}
                reserved = {row[0] for row in uow.connection.execute("SELECT public_port FROM business_previews")}
                public_port = next((value for value in range(config.first_port, config.last_port + 1)
                                    if value not in reserved and config.origin(value) not in self.dashboard_origins), None)
                if public_port is None:
                    raise DomainError("environment_unavailable", "No reserved preview listener port is available")
                row = {"id": new_id(), "business_id": business_id, "workload_port": port, "public_port": public_port,
                       "origin": config.origin(public_port), "active": 1, "revision": 1,
                       "created_at": utc_now(), "updated_at": utc_now()}
                uow.connection.execute("INSERT INTO business_previews (" + ",".join(row) + ") VALUES ("
                                       + ",".join("?" for _ in row) + ")", tuple(row.values()))
                return row

    def disable(self, business_id, port):
        business_identity(business_id)
        with self.runtime.application.writer_admission.writer():
            self.runtime.require_authority()
            with self.runtime.database.transaction() as uow:
                uow.connection.execute("UPDATE business_previews SET active=0, revision=revision+1, updated_at=? "
                                       "WHERE business_id=? AND workload_port=?", (utc_now(), business_id, port))


class _OwnerApplication:
    def __init__(self, proxy):
        self.proxy = proxy

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif event["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        else:
            async def safe_headers(message):
                if message["type"] == "http.response.start":
                    message["headers"] = [(key, value) for key, value in message.get("headers", [])
                                          if key.lower() not in {b"referrer-policy", b"x-content-type-options"}]
                    message["headers"] += [(b"referrer-policy", b"no-referrer"), (b"x-content-type-options", b"nosniff")]
                await send(message)
            await self.proxy(scope, receive, safe_headers)


class BusinessPreviews:
    def __init__(self, runtime, docker, dashboard_origins, *, config_loader=PreviewHostConfig.read,
                 sandbox_config_loader=HostConfig.read, server_factory=ListenerServer, interval=10):
        self.runtime, self.docker, self.config_loader = runtime, docker, config_loader
        self.sandbox_config_loader, self.server_factory = sandbox_config_loader, server_factory
        self.registry = PreviewRegistry(runtime, dashboard_origins, config_loader=config_loader)
        self.interval, self._servers, self._keys = interval, {}, {}
        self._stop, self._thread = threading.Event(), None
        self._guard = threading.RLock()
        from .browser_viewer_service import BusinessBrowserViewers
        self.browser_viewers = BusinessBrowserViewers(runtime, config_loader=config_loader,
            dashboard_origins=dashboard_origins, server_factory=server_factory, interval=interval)

    def binding(self, registration, config):
        self.runtime.require_authority()
        with self.runtime.database.read() as uow:
            saved = uow.connection.execute("SELECT * FROM business_previews WHERE id=?", (registration["id"],)).fetchone()
            if saved is None or not saved["active"] or dict(saved) != registration:
                raise DomainError("environment_conflict", "Preview registration is stale")
            business = uow.repository("businesses").get(registration["business_id"], required=True)
            state = uow.repository("business_sandboxes").get(business["id"], required=True)
        if (state["state"] != "ready" or state["desired_state"] != "running"
                or state["observed_generation"] != state["desired_generation"]):
            raise DomainError("environment_unavailable", "Preview business environment is not ready")
        current_config = self.config_loader(self.runtime.paths)
        if current_config != config or registration["origin"] != config.origin(registration["public_port"]):
            raise DomainError("environment_conflict", "Preview owner access configuration changed")
        if registration["origin"] in self.registry.dashboard_origins:
            raise DomainError("invalid_origin", "Preview origin conflicts with the dashboard")
        sandbox = self.sandbox_config_loader(self.runtime.paths)
        if not isinstance(sandbox, HostConfig) or not sandbox.enabled:
            raise DomainError("configuration_missing", "Sandbox execution is disabled")
        paths = BusinessPaths.registered(self.runtime.paths, business)
        owner = Ownership(hashlib.sha256(str(paths.root).encode()).hexdigest()[:20], business["id"])
        current = self.docker.owned("container", state["environment_id"], owner)
        network_name = paths.resource_prefix + "-agent"
        network = self.docker.owned("network", network_name, owner)
        if (not current or current.get("Config", {}).get("Image") != sandbox.workstation_image
                or not network or not network.get("Internal")
                or state["environment_id"] not in network.get("Containers", {})):
            raise DomainError("environment_conflict", "Preview workstation network is unavailable")
        address = current.get("NetworkSettings", {}).get("Networks", {}).get(network_name, {}).get("IPAddress")
        try:
            endpoint = ipaddress.IPv4Address(address)
            subnets = network["IPAM"]["Config"]
            member = network["Containers"][state["environment_id"]]["IPv4Address"]
            valid = (not endpoint.is_loopback and not endpoint.is_link_local
                and ipaddress.ip_interface(member).ip == endpoint
                and any(endpoint in ipaddress.ip_network(item["Subnet"])
                        and str(endpoint) != item.get("Gateway")
                        and endpoint not in {ipaddress.ip_network(item["Subnet"]).network_address,
                                             ipaddress.ip_network(item["Subnet"]).broadcast_address}
                        for item in subnets))
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise DomainError("environment_conflict", "Preview address is outside its observed private workload network")
        key = self._keys.setdefault(registration["id"], secrets.token_urlsafe(32))
        binding = PreviewBinding(business["id"], registration["origin"], key, state["environment_id"],
                                 state["observed_generation"], address, registration["workload_port"], network_name)
        if DockerPreviewObserver(self.docker, owner, current_binding=lambda: binding)(binding) is not True:
            raise DomainError("environment_conflict", "Preview workstation identity is stale")
        return binding

    def reconcile(self):
        with self.runtime.application.writer_admission.writer(), self._guard:
            self.runtime.require_authority()
            config = self.config_loader(self.runtime.paths)
            wanted = set()
            try:
                for row in self.registry.list() if config is not None else ():
                    identity = row["id"]
                    try:
                        binding = self.binding(row, config)
                    except DomainError:
                        continue
                    wanted.add(identity)
                    old = self._servers.get(identity)
                    if old and old[0] == binding and old[1] == config and old[2].running:
                        continue
                    if old:
                        old[2].stop(timeout=5)
                        del self._servers[identity]
                    proxy = PreviewProxy(binding, revalidate=lambda _binding, row=row: self.binding(row, config),
                        access_check=lambda scope, _binding: bool(scope.get("client"))
                        and scope["client"][0] in config.owner_addresses)
                    server = self.server_factory(_OwnerApplication(proxy), config.bind_address, row["public_port"])
                    self._servers[identity] = binding, config, server
                    server.start()
            finally:
                for identity in set(self._servers) - wanted:
                    self._servers[identity][2].stop(timeout=5)
                    del self._servers[identity]

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self.browser_viewers.start()
        self._stop.clear()
        def run():
            while not self._stop.is_set():
                try:
                    self.reconcile()
                except (DomainError, OSError, TimeoutError):
                    pass  # Existing status endpoint reflects missing/unready bindings.
                self._stop.wait(self.interval)
        self._thread = threading.Thread(target=run, name="qorqut-business-previews", daemon=True)
        self._thread.start()

    def stop(self, *, timeout=5):
        deadline = monotonic() + timeout
        self._stop.set()
        failure = None
        if self._thread:
            self._thread.join(max(0, deadline - monotonic()))
            if self._thread.is_alive():
                failure = TimeoutError("Business preview reconciliation is still draining")
        if failure is None:
            with self._guard:
                servers = list(self._servers.items())
            for identity, (_, _, server) in servers:
                try:
                    server.stop(timeout=max(0, deadline - monotonic()))
                    with self._guard:
                        if self._servers.get(identity, (None, None, None))[2] is server:
                            del self._servers[identity]
                except TimeoutError as exc:
                    failure = failure or exc
        # Viewer has an independent lifecycle lock, so still give it the
        # remaining aggregate budget even if preview reconciliation is stuck.
        try:
            self.browser_viewers.stop(timeout=max(0, deadline - monotonic()))
        except TimeoutError as exc:
            failure = failure or exc
        if failure:
            raise failure
