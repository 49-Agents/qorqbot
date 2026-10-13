"""One explicit, headless ASGI composition owning one application lifetime."""

from __future__ import annotations

import asyncio
import base64
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import uvicorn
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ..app import Application
from ..browser.models import Handoff
from ..browser.stream import BrowserSockets
from ..config import InstallationSettings
from ..contracts import CoreError, Invocation, OperationResult
from ..runtime import SessionMonitor
from ..runtime.native_hooks import NativeHookRequest, NativeHookResult
from ..runtime.terminal_models import CLIENT_MESSAGE
from ..runtime.terminals import MAX_AGENT_STREAMS, MAX_STREAMS
from ..runner.models import RunnerError
from ..storage.worker import JobWorker
from .boundary import Admission, Boundary, failure
from .configuration import InputError, json_object
from .lifetime import join_tasks as _join_tasks, settle_sync as _settle_sync
from .mcp import MCP, install_logging_boundary
from .static import StaticUI

MAX_CALLS = 4
MAX_WAITERS = 12
ADMISSION_TIMEOUT = 1.0
MAX_TERMINAL_TEXT = 96 * 1024
TERMINAL_SEND_TIMEOUT = 5.0
TERMINAL_WAITERS = 12
TERMINAL_ADMISSION_TIMEOUT = 5.0
_STATUS = {
    "invalid_argument": 400, "not_found": 404,
    "revision_conflict": 409, "idempotency_conflict": 409, "invalid_transition": 409,
    "target_changed": 409, "uncertain_effect": 409, "unsupported_capability": 422,
    "not_ready": 503, "storage_blocked": 503, "unavailable": 503, "unsupported_schema": 503,
}


class Dispatcher:
    """Four executing calls plus twelve async waiters, each waiting at most a second.

    Cancellation withdraws a waiting call, but never frees a running slot.
    Shutdown refuses waiters and drains executing calls before closing core.
    """

    def __init__(self, application: Application):
        self.application = application
        self.pool = ThreadPoolExecutor(max_workers=MAX_CALLS, thread_name_prefix="qorqut-invoke")
        self.pending = set()
        self.waiters = 0
        self.available = asyncio.Event()
        self.closing = False

    def _finished(self, future):
        self.pending.discard(future)
        self.available.set()
        # Retrieve failures even when the HTTP/MCP waiter has disconnected.
        if not future.cancelled():
            future.exception()

    async def _submit(self, function, *args):
        if self.closing:
            return None
        if len(self.pending) >= MAX_CALLS:
            if self.waiters >= MAX_WAITERS:
                return None
            self.waiters += 1
            try:
                async with asyncio.timeout(ADMISSION_TIMEOUT):
                    while not self.closing and len(self.pending) >= MAX_CALLS:
                        self.available.clear()
                        await self.available.wait()
            except TimeoutError:
                return None
            finally:
                self.waiters -= 1
        if self.closing:
            return None
        # No await between checking capacity and recording the submitted call.
        # Only executing calls enter the pool, never the asynchronous waiters.
        future = asyncio.get_running_loop().run_in_executor(self.pool, function, *args)
        self.pending.add(future)
        future.add_done_callback(self._finished)
        return future

    async def invoke(self, name, context, arguments) -> OperationResult:
        future = await self._submit(self.application.invoke, name, context, arguments)
        if future is None:
            return OperationResult(ok=False, error=CoreError("unavailable",
                "Operation capacity unavailable; call not admitted", retryable=True).error)
        try:
            return await asyncio.shield(future)
        except Exception:
            return OperationResult(ok=False, error=CoreError("unavailable",
                "Operation result unavailable; execution may have occurred. Preserve the original idempotency key.").error)

    async def close(self):
        self.closing = True
        self.available.set()
        await _settle_sync(self.pool.shutdown)

    async def native_hook(self, context, request) -> NativeHookResult:
        future = await self._submit(self.application.native_hooks.receive, context, request)
        if future is None:
            return NativeHookResult.result("unavailable")
        try:
            return await asyncio.shield(future)
        except Exception:
            return NativeHookResult.result("unavailable")


