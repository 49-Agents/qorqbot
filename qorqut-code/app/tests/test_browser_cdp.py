import base64
import asyncio
import hashlib
import httpx
import json
import select
import socket
import threading
import time

import pytest
import uvicorn
from websockets.sync.client import connect as ws_connect

from bazarlyk.browser.cdp import BrowserCDPProxy, BrowserCDPRoute
from bazarlyk.browser.socket import BrowserBinding
from qorqut_operations.contracts import DomainError


BUSINESS = "00000000-0000-4000-8000-000000000001"
BROWSER = "a" * 64
CAPABILITY = "c" * 40


class SocketConnector:
    def __init__(self, port):
        self.port = port
        self.sockets = []

    def connect(self, binding):
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.sockets.append(connection)
        return connection


class Upstream:
    def __init__(self, mode="json"):
        self.mode = mode
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(10)
        self.port = self.server.getsockname()[1]
        self.stop = False
        self.requests = []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        self.server.settimeout(.2)
        while not self.stop:
            try:
                connection, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection):
        with connection:
            connection.settimeout(5)
            data = bytearray()
            while b"\r\n\r\n" not in data and len(data) < 65536:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                data.extend(chunk)
            if b"\r\n\r\n" not in data:
                return
            header = bytes(data).split(b"\r\n\r\n", 1)[0]
            lines = header.split(b"\r\n")
            method, path, _ = lines[0].decode().split(" ", 2)
            headers = {}
            for line in lines[1:]:
                key, value = line.decode().split(":", 1)
                headers[key.lower()] = value.strip()
            self.requests.append((method, path, headers))
            if headers.get("upgrade", "").lower() == "websocket":
                if self.mode == "ws-redirect":
                    connection.sendall(b"HTTP/1.1 302 Found\r\nLocation: ws://127.0.0.1:9/nope\r\nContent-Length: 0\r\n\r\n")
                    return
                accept = base64.b64encode(hashlib.sha1(
                    (headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                ).digest()).decode()
                connection.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                                    "Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n").encode())
                while True:
                    frame = connection.recv(2)
                    if not frame:
                        return
                    length = frame[1] & 0x7f
                    if length == 126:
                        length = int.from_bytes(connection.recv(2), "big")
                    mask = connection.recv(4) if frame[1] & 0x80 else b""
                    payload = bytearray(connection.recv(length))
                    if mask:
                        for index in range(length):
                            payload[index] ^= mask[index % 4]
                    if frame[0] & 0xf == 8:
                        connection.sendall(b"\x88\x02\x03\xe8")
                        return
                    if frame[0] & 0xf == 1:
                        out = bytes(payload)
                        connection.sendall(b"\x81" + bytes((len(out),)) + out)
                return
            if self.mode == "redirect":
                connection.sendall(b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:9/nope\r\nContent-Length: 0\r\n\r\n")
                return
            body = json.dumps({
                "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/abc",
                "badUrl": "ws://127.0.0.1:9/devtools/page/nope",
            }, separators=(",", ":")).encode()
            connection.sendall((f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body)

    def close(self):
        self.stop = True
        self.server.close()
        self.thread.join(2)


class LocalASGI:
    def __init__(self, app):
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0,
                                                     lifespan="off", log_level="warning", access_log=False))
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(32)
        self.port = self.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(.01)
        assert self.server.started

    def close(self):
        self.server.should_exit = True
        self.thread.join(5)
        self.socket.close()


def make_app(upstream, *, allowed=True, interval=5):
    connector = SocketConnector(upstream.port)
    binding = BrowserBinding(BUSINESS, BROWSER, 4, "chrome-1", upstream.port)
    route = BrowserCDPRoute(binding, CAPABILITY, "http://preview.example")
    app = BrowserCDPProxy(route, connector=connector, authorize=lambda scope, route: allowed,
                          lease_interval=interval)
    return app, connector, route


def get_asgi(app, path):
    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://preview.example") as client:
            return await client.get(path)
    return asyncio.run(request())


def test_cdp_http_is_fixed_target_and_rewrites_only_expected_loopback_debugger_url():
    upstream = Upstream()
    app, connector, route = make_app(upstream)
    try:
        response = get_asgi(app, f"/{CAPABILITY}/json")
        assert response.status_code == 200
        value = response.json()
        assert value["webSocketDebuggerUrl"] == f"ws://preview.example:80/{CAPABILITY}/devtools/page/abc"
        assert value["badUrl"] == "ws://127.0.0.1:9/devtools/page/nope"
        assert upstream.requests[0][0:2] == ("GET", "/json")
        assert all(connection.fileno() == -1 for connection in connector.sockets)
    finally:
        upstream.close()


