"""The Chat change stream (``native_chat.http._events``) over a counting stand-in for the facade: resume from a position, the reset
of a cursor that is ahead, the heartbeat, the ``done`` event of a refused read and that a disconnect ends the stream's reads. The
intervals are shortened so nothing here waits long; reads are counted, never timed. Needs the service extras (Starlette, anyio);
without them the module is skipped."""
import isolated_cli_environment  # before any Qorqbot import

import asyncio
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError

try:
    import starlette  # noqa: F401
    SERVICE = True
except ImportError:  # the service extras are not installed
    SERVICE = False

OWNER = "owner-principal"
AGENT = "2f1c8a9e-5b1d-4a3e-9c1e-7d1f5a6b8c90"


class CountingChat:
    """``events`` as the facade answers it: one ``changed`` event while the position is behind the revision."""

    def __init__(self, revision=0):
        self.revision = revision
        self.reads = []  # the position of every read
        self.error = None

    def check_events(self, principal_id, agent_id):
        pass

    def events(self, principal_id, agent_id, cursor, *, limit=200):
        self.reads.append(cursor)
        if self.error is not None:
            raise self.error
        latest = self.revision
        if cursor >= latest:
            return [], latest
        return [{"cursor": latest, "kind": "changed", "data": {"revision": latest}}], latest


class Client:
    """One open stream: the request the route sees and the body it returns."""

    def __init__(self, chat, *, headers=None, query=None, **options):
        self.chat, self.headers, self.query, self.options = chat, headers or {}, query or {}, options
        self.disconnected = False
        self.body = self.pending = None

    async def open(self):
        from qorqut.native_chat import http

        async def is_disconnected():
            return self.disconnected
        request = SimpleNamespace(headers=self.headers, query_params=self.query, is_disconnected=is_disconnected)
        response = await http._events(self.chat, request, OWNER, AGENT, **self.options)
        self.body = response.body_iterator
        return self

    async def next(self, timeout=2):
        """The next chunk. A timeout leaves the read pending (cancelling it would end the generator), so a later call
        continues it."""
        if self.pending is None:
            self.pending = asyncio.ensure_future(self.body.__anext__())
        done, _ = await asyncio.wait({self.pending}, timeout=timeout)
        if not done:
            raise asyncio.TimeoutError
        task, self.pending = self.pending, None
        return task.result().decode()

    async def close(self):
        if self.pending is not None:
            self.pending.cancel()
            await asyncio.gather(self.pending, return_exceptions=True)
            self.pending = None
        await self.body.aclose()


def frames(text):
    return dict(line.split(": ", 1) for line in text.strip().splitlines())


@unittest.skipUnless(SERVICE, "Starlette is not installed")
class StreamBehaviourTest(unittest.TestCase):
    def setUp(self):
        from qorqut.native_chat import http
        for name, value in (("POLL_SECONDS", 0.01), ("HEARTBEAT_SECONDS", 3600)):
            patcher = mock.patch.object(http, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_resume_from_last_event_id_reads_from_that_position(self):
        chat = CountingChat(revision=5)

        async def run():
            client = await Client(chat, headers={"last-event-id": "3"}, query={"cursor": "1"}).open()
            try:
                return await client.next()
            finally:
                await client.close()
        first = frames(asyncio.run(run()))
        self.assertEqual((first["id"], first["event"]), ("5", "chat"))
        self.assertEqual(chat.reads[:2], [3, 3])  # the header wins over ?cursor=

    def test_an_invalid_last_event_id_falls_back_to_the_cursor_parameter(self):
        chat = CountingChat(revision=5)

        async def run():
            client = await Client(chat, headers={"last-event-id": "x"}, query={"cursor": "4"}).open()
            try:
                return await client.next()
            finally:
                await client.close()
        self.assertEqual(frames(asyncio.run(run()))["id"], "5")
        self.assertEqual(chat.reads[0], 4)

    def test_a_position_already_current_sends_nothing_until_the_revision_moves(self):
        chat = CountingChat(revision=5)

        async def run():
            client = await Client(chat, query={"cursor": "5"}).open()
            try:
                with self.assertRaises(asyncio.TimeoutError):
                    await client.next(timeout=0.1)
                chat.revision = 6
                return await client.next()
            finally:
                await client.close()
        self.assertEqual(frames(asyncio.run(run()))["id"], "6")

    def test_a_cursor_ahead_of_the_installation_resets_and_replays_from_zero(self):
        chat = CountingChat(revision=5)

        async def run():
            client = await Client(chat, query={"cursor": "9"}).open()
            try:
                return await client.next(), await client.next()
            finally:
                await client.close()
        reset, first = asyncio.run(run())
        self.assertEqual(reset, "event: reset\ndata: {}\n\n")
        self.assertEqual(frames(first)["id"], "5")
        self.assertEqual(chat.reads[:2], [9, 0])

    def test_an_idle_stream_sends_a_heartbeat(self):
        from qorqut.native_chat import http
        chat = CountingChat(revision=2)

        async def run():
            client = await Client(chat, query={"cursor": "2"}).open()
            try:
                return await client.next()
            finally:
                await client.close()
        with mock.patch.object(http, "HEARTBEAT_SECONDS", 0.05):
            self.assertEqual(asyncio.run(run()), ": heartbeat\n\n")

    def test_a_refused_read_ends_the_stream_with_done(self):
        chat = CountingChat(revision=2)

        async def run():
            client = await Client(chat, query={"cursor": "2"}).open()
            chat.error = DomainError("not_found", "Select an active registered agent")
            try:
                done = await client.next()
                with self.assertRaises(StopAsyncIteration):
                    await client.next()
                return done
            finally:
                await client.close()
        self.assertEqual(asyncio.run(run()), "event: done\ndata: {}\n\n")

    def test_a_disconnect_ends_the_stream_and_its_reads(self):
        chat = CountingChat(revision=1)

        async def run():
            client = await Client(chat, query={"cursor": "0"}).open()
            await client.next()
            client.disconnected = True
            with self.assertRaises(StopAsyncIteration):
                await client.next()
            ended = len(chat.reads)
            await asyncio.sleep(0.1)
            return ended
        ended = asyncio.run(run())
        self.assertEqual(len(chat.reads), ended)

    def test_closing_the_body_ends_the_stream_and_its_reads(self):
        chat = CountingChat(revision=1)

        async def run():
            client = await Client(chat, query={"cursor": "0"}).open()
            await client.next()
            await client.close()
            ended = len(chat.reads)
            await asyncio.sleep(0.1)
            return ended
        ended = asyncio.run(run())
        self.assertEqual(len(chat.reads), ended)


if __name__ == "__main__":
    unittest.main()