class _Slot:
    """One transport reservation, held from before attach until the stream is closed."""
    held = False
    agent_id = None


class TerminalSockets:
    """Attributed protocol framing; all terminal admission/effects are core's.

    Core stays the authority on capacity. This transport only counts the viewer
    lifetimes it owns, so that a viewer arriving while superseded ones are still
    attaching or closing waits briefly for their release instead of being refused.
    """
    def __init__(self, admission):
        self.admission = admission
        self.tasks = set()
        self.total = 0
        self.agents = {}
        self.waiting = set()

    def _free(self, agent_id):
        return self.total < MAX_STREAMS and self.agents.get(agent_id, 0) < MAX_AGENT_STREAMS

    def _release(self, slot):
        if not slot.held:
            return
        slot.held = False
        self.total -= 1
        left = self.agents[slot.agent_id] - 1
        if left:
            self.agents[slot.agent_id] = left
        else:
            del self.agents[slot.agent_id]
        for waiter in self.waiting:
            if not waiter.done():
                waiter.set_result(None)

    async def _reserve(self, agent_id, slot, gone):
        """Take a reservation, waiting at most five seconds for a transport release."""
        if not self._free(agent_id):
            if len(self.waiting) >= TERMINAL_WAITERS:
                raise TimeoutError
            async with asyncio.timeout(TERMINAL_ADMISSION_TIMEOUT):
                while not self._free(agent_id):
                    if gone.done() or self.admission.closing:
                        raise WebSocketDisconnect(1008)
                    waiter = asyncio.get_running_loop().create_future()
                    self.waiting.add(waiter)
                    try:
                        await asyncio.wait({waiter, gone}, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        self.waiting.discard(waiter)
                        waiter.cancel()
        # No await between the final check and the reservation.
        slot.agent_id, slot.held = agent_id, True
        self.total += 1
        self.agents[agent_id] = self.agents.get(agent_id, 0) + 1

    async def close(self):
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await _join_tasks(tasks)

    async def endpoint(self, websocket):
        task = asyncio.current_task()
        self.tasks.add(task)
        stream = None
        tasks = []
        actor = websocket.scope["qorqut.invocation"].actor_id
        replies = asyncio.Queue(maxsize=1)
        pending_replies = set()
        stopping = False

        def stop():
            nonlocal stopping
            stopping = True
            for delivered in pending_replies:
                if not delivered.done():
                    delivered.cancel()

        def admitted():
            if stopping or self.admission.closing:
                raise WebSocketDisconnect(1008)

        async def send(message=None, data=None):
            async with asyncio.timeout(TERMINAL_SEND_TIMEOUT):
                if data is not None:
                    await websocket.send_bytes(data)
                else:
                    await websocket.send_json(message)

        async def reply(message):
            if stopping:
                raise asyncio.CancelledError
            delivered = asyncio.get_running_loop().create_future()
            pending_replies.add(delivered)
            try:
                replies.put_nowait((message, delivered))
                await delivered  # No next frame is admitted behind a stalled reply.
            finally:
                pending_replies.discard(delivered)

        def error(code, related=None, sequence=None):
            return {"type": "error", "code": code, "message": "Terminal request was not confirmed",
                    "related_type": related, "sequence": sequence}

        async def receive():
            while True:
                message = await websocket.receive()
                admitted()  # Also applies to malformed/binary frames.
                if message["type"] == "websocket.disconnect":
                    return
                related, sequence = None, None
                try:
                    text = message.get("text")
                    if text is None or len(text.encode("utf-8")) > MAX_TERMINAL_TEXT:
                        raise InputError("Invalid Terminal frame")
                    body = json_object(text.encode("utf-8"))
                    related = body.get("type") if body.get("type") in ("control", "input", "resize") else None
                    sequence = body.get("sequence")
                    if type(sequence) is not int or not 1 <= sequence <= 2**63 - 1:
                        sequence = None
                    frame = CLIENT_MESSAGE.validate_python(body)
                except (InputError, ValidationError, UnicodeError):
                    await reply(error("invalid_argument", related, sequence))
                    continue
                admitted()
                try:
                    if frame.type == "control":
                        result = await _settle_sync(lambda: stream.control(frame.action, frame.expected_epoch))
                        result = {"type": "control", "control": result.model_dump(mode="json")}
                    else:
                        if frame.type == "input":
                            result = await _settle_sync(lambda: stream.input(base64.b64decode(frame.data, validate=True),
                                                                           epoch=frame.epoch, sequence=frame.sequence))
                        else:
                            result = await _settle_sync(lambda: stream.resize(frame.columns, frame.rows,
                                                                             epoch=frame.epoch, sequence=frame.sequence))
                        result = {"type": "frame", "kind": frame.type, **result.model_dump(mode="json")}
                    await reply(result)
                except (CoreError, RunnerError) as exc:
                    # settle_sync can raise the settled effect's error after
                    # cancellation. There is no writer left to deliver it to.
                    if stopping or asyncio.current_task().cancelling():
                        raise asyncio.CancelledError from exc
                    code = str(exc.error.code) if isinstance(exc, CoreError) else exc.code
                    # Codes are a closed application vocabulary; never expose
                    # arbitrary exception strings or local target paths.
                    if code not in _STATUS:
                        code = "unavailable"
                    await reply(error(code, related, sequence))
                    if code in {"target_changed", "not_found"}:
                        return

        latest = None

        async def watch():
            nonlocal latest
            while True:
                admitted()
                latest = await _settle_sync(stream.state)
                await asyncio.sleep(0.5)

        async def write():
            try:
                await send({"type": "hello", "protocol": 2, "viewer_id": str(stream.viewer_id),
                            "terminal": stream.initial.model_dump(mode="json")})
                dimensions = stream.initial.dimensions
                next_state = 0
                while True:
                    admitted()
                    if not replies.empty():
                        message, delivered = replies.get_nowait()
                        await send(message)
                        if not delivered.done():
                            delivered.set_result(None)
                    now = asyncio.get_running_loop().time()
                    if now >= next_state:
                        await send({"type": "state", "terminal": latest.model_dump(mode="json")})
                        next_state = now + 1
                    data, closed, read_dimensions = await _settle_sync(stream.read)
                    if read_dimensions is not None and read_dimensions != dimensions:
                        await send({"type": "size", **read_dimensions.model_dump()})
                        dimensions = read_dimensions
                    if data:
                        await send(data=data)  # Next read waits for this send.
                    elif closed:
                        return
                    else:
                        await asyncio.sleep(0.02)
            finally:
                stop()  # No queued or in-flight delivery may outlive its writer.

        slot = _Slot()
        gone = attaching = None
        try:
            admitted()
            agent_id = UUID(websocket.path_params["agent_id"])
            # Consume the connect event, then watch for a disconnect until accepted.
            # Any other pre-accept event is refused, never buffered as input.
            if getattr(websocket, "client_state", None) == WebSocketState.CONNECTING:
                if (await websocket.receive())["type"] != "websocket.connect":
                    raise WebSocketDisconnect(1008)
            gone = asyncio.ensure_future(websocket.receive())
            await self._reserve(agent_id, slot, gone)
            admitted()
            if gone.done():
                raise WebSocketDisconnect(1006)

            def attach():
                nonlocal stream
                stream = websocket.app.state.application.terminals.attach(agent_id, actor)

            # Assignment occurs inside the settled call, so cancellation after
            # creation cannot lose the owned attachment before finally cleanup.
            attaching = asyncio.ensure_future(_settle_sync(attach))
            await asyncio.wait({attaching, gone}, return_when=asyncio.FIRST_COMPLETED)
            if not attaching.done():
                raise WebSocketDisconnect(1006)
            attaching.result()
            admitted()
            gone.cancel()
            await _join_tasks([gone])
            if not gone.cancelled():
                raise WebSocketDisconnect(1006)
            latest = stream.initial
            async with asyncio.timeout(TERMINAL_SEND_TIMEOUT):
                await websocket.accept()
            tasks = [asyncio.create_task(function()) for function in (receive, watch, write)]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (CoreError, RunnerError, ValueError, OSError, WebSocketDisconnect, TimeoutError):
            pass
        finally:
            stop()
            for child in (*tasks, *(t for t in (gone,) if t is not None)):
                child.cancel()
            try:
                await _join_tasks(tasks + [t for t in (gone, attaching) if t is not None])
            finally:
                try:
                    if stream is not None:
                        await _settle_sync(stream.close)
                finally:
                    # The reservation outlives the attach call and the owned close.
                    self._release(slot)
                    self.tasks.discard(task)
                    try:
                        async with asyncio.timeout(TERMINAL_SEND_TIMEOUT):
                            await websocket.close(code=1008)
                    except (OSError, RuntimeError, TimeoutError, WebSocketDisconnect):
                        pass


def create_app(settings: InstallationSettings, ui_root: Path | None = None) -> Starlette:
    """Compose the logging/ASGI boundary; only lifespan startup opens storage."""
    if str(settings.listen_address) != "127.0.0.1":
        raise InputError("This increment requires listen_address 127.0.0.1")
    install_logging_boundary()
    admission = Admission()
    terminals = TerminalSockets(admission)
    browsers = BrowserSockets(admission)
    ui = StaticUI(ui_root) if ui_root is not None else None

    @asynccontextmanager
    async def lifespan(web):
        application = None
        dispatcher = None
        worker = None
        monitor = None

        def open_application():
            nonlocal application
            application = Application.open(settings)

        try:
            admission.closing = False
            if ui is not None:
                await _settle_sync(ui.validate)
            await _settle_sync(open_application)
            try:
                web.state.local_owner = await _settle_sync(application.local_owner)
            except CoreError as exc:
                if exc.error.code == "invalid_argument":
                    logging.getLogger(__name__).error("Local access requires exactly one registered owner")
                raise
            dispatcher = Dispatcher(application)
            mcp = MCP(dispatcher, application.registry, settings.listen_port)
            web.state.application, web.state.dispatcher = application, dispatcher
            web.state.mcp = mcp
            async with mcp.manager.run():
                worker = JobWorker(application.jobs)
                web.state.worker = worker
                await _settle_sync(worker.start)
                monitor = SessionMonitor(application.session_owners,
                    native_setup=application.native_setup, native_collector=application.native_collector,
                    native_attention=application.native_attention)
                web.state.session_monitor = monitor
                await _settle_sync(monitor.start)
                await _settle_sync(application.terminals.start)
                try:
                    yield
                finally:
                    admission.closing = True
        finally:
            admission.closing = True
            try:
                try:
                    try:
                        await browsers.close()
                    finally:
                        await terminals.close()
                finally:
                    if dispatcher is not None:
                        await dispatcher.close()
            finally:
                try:
                    if worker is not None:
                        await _settle_sync(worker.stop)
                finally:
                    try:
                        if monitor is not None:
                            await _settle_sync(monitor.stop)
                    finally:
                        if application is not None:
                            await _settle_sync(application.close)

    async def session(request):
        context = request.scope["qorqut.invocation"]
        result = await request.app.state.dispatcher.invoke("config_get", context, {})
        if not result.ok:
            return failure(_STATUS[result.error.code], result.error.code, result.error.message)
        return JSONResponse({"actor_id": str(context.actor_id), "api_version": 1})

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

    async def native_hook(request):
        # Internal observation with the same public actor attribution as MCP.
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return failure(415, "invalid_request", "Content-Type must be application/json")
        try:
            body = NativeHookRequest.model_validate(json_object(await request.body()))
        except (InputError, ValidationError, ValueError, UnicodeError, RecursionError):
            return JSONResponse(NativeHookResult.result("unavailable").model_dump(mode="json"), status_code=400)
        result = await request.app.state.dispatcher.native_hook(request.scope["qorqut.invocation"], body)
        status = 409 if result.outcome == "conflict" else 503 if result.outcome == "unavailable" else 200
        return JSONResponse(result.model_dump(mode="json"), status_code=status)

    async def browser_handoff(request):
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return failure(400, "invalid_argument", "Content-Type must be application/json")
        try:
            body = Handoff.model_validate(json_object(await request.body()))
        except (InputError, ValidationError, ValueError, UnicodeError, RecursionError):
            return failure(400, "invalid_argument", "Invalid browser handoff payload")
        # Protocol 1 carries the original public actor in its body. The browser
        # bridge sends no header and must not become the transport's local owner.
        result = await request.app.state.dispatcher.invoke("input_requests_create", Invocation(actor_id=body.actor_id),
            body.model_dump(mode="json", exclude={"protocol", "actor_id"}))
        if not result.ok:
            return failure(_STATUS[result.error.code], result.error.code, result.error.message)
        return JSONResponse({"protocol": 1, "actor_id": str(body.actor_id),
            "target_agent_id": str(body.target_agent_id), "idempotency_key": body.idempotency_key,
            **{key: result.data[key] for key in ("request_id", "conversation_id", "surface_id", "state", "browser_binding")}})

    async def live(request):
        return JSONResponse({"live": True}, headers={"Cache-Control": "no-store"})

    async def ready(request):
        result = await request.app.state.dispatcher.invoke("config_get",
            Invocation(actor_id=request.app.state.local_owner), {})
        healthy = (result.ok and request.app.state.worker.ready and request.app.state.session_monitor.ready
                   and request.app.state.application.terminals.ready)
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
        Route("/api/v1/session", session, methods=["GET"]),
        Route("/api/v1/schema", schema, methods=["GET"]),
        Route("/api/v1/operations/{name}", operation, methods=["POST"]),
        Route("/api/v1/native/hook", native_hook, methods=["POST"]),
        Route("/api/v1/browser/handoff", browser_handoff, methods=["POST"]),
        WebSocketRoute("/api/v1/browser/{request_id}/stream", browsers.endpoint),
        WebSocketRoute("/api/v1/terminal/{agent_id}", terminals.endpoint),
        # Stateless JSON MCP has no server-initiated stream. GET is optional in
        # Streamable HTTP; 405 avoids keeping an unused SSE stream alive forever.
        Route("/mcp", MCPRoute(), methods=["POST", "DELETE"]),
        Route("/api/{tail:path}", unknown_api, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]),
        *([Route("/", index, methods=["GET"]), Route("/assets/{asset:path}", asset, methods=["GET"])] if ui else []),
    ], middleware=[Middleware(Boundary, port=settings.listen_port, admission=admission)])
    app.router.redirect_slashes = False
    return app


