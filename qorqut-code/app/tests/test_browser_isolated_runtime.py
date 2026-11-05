"""Isolated desktop runtime pieces that need no root: deployment, probe, bridges, gateway and readiness."""
import errno
import json
import os
from pathlib import Path
import socket
import stat
import threading
import time
from types import SimpleNamespace

import pytest

from bazarlyk.browser import config, control
from bazarlyk.browser.isolated import bridge, deployment as deploy, gateway, netns, ready, session, setup
from bazarlyk.browser.isolated.processes import netns_identity
from qorqut_operations.contracts import DomainError
from foundation_support import BUSINESS, database_at
from test_browser_isolated_config import isolated, propose

UUID = BUSINESS


def deployment_value(tmp_path, **changes):
    value = {"schema_version": 1, "business_id": UUID, "root": str(tmp_path), "display": 23,
             "geometry": {"width": 1440, "height": 900}, "vnc": {"interface": "100.64.0.10", "port": 5923},
             "runtime_dir": str(tmp_path / "run"), "tools_dir": str(tmp_path / "tools")}
    return {**value, **changes}


# --- deployment -----------------------------------------------------------------------------------------


def test_deployment_parses_defaults_and_derives_private_paths(tmp_path):
    item = deploy.parse(deployment_value(tmp_path, node="/usr/bin/node", recheck_seconds=30))
    assert item.display_name == ":23" and item.node == "/usr/bin/node" and item.recheck_seconds == 30
    assert item.state_dir == tmp_path / ".bazarlyk/browsers" / UUID
    assert item.vnc_password_file == tmp_path / ".bazarlyk/secrets/browser-desktop" / UUID / "vnc.passwd"
    assert item.upstream_socket.parent == item.cdp_socket.parent == item.vnc_socket.parent == item.runtime_dir


@pytest.mark.parametrize("change", [
    {"schema_version": 2}, {"business_id": "smoke"}, {"business_id": "Business-49"}, {"display": 0}, {"display": "23"},
    {"geometry": {"width": 10, "height": 900}}, {"vnc": {"interface": "0.0.0.0", "port": 5923}},
    {"vnc": {"interface": "8.8.8.8", "port": 5923}}, {"vnc": {"interface": "fd7a::1", "port": 5923}},
    {"vnc": {"interface": "100.64.0.10", "port": 80}}, {"root": "relative"}, {"runtime_dir": "/run/../etc"},
    {"tools_dir": "tools"}, {"unknown": 1}, {"node": "node"}, {"recheck_seconds": 1},
    {"exit_check": {"host": "1.1.1.1", "path": "/"}}, {"exit_check": {"host": "api.ipify.org", "path": "x"}},
    {"proxy_server": "http://198.51.100.1:80"}, {"chrome": "/bin/sh"},
])
def test_deployment_refuses_anything_beyond_host_placement(tmp_path, change):
    with pytest.raises(DomainError):
        deploy.parse(deployment_value(tmp_path, **change))


def test_deployment_file_must_be_an_ordinary_operator_file(tmp_path):
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(deployment_value(tmp_path)))
    path.chmod(0o600)
    assert deploy.load(path).business_id == UUID
    path.chmod(0o666)
    with pytest.raises(DomainError):
        deploy.load(path)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    path.chmod(0o600)
    with pytest.raises(DomainError):
        deploy.load(link)
    path.write_text("not json")
    with pytest.raises(DomainError):
        deploy.load(path)


def test_deployment_collisions_include_other_desktops(tmp_path):
    first = deploy.parse(deployment_value(tmp_path))
    other = deploy.parse(deployment_value(tmp_path, business_id="7a49b000-0000-4000-8000-000000000049"))
    assert any("display 23" in p for p in deploy.collisions([first, other]))
    assert any("VNC port 5923" in p for p in deploy.collisions([first, other]))
    assert any("runtime directory" in p for p in deploy.collisions([first, other]))
    apart = deploy.parse(deployment_value(tmp_path, business_id="7a49b000-0000-4000-8000-000000000049", display=24,
                                          vnc={"interface": "100.64.0.10", "port": 5924}, runtime_dir=str(tmp_path / "run2")))
    assert deploy.collisions([first, apart]) == []
    assert deploy.collisions([first], desktops=[("ordubashy", 20, [5920, 9225]), ("marketing", 21, [5921])]) == []
    assert deploy.collisions([first], desktops=[("outlook", 23, [5922])]) == ["display 23 is used by both isolated " + UUID + " and desktop outlook"]


