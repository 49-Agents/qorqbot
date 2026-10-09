"""Human-only WebSocket relay to this browser's configured loopback TigerVNC."""

import anyio
from pydantic import Field
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from .contracts import Binding, BrowserError, FrozenModel
from .transport import BlockingWork, error_value

MAX_FRAME_BYTES = 64 * 1024
MAX_HELLO_BYTES = 4096
IO_TIMEOUT = 5
WATCH_INTERVAL = .05
# The observation budget includes worker scheduling and Store validation.
CHECK_TIMEOUT = 1.0


class ViewHello(FrozenModel):
    protocol: int = Field(strict=True, ge=1, le=1)
    binding: Binding


class Viewers:
    def __init__(self, gate):
        self.gate = gate
        self.work = BlockingWork()
        # Control/commands waiting on the held flock cannot starve its watcher.
        self.checks = anyio.CapacityLimiter(2)
        self.active = {}
        self.closing = False

    async def shutdown(self):
        self.closing = True
        pending = list(self.active.items())
        for scope, _ in pending:
            scope.cancel()
        for _, finished in pending:
            await finished.wait()

    async def check(self, binding, token):
        with anyio.fail_after(CHECK_TIMEOUT):
            # This is a read-only observation: timing out cannot leave an input
            # effect or lock acquisition running in an abandoned worker thread.
            await anyio.to_thread.run_sync(self.gate.check_view, binding, token,
                                          abandon_on_cancel=True, limiter=self.checks)

    async def connect(self, websocket: WebSocket):
        lease = stream = None
        entered = False
        close_code, reason = 1000, ""
        finished = anyio.Event()
        with anyio.CancelScope() as scope:
            self.active[scope] = finished
            try:
                if self.closing:
                    raise BrowserError("unavailable", "Service is shutting down")
                with anyio.fail_after(IO_TIMEOUT):
                    await websocket.accept(subprotocol=(
                        "binary" if "binary" in websocket.scope.get("subprotocols", []) else None))
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    hello = message.get("text")
                    if hello is None or len(hello.encode("utf-8")) > MAX_HELLO_BYTES:
                        raise BrowserError("invalid_argument", "A bounded JSON binding handshake is required")
                    binding = ViewHello.model_validate_json(hello).binding
                lease = self.gate.view(binding)
                # Enter/exit must finish even when a disconnect cancels its wait.
                with anyio.CancelScope(shield=True):
                    token = await self.work.run(lease.__enter__)
                    entered = True

                async with anyio.create_task_group() as tasks:
                    async def run(function):
                        nonlocal close_code, reason
                        try:
                            await function()
                        except (WebSocketDisconnect, anyio.EndOfStream):
                            pass
                        except Exception as error:
                            close_code, reason = self.close_error(error)
                        finally:
                            tasks.cancel_scope.cancel()

                    async def watch():
                        while True:
                            await self.check(binding, token)
                            await anyio.sleep(WATCH_INTERVAL)

                    async def to_vnc():
                        nonlocal close_code, reason
                        while True:
                            message = await websocket.receive()
                            if message["type"] == "websocket.disconnect":
                                return
                            data = message.get("bytes")
                            if data is None:
                                close_code, reason = 1003, "Binary RFB required"
                                return
                            if len(data) > MAX_FRAME_BYTES:
                                close_code, reason = 1009, "RFB frame exceeds limit"
                                return
                            await self.check(binding, token)
                            with anyio.fail_after(IO_TIMEOUT):
                                await stream.send(data)

                    async def to_viewer():
                        while True:
                            data = await stream.receive(MAX_FRAME_BYTES)
                            with anyio.fail_after(IO_TIMEOUT):
                                await websocket.send_bytes(data)

                    async def forward():
                        nonlocal stream
                        with anyio.fail_after(IO_TIMEOUT):
                            stream = await anyio.connect_tcp("127.0.0.1", self.gate.store.config.vnc_port)
                            await self.check(binding, token)
                            await websocket.send_json({"type": "ready", "protocol": 1,
                                                       "binding": binding.model_dump(mode="json")})
                        tasks.start_soon(run, to_vnc)
                        tasks.start_soon(run, to_viewer)
                        await anyio.sleep_forever()

                    tasks.start_soon(run, watch)
                    tasks.start_soon(run, forward)
                    await anyio.sleep_forever()
            except WebSocketDisconnect:
                pass
            except Exception as error:
                close_code, reason = self.close_error(error)
            finally:
                # All forwarding tasks have joined before sockets close. The
                # flock remains held through both closes, so reconcile cannot
                # acknowledge automation with an old relay still forwarding.
                with anyio.CancelScope(shield=True):
                    try:
                        if stream is not None:
                            await anyio.aclose_forcefully(stream)
                        with anyio.move_on_after(1):
                            if websocket.application_state != WebSocketState.DISCONNECTED:
                                await websocket.close(code=close_code, reason=reason)
                    except (OSError, RuntimeError, WebSocketDisconnect):
                        pass
                    finally:
                        if entered:
                            await self.work.run(lease.__exit__, None, None, None)
                        self.active.pop(scope, None)
                        finished.set()

    @staticmethod
    def close_error(error):
        status, value = error_value(error)
        return (1011 if status >= 500 and value["error"]["code"] != "not_ready" else 1008,
                value["error"]["code"])