class ServerFormatter(logging.Formatter):
    def format(self, record):
        # Uvicorn sometimes receives a preformatted lifespan traceback as msg.
        # Retain routine startup/shutdown logs, but never format failure payloads.
        if record.levelno >= logging.WARNING or record.exc_info or record.stack_info:
            return f"{record.levelname}: Core server diagnostic (details omitted)"
        return super().format(record)


def serve(settings: InstallationSettings, ui_root: Path | None = None) -> None:
    log_config = {"version": 1, "disable_existing_loggers": False,
        "formatters": {"private": {"()": ServerFormatter, "format": "%(levelname)s: %(message)s"}},
        "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "private", "stream": "ext://sys.stderr"}},
        "loggers": {
            "uvicorn": {"handlers": ["console"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": [], "level": "INFO", "propagate": True},
            "uvicorn.access": {"handlers": [], "propagate": False}}}
    server = uvicorn.Server(uvicorn.Config(create_app(settings, ui_root), host="127.0.0.1",
        port=settings.listen_port, workers=1, reload=False, proxy_headers=False, forwarded_allow_ips=[],
        access_log=False, log_config=log_config, server_header=False, date_header=False,
        http="h11", ws="websockets-sansio", ws_max_size=MAX_TERMINAL_TEXT, ws_per_message_deflate=False,
        lifespan="on", loop="asyncio", backlog=64, limit_concurrency=64,
        timeout_keep_alive=5, timeout_graceful_shutdown=10))
    try:
        server.run()
    except SystemExit:
        raise RuntimeError("Core server could not start") from None
    if not server.started:
        raise RuntimeError("Core server could not start")
