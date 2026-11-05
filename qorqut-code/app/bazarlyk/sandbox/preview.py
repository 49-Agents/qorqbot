"""Private per-business HTTP and WebSocket preview transport.

The proxy is deliberately a transport boundary.  Its target is an immutable
observed container address and registered port; callers cannot provide a URL,
host name, Docker argument, or upstream credential.  A trusted observer is
called immediately before every upstream connection so a replaced workstation
cannot receive traffic from an old preview binding.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hmac
import ipaddress
import re
from urllib.parse import urlsplit

import anyio
import httpx
from starlette.responses import JSONResponse
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from qorqut_operations.contracts import DomainError
from bazarlyk.transport.auth import normalize_origin


_CONTAINER = re.compile(r"[a-f0-9]{64}\Z")
_ACCESS_HEADER = "x-qorqut-preview-binding"
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
})
_SENSITIVE_REQUEST = frozenset({
    "authorization", "cookie", "forwarded", "proxy-authorization", "proxy-authentication",
    "x-forwarded-for", "x-forwarded-host", "x-forwarded-port", "x-forwarded-proto",
    "x-real-ip", "via", _ACCESS_HEADER,
})
_MAX_BODY = 8 * 1024 * 1024
_MAX_HEADERS = 64 * 1024
_MAX_WS_MESSAGE = 8 * 1024 * 1024


class _NoRedirectConnect(connect):
    """websockets client with its automatic redirect iterator disabled."""

    def process_redirect(self, error):
        # The public client exposes this hook on its maintained Connect class;
        # returning the original exception makes __await_impl__ re-raise it.
        return error


def _header(scope, name):
    needle = name.lower().encode()
    return next((value.decode("latin-1") for key, value in scope.get("headers", ()) if key.lower() == needle), None)


def header_access_check(scope, binding):
    """Reference owner-bound header check for an injected access boundary."""
    supplied = _header(scope, _ACCESS_HEADER)
    return isinstance(supplied, str) and hmac.compare_digest(supplied, binding.access_key)


def _connection_tokens(headers):
    values = set()
    for key, value in headers:
        if key.lower() == b"connection":
            values.update(part.strip().lower() for part in value.decode("latin-1").split(",") if part.strip())
    return values


def _request_path(scope):
    value = scope.get("raw_path")
    if value is None:
        value = scope.get("path", "/").encode("ascii")
    if not isinstance(value, bytes) or not value.startswith(b"/") or any(byte < 0x20 or byte == 0x7F for byte in value):
        raise DomainError("invalid_request", "Preview path is invalid")
    try:
        path = value.decode("ascii")
        query = scope.get("query_string", b"").decode("ascii")
    except (AttributeError, UnicodeDecodeError):
        raise DomainError("invalid_request", "Preview path is invalid") from None
    if any(byte < 0x20 or byte == 0x7F for byte in query.encode("ascii")):
        raise DomainError("invalid_request", "Preview query is invalid")
    return path, query


def _upstream_host(address, port):
    host = f"[{address}]" if ":" in address else address
    return f"{host}:{port}"


def _close_code(value):
    return value if isinstance(value, int) and 1000 <= value <= 4999 and value not in {1004, 1005, 1006, 1015} else 1002


def _status(exc):
    if exc.code in {"unauthenticated", "forbidden", "invalid_origin", "invalid_host", "invalid_configuration"}:
        return 403
    if exc.code in {"environment_conflict"}:
        return 409
    if exc.code in {"payload_too_large"}:
        return 413
    return 503


@dataclass(frozen=True)
class PreviewBinding:
    """One owner-issued binding to one observed private workload endpoint."""

    business_id: str
    origin: str
    access_key: str
    workstation_id: str
    generation: int
    address: str
    port: int
    network: str

    def __post_init__(self):
        # Import lazily to keep this boundary independent from storage models.
        from .config import business_identity
        business_identity(self.business_id)
        object.__setattr__(self, "origin", normalize_origin(self.origin))
        if not isinstance(self.access_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}\Z", self.access_key):
            raise DomainError("invalid_configuration", "Preview access binding is invalid")
        if not _CONTAINER.fullmatch(self.workstation_id):
            raise DomainError("invalid_configuration", "Preview workstation identity is invalid")
        if type(self.generation) is not int or self.generation < 1:
            raise DomainError("invalid_configuration", "Preview generation is invalid")
        try:
            address = ipaddress.ip_address(self.address)
        except ValueError:
            raise DomainError("invalid_configuration", "Preview workstation address is invalid") from None
        if address.is_unspecified or address.is_multicast or address.is_reserved or not address.is_private:
            raise DomainError("invalid_configuration", "Preview workstation address must be private")
        object.__setattr__(self, "address", str(address))
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise DomainError("invalid_configuration", "Preview port is invalid")
        if (not isinstance(self.network, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z", self.network)):
            raise DomainError("invalid_configuration", "Preview network identity is invalid")


class PreviewOrigins:
    """Registry enforcing dashboard separation and globally unique preview origins."""

    def __init__(self, dashboard_origins):
        self.dashboard_origins = frozenset(normalize_origin(value) for value in dashboard_origins)
        if not self.dashboard_origins:
            raise ValueError("Preview origin registry requires the dashboard origin")
        self._origins = {}
        self._businesses = {}

    def register(self, binding: PreviewBinding):
        if not isinstance(binding, PreviewBinding):
            raise TypeError("Preview registry accepts PreviewBinding values")
        if binding.origin in self.dashboard_origins:
            raise DomainError("invalid_origin", "A preview must use a dedicated origin")
        owner = self._origins.get(binding.origin)
        if owner is not None and owner != binding.business_id:
            raise DomainError("invalid_origin", "Preview origins cannot be shared by businesses")
        prior = self._businesses.get(binding.business_id)
        if prior is not None and prior != binding.origin:
            raise DomainError("invalid_origin", "A business preview origin cannot be changed in place")
        self._origins[binding.origin] = binding.business_id
        self._businesses[binding.business_id] = binding.origin

    def remove(self, binding: PreviewBinding):
        if self._origins.get(binding.origin) == binding.business_id:
            del self._origins[binding.origin]
        if self._businesses.get(binding.business_id) == binding.origin:
            del self._businesses[binding.business_id]


class DockerPreviewObserver:
    """Trusted current-state check for a binding before a proxy connection.

    ``current_binding`` must read the current committed runtime/DB record and
    return the exact active :class:`PreviewBinding`. Docker inspection then
    confirms the same full container ID, ownership labels, generation, running
    state, and private network address. The callback is intentionally
    synchronous because Docker and the database are trusted host operations;
    :class:`PreviewProxy` runs it in a worker thread.
    """

    def __init__(self, docker, ownership, *, current_binding):
        if not callable(current_binding):
            raise TypeError("Preview observer requires a current binding reader")
        self.docker, self.ownership, self.current_binding = docker, ownership, current_binding

    def __call__(self, binding):
        current = self.current_binding()
        if (not isinstance(current, PreviewBinding) or current != binding
                or binding.business_id != self.ownership.business_id):
            return False
        observed = self.docker.owned("container", binding.workstation_id, self.ownership)
        if observed is None or observed.get("Id") != binding.workstation_id:
            return False
        state = observed.get("State", {})
        labels = observed.get("Config", {}).get("Labels", {})
        if (not state.get("Running") or labels.get("qorqut.generation") != str(binding.generation)
                or labels.get("qorqut.role") != "agent"):
            return False
        networks = observed.get("NetworkSettings", {}).get("Networks", {})
        details = networks.get(binding.network)
        return isinstance(details, dict) and details.get("IPAddress") == binding.address


class PreviewProxy:
    """ASGI HTTP/WebSocket proxy for one validated :class:`PreviewBinding`.

    ``access_check`` is an owner-side access boundary supplied by trusted
    dashboard composition. Browsers cannot set the private binding header on
    ordinary navigation; the header is only a wire form for that explicit
    owner binding, never a dashboard credential forwarded to the app.
    """

    def __init__(self, binding: PreviewBinding, *, revalidate, access_check, client=None):
        if not callable(revalidate):
            raise TypeError("Preview proxy requires a trusted revalidation callback")
        if not callable(access_check):
            raise TypeError("Preview proxy requires an owner-side access boundary")
        self.binding = binding
        self.revalidate = revalidate
        self.access_check = access_check
        self.client = client

    def _authorize(self, scope):
        try:
            allowed = self.access_check(scope, self.binding)
        except Exception:
            allowed = False
        if allowed is not True:
            raise DomainError("forbidden", "The preview access binding is invalid")
        host = _header(scope, "host")
        expected = urlsplit(self.binding.origin).netloc
        if host is None or host.lower() != expected.lower():
            raise DomainError("invalid_host", "The preview host is not configured")
        origin = _header(scope, "origin")
        if origin is not None and normalize_origin(origin) != self.binding.origin:
            raise DomainError("invalid_origin", "The preview browser origin is not configured")
        try:
            current = self.revalidate(self.binding)
        except DomainError:
            raise
        except Exception:
            raise DomainError("environment_unavailable", "The preview workstation could not be verified", retryable=True) from None
        if current is not True and current != self.binding:
            raise DomainError("environment_conflict", "The preview workstation binding is stale")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            try:
                await anyio.to_thread.run_sync(self._authorize, scope)
                return await self._http(scope, receive, send)
            except DomainError as exc:
                return await JSONResponse({"error": exc.code}, status_code=_status(exc))(scope, receive, send)
        if scope["type"] == "websocket":
            try:
                await anyio.to_thread.run_sync(self._authorize, scope)
                return await self._websocket(scope, receive, send)
            except DomainError:
                await send({"type": "websocket.close", "code": 1008})
                return
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def _request_body(self, receive):
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise DomainError("operation_unavailable", "Preview client disconnected")
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > _MAX_BODY:
                raise DomainError("payload_too_large", "Preview request body is too large")
            if not message.get("more_body", False):
                return bytes(body)

    def _request_headers(self, scope):
        incoming = scope.get("headers", ())
        nominated = _connection_tokens(incoming)
        result = [("host", _upstream_host(self.binding.address, self.binding.port))]
        for key, value in incoming:
            name = key.decode("latin-1").lower()
            if name in _HOP_BY_HOP or name in nominated or name in _SENSITIVE_REQUEST or name == "host":
                continue
            result.append((name, value.decode("latin-1")))
        return result

    @staticmethod
    def _response_headers(response):
        nominated = _connection_tokens([(key.encode("latin-1"), value.encode("latin-1"))
                                        for key, value in response.headers.multi_items()
                                        if key.lower() == "connection"])
        result = []
        for key, value in response.headers.multi_items():
            name = key.lower()
            # App cookies could set a Domain for the dashboard origin.  Preview
            # content is untrusted, so cookies stay out of the owner session.
            if name in _HOP_BY_HOP or name in nominated or name in {"content-length", "set-cookie"}:
                continue
            if name == "location":
                parsed = urlsplit(value)
                if parsed.scheme or parsed.netloc:
                    # An absolute redirect could escape to the dashboard or a
                    # public origin.  Relative redirects remain on this
                    # dedicated preview origin.
                    continue
            result.append((name.encode(), value.encode("latin-1")))
        return result

    async def _http(self, scope, receive, send):
        body = await self._request_body(receive)
        # A client may take an arbitrary amount of time to upload its body;
        # repeat the check immediately before opening the target connection.
        await anyio.to_thread.run_sync(self._authorize, scope)
        path, query = _request_path(scope)
        target = f"http://{_upstream_host(self.binding.address, self.binding.port)}{path}"
        if query:
            target += "?" + query
        client = self.client or httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=20)
        close = self.client is None
        started = False
        try:
            request = client.build_request(scope["method"], target, headers=self._request_headers(scope), content=body)
            response = await client.send(request, stream=True)
            await send({"type": "http.response.start", "status": response.status_code,
                        "headers": self._response_headers(response)})
            started = True
            async for chunk in response.aiter_raw():
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b""})
            await response.aclose()
        except (httpx.HTTPError, OSError):
            if not started:
                await send({"type": "http.response.start", "status": 502, "headers": []})
                await send({"type": "http.response.body", "body": b"Preview upstream is unavailable"})
        finally:
            if close:
                await client.aclose()

    async def _websocket(self, scope, receive, send):
        await receive()  # websocket.connect
        await anyio.to_thread.run_sync(self._authorize, scope)
        try:
            path, query = _request_path(scope)
            uri = f"ws://{_upstream_host(self.binding.address, self.binding.port)}{path}"
            if query:
                uri += "?" + query
            offered = _header(scope, "sec-websocket-protocol")
            protocols = None
            if offered:
                protocols = [part.strip() for part in offered.split(",")]
                if any(not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", value) for value in protocols):
                    raise DomainError("invalid_configuration", "Preview WebSocket protocol is invalid")
            async with _NoRedirectConnect(uri, origin=self.binding.origin, subprotocols=protocols,
                               proxy=None, compression=None, max_size=_MAX_WS_MESSAGE,
                               max_queue=16, open_timeout=10, ping_interval=None) as upstream:
                message = {"type": "websocket.accept"}
                if upstream.subprotocol:
                    message["subprotocol"] = upstream.subprotocol
                await send(message)
                await self._relay_websocket(upstream, receive, send)
        except (OSError, asyncio.TimeoutError, ConnectionClosed, WebSocketException, DomainError) as exc:
            await send({"type": "websocket.close", "code": 1009 if isinstance(exc, DomainError) and exc.code == "payload_too_large" else 1011})

    async def _relay_websocket(self, upstream, receive, send):
        async def from_upstream():
            try:
                async for payload in upstream:
                    await send({"type": "websocket.send", "text": payload} if isinstance(payload, str)
                               else {"type": "websocket.send", "bytes": payload})
            except ConnectionClosed as exc:
                await send({"type": "websocket.close", "code": _close_code(exc.code)})
            else:
                # A clean upstream EOF doesn't produce a ConnectionClosed
                # exception from the async iterator.
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
                    if len(message["text"].encode("utf-8")) > _MAX_WS_MESSAGE:
                        raise DomainError("payload_too_large", "Preview WebSocket message is too large")
                    await upstream.send(message["text"])
                elif message.get("bytes") is not None:
                    if len(message["bytes"]) > _MAX_WS_MESSAGE:
                        raise DomainError("payload_too_large", "Preview WebSocket message is too large")
                    await upstream.send(message["bytes"])

        tasks = {asyncio.create_task(from_upstream()), asyncio.create_task(to_upstream())}
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
