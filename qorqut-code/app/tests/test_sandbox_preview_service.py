"""Persistent port ownership and a usable private owner navigation path."""

from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import threading
from types import SimpleNamespace

import httpx
import pytest

from qorqut_operations.contracts import DomainError, new_id, utc_now
from bazarlyk.sandbox.config import BusinessPaths, HostConfig, ResourceLimits
from bazarlyk.sandbox.preview_service import BusinessPreviews, PreviewHostConfig, PreviewRegistry
from qorqut_operations.storage.admission import RecoveryAdmission, InstallationRecoveryLock
from bazarlyk.sandbox.state import SandboxStateStore
from bazarlyk.storage import Database


IMAGE = "sha256:" + "a" * 64


@pytest.fixture
def fixture(tmp_path):
    database = Database.for_root(tmp_path).initialize()
    businesses = []
    with database.transaction() as uow:
        for name in ("first", "second"):
            workspace = tmp_path / name
            (workspace / "hq").mkdir(parents=True)
            row = uow.repository("businesses").insert({"id": new_id(), "slug": name, "name": name,
                "workspace": str(workspace), "hq_path": str(workspace / "hq")})
            SandboxStateStore().insert_intent(uow, row["id"])
            businesses.append(row)
    lock = InstallationRecoveryLock(tmp_path)
    runtime = SimpleNamespace(root=tmp_path, database=database, require_authority=lambda: None,
        application=SimpleNamespace(writer_admission=RecoveryAdmission(installation_lock=lock)))
    return runtime, businesses


def config(port=20000, owners=("127.0.0.1",)):
    return PreviewHostConfig("127.0.0.1", owners, port, port + 2)


def test_origins_persist_and_disabled_ports_cannot_be_reassigned(fixture):
    runtime, (first, second) = fixture
    registry = PreviewRegistry(runtime, ["http://127.0.0.1:20000"], config_loader=lambda _: config())
    one = registry.register(first["id"], 3000)
    assert one["public_port"] == 20001  # dashboard port is never allocated
    assert registry.register(first["id"], 3000) == one
    registry.disable(first["id"], 3000)
    restarted = PreviewRegistry(runtime, ["http://127.0.0.1:20000"], config_loader=lambda _: config())
    two = restarted.register(second["id"], 3000)
    assert two["public_port"] == 20002
    assert restarted.register(first["id"], 3000)["origin"] == one["origin"]
    with pytest.raises(DomainError, match="No reserved"):
        restarted.register(second["id"], 4000)


def test_registration_requires_admission_and_stable_host_binding(fixture):
    runtime, (first, _) = fixture
    current = [config()]
    registry = PreviewRegistry(runtime, ["http://127.0.0.1:1049"], config_loader=lambda _: current[0])
    registry.register(first["id"], 3000)
    current[0] = PreviewHostConfig("127.0.0.2", ("127.0.0.1",), 20000, 20002)
    with pytest.raises(DomainError, match="cannot be changed"):
        registry.register(first["id"], 3000)
    with InstallationRecoveryLock(runtime.root).exclusive(timeout=1):
        with pytest.raises(DomainError, match="during recovery"):
            registry.disable(first["id"], 3000)
    assert len(registry.list()) == 1


def test_private_config_rejects_public_unspecified_metadata_and_fifo(tmp_path):
    for address in ("0.0.0.0", "1.1.1.1", "169.254.169.254", "0.1.2.3", "::1"):
        with pytest.raises(DomainError):
            PreviewHostConfig(address, ("127.0.0.1",), 20000, 20002)
    assert PreviewHostConfig("100.64.12.45", ("100.65.0.2",), 20000, 20002)
    private = tmp_path / ".bazarlyk"
    private.mkdir()
    __import__("os").mkfifo(private / "preview-host.json")
    with pytest.raises(DomainError):
        PreviewHostConfig.read(tmp_path)


def free_port():
    with socket.socket() as current:
        current.bind(("127.0.0.1", 0))
        return current.getsockname()[1]


