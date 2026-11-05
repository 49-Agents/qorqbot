import base64
import hashlib
import http.client
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from qorqut_operations.contracts import DomainError
from bazarlyk.sandbox.preview import (DockerPreviewObserver, PreviewBinding, PreviewOrigins,
                                      PreviewProxy, _request_path, header_access_check)


BUSINESS = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"
WORKSTATION = "a" * 64
KEY = "k" * 40


def _read_until(sock, marker=b"\r\n\r\n"):
    data = bytearray()
    while marker not in data:
        # Read one byte so the HTTP body or first WebSocket frame remains in
        # the socket for the caller after the header terminator.
        chunk = sock.recv(1)
        if not chunk:
            raise AssertionError("socket closed before headers")
        data.extend(chunk)
        assert len(data) < 65536
    return bytes(data)


def _frame(opcode, payload=b"", *, mask=False):
    if isinstance(payload, str):
        payload = payload.encode()
    mask_bytes = b"\x12\x34\x56\x78" if mask else b""
    body = bytes(value ^ mask_bytes[index % 4] for index, value in enumerate(payload)) if mask else payload
    flag = 0x80 if mask else 0
    if len(payload) < 126:
        header = bytes((0x80 | opcode, flag | len(payload)))
    elif len(payload) <= 65535:
        header = bytes((0x80 | opcode, flag | 126)) + len(payload).to_bytes(2, "big")
    else:
        header = bytes((0x80 | opcode, flag | 127)) + len(payload).to_bytes(8, "big")
    return header + mask_bytes + body


def _unframe(data):
    first, second = data[:2]
    length = second & 0x7F
    offset = 2
    if length == 126:
        length = int.from_bytes(data[offset:offset + 2], "big")
        offset += 2
    mask = data[offset:offset + 4] if second & 0x80 else b""
    offset += 4 if mask else 0
    payload = bytearray(data[offset:offset + length])
    if mask:
        for index in range(length):
            payload[index] ^= mask[index % 4]
    return first & 0x0F, bytes(payload)


def _read_frame(sock):
    header = sock.recv(2)
    assert len(header) == 2
    length = header[1] & 0x7F
    if length == 126:
        length = int.from_bytes(sock.recv(2), "big")
    payload = bytearray()
    while len(payload) < length:
        payload.extend(sock.recv(length - len(payload)))
    return _unframe(header + bytes(payload))


class LocalUpstream:
    def __init__(self):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(10)
        self.server.settimeout(.2)
        self.port = self.server.getsockname()[1]
        self.stop = False
        self.requests = []
        self.websocket_masks = []
        self.redirect_ws_port = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
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
            header_block = _read_until(connection)
            header_lines = header_block[:-4].split(b"\r\n")
            method, path, _ = header_lines[0].decode().split(" ", 2)
            headers = {}
            for line in header_lines[1:]:
                key, value = line.decode().split(":", 1)
                headers[key.lower()] = value.strip()
            self.requests.append((method, path, headers))
            if headers.get("upgrade", "").lower() == "websocket":
                if path.startswith("/redirect-ws"):
                    connection.sendall((f"HTTP/1.1 302 Found\r\nLocation: ws://127.0.0.1:{self.redirect_ws_port}/canary\r\n"
                                        "Content-Length: 0\r\n\r\n").encode())
                    return
                accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                connection.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
                while True:
                    first = connection.recv(2)
                    if not first:
                        return
                    second = first[1]
                    self.websocket_masks.append(bool(second & 0x80))
                    length = second & 0x7F
                    if length == 126:
                        length = int.from_bytes(connection.recv(2), "big")
                    mask = connection.recv(4) if second & 0x80 else b""
                    payload = bytearray(connection.recv(length))
                    if mask:
                        for index in range(length):
                            payload[index] ^= mask[index % 4]
                    opcode = first[0] & 0x0F
                    if opcode == 8:
                        connection.sendall(_frame(8, b"\x03\xe8"))
                        return
                    if path.startswith("/close"):
                        connection.sendall(_frame(8, b"\x03\xe8"))
                        return
                    if opcode == 1:
                        connection.sendall(_frame(1, bytes(payload)))
                return
            content_length = int(headers.get("content-length", "0"))
            body = bytearray()
            while len(body) < content_length:
                body.extend(connection.recv(content_length - len(body)))
            response_body = b"preview-body"
            if path.startswith("/redirect"):
                response = (b"HTTP/1.1 302 Found\r\nLocation: http://dashboard.invalid/app\r\n"
                            b"Set-Cookie: owner=poison; Domain=dashboard.invalid\r\nContent-Length: 0\r\n\r\n")
            else:
                response = (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: X-Upstream-Private\r\n"
                            b"X-Upstream-Private: secret\r\n"
                            + f"Content-Length: {len(response_body)}\r\n\r\n".encode() + response_body)
            connection.sendall(response)

    def close(self):
        self.stop = True
        self.server.close()
        self.thread.join(2)


class LocalProxy:
    def __init__(self, app):
        self.app = app
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off",
            log_level="warning", access_log=False, proxy_headers=False))
        # uvicorn does not expose the selected port until a socket is supplied.
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(128)
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


