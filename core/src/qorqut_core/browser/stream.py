"""Same-origin ready-bound binary relay; browser remains the input authority."""

import asyncio
import json
from urllib.parse import urlsplit
from uuid import UUID

from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from ..contracts import CoreError
from ..transports.lifetime import join_tasks, settle_sync

MAX_CHUNK = 64 * 1024
IO_TIMEOUT = 5.0
MAX_VIEWERS = 16


class BrowserSockets:
    def __init__(self, admission):
        self.admission = admission
        self.tasks = set()
        self.requests = set()

    async def close(self):
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await join_tasks(tasks)

    async def endpoint(self, websocket):
        task = asyncio.current_task()
        request_id, peer, children = None, None, []
        claimed = False
        try:
            if self.admission.closing or len(self.tasks) >= MAX_VIEWERS:
                raise CoreError("unavailable", "Browser viewer capacity unavailable")
            request_id = UUID(websocket.path_params["request_id"])
            if request_id in self.requests:
                raise CoreError("not_ready", "A browser viewer is already open")
            self.tasks.add(task)
            self.requests.add(request_id)
            claimed = True
            browsers = websocket.app.state.application.browsers
            context = websocket.scope["qorqut.invocation"]
            target = await settle_sync(lambda: browsers.stream_target(request_id, context))
            url, binding = target
            destination = urlsplit(url)
            # Resolved exclusively from the saved attachment. No UI destination.
            peer = await connect(f"ws://{destination.netloc}/v1/browser/view",
                additional_headers={"X-Qorqut-Actor": str(context.actor_id)}, subprotocols=["binary"],
                open_timeout=IO_TIMEOUT, close_timeout=1, max_size=MAX_CHUNK, max_queue=1,
                write_limit=MAX_CHUNK, compression=None, proxy=None)
            async with asyncio.timeout(IO_TIMEOUT):
                await peer.send(json.dumps({"protocol": 1, "binding": binding.model_dump(mode="json")}))
                ready = await peer.recv()
            if (not isinstance(ready, str) or json.loads(ready) != {
                    "type": "ready", "protocol": 1, "binding": binding.model_dump(mode="json")}):
                raise CoreError("not_ready", "Browser relay did not acknowledge the exact binding")

            async def checked():
                if self.admission.closing or await settle_sync(lambda: browsers.stream_target(request_id, context)) != target:
                    raise CoreError("target_changed", "Browser request changed")

            await checked()
            async with asyncio.timeout(IO_TIMEOUT):
                await websocket.accept(subprotocol="binary" if "binary" in websocket.scope.get("subprotocols", []) else None)

            async def to_browser():
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    data = message.get("bytes")
                    if data is None or len(data) > MAX_CHUNK:
                        raise CoreError("invalid_argument", "Expected a bounded binary browser frame")
                    await checked()
                    async with asyncio.timeout(IO_TIMEOUT):
                        await peer.send(data)

            async def to_ui():
                async for data in peer:
                    if not isinstance(data, bytes) or len(data) > MAX_CHUNK:
                        raise CoreError("invalid_argument", "Invalid browser relay frame")
                    async with asyncio.timeout(IO_TIMEOUT):
                        await websocket.send_bytes(data)

            async def watch():
                while True:
                    await checked()
                    await asyncio.sleep(0.1)

            children = [asyncio.create_task(fn()) for fn in (to_browser, to_ui, watch)]
            await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
        except (CoreError, ValueError, OSError, TimeoutError, WebSocketException, WebSocketDisconnect):
            pass
        finally:
            for child in children:
                child.cancel()
            try:
                await join_tasks(children)
            finally:
                try:
                    if peer is not None:
                        await peer.close()
                finally:
                    if claimed:
                        self.tasks.discard(task)
                        self.requests.discard(request_id)
                    try:
                        async with asyncio.timeout(IO_TIMEOUT):
                            await websocket.close(code=1008)
                    except (RuntimeError, OSError, TimeoutError):
                        pass
