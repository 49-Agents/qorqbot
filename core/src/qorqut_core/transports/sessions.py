"""Process-local browser credentials. Only random-token digests are retained."""

from __future__ import annotations

import hashlib
import re
import secrets
from time import monotonic
from uuid import UUID

COOKIE_NAME = "qorqut_session"
SESSION_SECONDS = 8 * 60 * 60
MAX_SESSIONS = 256
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)
_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def session_cookie(headers: list[bytes]) -> str | None:
    """Refuse duplicate/ambiguous cookies instead of a first/last-value guess."""
    if not headers:
        return None
    if len(headers) != 1:
        raise ValueError("Ambiguous cookies")
    found = None
    for item in headers[0].split(b";"):
        name, sep, value = item.strip().partition(b"=")
        if not sep or not _NAME.fullmatch(name) or any(
                c < 0x21 or c > 0x7e or c in b'",;\\' for c in value):
            raise ValueError("Invalid cookie")
        if name == COOKIE_NAME.encode("ascii"):
            if found is not None:
                raise ValueError("Ambiguous cookies")
            found = value.decode("ascii")
    return found


class Sessions:
    """Accessed only by the ASGI event loop; absolute expiry never slides."""
    def __init__(self):
        self._entries: dict[bytes, tuple[UUID, float]] = {}

    def _expire(self):
        now = monotonic()
        self._entries = {key: entry for key, entry in self._entries.items() if entry[1] > now}

    @staticmethod
    def _digest(token: str | None) -> bytes | None:
        if token is None or not _TOKEN.fullmatch(token):
            return None
        return hashlib.sha256(token.encode("ascii")).digest()

    def actor(self, token: str | None) -> UUID | None:
        self._expire()
        entry = self._entries.get(self._digest(token))
        return entry[0] if entry else None

    def issue(self, actor_id: UUID, prior: str | None) -> str | None:
        self._expire()
        old = self._digest(prior)
        if len(self._entries) >= MAX_SESSIONS and old not in self._entries:
            return None
        token = secrets.token_urlsafe(32)
        digest = self._digest(token)
        while digest in self._entries:
            token = secrets.token_urlsafe(32)
            digest = self._digest(token)
        self._entries.pop(old, None)
        self._entries[digest] = (actor_id, monotonic() + SESSION_SECONDS)
        return token

    def revoke(self, token: str | None):
        self._entries.pop(self._digest(token), None)

    def clear(self):
        self._entries.clear()
