"""Private HTTP attribution, bounds and nonsecret error presentation."""

from dataclasses import dataclass
from functools import partial
import json
import logging
from pathlib import Path
from uuid import UUID

import anyio
import mcp
from pydantic import ValidationError
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket

from .contracts import BrowserError

MAX_BODY_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_REQUESTS = 32
MAX_WORKERS = 8
PRINCIPAL_SCOPE_KEY = "qorqut_browser.principal"

# Do not echo exception prose from validation, storage, paths or remote peers.
ERRORS = {
    "invalid_argument": (400, "Invalid request arguments"),
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
    agent_id: UUID


def attribute(headers: Headers, default: Principal) -> Principal:
    """Public local attribution only; no authentication or permission policy."""
    attribution = headers.getlist("x-qorqut-actor")
    if not attribution:
        return default
    try:
        if len(attribution) != 1:
            raise ValueError()
        return Principal(UUID(attribution[0]), default.agent_id)
    except ValueError:
        raise BrowserError("invalid_argument", "Invalid actor UUID") from None


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


def sanitize_sdk_logs():
    """Install once during assembly, before any SDK server is constructed.

    The SDK also calls root logging functions, so logger filters cannot cover
    its validation diagnostics. Sanitize at creation, before any handler sees
    a record, while retaining the existing factory and logging configuration.
    """
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_qorqut_browser_mcp_sanitizer", False):
        return
    package = Path(mcp.__file__).resolve().parent

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        sdk = isinstance(record.name, str) and (record.name == "mcp" or record.name.startswith("mcp."))
        if not sdk:
            try:
                sdk = Path(record.pathname).resolve().is_relative_to(package)
            except (OSError, ValueError, TypeError, RuntimeError):
                pass
        if sdk:
            record.msg = record.message = "MCP SDK diagnostic"
            record.args = ()
            record.exc_info = record.exc_text = record.stack_info = None
        return record

    factory._qorqut_browser_mcp_sanitizer = True
    logging.setLogRecordFactory(factory)


class LocalTransport:
    """Small ASGI boundary; the official SDK still dispatches all MCP messages."""

    def __init__(self, app, principal: Principal, port: int):
        self.app, self.principal = app, principal
        self.host = f"127.0.0.1:{port}"
        self.origin = f"http://{self.host}"
        self.requests = anyio.CapacityLimiter(MAX_REQUESTS)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        acquired = False
        try:
            headers = Headers(scope=scope)
            if (headers.getlist("host") != [self.host]
                    or headers.getlist("origin") not in ([], [self.origin])):
                raise BrowserError("invalid_argument", "Unconfigured HTTP origin or host")
            scope[PRINCIPAL_SCOPE_KEY] = attribute(headers, self.principal)
            # The local protocol has no query-string inputs.
            if scope.get("query_string"):
                raise BrowserError("invalid_argument", "Query arguments are unsupported")
            try:
                self.requests.acquire_nowait()
                acquired = True
            except anyio.WouldBlock:
                raise BrowserError("unavailable", "Concurrent request limit reached") from None
            if scope["type"] == "websocket":
                await self.app(scope, receive, send)
                return
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
            if scope["type"] == "websocket":
                websocket = WebSocket(scope, receive, send)
                if "websocket.http.response" in scope.get("extensions", {}):
                    await websocket.send_denial_response(failure(error))
                else:
                    await websocket.close(code=1008, reason=error_value(error)[1]["error"]["code"])
            else:
                await failure(error)(scope, receive, send)
            return
        finally:
            if acquired:
                self.requests.release()
        await send(start)
        await send({"type": "http.response.body", "body": bytes(output)})