class DockerFixture:
    def __init__(self, paths, identity):
        network = paths.resource_prefix + "-agent"
        self.container = {"Id": identity, "State": {"Running": True},
            "Config": {"Image": IMAGE, "Labels": {"qorqut.role": "agent", "qorqut.generation": "1"}},
            "NetworkSettings": {"Networks": {network: {"IPAddress": "172.18.9.2"}}}}
        self.network = {"Internal": True, "Containers": {identity: {"IPv4Address": "172.18.9.2/24"}},
                        "IPAM": {"Config": [{"Subnet": "172.18.9.0/24", "Gateway": "172.18.9.1"}]}}

    def owned(self, kind, identity, owner):
        return self.container if kind == "container" else self.network


@contextmanager
def upstream():
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(dict(self.headers.items()))
            self.send_response(200)
            self.send_header("Set-Cookie", "untrusted=1; Domain=127.0.0.1")
            self.end_headers()
            self.wfile.write(b"private preview")
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def ready_manager(fixture, owner_addresses):
    runtime, (business, _) = fixture
    identity = "b" * 64
    with runtime.database.transaction() as uow:
        store = SandboxStateStore()
        store.mark_ready(uow, store.claim(uow, business["id"]), environment_id=identity)
    docker = DockerFixture(BusinessPaths.registered(runtime.root, business), identity)
    selected = config(free_port(), owner_addresses)
    sandbox = HostConfig(IMAGE, IMAGE, ResourceLimits(512, 512, 256, 1, 512, 8), 1148, enabled=True)
    manager = BusinessPreviews(runtime, docker, ["http://127.0.0.1:1049"], config_loader=lambda _: selected,
        sandbox_config_loader=lambda _: sandbox)
    # Native owner listener/transport acceptance, with an explicit upstream
    # fixture mapping after production DB/Docker-network validation. This does
    # not claim an actual Docker workload was created by this test.
    observed_binding = manager.binding
    manager.binding = lambda row, cfg: replace(observed_binding(row, cfg), address="127.0.0.1")
    return manager, business


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "172.18.9.1", "172.18.10.2"])
def test_preview_rejects_host_gateway_metadata_and_foreign_subnet_before_connect(fixture, address):
    manager, business = ready_manager(fixture, ("127.0.0.1",))
    with upstream() as (port, seen):
        row = manager.registry.register(business["id"], port)
        selected = manager.config_loader(manager.runtime.root)
        network = next(iter(manager.docker.container["NetworkSettings"]["Networks"].values()))
        network["IPAddress"] = address
        with pytest.raises(DomainError, match="outside its observed"):
            manager.binding(row, selected)
        assert seen == []


def test_native_owner_navigation_reaches_only_registered_app_and_strips_credentials(fixture):
    manager, business = ready_manager(fixture, ("127.0.0.1",))
    with upstream() as (port, seen):
        row = manager.registry.register(business["id"], port)
        try:
            manager.reconcile()
            with httpx.Client(trust_env=False) as client:
                result = client.get(row["origin"], headers={"Cookie": "owner=secret", "Authorization": "Bearer secret"})
                assert result.status_code == 200 and result.text == "private preview"
                assert "set-cookie" not in result.headers
                assert not any(name.lower() in {"cookie", "authorization"} for name in seen[-1])
                assert client.get(row["origin"], headers={"Origin": "http://127.0.0.1:1049"}).status_code == 403
                manager.registry.disable(business["id"], port)
                assert client.get(row["origin"]).status_code == 409
                assert len(seen) == 1
            manager.reconcile()
            assert manager._servers == {}
        finally:
            manager.stop()


def test_native_preview_rejects_unapproved_peer_and_forwarded_spoof(fixture):
    manager, business = ready_manager(fixture, ("127.0.0.2",))
    with upstream() as (port, seen):
        row = manager.registry.register(business["id"], port)
        try:
            manager.reconcile()
            result = httpx.get(row["origin"], trust_env=False, headers={"X-Forwarded-For": "127.0.0.2"})
            assert result.status_code == 403 and seen == []
        finally:
            manager.stop()
