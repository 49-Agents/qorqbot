"""Transport-only Terminal admission: reservations span attach/open/close and never outlive a viewer.

The core Terminals service is replaced by a blocking fake; real Starlette WebSocket objects supply
the ASGI connect/disconnect events. Nothing here proves shell behaviour.
"""

import asyncio
import threading
import unittest
from types import SimpleNamespace
from uuid import UUID, uuid4
from unittest.mock import patch

from starlette.websockets import WebSocket

from qorqut_core.contracts import CoreError, Invocation
from qorqut_core.transports import server
from qorqut_core.transports.boundary import Admission
from qorqut_core.transports.server import TerminalSockets


class View:
    dimensions = None
    def model_dump(self, mode=None):
        return {"status": "available"}


class FakeStream:
    def __init__(self, service):
        self.service, self.viewer_id, self.initial, self.closed = service, uuid4(), View(), threading.Event()
        self.close_gate = threading.Event(); self.close_gate.set()
        self.close_started = threading.Event()
        self.close_calls = 0

    def state(self):
        return View()

    def read(self):
        return b"", self.closed.is_set(), None

    def close(self):
        self.close_calls += 1
        self.close_started.set()
        self.close_gate.wait(10)
        self.closed.set()


class FakeTerminals:
    def __init__(self):
        self.calls, self.streams = 0, []
        self.attach_gate = threading.Event(); self.attach_gate.set()
        self.attach_started = threading.Event()
        self.close_gate = threading.Event(); self.close_gate.set()
        self.refuse = False

    def attach(self, agent_id, actor_id):
        self.calls += 1
        self.attach_started.set()
        self.attach_gate.wait(10)
        if self.refuse:
            raise CoreError("unavailable", "Terminal stream capacity unavailable")
        stream = FakeStream(self); stream.close_gate = self.close_gate
        self.streams.append(stream)
        return stream


class Client:
    """A real Starlette WebSocket over an in-memory ASGI seam."""
    def __init__(self, terminals, agent_id, *, close_fails=False):
        self.incoming, self.sent, self.close_fails = asyncio.Queue(), [], close_fails
        self.incoming.put_nowait({"type": "websocket.connect"})
        scope = {"type": "websocket", "path_params": {"agent_id": str(agent_id)}, "headers": [],
                 "app": SimpleNamespace(state=SimpleNamespace(application=SimpleNamespace(terminals=terminals))),
                 "qorqut.invocation": Invocation(actor_id=uuid4())}
        self.ws = WebSocket(scope, self.incoming.get, self.send)

    async def send(self, message):
        if message["type"] == "websocket.close" and self.close_fails:
            raise OSError("client already gone")
        self.sent.append(message["type"])

    def disconnect(self):
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": 1001})

    @property
    def accepted(self):
        return "websocket.accept" in self.sent