# --- namespace probe ------------------------------------------------------------------------------------


def fake_net(tmp_path, *, devices=("lo",), routes=(), v6=("00000000000000000000000000000001 80 00000000000000000000000000000000 00 00000000000000000000000000000000 00000000 00000001 00000000 80200001       lo",)):
    net = tmp_path / "net"
    net.mkdir(exist_ok=True)
    (net / "dev").write_text("Inter-|   Receive\n face |bytes\n" + "".join(f"  {name}: 0 0\n" for name in devices))
    (net / "route").write_text("Iface\tDestination\tGateway\n" + "".join(routes))
    (net / "ipv6_route").write_text("\n".join(v6) + "\n")
    return net


def test_probe_accepts_only_loopback_without_routes(tmp_path):
    result = netns.probe(fake_net(tmp_path), unrouted=())
    assert result["interfaces"] == ["lo"] and result["netns"].startswith("net:[")


@pytest.mark.parametrize("kwargs", [
    {"devices": ("lo", "eth0")}, {"devices": ("lo", "tailscale0")}, {"devices": ("eth0",)},
    {"routes": ("eth0\t00000000\t0102A8C0\n",)},
    {"v6": ("fe800000000000000000000000000000 40 00000000000000000000000000000000 00 00000000000000000000000000000000 00000100 00000001 00000000 00000001     eth0",)},
])
def test_probe_refuses_any_other_interface_or_route(tmp_path, kwargs):
    with pytest.raises(DomainError, match="loopback-only"):
        netns.probe(fake_net(tmp_path, **kwargs), unrouted=())


def test_probe_needs_unrouted_addresses_to_be_unreachable(tmp_path):
    # A routed host (this test process's own network) does not pass the connection probes: an unrouted
    # address either connects (UDP), hangs or is refused, none of which is ENETUNREACH.
    with pytest.raises(DomainError, match="loopback-only"):
        netns.probe(fake_net(tmp_path), unrouted=((socket.AF_INET, socket.SOCK_DGRAM, "127.0.0.1"),))


# --- bridges --------------------------------------------------------------------------------------------