@pytest.fixture
def transport():
    upstream = LocalUpstream()
    checks = []
    binding = PreviewBinding(BUSINESS, f"http://127.0.0.1:{upstream.port + 1}", KEY,
                             WORKSTATION, 4, "127.0.0.1", upstream.port, "agent")
    proxy = LocalProxy(PreviewProxy(binding, revalidate=lambda current: checks.append(threading.get_ident()) or True,
                                   access_check=header_access_check))
    try:
        yield upstream, binding, proxy, checks
    finally:
        proxy.close()
        upstream.close()


def _headers(binding, *, origin=True):
    return {"Host": binding.origin.removeprefix("http://"),
            "X-Qorqut-Preview-Binding": binding.access_key,
            **({"Origin": binding.origin} if origin else {})}


def test_preview_http_is_fixed_target_and_strips_owner_headers(transport):
    upstream, binding, proxy, checks = transport
    with httpx.Client(base_url=f"http://127.0.0.1:{proxy.port}", trust_env=False) as client:
        response = client.post("/task?x=1", content=b"input", headers={**_headers(binding),
            "Authorization": "Bearer owner", "Cookie": "owner=session", "X-Forwarded-For": "10.0.0.1",
            "Connection": "X-Dashboard-Private", "X-Dashboard-Private": "secret"})
        assert response.status_code == 200 and response.text == "preview-body"
    assert len(checks) == 2
    assert all(thread_id != threading.get_ident() for thread_id in checks)
    method, path, headers = upstream.requests[-1]
    assert (method, path) == ("POST", "/task?x=1")
    assert all(name not in headers for name in ("authorization", "cookie", "x-forwarded-for", "x-qorqut-preview-binding", "x-dashboard-private"))
    assert headers["host"] == f"127.0.0.1:{upstream.port}"
    assert "x-upstream-private" not in response.headers


def test_preview_does_not_follow_or_export_upstream_redirects(transport):
    _, binding, proxy, _ = transport
    with httpx.Client(base_url=f"http://127.0.0.1:{proxy.port}", trust_env=False, follow_redirects=False) as client:
        response = client.get("/redirect", headers=_headers(binding))
    assert response.status_code == 302
    assert "location" not in response.headers and "set-cookie" not in response.headers


def test_preview_revalidates_and_rejects_wrong_binding(transport):
    _, binding, proxy, checks = transport
    original = proxy.app.revalidate
    proxy.app.revalidate = lambda current: False
    with httpx.Client(base_url=f"http://127.0.0.1:{proxy.port}", trust_env=False) as client:
        response = client.get("/", headers=_headers(binding))
    assert response.status_code == 409 and checks == []
    proxy.app.revalidate = original
    with httpx.Client(base_url=f"http://127.0.0.1:{proxy.port}", trust_env=False) as client:
        response = client.get("/", headers={**_headers(binding), "Origin": "http://dashboard.invalid:1049"})
    assert response.status_code == 403


def test_preview_requires_owner_binding_and_exact_preview_host(transport):
    _, binding, proxy, _ = transport
    with httpx.Client(base_url=f"http://127.0.0.1:{proxy.port}", trust_env=False) as client:
        missing = client.get("/", headers={"Host": binding.origin.removeprefix("http://")})
        wrong_host = client.get("/", headers={**_headers(binding), "Host": "dashboard.invalid:1049"})
    assert missing.status_code == 403 and wrong_host.status_code == 403


@pytest.mark.parametrize("raw_path", [b"evil.invalid/x", b"/bad\r\nheader", b"/bad\x00path", b"/bad\x7fpath"])
def test_preview_rejects_paths_that_cannot_become_an_upstream_request_target(raw_path):
    with pytest.raises(DomainError):
        _request_path({"raw_path": raw_path, "query_string": b""})


def test_preview_rejects_control_characters_in_query():
    with pytest.raises(DomainError):
        _request_path({"raw_path": b"/ok", "query_string": b"x=1\r\nHost: forged"})


def test_preview_registry_separates_dashboard_and_business_origins(transport):
    _, binding, _, _ = transport
    origins = PreviewOrigins(["http://dashboard.invalid:1049"])
    with pytest.raises(DomainError):
        origins.register(PreviewBinding(BUSINESS, "http://dashboard.invalid:1049", KEY, WORKSTATION, 1, "127.0.0.1", 8080, "agent"))
    origins.register(binding)
    with pytest.raises(DomainError):
        origins.register(PreviewBinding(OTHER, binding.origin, "z" * 40, "b" * 64, 1, "127.0.0.1", 8081, "agent"))


