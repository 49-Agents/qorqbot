"""The Chat change stream (``native_chat.http._events``) over a counting stand-in for the facade: resume from a position, the reset
of a cursor that is ahead, the heartbeat, the ``done`` event of a refused read and that a disconnect ends the stream's reads. The
intervals are shortened so nothing here waits long; reads are counted, never timed. Needs the service extras (Starlette, anyio);
without them the module is skipped."""
import isolated_cli_environment  # before any Qorqbot import

import asyncio
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError

try:
    import anyio
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
    INTERVALS = (("POLL_SECONDS", 0.01), ("HEARTBEAT_SECONDS", 3600))

    def setUp(self):
        from qorqut.native_chat import http
        for name, value in self.INTERVALS:
            patcher = mock.patch.object(http, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def client(self, chat, **options):
        return Client(chat, **options)

    def test_a_resume_from_last_event_id_reads_from_that_position(self):
        chat = CountingChat(revision=5)

        async def run():
            client = await self.client(chat, headers={"last-event-id": "3"}, query={"cursor": "1"}).open()
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
            client = await self.client(chat, headers={"last-event-id": "x"}, query={"cursor": "4"}).open()
            try:
                return await client.next()
            finally:
                await client.close()
        self.assertEqual(frames(asyncio.run(run()))["id"], "5")
        self.assertEqual(chat.reads[0], 4)

    def test_a_position_already_current_sends_nothing_until_the_revision_moves(self):
        chat = CountingChat(revision=5)

        async def run():
            client = await self.client(chat, query={"cursor": "5"}).open()
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
            client = await self.client(chat, query={"cursor": "9"}).open()
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
            client = await self.client(chat, query={"cursor": "2"}).open()
            try:
                return await client.next()
            finally:
                await client.close()
        with mock.patch.object(http, "HEARTBEAT_SECONDS", 0.05):
            self.assertEqual(asyncio.run(run()), ": heartbeat\n\n")

    def test_a_refused_read_ends_the_stream_with_done(self):
        chat = CountingChat(revision=2)

        async def run():
            client = await self.client(chat, query={"cursor": "2"}).open()
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
            client = await self.client(chat, query={"cursor": "0"}).open()
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
            client = await self.client(chat, query={"cursor": "0"}).open()
            await client.next()
            await client.close()
            ended = len(chat.reads)
            await asyncio.sleep(0.1)
            return ended
        ended = asyncio.run(run())
        self.assertEqual(len(chat.reads), ended)



@unittest.skipUnless(SERVICE, "Starlette is not installed")
class FedStreamBehaviourTest(StreamBehaviourTest):
    """The same behaviour with the change feed: here nothing commits, so the short fallback is what reads."""
    INTERVALS = (("POLL_SECONDS", 0.01), ("FALLBACK_SECONDS", 0.03), ("HEARTBEAT_SECONDS", 3600))

    def client(self, chat, **options):
        from qorqut.service.changes import ChangeFeed
        if not hasattr(self, "feed"):
            self.feed = ChangeFeed()
        return Client(chat, changes=self.feed, **options)

    def tearDown(self):
        from qorqut.native_chat.stream_wake import wakes_for
        if hasattr(self, "feed"):
            self.assertEqual(len(wakes_for(self.feed)), 0)


class CommittingChat(CountingChat):
    """A commit lands while one read runs, after the read took its snapshot: the read reports the old revision."""

    def __init__(self, feed, revision, commit_on_read):
        super().__init__(revision)
        self.feed, self.commit_on_read = feed, commit_on_read

    def events(self, principal_id, agent_id, cursor, *, limit=200):
        result = super().events(principal_id, agent_id, cursor, limit=limit)
        if len(self.reads) == self.commit_on_read:
            self.revision += 1
            commit(self.feed)
        return result


def commit(feed):
    """What ``Database._unit`` reports after a writable unit that changed a row."""
    feed.publish_commit(captured=[], operation="native_chat.working", project_id=None, wrote=True)


@unittest.skipUnless(SERVICE, "Starlette is not installed")
class WakeTest(unittest.TestCase):
    """The stream reads after a commit or a wildcard, at most once per interval, and on the fallback; nothing in between."""

    def setUp(self):
        from qorqut.service.changes import ChangeFeed
        self.feed = ChangeFeed()
        self.intervals(POLL_SECONDS=0.05, FALLBACK_SECONDS=3600, HEARTBEAT_SECONDS=3600)

    def intervals(self, **values):
        from qorqut.native_chat import http
        for name, value in values.items():
            patcher = mock.patch.object(http, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        from qorqut.native_chat.stream_wake import wakes_for
        self.assertEqual(len(wakes_for(self.feed)), 0)

    def open(self, chat, cursor):
        return Client(chat, query={"cursor": str(cursor)}, changes=self.feed).open()

    async def idle(self, client, seconds):
        with self.assertRaises(asyncio.TimeoutError):
            await client.next(timeout=seconds)

    def test_no_read_while_nothing_is_committed(self):
        chat = CountingChat(revision=2)

        async def run():
            client = await self.open(chat, 2)
            try:
                await self.idle(client, 0.6)
            finally:
                await client.close()
        asyncio.run(run())
        self.assertEqual(len(chat.reads), 2)  # the opening check and the first read; polling would have read about twelve more

    def test_a_commit_from_another_thread_is_read_and_delivered(self):
        chat = CountingChat(revision=2)

        async def run():
            client = await self.open(chat, 2)
            try:
                await self.idle(client, 0.2)
                before = len(chat.reads)
                chat.revision = 3
                await anyio.to_thread.run_sync(commit, self.feed)
                return before, await client.next(timeout=1)
            finally:
                await client.close()
        before, chunk = asyncio.run(run())
        self.assertEqual(before, 2)
        self.assertEqual(frames(chunk)["id"], "3")
        self.assertEqual(len(chat.reads), 3)

    def test_a_write_by_another_process_wakes_the_stream_through_the_wildcard(self):
        chat = CountingChat(revision=2)

        async def run():
            client = await self.open(chat, 2)
            try:
                await self.idle(client, 0.2)
                chat.revision = 3
                await anyio.to_thread.run_sync(self.feed.publish_wildcard)
                return await client.next(timeout=1)
            finally:
                await client.close()
        self.assertEqual(frames(asyncio.run(run()))["id"], "3")

    def test_a_commit_during_a_read_is_not_lost(self):
        chat = CommittingChat(self.feed, revision=2, commit_on_read=2)  # the first read of the loop

        async def run():
            client = await self.open(chat, 2)
            try:
                return await client.next(timeout=1)
            finally:
                await client.close()
        self.assertEqual(frames(asyncio.run(run()))["id"], "3")
        self.assertEqual(len(chat.reads), 3)

    def test_many_commits_are_read_at_most_once_per_interval(self):
        self.intervals(POLL_SECONDS=0.2)
        chat = CountingChat(revision=2)
        stop = threading.Event()
        commits = []

        def keep_committing():
            while not stop.is_set():
                commit(self.feed)
                commits.append(1)
                time.sleep(0.002)

        async def run():
            client = await self.open(chat, 2)
            try:
                await self.idle(client, 0.1)
                before = len(chat.reads)
                writer = threading.Thread(target=keep_committing)
                writer.start()
                try:
                    await self.idle(client, 0.7)
                finally:
                    stop.set()
                    writer.join()
                return before, len(chat.reads)
            finally:
                await client.close()
        before, after = asyncio.run(run())
        self.assertGreater(len(commits), 20)
        self.assertGreaterEqual(after - before, 1)
        self.assertLessEqual(after - before, 5)  # 0.7 s at one read per 0.2 s, plus the read already due when it began

    def test_the_fallback_reads_without_a_commit(self):
        self.intervals(POLL_SECONDS=0.02, FALLBACK_SECONDS=0.15)
        chat = CountingChat(revision=2)

        async def run():
            client = await self.open(chat, 2)
            try:
                await self.idle(client, 0.5)
            finally:
                await client.close()
        asyncio.run(run())
        self.assertGreaterEqual(len(chat.reads) - 2, 1)
        self.assertLessEqual(len(chat.reads) - 2, 3)  # 0.5 s at one read per 0.15 s; polling would have read about 25

    def test_the_heartbeat_is_on_time_without_commits(self):
        self.intervals(HEARTBEAT_SECONDS=0.15)
        chat = CountingChat(revision=2)

        async def run():
            client = await self.open(chat, 2)
            try:
                return await client.next(timeout=1)
            finally:
                await client.close()
        self.assertEqual(asyncio.run(run()), ": heartbeat\n\n")

    def test_a_stop_ends_a_waiting_stream(self):
        from qorqut.transport.http import STOPPING
        chat = CountingChat(revision=2)

        async def run():
            client = await self.open(chat, 2)
            try:
                await self.idle(client, 0.1)
                STOPPING.set()
                with self.assertRaises(StopAsyncIteration):
                    await client.next(timeout=1)
            finally:
                STOPPING.clear()
                await client.close()
        asyncio.run(run())

    def test_streams_opened_and_closed_leave_nothing_behind(self):
        from qorqut.native_chat.stream_wake import wakes_for

        async def one(ending):
            chat = CountingChat(revision=1)
            client = await self.open(chat, 0)
            await client.next()
            self.assertEqual(len(wakes_for(self.feed)), 1)
            if ending == "disconnect":
                client.disconnected = True
                with self.assertRaises(StopAsyncIteration):
                    await client.next()
            elif ending == "refused":
                chat.error = DomainError("not_found", "Select an active registered agent")
                commit(self.feed)
                self.assertEqual(await client.next(), "event: done\ndata: {}\n\n")
            await client.close()  # what Starlette does when the client goes away mid-wait

        async def run():
            for index in range(30):
                await one(("close", "disconnect", "refused")[index % 3])
        asyncio.run(run())
        self.assertEqual(len(wakes_for(self.feed)), 0)
        self.assertEqual(len(self.feed._commit_listeners), 1)
        self.assertEqual(len(self.feed._wildcard_listeners), 1)
        commit(self.feed)  # a notice with nothing open reaches no stream and raises nothing
        self.assertEqual(self.feed.publish_failures, 0)



class WildcardListenerTest(unittest.TestCase):
    """``ChangeFeed.add_wildcard_listener``: called on a write by another process only, and never raising into the ticker."""

    def test_a_wildcard_calls_wildcard_listeners_and_a_commit_does_not(self):
        from qorqut.service.changes import ChangeFeed
        feed, seen = ChangeFeed(), []
        feed.add_wildcard_listener(lambda: seen.append("wildcard"))
        feed.add_commit_listener(lambda: seen.append("commit"))
        commit(feed)
        feed.publish_wildcard()
        self.assertEqual(seen, ["commit", "wildcard"])

    def test_a_failing_wildcard_listener_is_counted_and_the_others_still_run(self):
        from qorqut.service.changes import ChangeFeed
        feed, seen = ChangeFeed(), []

        def broken():
            raise RuntimeError("listener failed")
        feed.add_wildcard_listener(broken)
        feed.add_wildcard_listener(lambda: seen.append("ran"))
        with self.assertLogs("qorqut.service.changes", "ERROR"):
            feed.publish_wildcard()
        self.assertEqual((seen, feed.publish_failures), (["ran"], 1))


if __name__ == "__main__":
    unittest.main()
