"""Owner viewer composition tests without a browser, Docker, or model call."""

import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest

from bazarlyk.browser.control import business_control
from bazarlyk.browser.socket import BrowserBinding
from qorqut_operations.contracts import DomainError, Principal, new_id
from bazarlyk.sandbox.browser_viewer_service import BusinessBrowserViewers
from bazarlyk.sandbox.listener import ListenerServer, PrivatePeerBoundary
from bazarlyk.sandbox.preview_service import PreviewHostConfig
from qorqut_operations.storage.admission import InstallationRecoveryLock, RecoveryAdmission
from bazarlyk.sandbox.state import SandboxStateStore
from test_agent_configuration import Fixture
from test_business_browser import configure, roles


class Server:
    def __init__(self, app, address, port):
        self.app, self.address, self.port, self.running = app, address, port, False

    def start(self):
        self.running = True

    def stop(self, *, timeout=5):
        self.running = False


class BlockingServer(Server):
    stopped, release = threading.Event(), threading.Event()

    def stop(self, *, timeout=5):
        self.stopped.set()
        assert self.release.wait(timeout)
        super().stop(timeout=timeout)


def request(app, method, path, *, peer="127.0.0.1", origin=True, headers=None):
    async def run():
        transport = httpx.ASGITransport(app=app, client=(peer, 30000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:20100") as client:
            values = {"Origin": "http://127.0.0.1:20100"} if origin else {}
            return await client.request(method, path, headers={**values, **(headers or {})})
    return asyncio.run(run())


def test_viewer_native_listener_completes_startup_and_shutdown():
    viewer = BusinessBrowserViewers(SimpleNamespace(), config_loader=lambda _: None)
    server = ListenerServer(PrivatePeerBoundary(viewer, ("127.0.0.1",)), "127.0.0.1", 0)
    try:
        server.start()
        assert server.running
        with httpx.Client(trust_env=False, timeout=5) as client:
            assert client.get(f"http://127.0.0.1:{server.port}/").status_code == 404
    finally:
        server.stop(timeout=5)
    assert not server.running and not server.thread.is_alive()


def manager(tmp_path, roles):
    fixture = Fixture(tmp_path)
    agent = fixture.create()
    with fixture.db.transaction() as uow:
        uow.repository("installation").insert({"id": "service", "installation_id": new_id(), "owner_principal_id": fixture.owner.id})
    configure(fixture.db, fixture.root, fixture.business_id)
    environment = "a" * 64
    with fixture.db.transaction() as uow:
        store = SandboxStateStore()
        store.insert_intent(uow, fixture.business_id)
        store.mark_ready(uow, store.claim(uow, fixture.business_id), environment_id=environment)
    binding = [BrowserBinding(fixture.business_id, "b" * 64, 1, "b" * 64 + ":44:99", 19400)]
    runtime = SimpleNamespace(root=fixture.root, database=fixture.db, resolver=fixture.resolver,
        require_authority=lambda: None,
        application=SimpleNamespace(writer_admission=RecoveryAdmission(installation_lock=InstallationRecoveryLock(fixture.root))),
        sandbox=SimpleNamespace(docker=object(), observed_browser_binding=lambda identity: binding[0] if identity == fixture.business_id else None))
    selected = PreviewHostConfig("127.0.0.1", ("127.0.0.1",), 20000, 20002, 20100)
    value = BusinessBrowserViewers(runtime, config_loader=lambda _: selected, server_factory=Server)
    value.reconcile()
    return value, fixture, agent, binding, selected


def test_owner_viewer_requires_peer_origin_active_membership_and_explicit_same_lease(roles, tmp_path):
    value, fixture, _agent, binding, selected = manager(tmp_path, roles)
    app = value._server.app
    root = "/" + fixture.business_id + "/"
    assert value.link(fixture.business_id) == selected.viewer_origin() + root
    page = request(app, "GET", root)
    assert page.status_code == 200, page.text
    assert request(app, "GET", root, peer="127.0.0.2").status_code == 403
    assert request(app, "GET", root, peer="127.0.0.2", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
    assert request(app, "GET", root, origin=False).status_code == 200
    assert request(app, "GET", root, peer="127.0.0.1", origin=True).headers["cache-control"] == "no-store"
    assert request(app, "POST", root + "api/acquire", origin=False).status_code == 403
    assert request(app, "POST", root + "api/acquire").json() == {"acquired": True}
    other = Principal(new_id(), "human", "Other owner")
    with fixture.db.transaction() as uow:
        uow.repository("principals").insert({"id": other.id, "kind": "human", "display_name": other.display_name})
        uow.repository("business_memberships", business_id=fixture.business_id).insert({"principal_id": other.id, "is_owner": True})
    with pytest.raises(DomainError, match="Another task"):
        business_control(fixture.resolver, other, "acquire", "d" * 64, owner_business_id=fixture.business_id,
                         sandbox_target=lambda business, config: {"generation": 1, "instance": binding[0].instance_id})
    assert request(app, "POST", root + "api/release").json() == {"released": True}
    assert request(app, "GET", root, origin=True).status_code == 200
    with fixture.db.transaction() as uow:
        membership = uow.repository("business_memberships", business_id=fixture.business_id).get(fixture.owner.id)
        uow.repository("business_memberships", business_id=fixture.business_id).update(fixture.owner.id, {"active": False},
                                                                                        expected_revision=membership["revision"])
    assert request(app, "GET", root).status_code == 403


def test_owner_viewer_rejects_stale_replacement_and_never_exposes_raw_cdp(roles, tmp_path):
    value, fixture, _agent, binding, _selected = manager(tmp_path, roles)
    app, root = value._server.app, "/" + fixture.business_id + "/"
    assert request(app, "POST", root + "api/acquire").status_code == 200
    binding[0] = BrowserBinding(fixture.business_id, "c" * 64, 1, "c" * 64 + ":45:100", 19401)
    assert request(app, "GET", root).status_code == 409
    value.reconcile()
    assert request(value._server.app, "POST", root + "api/acquire").status_code == 200
    page = request(value._server.app, "GET", root)
    assert "127.0.0.1:19401" not in page.text and "devtools" not in page.text


def test_owner_lease_changes_cannot_bypass_cross_process_recovery_admission(roles, tmp_path):
    value, fixture, _agent, _binding, _selected = manager(tmp_path, roles)
    app, root = value._server.app, "/" + fixture.business_id + "/"
    with value.runtime.application.writer_admission.installation_lock.exclusive(timeout=1):
        response = request(app, "POST", root + "api/acquire")
        assert response.status_code == 400 and response.json() == {"error": "maintenance"}
    lease = fixture.root / ".bazarlyk/browsers" / fixture.business_id / "lease.json"
    assert not lease.exists()
    assert request(app, "POST", root + "api/acquire").status_code == 200
    before = lease.read_bytes()
    with value.runtime.application.writer_admission.installation_lock.exclusive(timeout=1):
        assert request(app, "POST", root + "api/release").json() == {"error": "maintenance"}
    assert lease.read_bytes() == before
    assert request(app, "POST", root + "api/release").status_code == 200
    assert request(app, "GET", root + "%C3%A9").status_code == 403


def test_viewer_configuration_reserves_a_separate_private_origin(roles, tmp_path):
    with pytest.raises(DomainError, match="outside the preview range"):
        PreviewHostConfig("127.0.0.1", ("127.0.0.1",), 20000, 20002, 20001)
    value, fixture, _agent, _binding, _selected = manager(tmp_path, roles)
    conflicted = PreviewHostConfig("127.0.0.1", ("127.0.0.1",), 20000, 20002, 20100)
    other = BusinessBrowserViewers(value.runtime, config_loader=lambda _: conflicted,
        dashboard_origins=[conflicted.viewer_origin()], server_factory=Server)
    with pytest.raises(DomainError, match="conflicts with the dashboard"):
        other.reconcile()


def test_viewer_retries_failed_listener_and_never_joins_under_route_guard(roles, tmp_path):
    value, fixture, _agent, _binding, selected = manager(tmp_path, roles)
    value.stop()
    attempts = []
    class Flaky(Server):
        def start(self):
            attempts.append(self)
            if len(attempts) == 1:
                raise OSError("synthetic bind failure")
            super().start()
    value.server_factory = Flaky
    with pytest.raises(OSError):
        value.reconcile()
    assert value._server is None
    value.reconcile()
    assert value._server.running and len(attempts) == 2

    selected_config = [selected]
    value.config_loader = lambda _: selected_config[0]
    BlockingServer.stopped.clear()
    BlockingServer.release.clear()
    value.stop()
    value.server_factory = BlockingServer
    value.reconcile()
    selected_config[0] = PreviewHostConfig("127.0.0.1", ("127.0.0.1",), 20000, 20002, 20101)
    worker = threading.Thread(target=value.reconcile)
    worker.start()
    assert BlockingServer.stopped.wait(1)
    # The old server remains usable while its bounded stop waits.  This would
    # deadlock if route lookup needed the lifecycle lock or a held read guard.
    assert request(value._server.app, "GET", "/" + fixture.business_id + "/").status_code == 200
    BlockingServer.release.set()
    worker.join(2)
    assert not worker.is_alive() and value._server.port == 20101


def test_viewer_stop_does_not_deadlock_on_queued_reconcile_or_forget_uncertain_start(roles, tmp_path):
    value, _fixture, _agent, _binding, _selected = manager(tmp_path, roles)
    original = value._server
    assert value._lifecycle.acquire(timeout=1)
    worker = threading.Thread(target=value.reconcile)
    value._thread = worker
    worker.start()
    try:
        with pytest.raises(TimeoutError, match="reconciliation"):
            value.stop(timeout=0.01)
        assert value._server is original
    finally:
        value._lifecycle.release()
    worker.join(2)
    assert not worker.is_alive()

    value._thread = None
    value.stop()
    pending = []
    class PartialStart(Server):
        stop_fails = True
        def start(self):
            self.running = True
            raise OSError("synthetic partial start")
        def stop(self, *, timeout=5):
            if self.stop_fails:
                raise TimeoutError("synthetic uncertain writer")
            super().stop(timeout=timeout)
    value.server_factory = PartialStart
    with pytest.raises(OSError):
        value.reconcile()
    pending_server = value._server
    assert isinstance(pending_server, PartialStart)
    with pytest.raises(TimeoutError, match="uncertain"):
        value.reconcile()
    assert value._server is pending_server
    pending_server.stop_fails = False
    value.server_factory = Server
    value.reconcile()
    assert isinstance(value._server, Server) and value._server is not pending_server


def test_preview_stop_uses_one_deadline_for_preview_and_viewer(monkeypatch, roles, tmp_path):
    from bazarlyk.sandbox import preview_service
    from bazarlyk.sandbox.preview_service import BusinessPreviews
    value, _fixture, _agent, _binding, _selected = manager(tmp_path, roles)
    clock, timeouts = [10.0], []
    monkeypatch.setattr(preview_service, "monotonic", lambda: clock[0])
    class TickServer:
        def stop(self, *, timeout=5):
            timeouts.append(timeout)
            clock[0] += 2
    class TickViewers:
        def stop(self, *, timeout=5):
            timeouts.append(timeout)
            clock[0] += 2
    previews = BusinessPreviews(value.runtime, object(), ["http://127.0.0.1:1049"],
        config_loader=lambda _: None, server_factory=Server)
    previews._servers = {"one": (None, None, TickServer())}
    previews.browser_viewers = TickViewers()
    previews.stop(timeout=3)
    assert timeouts == [3.0, 1.0]