def test_docker_observer_requires_current_db_binding_and_exact_owned_container():
    binding = PreviewBinding(BUSINESS, "http://preview.invalid:8080", KEY, WORKSTATION, 4, "127.0.0.1", 8080, "agent")
    class Docker:
        def owned(self, kind, identity, ownership):
            assert (kind, identity) == ("container", WORKSTATION)
            return {"Id": WORKSTATION, "State": {"Running": True},
                    "Config": {"Labels": {"qorqut.generation": "4", "qorqut.role": "agent"}},
                    "NetworkSettings": {"Networks": {"agent": {"IPAddress": "127.0.0.1"}}}}
    owner = type("Ownership", (), {"business_id": BUSINESS})()
    observer = DockerPreviewObserver(Docker(), owner, current_binding=lambda: binding)
    assert observer(binding) is True
    assert observer(PreviewBinding(BUSINESS, binding.origin, KEY, "b" * 64, 4, "127.0.0.1", 8080, "agent")) is False

    class Replaced(Docker):
        def owned(self, *args):
            return {"Id": "b" * 64, "State": {"Running": True},
                    "Config": {"Labels": {"qorqut.generation": "4", "qorqut.role": "agent"}},
                    "NetworkSettings": {"Networks": {"agent": {"IPAddress": "127.0.0.1"}}}}
    assert DockerPreviewObserver(Replaced(), owner, current_binding=lambda: binding)(binding) is False

    class WrongNetwork(Docker):
        def owned(self, *args):
            return {"Id": WORKSTATION, "State": {"Running": True},
                    "Config": {"Labels": {"qorqut.generation": "4", "qorqut.role": "agent"}},
                    "NetworkSettings": {"Networks": {"browser": {"IPAddress": "127.0.0.1"}}}}
    assert DockerPreviewObserver(WrongNetwork(), owner, current_binding=lambda: binding)(binding) is False


def test_preview_websocket_relay_uses_fixed_target_and_no_dashboard_credentials(transport):
    upstream, binding, proxy, checks = transport
    key = base64.b64encode(b"0123456789abcdef").decode()
    connection = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    try:
        connection.sendall((f"GET /socket HTTP/1.1\r\nHost: {binding.origin.removeprefix('http://')}\r\n"
                            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                            f"Origin: {binding.origin}\r\nX-Qorqut-Preview-Binding: {binding.access_key}\r\n"
                            "Authorization: Bearer owner\r\nCookie: owner=session\r\n\r\n").encode())
        response = _read_until(connection)
        assert b"101 Switching Protocols" in response
        connection.sendall(_frame(1, "hello", mask=True))
        opcode, payload = _read_frame(connection)
        assert opcode == 1 and payload == b"hello"
        connection.sendall(_frame(8, b"\x03\xe8", mask=True))
    finally:
        connection.close()
    assert checks and upstream.requests[-1][1] == "/socket"
    assert all(thread_id != threading.get_ident() for thread_id in checks)
    assert all(name not in upstream.requests[-1][2] for name in ("authorization", "cookie"))
    assert upstream.websocket_masks and all(upstream.websocket_masks)


def test_preview_websocket_does_not_follow_redirect_to_second_listener(transport):
    upstream, binding, proxy, _ = transport
    canary = LocalUpstream()
    upstream.redirect_ws_port = canary.port
    connection = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    try:
        connection.sendall((f"GET /redirect-ws HTTP/1.1\r\nHost: {binding.origin.removeprefix('http://')}\r\n"
                            "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: MDEyMzQ1Njc4OWFiY2RlZg==\r\n"
                            "Sec-WebSocket-Version: 13\r\n"
                            f"Origin: {binding.origin}\r\nX-Qorqut-Preview-Binding: {binding.access_key}\r\n\r\n").encode())
        response = _read_until(connection)
        assert b"101 Switching Protocols" not in response
    finally:
        connection.close()
        canary.close()
    assert not canary.requests


def test_preview_websocket_forwards_clean_upstream_close(transport):
    upstream, binding, proxy, _ = transport
    connection = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    try:
        connection.sendall((f"GET /close HTTP/1.1\r\nHost: {binding.origin.removeprefix('http://')}\r\n"
                            "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: MDEyMzQ1Njc4OWFiY2RlZg==\r\n"
                            "Sec-WebSocket-Version: 13\r\n"
                            f"Origin: {binding.origin}\r\nX-Qorqut-Preview-Binding: {binding.access_key}\r\n\r\n").encode())
        assert b"101 Switching Protocols" in _read_until(connection)
        connection.sendall(_frame(1, "close-now", mask=True))
        opcode, payload = _read_frame(connection)
        assert opcode == 8 and payload[:2] == b"\x03\xe8"
    finally:
        connection.close()


def test_preview_websocket_rejects_oversized_client_message(transport):
    _, binding, proxy, _ = transport
    connection = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
    try:
        connection.sendall((f"GET /oversize HTTP/1.1\r\nHost: {binding.origin.removeprefix('http://')}\r\n"
                            "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: MDEyMzQ1Njc4OWFiY2RlZg==\r\n"
                            "Sec-WebSocket-Version: 13\r\n"
                            f"Origin: {binding.origin}\r\nX-Qorqut-Preview-Binding: {binding.access_key}\r\n\r\n").encode())
        assert b"101 Switching Protocols" in _read_until(connection)
        connection.sendall(_frame(1, b"x" * (8 * 1024 * 1024 + 1), mask=True))
        opcode, payload = _read_frame(connection)
        assert opcode == 8 and payload[:2] == b"\x03\xf1"
    finally:
        connection.close()
