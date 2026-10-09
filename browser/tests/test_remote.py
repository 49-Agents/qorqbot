"""Grouped relay/gate checks plus one real installed-desktop RFB flow."""

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from qorqut_browser.config import ServiceConfig
from qorqut_browser.contracts import BrowserError, EffectResult
from qorqut_browser.control import Gate
from qorqut_browser.server import create_app
from qorqut_browser.state import Store
from qorqut_browser.supervisor import verify_process
from qorqut_browser.desktop import read_state
from test_browser import configuration, ready
from test_desktop import DesktopFixture
from wire_fixture import private_file


class Peer:
    """ASGI peer lets the fixture observe closure before control commits."""
    def __init__(self, app, port):
        self.input, self.output = asyncio.Queue(), asyncio.Queue()
        self.closed = False
        self.task = asyncio.create_task(app({"type": "websocket", "path": "/v1/browser/view",
            "query_string": b"", "headers": [(b"host", f"127.0.0.1:{port}".encode()),
                (b"x-qorqut-actor", str(uuid4()).encode())], "subprotocols": ["binary"]},
            self.input.get, self.send))
        self.input.put_nowait({"type": "websocket.connect"})

    async def send(self, message):
        if message["type"] == "websocket.close":
            self.closed = True
        await self.output.put(message)

    async def receive(self):
        return await asyncio.wait_for(self.output.get(), 3)

    async def hello(self, binding):
        accepted = await self.receive()
        assert accepted == {"type": "websocket.accept", "subprotocol": "binary", "headers": []}
        self.input.put_nowait({"type": "websocket.receive", "text": json.dumps({
            "protocol": 1, "binding": binding})})
        return await self.receive()

    async def finish(self):
        self.input.put_nowait({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self.task, 3)


class RelayGateTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="browser-view-gate-")
        self.addCleanup(temporary.cleanup)
        self.connections = []
        self.effects = []
        self.check_on_ready = None

        async def vnc(reader, writer):
            self.connections.append(writer)
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        listener = await asyncio.start_server(vnc, "127.0.0.1", 0)
        async def close_listener():
            listener.close()
            await listener.wait_closed()
        self.addAsyncCleanup(close_listener)
        browser = configuration(Path(temporary.name)).model_copy(update={
            "vnc_port": listener.sockets[0].getsockname()[1]})
        self.store = Store(browser)
        self.store.initialize()
        config = ServiceConfig(browser=browser, helper_executable=Path(sys.executable),
                               actor_id=uuid4(), agent_id=browser.binding.identity.agent_id)

        def readiness(config):
            ready(config)
            if self.check_on_ready:
                self.check_on_ready()

        def effect(command, _):
            self.effects.append(command)
            return EffectResult(text="fixture")

        self.app = create_app(config, readiness=readiness, executor=effect)
        started = asyncio.Event()
        self.stop_requested = asyncio.Event()
        async def lifetime():
            async with self.app.app.lifespan_context(self.app.app):
                started.set()
                await self.stop_requested.wait()
        self.lifespan = asyncio.create_task(lifetime())
        await asyncio.wait_for(started.wait(), 3)
        self.addAsyncCleanup(self.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
            base_url=f"http://127.0.0.1:{browser.service_port}")
        self.addAsyncCleanup(self.client.aclose)

    async def stop(self):
        if self.lifespan is not None:
            self.stop_requested.set()
            await asyncio.wait_for(self.lifespan, 3)
            self.lifespan = None

    async def control(self, mode):
        current = self.store.status().binding.model_dump(mode="json")
        desired = {**current, "control_epoch": current["control_epoch"] + 1}
        response = await self.client.post("/v1/browser/control", json={"command_id": str(uuid4()),
            "expected": current, "desired": desired, "mode": mode, "evidence": "Isolated relay fixture"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["outcome"], "completed", response.text)
        return desired

    def peer(self):
        peer = Peer(self.app, self.store.config.service_port)
        self.addAsyncCleanup(peer.finish)
        return peer

    async def open(self, binding):
        peer = self.peer()
        response = await peer.hello(binding)
        self.assertEqual(json.loads(response["text"]), {"type": "ready", "protocol": 1, "binding": binding})
        return peer

    async def test_human_admission_lock_epoch_and_lifespan(self):
        binding = await self.control("automation")
        denied = await self.peer().hello(binding)
        self.assertEqual((denied["code"], denied["reason"]), (1008, "not_ready"))
        human = await self.control("human")
        stale = await self.peer().hello(binding)
        self.assertEqual(stale["reason"], "target_changed")
        peer = await self.open(human)
        with self.assertRaises(BrowserError):
            with self.store.lock(0):
                self.fail("Viewer must hold the process-shared command lock")
        denied = await self.peer().hello(human)
        self.assertEqual(denied["reason"], "not_ready")
        check_view = Gate.check_view

        def slow_check(gate, binding, token):
            time.sleep(.12)  # Valid observation exceeding the 50 ms poll target.
            return check_view(gate, binding, token)

        with patch.object(Gate, "check_view", slow_check):
            peer.input.put_nowait({"type": "websocket.receive", "bytes": b"bounded fixture bytes"})
            self.assertEqual((await peer.receive())["bytes"], b"bounded fixture bytes")
            self.assertFalse(peer.closed)

        # Gate calls readiness only after acquiring the process-shared lock.
        # The relay's close has to be observable at this pre-receipt boundary.
        self.check_on_ready = lambda: self.assertTrue(peer.closed)
        await self.control("automation")
        self.assertEqual((await peer.receive())["reason"], "target_changed")
        await asyncio.wait_for(peer.task, 3)
        self.assertTrue(all(writer.is_closing() for writer in self.connections))
        self.check_on_ready = None
        human = await self.control("human")
        peer = await self.open(human)
        await self.stop()  # ASGI lifespan closes an active viewer without a release.
        self.assertTrue(peer.closed)
        self.assertTrue(peer.task.done())
        self.assertEqual(self.store.status().mode, "human")
        with self.store.lock(0):
            pass

    async def test_state_and_service_fences_close_idle_viewer(self):
        human = await self.control("human")
        peer = await self.open(human)
        await peer.finish()
        self.assertEqual(self.store.status().mode, "human")
        self.assertTrue(self.store.status().ready)
        for field, value in (("ready", 0), ("mode", "reconciling"), ("service_token", str(uuid4()))):
            peer = await self.open(human)
            with self.store.transaction() as db:
                previous = db.execute(f"SELECT {field} FROM browser_state").fetchone()[0]
                db.execute(f"UPDATE browser_state SET {field}=?", (value,))
            closed = await peer.receive()
            self.assertEqual(closed["type"], "websocket.close")
            self.assertEqual(closed["reason"], "not_ready")
            await asyncio.wait_for(peer.task, 3)
            with self.store.transaction() as db:
                db.execute(f"UPDATE browser_state SET {field}=?", (previous,))


class RFB:
    """Minimal test client for TigerVNC's None-security, raw-pixel RFB flow."""
    def __init__(self, socket):
        self.socket, self.buffer = socket, bytearray()

    async def read(self, size):
        while len(self.buffer) < size:
            frame = await asyncio.wait_for(self.socket.recv(), 5)
            assert isinstance(frame, bytes)
            self.buffer.extend(frame)
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    async def initialize(self):
        assert await self.read(12) == b"RFB 003.008\n"
        await self.socket.send(b"RFB 003.008\n")
        assert await self.read(2) == b"\x01\x01"
        await self.socket.send(b"\x01")
        assert await self.read(4) == b"\0\0\0\0"
        await self.socket.send(b"\x01")  # shared desktop
        initial = await self.read(24)
        self.width, self.height = struct.unpack("!HH", initial[:4])
        await self.read(struct.unpack("!I", initial[20:24])[0])
        # 32 bpp, little endian, true color, RGB shifts 16/8/0; raw rectangles.
        await self.socket.send(struct.pack("!BBBBBBBBHHHBBBxxx", 0, 0, 0, 0,
                                          32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
        await self.socket.send(struct.pack("!BBHi", 2, 0, 1, 0))

    async def pixels(self):
        await self.socket.send(struct.pack("!BBHHHH", 3, 0, 0, 0, self.width, self.height))
        header = await self.read(4)
        assert header[0] == 0
        size = 0
        for _ in range(struct.unpack("!H", header[2:])[0]):
            x, y, width, height, encoding = struct.unpack("!HHHHi", await self.read(12))
            assert encoding == 0
            data = await self.read(width * height * 4)
            assert len(set(data)) > 1  # actual rendered desktop pixels
            size += len(data)
        assert size > 0
        return size

    async def click(self, x, y):
        await self.socket.send(struct.pack("!BBHH", 5, 1, x, y))
        await self.socket.send(struct.pack("!BBHH", 5, 0, x, y))

    async def type(self, text):
        for char in text:
            await self.socket.send(struct.pack("!BBHI", 4, 1, 0, ord(char)))
            await self.socket.send(struct.pack("!BBHI", 4, 0, 0, ord(char)))


class RealViewerTest(DesktopFixture):
    @asynccontextmanager
    async def viewer(self, binding, *, binary=True):
        async with connect(self.url.replace("http:", "ws:") + "/v1/browser/view",
                additional_headers={"X-Qorqut-Actor": str(uuid4())}, proxy=None,
                subprotocols=["binary"] if binary else None, max_size=65536, max_queue=4) as socket:
            self.assertEqual(socket.subprotocol, "binary" if binary else None)
            await socket.send(json.dumps({"protocol": 1, "binding": binding}))
            self.assertEqual(json.loads(await socket.recv()), {"type": "ready", "protocol": 1, "binding": binding})
            yield socket

    async def coordinates(self, selector):
        # Read-only CDP observation, used only to locate this ordinary test form
        # inside Chrome's actual window decorations. Input travels through RFB.
        async with httpx.AsyncClient(trust_env=False) as client:
            tabs = (await client.get(self.config.browser.cdp_endpoint + "/json/list")).json()
        tab = next(tab for tab in tabs if tab["type"] == "page" and str(self.site.server_port) in tab["url"])
        async with connect(tab["webSocketDebuggerUrl"], proxy=None) as socket:
            await socket.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
                "expression": "(() => {const r=document.querySelector(" + json.dumps(selector) + ").getBoundingClientRect();"
                    "return [Math.round(screenX+(outerWidth-innerWidth)/2+r.x+r.width/2),"
                    "Math.round(screenY+outerHeight-innerHeight+r.y+r.height/2)];})()",
                "returnByValue": True}}))
            while True:
                result = json.loads(await socket.recv())
                if result.get("id") == 1:
                    return result["result"]["result"]["value"]

    async def test_actual_rfb_input_release_disconnect_and_mcp_shutdown(self):
        initialized = self.cli("init")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        await self.start_desktop()
        chrome = read_state(self.config.browser).processes["chrome"]
        await self.start_service()
        await self.reconcile()
        async with self.session() as session:
            await self.action(session, {"kind": "navigate", "url": f"http://127.0.0.1:{self.site.server_port}/"})
        field = await self.coordinates("#name")
        button = await self.coordinates("#save")
        human = await self.reconcile("human")
        async with self.viewer(human) as socket:
            rfb = RFB(socket)
            await rfb.initialize()
            pixels = await rfb.pixels()
            with self.assertRaises(BrowserError):
                with Store(self.config.browser).lock(0):
                    self.fail("Real viewer did not hold the process-shared lock")
            async with self.session() as session:
                blocked = await session.call_tool("browser_turn", self.command({"kind": "snapshot"}))
                self.assertEqual(blocked.structuredContent["error"]["code"], "not_ready")
            async with connect(self.url.replace("http:", "ws:") + "/v1/browser/view", proxy=None) as second:
                await second.send(json.dumps({"protocol": 1, "binding": human}))
                with self.assertRaises(ConnectionClosed) as denied:
                    await second.recv()
                self.assertEqual(denied.exception.rcvd.reason, "not_ready")
            await rfb.click(*field)
            await rfb.type("human relay")
            await rfb.click(*button)
            await rfb.pixels()  # ordered server response after the input stream

            async def closed_at():
                await socket.wait_closed()
                return time.monotonic()

            closed = asyncio.create_task(closed_at())
            automation = await self.reconcile()
            receipt_at = time.monotonic()
            self.assertLessEqual(await asyncio.wait_for(closed, 2), receipt_at)
            with self.assertRaises(ConnectionClosed):
                await rfb.type("obsolete")
        async with self.session() as session:
            _, snapshot = await self.action(session, {"kind": "snapshot"})
            self.assertIn("Saved human relay", snapshot["result"]["text"])
            _, screenshot = await self.action(session, {"kind": "screenshot"})
        human = await self.reconcile("human")
        async with self.viewer(human, binary=False) as socket:
            await RFB(socket).initialize()
        self.assertEqual(Store(self.config.browser).status().mode, "human")
        self.assertTrue(Store(self.config.browser).status().ready)
        async with self.viewer(human) as socket:
            await RFB(socket).initialize()
            await asyncio.to_thread(self.stop_service)
            await asyncio.wait_for(socket.wait_closed(), 2)
        verify_process(chrome)
        retained = await self.start_service()
        self.assertEqual(retained["control"]["mode"], "human")
        self.assertFalse(retained["control"]["ready"])
        verify_process(chrome)
        self.stop_service()
        private_file(self.root / "result.json", {"result": "passed", "human_binding": human,
            "released_binding": automation, "rfb_pixel_bytes": pixels, "field": field, "button": button,
            "stream_closed_before_control_receipt": True, "disconnect_retained_human": True,
            "mcp_shutdown_retained_chrome": chrome.model_dump(mode="json"),
            "snapshot": snapshot, "screenshot": screenshot, "runtime": str(self.runtime),
            "vnc_port": self.config.browser.vnc_port, "service_port": self.config.browser.service_port,
            "cdp_port": self.config.browser.cdp_port, "display": self.config.browser.display})


if __name__ == "__main__":
    unittest.main()
