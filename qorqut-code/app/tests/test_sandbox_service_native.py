"""Opt-in disposable composition acceptance; no agent/model session is started."""

import asyncio
import json
import os
from pathlib import Path
import socket
import time
from urllib.parse import quote, urlsplit

import httpx
from websockets.sync.client import connect as ws_connect

import pytest

from bazarlyk.browser.socket import BrowserSocketConnector
from bazarlyk.browser.viewer import BrowserCDP
from bazarlyk.browser.access import BusinessBrowserAccess
from qorqut_operations.storage.admission import InstallationRecoveryLock, RecoveryAdmission
from bazarlyk.sandbox.config import BusinessPaths, HostConfig, ResourceLimits
from bazarlyk.sandbox.docker import Docker
from bazarlyk.sandbox.linux import LinuxNetwork
from bazarlyk.sandbox.network import ProxyEndpoint
from bazarlyk.sandbox.preview_service import BusinessPreviews
from bazarlyk.sandbox.service import BusinessSandboxService
from bazarlyk.sandbox.state import SandboxStateStore
from test_sandbox_readiness import _MCP_CHECK
from test_service_transports import ServiceFixture


pytestmark = pytest.mark.skipif(os.environ.get("QORQUT_TEST_SANDBOX_INTEGRATED") != "1",
                              reason="Explicit disposable composed sandbox acceptance only")

WORKSTATION = os.environ.get("QORQUT_TEST_WORKSTATION_IMAGE",
    "qorqut-business-sandbox-workstation@sha256:8cb359f1de8240b517fc8c01da76a214473dd0041c147c5ff6e05321d4d89f12")
BROWSER = os.environ.get("QORQUT_TEST_BROWSER_IMAGE",
    "qorqut-business-sandbox-browser@sha256:0a2f2c97bd4df60f80723a916e28523ecd7982eb5a28050931b9884444988f70")


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _preview_canary_server(port):
    """A disposable raw HTTP/WebSocket echo server inside the real workstation."""
    return r'''
import base64
import hashlib
import socket
import sys

listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("0.0.0.0", int(sys.argv[1])))
listener.listen(16)
while True:
    connection, _peer = listener.accept()
    with connection:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = connection.recv(4096)
            if not chunk:
                break
            data += chunk
        header = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        lines = header.split("\r\n")
        fields = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.lower()] = value.strip()
        if fields.get("upgrade", "").lower() == "websocket":
            accept = base64.b64encode(hashlib.sha1((fields["sec-websocket-key"] +
                "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            connection.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n" +
                "Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n").encode())
            first = connection.recv(2)
            size = first[1] & 0x7f
            if size == 126:
                size = int.from_bytes(connection.recv(2), "big")
            mask = connection.recv(4)
            payload = b""
            while len(payload) < size:
                payload += connection.recv(size - len(payload))
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            echoed = b"echo:" + payload
            prefix = b"\x81" + bytes([len(echoed)]) if len(echoed) < 126 else b"\x81\x7e" + len(echoed).to_bytes(2, "big")
            connection.sendall(prefix + echoed)
        else:
            body = b"native-private-preview"
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: " +
                str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
'''


def _foreign_preview_status(origin):
    """Connect from another loopback peer; forwarded headers must not affect it."""
    parsed = urlsplit(origin)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.bind(("127.0.0.2", 0))
        client.settimeout(10)
        client.connect((parsed.hostname, parsed.port))
        client.sendall(("GET / HTTP/1.1\r\nHost: " + parsed.netloc +
                         "\r\nX-Forwarded-For: 127.0.0.1\r\nConnection: close\r\n\r\n").encode())
        return client.recv(1024).split(b"\r\n", 1)[0]


