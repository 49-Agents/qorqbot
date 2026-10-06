"""One explicit, headless ASGI composition owning one application lifetime."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..app import Application
from ..config import InstallationSettings
from ..contracts import CoreError, Invocation, OperationResult
from ..storage.worker import JobWorker
from .boundary import Admission, Boundary, failure
from .configuration import Credentials, InputError, json_object
from .lifetime import settle_sync as _settle_sync
from .mcp import MCP, install_logging_boundary
from .sessions import COOKIE_NAME, SESSION_SECONDS, Sessions
from .static import StaticUI

MAX_CALLS = 4
_STATUS = {
    "invalid_argument": 400, "not_found": 404,
    "revision_conflict": 409, "idempotency_conflict": 409, "invalid_transition": 409,
    "target_changed": 409, "uncertain_effect": 409, "unsupported_capability": 422,
    "not_ready": 503, "storage_blocked": 503, "unavailable": 503, "unsupported_schema": 503,
}


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


def create_app(settings: InstallationSettings, credentials: Credentials, ui_root: Path | None = None) -> Starlette:
    """Compose the logging/ASGI boundary; only lifespan startup opens storage."""
    if str(settings.listen_address) != "127.0.0.1":
        raise InputError("This increment requires listen_address 127.0.0.1")
    install_logging_boundary()
    sessions, admission = Sessions(), Admission()
    ui = StaticUI(ui_root) if ui_root is not None else None

    @asynccontextmanager
    async def lifespan(web):
        application = None
        dispatcher = None
        worker = None

        def open_application():
            nonlocal application
            application = Application.open(settings)

        try:
            admission.closing = False
            sessions.clear()
            if ui is not None:
                await _settle_sync(ui.validate)
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
                worker = JobWorker(application.jobs)
                web.state.worker = worker
                await _settle_sync(worker.start)
                try:
                    yield
                finally:
                    admission.closing = True
        finally:
            admission.closing = True
            sessions.clear()
            try:
                if dispatcher is not None:
                    await dispatcher.close()
            finally:
                try:
                    if worker is not None:
                        await _settle_sync(worker.stop)
                finally:
                    if application is not None:
                        await _settle_sync(application.close)

    def identity(actor_id):
        return JSONResponse({"authenticated": True, "actor_id": str(actor_id), "api_version": 1})

    async def session(request):
        prior = request.scope.get("qorqut.session_cookie")
        if request.method in {"GET", "HEAD"}:
            return identity(request.scope["qorqut.invocation"].actor_id)
        if request.method == "DELETE":
            sessions.revoke(prior)
            response = JSONResponse({"authenticated": False})
            response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict")
            return response
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return failure(415, "invalid_request", "Content-Type must be application/json")
        try:
            arguments = json_object(await request.body())
            if set(arguments) != {"token"} or not isinstance(arguments["token"], str):
                raise InputError("Invalid sign-in input")
            actor_id = credentials.actor(b"Bearer " + arguments["token"].encode("ascii"))
        except (InputError, UnicodeError):
            return failure(400, "invalid_request", "Expected a sign-in token")
        if actor_id is None:
            return failure(401, "unauthenticated", "Invalid sign-in credential")
        result = await request.app.state.dispatcher.invoke("config_get", Invocation(actor_id=actor_id), {})
        if not result.ok:
            return failure(503, "unavailable", "Sign-in identity is unavailable")
        if admission.closing:
            return failure(503, "unavailable", "Server is stopping")
        token = sessions.issue(actor_id, prior)
        if token is None:
            return failure(503, "unavailable", "Browser session capacity unavailable")
        response = identity(actor_id)
        response.set_cookie(COOKIE_NAME, token, max_age=SESSION_SECONDS, path="/", httponly=True, samesite="strict")
        return response

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
        healthy = result.ok and request.app.state.worker.ready
        return JSONResponse({"ready": healthy}, status_code=200 if healthy else 503,
                            headers={"Cache-Control": "no-store"})

    async def index(request):
        return ui.response()

    async def asset(request):
        return ui.response(request.path_params["asset"])

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
        Route("/api/v1/session", session, methods=["GET", "POST", "DELETE"]),
        Route("/api/v1/schema", schema, methods=["GET"]),
        Route("/api/v1/operations/{name}", operation, methods=["POST"]),
        # Stateless JSON MCP has no server-initiated stream. GET is optional in
        # Streamable HTTP; 405 avoids keeping an unused SSE stream alive forever.
        Route("/mcp", MCPRoute(), methods=["POST", "DELETE"]),
        Route("/api/{tail:path}", unknown_api, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]),
        *([Route("/", index, methods=["GET"]), Route("/assets/{asset:path}", asset, methods=["GET"])] if ui else []),
    ], middleware=[Middleware(Boundary, credentials=credentials, port=settings.listen_port,
                              sessions=sessions, admission=admission)])
    app.router.redirect_slashes = False
    return app


class ServerFormatter(logging.Formatter):
    def format(self, record):
        # Uvicorn sometimes receives a preformatted lifespan traceback as msg.
        # Retain routine startup/shutdown logs, but never format failure payloads.
        if record.levelno >= logging.WARNING or record.exc_info or record.stack_info:
            return f"{record.levelname}: Core server diagnostic (details omitted)"
        return super().format(record)


def serve(settings: InstallationSettings, credentials: Credentials, ui_root: Path | None = None) -> None:
    log_config = {"version": 1, "disable_existing_loggers": False,
        "formatters": {"private": {"()": ServerFormatter, "format": "%(levelname)s: %(message)s"}},
        "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "private", "stream": "ext://sys.stderr"}},
        "loggers": {
            "uvicorn": {"handlers": ["console"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": [], "level": "INFO", "propagate": True},
            "uvicorn.access": {"handlers": [], "propagate": False}}}
    server = uvicorn.Server(uvicorn.Config(create_app(settings, credentials, ui_root), host="127.0.0.1",
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
