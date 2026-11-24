"""Wakes for the open Chat streams: a stream reads the database only after something was committed.

One ``StreamWakes`` per change feed, registered once as a commit listener and a wildcard listener, fans each notice out to the
streams open at that moment. A notice is not per agent: every open stream reads once, and the read itself decides whether its
agent's revision moved. A notice arrives on the committing thread (or the feed's external-writer ticker), so it reaches a stream's
event loop only through ``call_soon_threadsafe``; nothing on the loop is touched from that thread.
"""

from __future__ import annotations

import asyncio
import threading
import weakref

_LOCK = threading.Lock()
_BY_FEED: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


class Wake:
    """One stream's wake. ``set`` may be called from any thread; ``clear``, ``is_set`` and ``wait`` only on the stream's loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._event = asyncio.Event()
        self._lock = threading.Lock()
        self._scheduled = False  # a set is already on its way to the loop: later notices until the next clear add nothing

    def set(self) -> None:
        with self._lock:
            if self._scheduled:
                return
            self._scheduled = True
        try:
            self._loop.call_soon_threadsafe(self._event.set)
        except RuntimeError:
            pass  # the loop is closed: the stream is gone

    def clear(self) -> None:
        """Called before each read, so a commit during the read leaves the wake set and is read next."""
        with self._lock:
            self._scheduled = False
        self._event.clear()

    def is_set(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


class StreamWakes:
    def __init__(self):
        self._lock = threading.Lock()
        self._wakes: set[Wake] = set()

    def notify(self) -> None:
        with self._lock:
            wakes = list(self._wakes)
        for wake in wakes:
            wake.set()

    def open(self) -> Wake | None:
        """A wake for a stream on the running loop, or None when the loop is not asyncio (the stream then reads on its interval)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        wake = Wake(loop)
        with self._lock:
            self._wakes.add(wake)
        return wake

    def close(self, wake: Wake | None) -> None:
        if wake is not None:
            with self._lock:
                self._wakes.discard(wake)

    def __len__(self) -> int:
        with self._lock:
            return len(self._wakes)


def wakes_for(changes) -> StreamWakes | None:
    """The one fan-out of this change feed, registered on first use and kept for the feed's lifetime. None without a feed."""
    if changes is None:
        return None
    with _LOCK:
        wakes = _BY_FEED.get(changes)
        if wakes is None:
            wakes = _BY_FEED[changes] = StreamWakes()
            changes.add_commit_listener(wakes.notify)
            changes.add_wildcard_listener(wakes.notify)
        return wakes
