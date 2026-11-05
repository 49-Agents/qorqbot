"""Capability-scoped HTTP and WebSocket transport for private Chrome CDP.

The browser remains loopback-only in its container.  ``BrowserSocketConnector``
provides the already-connected socket after checking the exact browser
container, generation, and Chrome instance.  This module only selects fixed CDP
paths and delegates owner lease authorization to trusted composition.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import re
from urllib.parse import urlsplit, urlunsplit

import anyio
from starlette.responses import Response
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from qorqut_operations.contracts import DomainError
from bazarlyk.transport.auth import normalize_origin
from .socket import BrowserBinding


_CAPABILITY = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")
_TARGET = re.compile(r"(?:browser|page)/[A-Za-z0-9._~-]{1,256}\Z")
_HTTP_TARGETS = frozenset({"json", "json/list", "json/version", "json/protocol"})
_HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                         "te", "trailer", "transfer-encoding", "upgrade"})
_MAX_HEADER = 64 * 1024
_MAX_BODY = 8 * 1024 * 1024
_MAX_WS_MESSAGE = 8 * 1024 * 1024


class _NoRedirectConnect(connect):
    """Use websockets' maintained client while refusing all HTTP redirects."""

    def process_redirect(self, error):
        return error


@dataclass(frozen=True)
class BrowserCDPRoute:
    """Trusted browser binding and the owner-issued capability URL segment."""

    browser: BrowserBinding
    capability: str
    public_origin: str

    def __post_init__(self):
        if not isinstance(self.browser, BrowserBinding):
            raise TypeError("CDP route requires a browser binding")
        if not isinstance(self.capability, str) or not _CAPABILITY.fullmatch(self.capability):
            raise DomainError("invalid_configuration", "CDP capability is invalid")
        object.__setattr__(self, "public_origin", normalize_origin(self.public_origin))