def test_cdp_rejects_foreign_lease_and_non_fixed_paths_without_connecting():
    upstream = Upstream()
    app, connector, route = make_app(upstream, allowed=False)
    try:
        denied = get_asgi(app, f"/{CAPABILITY}/json")
        malformed = get_asgi(app, f"/{CAPABILITY}/anything")
        query = get_asgi(app, f"/{CAPABILITY}/json?x=1")
        assert denied.status_code == 403 and malformed.status_code == 400 and query.status_code == 400
        assert connector.sockets == [] and upstream.requests == []
    finally:
        upstream.close()


def test_cdp_does_not_follow_http_redirect():
    upstream = Upstream(mode="redirect")
    app, connector, route = make_app(upstream)
    try:
        response = get_asgi(app, f"/{CAPABILITY}/json/version")
        assert response.status_code == 502
        assert len(upstream.requests) == 1
    finally:
        upstream.close()


def test_cdp_websocket_uses_fixed_target_and_closes_socket():
    upstream = Upstream(mode="ws")
    app, connector, route = make_app(upstream, interval=.1)
    proxy = LocalASGI(app)
    try:
        with ws_connect(f"ws://127.0.0.1:{proxy.port}/{CAPABILITY}/devtools/page/abc") as client:
            client.send("cdp")
            assert client.recv() == "cdp"
        assert upstream.requests[0][1] == "/devtools/page/abc"
        # The native client close handshake can complete before ASGI delivers
        # its disconnect event and the proxy finishes upstream cleanup.
        deadline = time.monotonic() + 2
        while any(connection.fileno() != -1 for connection in connector.sockets) and time.monotonic() < deadline:
            time.sleep(.01)
        assert all(connection.fileno() == -1 for connection in connector.sockets)
    finally:
        proxy.close()
        upstream.close()


def test_cdp_websocket_redirect_is_not_followed():
    upstream = Upstream(mode="ws-redirect")
    app, connector, route = make_app(upstream)
    proxy = LocalASGI(app)
    try:
        with pytest.raises(Exception):
            with ws_connect(f"ws://127.0.0.1:{proxy.port}/{CAPABILITY}/devtools/browser/id"):
                pass
        assert len(upstream.requests) == 1
    finally:
        proxy.close()
        upstream.close()


@pytest.mark.parametrize("suffix", ["@127.0.0.1:9/devtools/page/id", "/devtools/page/id?secret=1",
                                    "/devtools/../../admin", "/devtools/page/id#fragment"])
def test_debugger_url_cannot_redirect_native_client_outside_binding(suffix):
    upstream = Upstream()
    app, _, _ = make_app(upstream)
    try:
        with pytest.raises(DomainError):
            app._rewrite_url(f"ws://127.0.0.1:{upstream.port}" + suffix)
        with pytest.raises(DomainError):
            app._rewrite_url("ws://foreign.example/devtools/page/id")
        assert get_asgi(app, f"/{CAPABILITY}/json/version/").status_code == 200
    finally:
        upstream.close()


def test_cdp_relay_revokes_live_lease_and_cleans_up_cancelled_tasks():
    async def exercise():
        class UpstreamConnection:
            def __aiter__(self):
                return self
            async def __anext__(self):
                await asyncio.Future()
            async def close(self, **_):
                pass
        binding = BrowserBinding(BUSINESS, BROWSER, 1, "chrome", 9222)
        app = BrowserCDPProxy(BrowserCDPRoute(binding, CAPABILITY, "http://preview.example"),
            connector=SocketConnector(9222), authorize=lambda *_: False, lease_interval=.01)
        scope = {"path": f"/{CAPABILITY}/devtools/page/id", "query_string": b""}
        async def receive():
            await asyncio.Future()
        async def send(_):
            pass
        with pytest.raises(DomainError, match="lease"):
            await asyncio.wait_for(app._relay_websocket(UpstreamConnection(), scope, receive, send), 1)
        app.authorize = lambda *_: True
        task = asyncio.create_task(app._relay_websocket(UpstreamConnection(), scope, receive, send))
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not [other for other in asyncio.all_tasks() if other is not asyncio.current_task()]
    asyncio.run(exercise())