def echo_server(path=None):
    server = socket.socket(socket.AF_UNIX if path else socket.AF_INET)
    server.bind(str(path) if path else ("127.0.0.1", 0))
    server.listen(8)
    def serve():
        while True:
            try:
                client, _ = server.accept()
            except OSError:
                return
            def one(connection):
                with connection:
                    while data := connection.recv(4096):
                        connection.sendall(data.upper())
            threading.Thread(target=one, args=(client,), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    return server


def exchange(client, data=b"hello"):
    client.settimeout(5)
    client.sendall(data)
    return client.recv(64)


def test_forwarder_copies_bytes_to_one_fixed_destination(tmp_path):
    backend = echo_server(tmp_path / "backend.sock")
    listener = bridge.tcp_listener("127.0.0.1", 0)
    forwarder = bridge.Forwarder("test", listener, lambda: bridge.dial_unix(tmp_path / "backend.sock")).start()
    try:
        with socket.create_connection(listener.getsockname()) as client:
            assert exchange(client) == b"HELLO"
            assert exchange(client, b"again") == b"AGAIN"
        assert forwarder.alive()
    finally:
        forwarder.close()
        backend.close()
    assert not forwarder.alive()
    with pytest.raises(OSError):
        socket.create_connection(listener.getsockname(), timeout=1)


def test_forwarder_survives_a_dead_destination_and_bounds_connections(tmp_path):
    listener = bridge.tcp_listener("127.0.0.1", 0)
    forwarder = bridge.Forwarder("test", listener, lambda: bridge.dial_unix(tmp_path / "absent.sock"), limit=2).start()
    try:
        with socket.create_connection(listener.getsockname()) as client:
            client.settimeout(5)
            assert client.recv(8) == b""  # destination unreachable: closed, not hung
        assert forwarder.alive()
    finally:
        forwarder.close()
    unstarted = bridge.Forwarder("idle", bridge.tcp_listener("127.0.0.1", 0), lambda: None)
    unstarted.close()  # closing a forwarder that never started must not raise


def test_unix_listener_modes_and_refusals(tmp_path):
    path = tmp_path / "one.sock"
    listener = bridge.unix_listener(path)
    assert stat.S_IMODE(path.lstat().st_mode) == 0o600
    with pytest.raises(DomainError, match="already served"):
        bridge.unix_listener(path, replace_stale=True)
    listener.close()
    with pytest.raises(DomainError, match="stale"):
        bridge.unix_listener(path)
    bridge.unix_listener(path, replace_stale=True).close()
    (tmp_path / "regular").write_text("x")
    with pytest.raises(DomainError, match="another object"):
        bridge.unix_listener(tmp_path / "regular", replace_stale=True)
    assert (tmp_path / "regular").read_text() == "x"
    for wildcard in ("0.0.0.0", "", "::"):
        with pytest.raises(DomainError):
            bridge.tcp_listener(wildcard, 0)
    busy = bridge.tcp_listener("127.0.0.1", 0)
    with pytest.raises(DomainError, match="in use"):
        bridge.tcp_listener("127.0.0.1", busy.getsockname()[1])
    busy.close()


# --- gateway --------------------------------------------------------------------------------------------


class Gateway:
    """Real gateway thread against a temp database, temp runtime directory and a local stand-in proxy."""

    def __init__(self, tmp_path, monkeypatch, **assign):
        monkeypatch.setattr(config, "port_available", lambda port: True)
        self.tmp_path, self.db = tmp_path, database_at(tmp_path)
        self.block = propose(self.db, tmp_path, BUSINESS, isolated(**assign))
        self.runtime = tmp_path / "run"
        self.runtime.mkdir(mode=0o700)
        vnc = socket.socket()
        vnc.bind(("127.0.0.1", 0))
        self.vnc_port = vnc.getsockname()[1]
        vnc.close()
        self.deployment = deploy.parse(deployment_value(tmp_path, vnc={"interface": "127.0.0.1", "port": self.vnc_port}))
        self.proxy = echo_server()
        proxy_port = self.proxy.getsockname()[1]
        monkeypatch.setattr(gateway, "proxy_endpoint", lambda server: ("127.0.0.1", proxy_port))
        self.stop, self.error = threading.Event(), []

    def start(self):
        def run():
            try:
                gateway.serve(self.db, self.deployment, stop=self.stop)
            except BaseException as error:
                self.error.append(error)
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        return self

    def published(self, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if gateway.gateway_file(self.deployment).exists():
                return json.loads(gateway.gateway_file(self.deployment).read_text())
            if self.error:
                raise self.error[0]
            time.sleep(0.05)
        raise AssertionError("gateway never published")

    def finish(self):
        self.stop.set()
        self.thread.join(10)
        self.proxy.close()


@pytest.fixture
def running_gateway(tmp_path, monkeypatch):
    fixture = Gateway(tmp_path, monkeypatch).start()
    yield fixture
    fixture.finish()


def test_gateway_bridges_only_its_three_fixed_paths_and_cleans_up(tmp_path, monkeypatch):
    fixture = Gateway(tmp_path, monkeypatch).start()
    state = fixture.published()
    try:
        assert state["business_id"] == BUSINESS and state["generation"] == fixture.block["generation"]
        assert state["pid"] == os.getpid() and state["netns"] == netns_identity()
        assert stat.S_IMODE((fixture.runtime / "upstream.sock").lstat().st_mode) == 0o600
        assert stat.S_IMODE(gateway.gateway_file(fixture.deployment).lstat().st_mode) == 0o600
        # 1. upstream socket -> the frozen proxy address; the gateway sees bytes only, never credentials.
        with bridge.dial_unix(fixture.runtime / "upstream.sock") as client:
            assert exchange(client) == b"HELLO"
        # 2. host loopback CDP and 3. VNC listener -> the namespace's sockets (absent here: closed, not fallback).
        cdp_backend, vnc_backend = echo_server(fixture.runtime / "cdp.sock"), echo_server(fixture.runtime / "vnc.sock")
        try:
            with socket.create_connection(("127.0.0.1", fixture.block["cdp_port"])) as client:
                assert exchange(client, b"cdp") == b"CDP"
            with socket.create_connection(("127.0.0.1", fixture.vnc_port)) as client:
                assert exchange(client, b"rfb") == b"RFB"
        finally:
            cdp_backend.close()
            vnc_backend.close()
        assert control.owns_listener(os.getpid(), fixture.block["cdp_port"], "127.0.0.1")
        assert not control.owns_listener(os.getpid(), fixture.block["cdp_port"], "10.9.9.9")
    finally:
        fixture.finish()
    assert not fixture.error
    assert not gateway.gateway_file(fixture.deployment).exists() and not (fixture.runtime / "upstream.sock").exists()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", fixture.vnc_port), timeout=1)


def test_second_gateway_and_occupied_ports_are_refused(tmp_path, monkeypatch):
    fixture = Gateway(tmp_path, monkeypatch).start()
    fixture.published()
    try:
        with pytest.raises(DomainError, match="already running"):
            gateway.serve(fixture.db, fixture.deployment, stop=threading.Event())
        assert gateway.gateway_file(fixture.deployment).exists() and fixture.thread.is_alive()
    finally:
        fixture.finish()
    occupant = socket.socket()
    occupant.bind(("127.0.0.1", fixture.block["cdp_port"]))
    occupant.listen(1)
    try:
        with pytest.raises(DomainError) as refusal:
            gateway.serve(fixture.db, fixture.deployment, stop=threading.Event())
        assert refusal.value.code == "busy"
        occupant.settimeout(0.2)
        with pytest.raises(socket.timeout):
            occupant.accept()  # never connected to, adopted or closed
        assert not (fixture.runtime / "upstream.sock").exists()
    finally:
        occupant.close()


def test_gateway_stops_on_config_change_and_refuses_wrong_state(tmp_path, monkeypatch):
    fixture = Gateway(tmp_path, monkeypatch).start()
    fixture.published()
    propose(fixture.db, tmp_path, BUSINESS, {"enabled": False})
    fixture.thread.join(10)
    assert not fixture.thread.is_alive() and not fixture.error
    assert not gateway.gateway_file(fixture.deployment).exists()
    fixture.proxy.close()
    with pytest.raises(DomainError, match="not enabled"):
        gateway.serve(fixture.db, fixture.deployment, stop=threading.Event())
    propose(fixture.db, tmp_path, BUSINESS, {"enabled": True})
    # A missing, shared or foreign runtime directory is never created or chmodded here.
    fixture.runtime.rmdir()
    with pytest.raises(DomainError, match="does not exist"):
        gateway.serve(fixture.db, fixture.deployment, stop=threading.Event())
    fixture.runtime.mkdir(mode=0o755)
    fixture.runtime.chmod(0o755)
    with pytest.raises(DomainError, match="private directory"):
        gateway.serve(fixture.db, fixture.deployment, stop=threading.Event())
    assert stat.S_IMODE(fixture.runtime.stat().st_mode) == 0o755


def test_gateway_refuses_a_legacy_or_sandbox_business(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "port_available", lambda port: True)
    db = database_at(tmp_path)
    propose(db, tmp_path, BUSINESS, {"enabled": True, "proxy_server": "http://proxy.example:80"})
    (tmp_path / "run").mkdir(mode=0o700)
    item = deploy.parse(deployment_value(tmp_path))
    with pytest.raises(DomainError, match="not configured for the isolated"):
        gateway.serve(db, item, stop=threading.Event())


# --- readiness ------------------------------------------------------------------------------------------


class World:
    """Consistent receipts plus injectable process facts, so each tamper is one changed fact."""

    def __init__(self, tmp_path):
        self.directory = tmp_path / "state"
        self.directory.mkdir()
        self.runtime = tmp_path / "run"
        self.runtime.mkdir()
        for name in ("upstream.sock", "cdp.sock", "vnc.sock"):
            path = self.runtime / name
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(path))
            setattr(self, name.split(".")[0], listener)
        self.config = {"generation": "g1", "user_data_dir": "/profile", "cdp_port": 19400}
        self.identities = {10: "boot:10", 11: "boot:11", 12: "boot:12", 13: "boot:13", 20: "boot:20"}
        self.netns = {11: "net:[2]", 20: "net:[1]"}
        self.listeners = {(11, 19400, "127.0.0.1"), (20, 19400, "127.0.0.1"), (20, 5923, "100.64.0.10")}
        self.gateway = {"instance": "gw", "pid": 20, "identity": "boot:20", "netns": "net:[1]", "business_id": UUID,
                        "generation": "g1", "cdp_port": 19400, "vnc_port": 5923, "vnc_interface": "100.64.0.10",
                        "runtime_dir": str(self.runtime)}
        self.status = {"instance": "i1", "mode": "isolated-desktop-v1", "business_id": UUID, "generation": "g1",
                       "user_data_dir": "/profile", "cdp_port": 19400, "netns": "net:[2]",
                       "manager_pid": 10, "manager_identity": "boot:10", "chrome_pid": 11, "chrome_identity": "boot:11",
                       "relay_pid": 12, "relay_identity": "boot:12", "xvnc_pid": 13, "xvnc_identity": "boot:13",
                       "gateway": {key: self.gateway[key] for key in ("instance", "pid", "identity", "netns")}}

    def write(self):
        (self.directory / "ready.json").write_text(json.dumps(self.status))
        (self.directory / "gateway.json").write_text(json.dumps(self.gateway))

    def check(self):
        self.write()
        return ready.ready_target(self.directory, self.config, identity=lambda pid: self.identities[pid],
                                  listener=lambda pid, port, address=None: (pid, port, address) in self.listeners,
                                  netns=lambda pid: self.netns[pid])


def test_ready_target_needs_both_sides_to_match(tmp_path):
    world = World(tmp_path)
    target = world.check()
    assert target == {"cdp_endpoint": "http://127.0.0.1:19400", "generation": "g1", "instance": "i1:gw"}
    # Sockets are real files in the runtime directory; drop one and readiness disappears.
    world.vnc.close()
    (world.runtime / "vnc.sock").unlink()
    with pytest.raises(DomainError):
        world.check()


@pytest.mark.parametrize("tamper", [
    lambda w: w.status.update(generation="old"), lambda w: w.gateway.update(generation="old"),
    lambda w: w.status.update(mode="legacy"), lambda w: w.status.update(user_data_dir="/other"),
    lambda w: w.status.update(cdp_port=19401), lambda w: w.status.update(chrome_identity="boot:99"),
    lambda w: w.status.update(manager_identity="boot:98"), lambda w: w.status.update(relay_identity="x"),
    lambda w: w.status.update(xvnc_identity="x"), lambda w: w.identities.update({20: "boot:new"}),
    lambda w: w.gateway.update(instance="restarted"), lambda w: w.status["gateway"].update(pid=21),
    lambda w: w.netns.update({11: "net:[1]"}), lambda w: w.status.update(netns="net:[1]"),
    lambda w: w.netns.update({20: "net:[3]"}), lambda w: w.listeners.discard((11, 19400, "127.0.0.1")),
    lambda w: w.listeners.discard((20, 19400, "127.0.0.1")), lambda w: w.listeners.discard((20, 5923, "100.64.0.10")),
    lambda w: w.status.pop("xvnc_pid"), lambda w: w.status.update(business_id="another"),
])
def test_ready_target_rejects_every_stale_or_mixed_fact(tmp_path, tamper):
    world = World(tmp_path)
    assert world.check()
    tamper(world)
    with pytest.raises(DomainError, match="stopped or needs an operator restart"):
        world.check()


def test_ready_target_without_receipts_is_unavailable(tmp_path):
    with pytest.raises(DomainError):
        control.ready_target(tmp_path, {"mode": "isolated-desktop-v1", "generation": "g", "user_data_dir": "/p", "cdp_port": 19400})
    (tmp_path / "ready.json").write_text('{"generation":"g"}')
    with pytest.raises(DomainError):
        control.ready_target(tmp_path, {"mode": "isolated-desktop-v1", "generation": "g", "user_data_dir": "/p", "cdp_port": 19400})


# --- session commands and small helpers -----------------------------------------------------------------


def test_session_commands_keep_the_sandbox_and_hold_no_secret(tmp_path):
    item = deploy.parse(deployment_value(tmp_path, node="/usr/bin/node"))
    block = {"user_data_dir": str(tmp_path / "profile"), "cdp_port": 19400}
    chrome = session.chrome_command(deploy.Binaries(), item, block, 4242)
    assert "--no-sandbox" not in chrome and not any(arg.startswith("--headless") for arg in chrome)
    assert "--proxy-server=http://127.0.0.1:4242" in chrome and "--proxy-bypass-list=<-loopback>" in chrome
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in chrome and "--disable-quic" in chrome
    assert "--start-maximized" in chrome and f"--remote-debugging-port=19400" in chrome
    xvnc = session.xvnc_command(deploy.Binaries(), item)
    assert xvnc[xvnc.index("-rfbport") + 1] == "-1" and xvnc[xvnc.index("-nolisten") + 1] == "tcp"
    assert xvnc[xvnc.index("-rfbunixpath") + 1] == str(item.vnc_socket) and "VncAuth" in xvnc
    assert session.relay_command(item) == ["/usr/bin/node", str(item.tools_dir / "relay.cjs")]
    for argv in (chrome, xvnc):
        assert not any("@" in arg for arg in argv)  # no proxy URL with userinfo, no secret material
        assert "--password-store=basic" in argv or "-PasswordFile" in argv


def _bound_socket(path, backlog=1):
    """A listening own socket that this test can leave unanswered."""
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(backlog)
    return server


def _fill_backlog(path, backlog=0):
    """Queue the connections a backlog holds so the next probe blocks (real backlog pressure, no accept)."""
    clients = []
    for _ in range(backlog + 1):
        client = socket.socket(socket.AF_UNIX)
        client.settimeout(1)
        client.connect(str(path))
        clients.append(client)
    return clients


@pytest.mark.parametrize("claim", [
    lambda path: bridge.unix_listener(path, replace_stale=True),
    lambda path: session.clear_stale_socket(path),
], ids=["listener", "session"])
def test_a_socket_whose_liveness_is_ambiguous_is_never_removed(tmp_path, claim):
    # A full backlog: the probe times out although a live server owns the endpoint.
    path = tmp_path / "busy.sock"
    server = _bound_socket(path, backlog=0)
    clients = _fill_backlog(path)
    try:
        before = os.lstat(path)
        with pytest.raises(DomainError, match="could not be shown stale"):
            claim(path)
        after = os.lstat(path)
        assert (after.st_ino, after.st_dev) == (before.st_ino, before.st_dev)
    finally:
        for client in clients:
            client.close()
        server.close()
        path.unlink(missing_ok=True)
    # An access failure: the kernel refuses the probe, which says nothing about staleness.
    if os.getuid() != 0:
        server = _bound_socket(path)
        os.chmod(path, 0)
        try:
            with pytest.raises(DomainError, match="could not be shown stale"):
                claim(path)
            assert path.exists()
        finally:
            server.close()
            os.chmod(path, 0o600)
            path.unlink(missing_ok=True)


@pytest.mark.parametrize("failure", [TimeoutError("t"), PermissionError("p"), BlockingIOError("b"), OSError(errno.EIO, "io")])
def test_every_unproven_probe_failure_preserves_the_endpoint_and_closes_the_probe(tmp_path, monkeypatch, failure):
    path = tmp_path / "s.sock"
    _bound_socket(path).close()  # a real, currently unserved socket file
    probes = []

    class Probe:
        def __init__(self, *args):
            probes.append(self)
            self.closed = False

        def settimeout(self, value):
            pass

        def connect(self, address):
            raise failure

        def close(self):
            self.closed = True
    monkeypatch.setattr(bridge, "socket", SimpleNamespace(socket=Probe, AF_UNIX=socket.AF_UNIX))
    for claim in (lambda: bridge.claim_socket_path(path, replace_stale=True), lambda: session.clear_stale_socket(path)):
        with pytest.raises(DomainError, match="could not be shown stale"):
            claim()
    assert path.exists() and probes and all(probe.closed for probe in probes)


def test_a_vanished_or_positively_stale_endpoint_is_absent_and_the_probe_closes(tmp_path, monkeypatch):
    path = tmp_path / "s.sock"
    probes = []
    real = socket.socket

    class Probe(real):
        def __init__(self, *args):
            super().__init__(*args)
            probes.append(self)

        def connect(self, address):
            if outcome == "vanished":
                os.unlink(address)
                raise FileNotFoundError(errno.ENOENT, "gone")
            return super().connect(address)
    monkeypatch.setattr(bridge, "socket", SimpleNamespace(socket=Probe, AF_UNIX=socket.AF_UNIX))
    outcome = "vanished"
    _bound_socket(path).close()
    bridge.claim_socket_path(path, replace_stale=False)  # gone while probing: absent, nothing to refuse or remove
    assert not path.exists()
    outcome = "stale"
    _bound_socket(path).close()
    with pytest.raises(DomainError, match="stale"):
        bridge.claim_socket_path(path, replace_stale=False)
    assert path.exists()  # positively stale, but the caller did not ask for replacement
    bridge.claim_socket_path(path, replace_stale=True)
    assert not path.exists() and probes and all(probe.fileno() == -1 for probe in probes)


def test_a_stale_endpoint_replaced_while_it_was_probed_is_preserved(tmp_path, monkeypatch):
    path = tmp_path / "s.sock"
    _bound_socket(path).close()
    os.link(path, tmp_path / "pinned")  # keeps the old inode allocated so the replacement is a different one
    real = socket.socket
    replacement = []

    class Probe(real):
        def connect(self, address):
            try:
                super().connect(address)
            finally:
                # A new owner takes the path between the refused probe and the removal.
                os.unlink(address)
                replacement.append(_bound_socket(address))
    monkeypatch.setattr(bridge, "socket", SimpleNamespace(socket=Probe, AF_UNIX=socket.AF_UNIX))
    with pytest.raises(DomainError, match="changed while it was checked"):
        bridge.claim_socket_path(path, replace_stale=True)
    assert path.exists()
    replacement[0].close()


def test_a_foreign_object_at_a_socket_path_is_never_probed_or_removed(tmp_path):
    path = tmp_path / "file"
    path.write_text("keep")
    for claim in (lambda: bridge.claim_socket_path(path, replace_stale=True), lambda: session.clear_stale_socket(path)):
        with pytest.raises(DomainError, match="another object"):
            claim()
    assert path.read_text() == "keep"


def test_clear_stale_socket_only_removes_an_unanswered_own_socket(tmp_path):
    path = tmp_path / "s.sock"
    session.clear_stale_socket(path)  # nothing there
    server = bridge.unix_listener(path)
    with pytest.raises(DomainError, match="another process"):
        session.clear_stale_socket(path)
    server.close()
    session.clear_stale_socket(path)
    assert not path.exists()
    (tmp_path / "file").write_text("keep")
    with pytest.raises(DomainError):
        session.clear_stale_socket(tmp_path / "file")
    assert (tmp_path / "file").read_text() == "keep"


def test_session_preflight_refuses_proxy_environment_and_routed_hosts(tmp_path, monkeypatch):
    item = deploy.parse(deployment_value(tmp_path))
    fake = session.Session(None, item, {"user_data_dir": str(tmp_path / "p"), "cdp_port": 19400}, tmp_path,
                           environment={"HTTPS_PROXY": "http://x:1"})
    with pytest.raises(DomainError, match="proxy environment"):
        fake.preflight()
    fake = session.Session(None, item, {"user_data_dir": str(tmp_path / "p"), "cdp_port": 19400}, tmp_path, environment={})
    # This machine has network interfaces, so the real probe refuses; nothing else is attempted first.
    monkeypatch.setattr(netns, "interfaces", lambda net=None: ["lo", "eth0"])
    with pytest.raises(DomainError, match="loopback-only"):
        fake.preflight()
    assert not (tmp_path / "ready.json").exists()


def test_backup_walk_skips_the_private_browser_folder_only_at_the_hq_top(tmp_path):
    from bazarlyk.storage import recovery
    hq = tmp_path / "hq"
    (hq / "browser/profile").mkdir(parents=True)
    (hq / "browser/profile/SingletonLock").symlink_to("host-1234")
    (hq / "browser/setup.json").write_text("{}")
    (hq / "context").mkdir()
    (hq / "context/note.md").write_text("authored")
    (hq / "context/browser").mkdir()
    (hq / "context/browser/kept.md").write_text("authored")
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(hq / "browser/profile/SingletonSocket"))
        names = {path.relative_to(hq).as_posix() for path in recovery._walk(hq, top_skip=recovery.PRIVATE_HQ_DIRECTORIES)}
        assert names == {"context/note.md", "context/browser/kept.md"}
        # Without the exclusion the same tree is an incomplete, refused backup (why the folder is private).
        with pytest.raises(DomainError, match="link or special file"):
            list(recovery._walk(hq))