class BrowserCDPProxy:
    """ASGI transport for one trusted browser CDP route.

    ``authorize`` is an owner-side capability and lease check.  It receives the
    request scope and route and must return exactly ``True``.  Dashboard
    credentials and request headers are never sent to Chrome.
    """

    def __init__(self, route: BrowserCDPRoute, *, connector, authorize, lease_interval=5):
        if not isinstance(route, BrowserCDPRoute):
            raise TypeError("CDP proxy requires a trusted route")
        if not callable(getattr(connector, "connect", None)):
            raise TypeError("CDP proxy requires a socket connector")
        if not callable(authorize):
            raise TypeError("CDP proxy requires an owner lease authorizer")
        if not 0 < lease_interval <= 60:
            raise ValueError("CDP lease interval is invalid")
        self.route, self.connector, self.authorize, self.lease_interval = route, connector, authorize, lease_interval

    @staticmethod
    def _path(scope):
        raw = scope.get("raw_path")
        if raw is None:
            value = scope.get("path", "")
            if not isinstance(value, str):
                raise DomainError("invalid_request", "CDP path is invalid")
            try:
                raw = value.encode("ascii")
            except UnicodeEncodeError:
                raise DomainError("invalid_request", "CDP path is invalid") from None
        if not isinstance(raw, bytes) or not raw.startswith(b"/") or any(byte < 0x20 or byte == 0x7f for byte in raw):
            raise DomainError("invalid_request", "CDP path is invalid")
        try:
            value = raw.decode("ascii")
        except UnicodeDecodeError:
            raise DomainError("invalid_request", "CDP path is invalid") from None
        query = scope.get("query_string", b"")
        if query:
            raise DomainError("invalid_request", "CDP query is not supported")
        segments = value.split("/")
        if len(segments) < 3 or segments[0] != "" or segments[1] == "":
            raise DomainError("invalid_request", "CDP capability path is invalid")
        capability = segments[1]
        if not _CAPABILITY.fullmatch(capability):
            raise DomainError("invalid_request", "CDP capability path is invalid")
        target = "/".join(segments[2:])
        # Playwright's native connectOverCDP appends json/version/.
        if target.removesuffix("/") in _HTTP_TARGETS:
            return capability, target.removesuffix("/"), False
        if target.startswith("devtools/") and _TARGET.fullmatch(target.removeprefix("devtools/")):
            return capability, target.removeprefix("devtools/"), True
        raise DomainError("invalid_request", "CDP target path is invalid")

    def _authorize(self, scope):
        capability, _target, _websocket = self._path(scope)
        if capability != self.route.capability:
            raise DomainError("forbidden", "CDP capability is invalid")
        try:
            allowed = self.authorize(scope, self.route)
        except DomainError:
            raise
        except Exception:
            allowed = False
        if allowed is not True:
            raise DomainError("forbidden", "CDP browser lease is invalid")

    def _upstream_path(self, scope):
        _capability, target, websocket = self._path(scope)
        if websocket:
            return "/devtools/" + target
        return "/" + target

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            try:
                if scope.get("method") != "GET":
                    raise DomainError("invalid_request", "CDP accepts GET only")
                await anyio.to_thread.run_sync(self._authorize, scope)
                return await self._http(scope, send)
            except DomainError as exc:
                status = 403 if exc.code == "forbidden" else 400 if exc.code == "invalid_request" else 503
                response = Response(json.dumps({"error": exc.code}), status_code=status,
                                    media_type="application/json")
                return await response(scope, receive, send)
        if scope.get("type") == "websocket":
            try:
                await receive()
                await anyio.to_thread.run_sync(self._authorize, scope)
                return await self._websocket(scope, receive, send)
            except DomainError:
                await send({"type": "websocket.close", "code": 1008})
            except (OSError, asyncio.TimeoutError, ConnectionClosed, WebSocketException):
                await send({"type": "websocket.close", "code": 1011})
            return
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def _connect(self):
        # Docker inspection and the binding/lease read happen off the event
        # loop.  BrowserSocketConnector performs its own two exact-state checks.
        return await anyio.to_thread.run_sync(self.connector.connect, self.route.browser)

    @staticmethod
    async def _headers(reader):
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = await reader.read(min(4096, _MAX_HEADER + 1 - len(data)))
            if not chunk:
                raise OSError("CDP upstream closed before headers")
            data.extend(chunk)
            if len(data) > _MAX_HEADER:
                raise DomainError("payload_too_large", "CDP response headers are too large")
        head, remainder = bytes(data).split(b"\r\n\r\n", 1)
        lines = head.split(b"\r\n")
        try:
            version, status, _reason = lines[0].decode("latin-1").split(" ", 2)
            status = int(status)
            if version != "HTTP/1.1" or not 100 <= status <= 599:
                raise ValueError
            headers = []
            for line in lines[1:]:
                key, value = line.decode("latin-1").split(":", 1)
                key = key.lower()
                if not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", key) or "\r" in value or "\n" in value:
                    raise ValueError
                headers.append((key, value.strip()))
        except (ValueError, UnicodeDecodeError):
            raise OSError("invalid CDP upstream response") from None
        return status, headers, remainder

    @staticmethod
    async def _body(reader, headers, remainder):
        lengths = [value for key, value in headers if key == "content-length"]
        if len(lengths) > 1 or any(key == "transfer-encoding" for key, _ in headers):
            raise OSError("ambiguous CDP response framing")
        if lengths:
            try:
                size = int(lengths[-1])
            except ValueError:
                raise OSError("invalid CDP content length") from None
            if size < 0 or size > _MAX_BODY:
                raise DomainError("payload_too_large", "CDP response body is too large")
            body = bytearray(remainder)
            while len(body) < size:
                chunk = await reader.read(size - len(body))
                if not chunk:
                    raise OSError("CDP upstream truncated response")
                body.extend(chunk)
            return bytes(body[:size])
        body = bytearray(remainder)
        while True:
            chunk = await reader.read(min(65536, _MAX_BODY + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > _MAX_BODY:
                raise DomainError("payload_too_large", "CDP response body is too large")
        return bytes(body)

    def _rewrite_url(self, value):
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except (AttributeError, ValueError):
            raise DomainError("environment_conflict", "CDP debugger endpoint is invalid") from None
        if (parsed.scheme != "ws" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password
                or port != self.route.browser.cdp_port or parsed.query or parsed.fragment
                or not parsed.path.startswith("/devtools/")
                or not _TARGET.fullmatch(parsed.path.removeprefix("/devtools/"))):
            raise DomainError("environment_conflict", "CDP debugger endpoint is outside its registered binding")
        public = urlsplit(self.route.public_origin)
        scheme = "wss" if public.scheme == "https" else "ws"
        return urlunsplit((scheme, public.netloc, "/" + self.route.capability + parsed.path,
                           parsed.query, parsed.fragment))

    def _rewrite_body(self, body, headers):
        content_type = next((value for key, value in headers if key == "content-type"), "")
        if "json" not in content_type.lower():
            return body
        try:
            value = json.loads(body)
        except (TypeError, ValueError, UnicodeDecodeError):
            return body

        def rewrite(node):
            if isinstance(node, dict):
                return {key: (self._rewrite_url(item) if key in {"webSocketDebuggerUrl", "webSocketUrl"}
                              and isinstance(item, str) else rewrite(item)) for key, item in node.items()}
            if isinstance(node, list):
                return [rewrite(item) for item in node]
            return node

        rewritten = rewrite(value)
        return json.dumps(rewritten, separators=(",", ":")).encode()

    async def _http(self, scope, send):
        sock = await self._connect()
        writer = None
        started = False
        try:
            reader, writer = await asyncio.open_connection(sock=sock)
            path = self._upstream_path(scope)
            request = (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.route.browser.cdp_port}\r\n"
                       "Connection: close\r\nAccept: application/json\r\n\r\n").encode("ascii")
            writer.write(request)
            await writer.drain()
            async with asyncio.timeout(10):
                status, headers, remainder = await self._headers(reader)
                body = await self._body(reader, headers, remainder)
            if 300 <= status < 400:
                raise DomainError("environment_conflict", "CDP redirects are unavailable")
            body = self._rewrite_body(body, headers)
            response_headers = [(b"content-type", value.encode("latin-1")) for key, value in headers
                                if key == "content-type"]
            response_headers.append((b"content-length", str(len(body)).encode()))
            response_headers.extend([(b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer")])
            await send({"type": "http.response.start", "status": status, "headers": response_headers})
            started = True
            await send({"type": "http.response.body", "body": body})
        except (OSError, asyncio.TimeoutError, DomainError):
            if not started:
                await send({"type": "http.response.start", "status": 502, "headers": []})
                await send({"type": "http.response.body", "body": b"CDP upstream unavailable"})
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
            else:
                sock.close()

    async def _websocket(self, scope, receive, send):
        sock = await self._connect()
        path = self._upstream_path(scope)
        uri = f"ws://127.0.0.1:{self.route.browser.cdp_port}{path}"
        try:
            async with _NoRedirectConnect(uri, sock=sock, proxy=None, compression=None,
                                          max_size=_MAX_WS_MESSAGE, max_queue=16,
                                          open_timeout=10, ping_interval=None) as upstream:
                await send({"type": "websocket.accept"})
                await self._relay_websocket(upstream, scope, receive, send)
        except (OSError, asyncio.TimeoutError, ConnectionClosed, WebSocketException, DomainError):
            await send({"type": "websocket.close", "code": 1011})
        finally:
            sock.close()

    async def _relay_websocket(self, upstream, scope, receive, send):
        async def from_upstream():
            try:
                async for payload in upstream:
                    await send({"type": "websocket.send", "text": payload} if isinstance(payload, str)
                               else {"type": "websocket.send", "bytes": payload})
            except ConnectionClosed as exc:
                code = exc.code if isinstance(exc.code, int) and 1000 <= exc.code <= 4999 and exc.code not in {1004, 1005, 1006, 1015} else 1000
                await send({"type": "websocket.close", "code": code})
            else:
                await send({"type": "websocket.close", "code": 1000})

        async def to_upstream():
            while True:
                message = await receive()
                if message["type"] == "websocket.disconnect":
                    await upstream.close(code=1000)
                    return
                if message["type"] != "websocket.receive":
                    continue
                if message.get("text") is not None:
                    if len(message["text"].encode()) > _MAX_WS_MESSAGE:
                        raise DomainError("payload_too_large", "CDP WebSocket message is too large")
                    await upstream.send(message["text"])
                elif message.get("bytes") is not None:
                    if len(message["bytes"]) > _MAX_WS_MESSAGE:
                        raise DomainError("payload_too_large", "CDP WebSocket message is too large")
                    await upstream.send(message["bytes"])

        async def lease_watch():
            while True:
                await asyncio.sleep(self.lease_interval)
                await anyio.to_thread.run_sync(self._authorize, scope)

        tasks = {asyncio.create_task(from_upstream()), asyncio.create_task(to_upstream()),
                 asyncio.create_task(lease_watch())}
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise result
