"""Request attribution, origin/framing checks and bounded network admission."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from starlette.responses import JSONResponse

from ..contracts import Invocation
from .configuration import MAX_INPUT_BYTES, Credentials
from .sessions import Sessions, session_cookie

MAX_REQUESTS = 32
BODY_TIMEOUT = 10.0
SEND_TIMEOUT = 30.0
SECURITY_HEADERS = {
    b"cache-control": b"no-store",
    b"x-content-type-options": b"nosniff",
    b"content-security-policy": (
        b"default-src 'self'; script-src 'self'; style-src 'self'; "
        b"img-src 'self' data:; connect-src 'self'; object-src 'none'; "
        b"base-uri 'none'; frame-ancestors 'self'; form-action 'self'"),
}


@dataclass
class Admission:
    closing: bool = False


def failure(status: int, code: str, message: str, *, headers=None) -> JSONResponse:
    # Transport refusals are not OperationResults and have no invented receipt.
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers={"Cache-Control": "no-store", **(headers or {})})


def origins(port: int) -> tuple[list[str], list[str]]:
    hosts = [f"127.0.0.1:{port}", f"localhost:{port}"]
    if port == 80:
        hosts += ["127.0.0.1", "localhost"]
    return hosts, [f"http://{host}" for host in hosts]


class Boundary:
    def __init__(self, app, *, credentials: Credentials, port: int,
                 sessions: Sessions | None = None, admission: Admission | None = None):
        self.app, self.credentials = app, credentials
        self.sessions = sessions if sessions is not None else Sessions()
        self.admission = admission if admission is not None else Admission()
        hosts, allowed_origins = origins(port)
        self.hosts = {s.encode("ascii") for s in hosts}
        self.origins = {s.encode("ascii") for s in allowed_origins}
        self.active = 0

    def _check(self, scope):
        headers = {}
        for name, value in scope["headers"]:
            headers.setdefault(name.lower(), []).append(value)
        host = headers.get(b"host", [])
        if len(host) != 1 or host[0] not in self.hosts:
            return failure(421, "invalid_host", "Host is not allowed")
        origin = headers.get(b"origin", [])
        if origin and (len(origin) != 1 or origin[0] not in self.origins):
            return failure(403, "invalid_origin", "Origin is not allowed")
        if b"x-qorqut-actor" in headers:
            return failure(400, "invalid_request", "Caller identity overrides are not accepted")
        path = scope["path"]
        protected = path == "/api" or path.startswith("/api/") or path == "/mcp" or path.startswith("/mcp/")
        if protected:
            auth = headers.get(b"authorization", [])
            try:
                cookie = session_cookie(headers.get(b"cookie", []))
            except ValueError:
                return failure(400, "invalid_request", "Ambiguous or invalid cookies")
            api_v1 = path == "/api/v1" or path.startswith("/api/v1/")
            session_route = path == "/api/v1/session"
            if len(auth) > 1 or (api_v1 and auth and cookie is not None):
                return failure(401, "unauthenticated", "Ambiguous credentials",
                               headers={"WWW-Authenticate": "Bearer"})
            method = scope.get("method", "GET")
            if ((session_route and method in {"POST", "DELETE"}) or
                    (api_v1 and cookie is not None and method == "POST")) and not origin:
                return failure(403, "invalid_origin", "Origin is required")
            if session_route:
                if auth:
                    return failure(401, "unauthenticated", "Browser session required")
                scope["qorqut.session_cookie"] = cookie
                actor = self.sessions.actor(cookie)
                if method in {"GET", "HEAD"} and actor is None:
                    return failure(401, "unauthenticated", "Browser session required")
            else:
                actor = (self.sessions.actor(cookie) if api_v1 and cookie is not None
                         else self.credentials.actor(auth[0]) if len(auth) == 1 else None)
                if actor is None:
                    return failure(401, "unauthenticated", "Authentication required",
                                   headers={"WWW-Authenticate": "Bearer"})
            if actor is not None:
                scope["qorqut.invocation"] = Invocation(actor_id=actor)
        if scope.get("query_string"):
            return failure(400, "invalid_request", "Query parameters are not supported")
        # Reject ambiguous framing even when this ASGI app is embedded elsewhere.
        lengths = headers.get(b"content-length", [])
        encodings = headers.get(b"transfer-encoding", [])
        if (len(lengths) > 1 or len(encodings) > 1 or (lengths and encodings)
                or (encodings and encodings[0].lower() != b"chunked")):
            return failure(400, "invalid_request", "Invalid request framing")
        if lengths:
            if not lengths[0].isdigit() or len(lengths[0]) > 10:
                return failure(400, "invalid_request", "Invalid request framing")
            if int(lengths[0]) > MAX_INPUT_BYTES:
                return failure(413, "body_too_large", "Request body exceeds 1 MiB")
        if b"content-encoding" in headers and headers[b"content-encoding"] != [b"identity"]:
            return failure(415, "invalid_request", "Encoded request bodies are not supported")
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def bounded_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message = {**message, "headers": [
                    (key, value) for key, value in message.get("headers", [])
                    if key.lower() not in SECURITY_HEADERS] + list(SECURITY_HEADERS.items())}
            async with asyncio.timeout(SEND_TIMEOUT):
                await send(message)

        rejected = (failure(503, "unavailable", "Server is stopping")
                    if self.admission.closing else self._check(scope))
        if rejected is not None:
            await rejected(scope, receive, bounded_send)
            return
        if self.active >= MAX_REQUESTS:
            await failure(503, "unavailable", "Request capacity unavailable")(scope, receive, bounded_send)
            return
        self.active += 1
        try:
            body = bytearray()
            async with asyncio.timeout(BODY_TIMEOUT):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return  # No dispatch of an incomplete request.
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > MAX_INPUT_BYTES:
                        await failure(413, "body_too_large", "Request body exceeds 1 MiB")(scope, receive, bounded_send)
                        return
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
            lengths = [v for k, v in scope["headers"] if k.lower() == b"content-length"]
            if lengths and int(lengths[0]) != len(body):
                await failure(400, "invalid_request", "Incomplete request body")(scope, receive, bounded_send)
                return
            saved = bytes(body)
            del body
            consumed = False

            async def replay():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": saved, "more_body": False}
                return await receive()

            await self.app(scope, replay, bounded_send)
        except TimeoutError:
            if not started:
                await failure(408, "timeout", "Request I/O timed out")(scope, receive, bounded_send)
        except Exception:
            if not started:
                await failure(500, "unavailable", "Request failed; execution may have occurred")(scope, receive, bounded_send)
        finally:
            self.active -= 1