class TerminalAdmission(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.terminals, self.sockets, self.agent = FakeTerminals(), TerminalSockets(Admission()), uuid4()
        self.addAsyncCleanup(self.sockets.close)

    async def open(self, **options):
        client = Client(self.terminals, self.agent, **options)
        return client, asyncio.create_task(self.sockets.endpoint(client.ws))

    async def until(self, condition):
        async with asyncio.timeout(5):
            while not condition():
                await asyncio.sleep(0.01)

    def assertIdle(self):
        self.assertEqual((self.sockets.total, self.sockets.agents, len(self.sockets.waiting), self.sockets.tasks), (0, {}, 0, set()))

    async def live(self, count):
        opened = [await self.open() for _ in range(count)]
        await self.until(lambda: all(client.accepted for client, _ in opened))
        return opened

    async def finish(self, opened):
        for client, _ in opened:
            client.disconnect()
        await asyncio.gather(*(task for _, task in opened))

    async def test_final_viewer_waits_for_four_closing_lifetimes_then_attaches(self):
        first = await self.live(4)
        self.terminals.close_gate.clear()
        for client, _ in first:
            client.disconnect()
        await self.until(lambda: all(s.close_started.is_set() for s in self.terminals.streams))
        final, final_task = await self.open()
        await asyncio.sleep(0.15)
        self.assertEqual(self.terminals.calls, 4, "attach must wait for a transport release")
        self.assertFalse(final.accepted)
        self.terminals.close_gate.set()
        await self.until(lambda: final.accepted)
        self.assertEqual(self.terminals.calls, 5)
        await self.finish([(final, final_task)])
        await asyncio.gather(*(task for _, task in first))
        self.assertTrue(all(s.close_calls == 1 for s in self.terminals.streams))
        self.assertIdle()

    async def test_queued_disconnected_viewer_never_attaches(self):
        held = await self.live(4)
        queued, task = await self.open()
        await self.until(lambda: len(self.sockets.waiting) == 1)
        queued.disconnect()
        await task
        self.assertEqual(self.terminals.calls, 4)
        self.assertFalse(queued.accepted)
        self.assertEqual(len(self.sockets.waiting), 0)
        await self.finish(held)
        self.assertIdle()

    async def test_cancel_during_attach_settles_closes_and_releases(self):
        self.terminals.attach_gate.clear()
        client, task = await self.open()
        await asyncio.to_thread(self.terminals.attach_started.wait, 5)
        task.cancel()
        await asyncio.sleep(0.1)
        self.assertFalse(task.done(), "cancellation must wait for the running attach")
        self.assertEqual(self.sockets.total, 1, "capacity is held while attach can still create a stream")
        self.terminals.attach_gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual([s.close_calls for s in self.terminals.streams], [1])
        self.assertFalse(client.accepted)
        self.assertIdle()

    async def test_disconnect_during_attach_closes_created_stream_once(self):
        self.terminals.attach_gate.clear()
        client, task = await self.open()
        await asyncio.to_thread(self.terminals.attach_started.wait, 5)
        client.disconnect()
        await asyncio.sleep(0.1)
        self.assertFalse(task.done())
        self.terminals.attach_gate.set()
        await task
        self.assertEqual([s.close_calls for s in self.terminals.streams], [1])
        self.assertFalse(client.accepted)
        self.assertIdle()

    async def test_wait_is_finite_and_waiters_are_bounded(self):
        held = await self.live(4)
        with patch.object(server, "TERMINAL_ADMISSION_TIMEOUT", 0.2), patch.object(server, "TERMINAL_WAITERS", 1):
            first, first_task = await self.open()
            await self.until(lambda: len(self.sockets.waiting) == 1)
            second, second_task = await self.open()
            await second_task                      # waiter budget full: refused at once
            self.assertFalse(first_task.done())
            await first_task                       # timed out
        for client in (first, second):
            self.assertFalse(client.accepted)
            self.assertEqual(client.sent[-1], "websocket.close")
        self.assertEqual(self.terminals.calls, 4)
        await self.finish(held)
        self.assertIdle()

    async def test_core_refusal_and_already_gone_client_are_quiet(self):
        self.terminals.refuse = True
        client, task = await self.open(close_fails=True)
        await task                                  # OSError on the final best-effort close is harmless
        self.assertFalse(client.accepted)
        self.assertEqual(self.terminals.calls, 1)
        self.assertIdle()

    async def test_unexpected_pre_accept_data_is_refused_not_buffered(self):
        self.terminals.attach_gate.clear()
        client, task = await self.open()
        await asyncio.to_thread(self.terminals.attach_started.wait, 5)
        client.incoming.put_nowait({"type": "websocket.receive", "text": "{}"})
        await asyncio.sleep(0.05)
        self.terminals.attach_gate.set()
        await task
        self.assertFalse(client.accepted)
        self.assertEqual([s.close_calls for s in self.terminals.streams], [1])
        self.assertIdle()