def cdp_version(service, owner, business_id):
    binding = service.observed_browser_binding(business_id)
    connector = BrowserSocketConnector(service.docker, owner,
                                       current_binding=lambda: service.observed_browser_binding(business_id))
    connection = connector.connect(binding)
    try:
        connection.settimeout(10)
        connection.sendall(b"GET /json/version HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = connection.recv(8192)
            assert chunk
            response += chunk
        headers, body = response.split(b"\r\n\r\n", 1)
        lines = headers.split(b"\r\n")
        assert lines[0].startswith(b"HTTP/1.1 200")
        length = next(int(line.split(b":", 1)[1].strip()) for line in lines[1:]
                      if line.lower().startswith(b"content-length:"))
        while len(body) < length:
            chunk = connection.recv(8192)
            assert chunk
            body += chunk
        return binding, json.loads(body[:length])
    finally:
        connection.close()


def synthetic_form_interaction(service, owner, business_id):
    """Exercise the viewer's fixed CDP client against native Chrome only."""
    binding = service.observed_browser_binding(business_id)
    connector = BrowserSocketConnector(service.docker, owner,
                                       current_binding=lambda: service.observed_browser_binding(business_id))

    async def run():
        client = BrowserCDP(connector, binding)
        tabs = await client.tabs()
        assert tabs
        target = tabs[0]["id"]
        page = """<meta charset=utf-8><style>body{margin:0}input{position:absolute;left:10px;top:10px;width:180px;height:30px}button{position:absolute;left:10px;top:60px;width:100px;height:30px}</style><input id=field><button id=submit onclick=\"document.title='submitted:'+field.value\">Submit</button>"""
        await client.action(target, "Page.navigate", {"url": "data:text/html," + quote(page)})
        for _ in range(20):
            state = await client._page(target, "Runtime.evaluate", {"expression": "document.readyState", "returnByValue": True})
            if state.get("result", {}).get("value") == "complete":
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("native synthetic form did not load")
        assert await client.screenshot(target)
        pressed = {"type": "mousePressed", "x": 40, "y": 25, "button": "left", "clickCount": 1}
        released = {"type": "mouseReleased", "x": 40, "y": 25, "button": "left", "clickCount": 1}
        await client.action(target, "Input.dispatchMouseEvent", pressed)
        await client.action(target, "Input.dispatchMouseEvent", released)
        await client.action(target, "Input.insertText", {"text": "synthetic-canary"})
        await client.action(target, "Input.dispatchKeyEvent", {"type": "keyDown", "key": "!", "code": "Digit1",
                            "text": "!", "windowsVirtualKeyCode": 49, "modifiers": 8})
        await client.action(target, "Input.dispatchKeyEvent", {"type": "keyUp", "key": "!", "code": "Digit1",
                            "windowsVirtualKeyCode": 49, "modifiers": 8})
        pressed.update({"x": 55, "y": 75})
        released.update({"x": 55, "y": 75})
        await client.action(target, "Input.dispatchMouseEvent", pressed)
        await client.action(target, "Input.dispatchMouseEvent", released)
        state = await client._page(target, "Runtime.evaluate", {
            "expression": "JSON.stringify({value:field.value,title:document.title})", "returnByValue": True,
        })
        assert json.loads(state["result"]["value"]) == {
            "value": "synthetic-canary!", "title": "submitted:synthetic-canary!",
        }

    asyncio.run(run())


def test_composed_service_repeats_replaces_and_preserves_only_persistent_files(tmp_path):
    service_fixture = ServiceFixture(tmp_path)
    runtime, business_id = service_fixture.runtime, service_fixture.fixture.business_id
    with runtime.database.read() as uow:
        business = dict(uow.repository("businesses").get(business_id, required=True))
    root = Path(runtime.root)
    # ServiceFixture is deliberately lightweight; install the same concrete
    # cross-process admission used by production preview registration.
    runtime.application.writer_admission = RecoveryAdmission(installation_lock=InstallationRecoveryLock(root))
    browser = root / ".bazarlyk" / "browsers" / business_id
    (root / ".bazarlyk" / "secrets" / "browser-proxy").mkdir(parents=True, exist_ok=True, mode=0o700)
    credential = root / ".bazarlyk" / "secrets" / "browser-proxy" / (business_id + ".json")
    credential.write_text(json.dumps({"username": "", "password": ""}))
    credential.chmod(0o600)
    business_config = {"browser": {"enabled": True, "proxy_server": "http://127.0.0.1:9",
        "cdp_port": 19400, "user_data_dir": str(browser / "profile"),
        "downloads_dir": str(browser / "downloads"), "credentials_ref": "browser-proxy/" + business_id,
        "generation": business_id}}
    with runtime.database.transaction() as uow:
        row = uow.repository("businesses").get(business_id, required=True)
        uow.repository("businesses").update(business_id, {"config_json": business_config},
                                            expected_revision=row["revision"])
    with runtime.database.read() as uow:
        business = dict(uow.repository("businesses").get(business_id, required=True))
    paths = BusinessPaths.registered(root, business)
    # Chrome's sandboxed process tree needs headroom for its normal renderer,
    # zygote, utility and crashpad workers while readiness probes execute.
    config = HostConfig(WORKSTATION, BROWSER, ResourceLimits(768, 512, 256, 1.5, 512, 8), 1148, enabled=True)
    docker = Docker(command=("sudo", "-n", "docker"))
    network = LinuxNetwork(docker, command=("sudo", "-n"))
    service = BusinessSandboxService(runtime, docker=docker, network=network,
        config_loader=lambda _: config)
    runtime.sandbox = service
    runtime.browser_access = BusinessBrowserAccess(runtime)
    owner = service.driver.ownership(paths)
    proxy = ProxyEndpoint("http", "8.8.8.8", 80)  # Disposable fixed fixture; no live proxy lookup.
    service.driver.proxy_loader = lambda *_: proxy
    first = second = None
    try:
        first = service.driver.reconcile(business, paths, config, 1, heartbeat=lambda: None)
        first_id = first.environment_id
        first_browser = service.driver._inventory("container", owner)
        assert len(first_browser) == 3
        first_roles = {row["Config"]["Labels"]["qorqut.role"]: row["Id"] for row in first_browser}
        store = SandboxStateStore()
        with runtime.database.transaction() as uow:
            store.insert_intent(uow, business_id)
            lease = store.claim(uow, business_id)
        with runtime.database.transaction() as uow:
            store.mark_ready(uow, lease, environment_id=first_id)
        preview_port, workload_port = free_port(), 18123
        preview_host = root / ".bazarlyk" / "preview-host.json"
        preview_host.write_text(json.dumps({"schema_version": 1, "bind_address": "127.0.0.1",
            "owner_addresses": ["127.0.0.1"], "first_port": preview_port, "last_port": preview_port}))
        preview_host.chmod(0o600)
        previews = BusinessPreviews(runtime, docker, ["http://127.0.0.1:1049"],
            sandbox_config_loader=lambda _: config, interval=60)
        # No host-side target mapping: this raw server is reachable only at the
        # actual observed workstation address selected by BusinessPreviews.binding.
        docker.call("exec", "--detach", first_id, "/opt/qorqut/venv/bin/python", "-u", "-c",
                    _preview_canary_server(workload_port), str(workload_port), timeout=60)
        registration = previews.registry.register(business_id, workload_port)
        previews.reconcile()
        with httpx.Client(trust_env=False, timeout=10) as client:
            for _ in range(50):
                response = client.get(registration["origin"])
                if response.status_code == 200:
                    break
                time.sleep(0.1)
            assert response.status_code == 200 and response.text == "native-private-preview"
            assert _foreign_preview_status(registration["origin"]) == b"HTTP/1.1 403 Forbidden"
            with ws_connect(registration["origin"].replace("http://", "ws://") + "/echo",
                            origin=registration["origin"], proxy=None, open_timeout=10) as websocket:
                websocket.send("owner-peer")
                assert websocket.recv() == "echo:owner-peer"
        preview_binding = previews.binding(registration, previews.config_loader(root))
        observed_agent = docker.owned("container", first_id, owner)
        observed_address = observed_agent["NetworkSettings"]["Networks"][preview_binding.network]["IPAddress"]
        assert preview_binding.address == observed_address and preview_binding.address != "127.0.0.1"
        binding, version = cdp_version(service, owner, business_id)
        assert binding.browser_id == first_roles["browser"] and binding.generation == 1
        assert version["Browser"].startswith("Chrome/") and service.owns_browser_lock(business_id)
        synthetic_form_interaction(service, owner, business_id)
        docker.exec(first_id, owner, "tmux", "-L", "integration", "new-session", "-d", "-s", "kept")
        docker.exec(next(row["Id"] for row in first_browser if row["Config"]["Labels"]["qorqut.role"] == "browser"),
                    owner, "/bin/sh", "-c", "printf canary > \"$1/.qorqut-profile-canary\"", "canary",
                    str(paths.browser / "profile"))
        repeat = service.driver.reconcile(business, paths, config, 1, heartbeat=lambda: None)
        assert repeat.environment_id == first_id
        assert {row["Config"]["Labels"]["qorqut.role"]: row["Id"]
                for row in service.driver._inventory("container", owner)} == first_roles
        docker.exec(first_id, owner, "tmux", "-L", "integration", "has-session", "-t", "kept")
        second = service.driver.reconcile(business, paths, config, 2, heartbeat=lambda: None)
        assert second.environment_id != first_id
        # The retained listener immediately refuses a replaced environment; it
        # never falls back to a host or caller-selected upstream address.
        with httpx.Client(trust_env=False, timeout=10) as client:
            assert client.get(registration["origin"]).status_code == 409
        with runtime.database.transaction() as uow:
            stopped = store.claim(uow, business_id)
            assert stopped is not None
            store.mark_stopped(uow, stopped, environment_id=first_id)
        with httpx.Client(trust_env=False, timeout=10) as client:
            assert client.get(registration["origin"]).status_code == 503
        previews.stop()
        assert previews._servers == {}
        assert {row["Config"]["Labels"]["qorqut.role"]: row["Id"]
                for row in service.driver._inventory("container", owner)} != first_roles
        assert (paths.browser / "profile" / ".qorqut-profile-canary").read_text() == "canary"
        with pytest.raises(Exception):
            docker.exec(second.environment_id, owner, "tmux", "-L", "integration", "has-session", "-t", "kept")
        registrations = [paths.workspace / "hq" / "agents" / name / ".bazarlyk" / "registration.json"
                         for name in ("growth", "manager")]
        binding, _ = service.listeners._servers[business_id]
        docker.exec(second.environment_id, owner, "/opt/qorqut/venv/bin/python", "-I", "-c", _MCP_CHECK,
            input=json.dumps({"endpoint": f"http://{binding.address}:{binding.port}",
                              "registrations": [str(value) for value in registrations],
                              "agents": [service_fixture.agent.id, service_fixture.manager.id]}))
    finally:
        try:
            if 'previews' in locals():
                previews.stop()
        finally:
            try:
                service.listeners.stop()
            finally:
                service.driver._drain(owner, service.driver._inventory("container", owner), business=business, paths=paths)
                for current in service.driver._inventory("network", owner):
                    network.remove_bridge(current["Id"])
                    docker.call("network", "rm", current["Id"], timeout=180)
        assert service.driver._inventory("container", owner) == []
        assert service.driver._inventory("network", owner) == []
