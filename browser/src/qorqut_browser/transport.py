"""Private HTTP attribution, bounds and nonsecret error presentation."""

from dataclasses import dataclass
from functools import partial
import hmac
import json
import logging
from uuid import UUID

import anyio
from pydantic import ValidationError
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from .config import Credentials
from .contracts import BrowserError

MAX_BODY_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_REQUESTS = 32
MAX_WORKERS = 8
PRINCIPAL_SCOPE_KEY = "qorqut_browser.principal"

# Do not echo exception prose from validation, storage, paths or remote peers.
ERRORS = {
    "invalid_argument": (400, "Invalid request arguments"),
    "unauthenticated": (401, "Missing or invalid attribution"),
    "not_found": (404, "Receipt or resource not found"),
    "target_changed": (409, "Browser binding changed"),
    "idempotency_conflict": (409, "Identifier already has different arguments or attribution"),
    "revision_conflict": (409, "Revision changed"),
    "invalid_transition": (409, "Conflicting transition"),
    "uncertain_effect": (409, "An effect may have occurred; retain the original identifier and arguments"),
    "unsupported_capability": (422, "Unsupported capability"),
    "unavailable": (503, "Service unavailable"),
    "not_ready": (503, "Browser admission or state is not ready"),
    "storage_blocked": (503, "Receipt storage is unavailable"),
}


def error_value(error: Exception) -> tuple[int, dict]:
    code = "unavailable"
    if isinstance(error, BrowserError) and error.code in ERRORS:
        code = error.code
    elif isinstance(error, (ValidationError, ValueError, UnicodeError)):
        code = "invalid_argument"
    status, message = ERRORS[code]
    return status, {"error": {"code": code, "message": message}}


def failure(error: Exception) -> JSONResponse:
    status, value = error_value(error)
    return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})


@dataclass(frozen=True)
class Principal:
    actor_id: UUID
    agent_id: UUID | None
    application: bool = False


def authenticate(headers: Headers, credentials: Credentials, *, mcp: bool) -> Principal:
    def invalid():
        return BrowserError("unauthenticated", "Invalid attribution")

    values = headers.getlist("authorization")
    if len(values) != 1:
        raise invalid()
    scheme, separator, token = values[0].partition(" ")
    if scheme.lower() != "bearer" or not separator or not token.isascii():
        raise invalid()
    attribution = headers.getlist("x-qorqut-actor")
    if hmac.compare_digest(token, credentials.application_token.get_secret_value()):
        if mcp or len(attribution) != 1:
            raise invalid()
        try:
            return Principal(UUID(attribution[0]), None, True)
        except ValueError:
            raise invalid() from None
    for actor in credentials.actors:
        if hmac.compare_digest(token, actor.token.get_secret_value()):
            if attribution:
                raise invalid()
            return Principal(actor.actor_id, actor.agent_id)
    raise invalid()


class BlockingWork:
    def __init__(self):
        self.limiter = anyio.CapacityLimiter(MAX_WORKERS)

    async def run(self, function, *args, **kwargs):
        # Cancellation of the request cannot release an executing effect's lock.
        # The gate/helper also preserve the inherited OS lock on process death.
        try:
            with anyio.CancelScope(shield=True):
                return await anyio.to_thread.run_sync(partial(function, *args, **kwargs),
                                                      abandon_on_cancel=False, limiter=self.limiter)
        except BrowserError:
            raise
        except Exception:
            # A failed state read is not an invalid model argument. Argument
            # validation happens before this boundary in each transport.
            raise BrowserError("unavailable", "Operation is unavailable") from None


class _SDKLogFilter(logging.Filter):
    def filter(self, record):
        # The pinned SDK's debug and validation diagnostics include full messages.
        # Keep diagnostic level/module but never payloads or traceback prose.
        record.msg, record.args = "MCP transport diagnostic", ()
        record.exc_info = record.exc_text = record.stack_info = None
        return True


def sanitize_sdk_logs():
    for name in ("mcp.server.lowlevel.server", "mcp.server.streamable_http",
                 "mcp.server.streamable_http_manager", "mcp.server.transport_security"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, _SDKLogFilter) for f in logger.filters):
            logger.addFilter(_SDKLogFilter())


class PrivateTransport:
    """Small ASGI boundary; the official SDK still dispatches all MCP messages."""

    def __init__(self, app, credentials: Credentials, port: int):
        self.app, self.credentials = app, credentials
        self.host = f"127.0.0.1:{port}"
        self.origin = f"http://{self.host}"
        self.requests = anyio.CapacityLimiter(MAX_REQUESTS)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        acquired = False
        try:
            headers = Headers(scope=scope)
            if (headers.getlist("host") != [self.host]
                    or headers.getlist("origin") not in ([], [self.origin])):
                raise BrowserError("invalid_argument", "Unconfigured HTTP origin or host")
            scope[PRINCIPAL_SCOPE_KEY] = authenticate(headers, self.credentials,
                                                    mcp=scope["path"].rstrip("/") == "/mcp")
            # Identity/credentials belong in headers, never query strings.
            if scope.get("query_string"):
                raise BrowserError("invalid_argument", "Query arguments are unsupported")
            try:
                self.requests.acquire_nowait()
                acquired = True
            except anyio.WouldBlock:
                raise BrowserError("unavailable", "Concurrent request limit reached") from None
            lengths = headers.getlist("content-length")
            if lengths and (len(lengths) != 1 or not lengths[0].isascii()
                            or not lengths[0].isdigit() or int(lengths[0]) > MAX_BODY_BYTES):
                raise BrowserError("invalid_argument", "Invalid or oversized request body")
            body = bytearray()
            with anyio.fail_after(10):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > MAX_BODY_BYTES:
                        raise BrowserError("invalid_argument", "Request body exceeds limit")
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
            if scope["method"] == "POST" and headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
                raise BrowserError("invalid_argument", "JSON body required")
            first = True

            async def replay():
                nonlocal first
                if first:
                    first = False
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            # Stateless JSON MCP has no long-lived response stream. Bound the
            # complete response before sending, including SDK-generated errors.
            start = None
            output = bytearray()

            async def collect(message):
                nonlocal start
                if message["type"] == "http.response.start":
                    start = message
                elif message["type"] == "http.response.body":
                    chunk = message.get("body", b"")
                    if len(output) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise BrowserError("uncertain_effect", "Response exceeded bound after possible effect")
                    output.extend(chunk)

            await self.app(scope, replay, collect)
            if start is None:
                raise BrowserError("unavailable", "No transport response")
            if start["status"] == 405 and scope["path"] != "/mcp":
                raise BrowserError("invalid_argument", "Unsupported HTTP method")
            if scope["path"] == "/mcp" and output:
                # SDK JSON-RPC validation errors can embed raw invalid input.
                # Preserve protocol code/ID, but remove diagnostic data/prose.
                try:
                    value = json.loads(output)
                except (ValueError, UnicodeError):
                    value = None
                if isinstance(value, dict) and "error" in value:
                    value["error"] = {"code": value["error"].get("code", -32603),
                                      "message": "Invalid or unavailable MCP request"}
                    output = bytearray(json.dumps(value).encode())
            start["headers"] = [(k, v) for k, v in start["headers"]
                                if k.lower() not in {b"content-length", b"cache-control"}]
            start["headers"].extend([(b"content-length", str(len(output)).encode()),
                                     (b"cache-control", b"no-store")])
        except Exception as error:
            await failure(error)(scope, receive, send)
            return
        finally:
            if acquired:
                self.requests.release()
        await send(start)
        await send({"type": "http.response.body", "body": bytes(output)})
