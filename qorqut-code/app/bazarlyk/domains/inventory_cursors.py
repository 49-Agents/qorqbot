"""Short, process-local continuation handles for startup inventory pages."""

from collections import OrderedDict
from copy import deepcopy
import json
import re
import secrets
import threading
import time

from bazarlyk.contracts import DomainError


_HANDLE = re.compile(r"^i[A-Za-z0-9_-]{15}$")
_MAX_HANDLES = 4096
_LIFETIME_SECONDS = 15 * 60


class InventoryCursors:
    """Bounded cache of positions; handles carry no authority and expire on restart."""

    def __init__(self, *, clock=time.monotonic, max_handles=_MAX_HANDLES,
                 lifetime_seconds=_LIFETIME_SECONDS):
        self._clock = clock
        self._max_handles = max_handles
        self._lifetime_seconds = lifetime_seconds
        self._entries = OrderedDict()
        self._by_position = {}
        self._lock = threading.Lock()

    def _prune(self, now):
        for token, (_, expires, position_key) in list(self._entries.items()):
            if expires <= now:
                self._entries.pop(token, None)
                if self._by_position.get(position_key) == token:
                    self._by_position.pop(position_key, None)
        while len(self._entries) > self._max_handles:
            token, (_, _, position_key) = self._entries.popitem(last=False)
            if self._by_position.get(position_key) == token:
                self._by_position.pop(position_key, None)

    def issue(self, binding, positions):
        now = self._clock()
        position_key = (binding, json.dumps(positions, sort_keys=True, separators=(",", ":")))
        with self._lock:
            self._prune(now)
            existing = self._by_position.get(position_key)
            if existing is not None:
                self._entries.move_to_end(existing)
                return existing
            token = "i" + secrets.token_urlsafe(12)[:15]
            while token in self._entries:
                token = "i" + secrets.token_urlsafe(12)[:15]
            self._entries[token] = ((binding, deepcopy(positions)), now + self._lifetime_seconds, position_key)
            self._by_position[position_key] = token
            self._prune(now)
        return token

    def resolve(self, token, binding):
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(token)
            if entry is None:
                raise DomainError("inventory_cursor_expired",
                    "Inventory continuation expired or is unavailable; restart with inventory=true and no inventory_after")
            (saved_binding, positions), _, _ = entry
            if saved_binding != binding:
                raise DomainError("validation_error", "Inventory cursor does not match this agent, scope, or page size")
            # Keep handles replayable for their fixed lifetime; access does not extend expiry.
            self._entries.move_to_end(token)
            return deepcopy(positions)


inventory_cursors = InventoryCursors()


def is_handle(value):
    return isinstance(value, str) and _HANDLE.fullmatch(value) is not None
