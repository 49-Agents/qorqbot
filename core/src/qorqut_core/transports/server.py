"""One explicit, headless ASGI composition owning one application lifetime."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import anyio
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..app import Application
from ..config import InstallationSettings
from ..contracts import CoreError, Invocation, OperationResult
from .boundary import Boundary, failure
from .configuration import Credentials, InputError, json_object
from .mcp import MCP, install_logging_boundary

MAX_CALLS = 4
_STATUS = {
    "invalid_argument": 400, "not_found": 404,
    "revision_conflict": 409, "idempotency_conflict": 409, "invalid_transition": 409,
    "target_changed": 409, "uncertain_effect": 409, "unsupported_capability": 422,
    "not_ready": 503, "storage_blocked": 503, "unavailable": 503, "unsupported_schema": 503,
}


async def _settle_sync(function):
    """Finish lifetime work even after Task.cancel(), then propagate cancellation.

    AnyIO shielding alone does not shield raw asyncio cancellation. An opener
    must publish its owned Application before cancellation can leave this await.
    """
    future = asyncio.get_running_loop().run_in_executor(None, function)
    cancelled = False
    with anyio.CancelScope(shield=True):
        while True:
            try:
                result = await asyncio.shield(future)
                break
            except asyncio.CancelledError:
                cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


class Dispatcher:
    """At most four admitted calls, with no unbounded thread-executor queue.

    A disconnected/cancelled waiter does not cancel its running invocation or
    free its slot. Shutdown drains every admitted invocation before closing core.
    """

    def __init__(self, application: Application):
        self.application = application
        self.pool = ThreadPoolExecutor(max_workers=MAX_CALLS, thread_name_prefix="qorqut-invoke")
        self.pending = set()
        self.closing = False

    def _finished(self, future):
        self.pending.discard(future)
        # Retrieve failures even when the HTTP/MCP waiter has disconnected.
        if not future.cancelled():
            future.exception()

    async def invoke(self, name, context, arguments) -> OperationResult:
        if self.closing or len(self.pending) >= MAX_CALLS:
            return OperationResult(ok=False, error=CoreError("unavailable", "Operation capacity unavailable; call not admitted").error)
        future = asyncio.get_running_loop().run_in_executor(self.pool, self.application.invoke, name, context, arguments)
        self.pending.add(future)
        future.add_done_callback(self._finished)
        try:
            return await asyncio.shield(future)
        except Exception:
            return OperationResult(ok=False, error=CoreError("unavailable",
                "Operation result unavailable; execution may have occurred. Preserve the original idempotency key.").error)

    async def close(self):
        self.closing = True
        await _settle_sync(self.pool.shutdown)


def create_app(settings: InstallationSettings, credentials: Credentials) -> Starlette:
    """Compose the logging/ASGI boundary; only lifespan startup opens storage."""
    if str(settings.listen_address) != "127.0.0.1":
        raise InputError("This increment requires listen_address 127.0.0.1")
    install_logging_boundary()

    @asynccontextmanager
    async def lifespan(web):
        application = None
        dispatcher = None

        def open_application():
            nonlocal application
            application = Application.open(settings)

        try:
            await _settle_sync(open_application)
            dispatcher = Dispatcher(application)
            for actor_id in credentials.bindings.values():
                result = await dispatcher.invoke("config_get", Invocation(actor_id=actor_id), {})
                if not result.ok:
                    raise RuntimeError("Configured credential identity is unavailable")
            mcp = MCP(dispatcher, application.registry, settings.listen_port)
            web.state.application, web.state.dispatcher = application, dispatcher
            web.state.mcp = mcp
            async with mcp.manager.run():
                yield
        finally:
            try:
                if dispatcher is not None:
                    await dispatcher.close()
            finally:
                if application is not None:
                    await _settle_sync(application.close)

    async def operation(request):
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return failure(415, "invalid_request", "Content-Type must be application/json")
        try:
            arguments = json_object(await request.body())
        except InputError:
            return failure(400, "invalid_request", "Expected a valid JSON object")
        result = await request.app.state.dispatcher.invoke(request.path_params["name"],
            request.scope["qorqut.invocation"], arguments)
        status = 200 if result.ok else _STATUS[result.error.code]
        return JSONResponse(result.model_dump(mode="json"), status_code=status, headers={"Cache-Control": "no-store"})

    async def schema(request):
        registry = request.app.state.application.registry
        return JSONResponse({"api_version": 1, "result_schema": OperationResult.model_json_schema(),
            "operations": [{"name": op.name, "mutation": op.mutation,
                "input_schema": op.arguments.model_json_schema(),
                "output_data_schema": op.output.model_json_schema() if op.output is not None else None}
                for op in registry.values()]}, headers={"Cache-Control": "no-store"})

    async def live(request):
        return JSONResponse({"live": True}, headers={"Cache-Control": "no-store"})

    async def ready(request):
        result = await request.app.state.dispatcher.invoke("config_get",
            Invocation(actor_id=credentials.readiness_actor_id), {})
        return JSONResponse({"ready": result.ok}, status_code=200 if result.ok else 503,
                            headers={"Cache-Control": "no-store"})

    async def unknown_api(request):
        tail = request.path_params["tail"]
        if tail.split("/", 1)[0] != "v1":
            return failure(400, "unsupported_api_version", "Only API version 1 is supported")
        return failure(404, "not_found", "Route is not implemented")

    async def mcp_endpoint(scope, receive, send):
        await scope["app"].state.mcp(scope, receive, send)

    # An ASGI callable object avoids Starlette treating the SDK as a Request endpoint.
    class MCPRoute:
        async def __call__(self, scope, receive, send):
            await mcp_endpoint(scope, receive, send)

    app = Starlette(lifespan=lifespan, routes=[
        Route("/health/live", live, methods=["GET"]),
        Route("/health/ready", ready, methods=["GET"]),
        Route("/api/v1/schema", schema, methods=["GET"]),
        Route("/api/v1/operations/{name}", operation, methods=["POST"]),
        # Stateless JSON MCP has no server-initiated stream. GET is optional in
        # Streamable HTTP; 405 avoids keeping an unused SSE stream alive forever.
        Route("/mcp", MCPRoute(), methods=["POST", "DELETE"]),
        Route("/api/{tail:path}", unknown_api, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]),
    ], middleware=[Middleware(Boundary, credentials=credentials, port=settings.listen_port)])
    app.router.redirect_slashes = False
    return app


class ServerFormatter(logging.Formatter):
    def format(self, record):
        # Uvicorn sometimes receives a preformatted lifespan traceback as msg.
        # Retain routine startup/shutdown logs, but never format failure payloads.
        if record.levelno >= logging.WARNING or record.exc_info or record.stack_info:
            return f"{record.levelname}: Core server diagnostic (details omitted)"
        return super().format(record)


def serve(settings: InstallationSettings, credentials: Credentials) -> None:
    log_config = {"version": 1, "disable_existing_loggers": False,
        "formatters": {"private": {"()": ServerFormatter, "format": "%(levelname)s: %(message)s"}},
        "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "private", "stream": "ext://sys.stderr"}},
        "loggers": {
            "uvicorn": {"handlers": ["console"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": [], "level": "INFO", "propagate": True},
            "uvicorn.access": {"handlers": [], "propagate": False}}}
    server = uvicorn.Server(uvicorn.Config(create_app(settings, credentials), host="127.0.0.1",
        port=settings.listen_port, workers=1, reload=False, proxy_headers=False, forwarded_allow_ips=[],
        access_log=False, log_config=log_config, server_header=False, date_header=False,
        http="h11", ws="none", lifespan="on", loop="asyncio", backlog=64, limit_concurrency=64,
        timeout_keep_alive=5, timeout_graceful_shutdown=10))
    try:
        server.run()
    except SystemExit:
        raise RuntimeError("Core server could not start") from None
    if not server.started:
        raise RuntimeError("Core server could not start")
