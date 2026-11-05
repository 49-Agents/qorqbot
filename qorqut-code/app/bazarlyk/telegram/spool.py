"""Short-lived private spool for sensitive owner input.

A secret inbound body is written here (0600, fsynced) before the queue pointer commits and the poll cursor advances, and
is referenced from the database only by an opaque ID. Files live in a 0700 directory outside the repositories, the
database, backups, exports and history. Deletion is best effort: this is not a secure-erasure claim, and client
transcripts may retain what a terminal client received.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import time

from qorqut_operations.contracts import DomainError
from .private_files import read_private_file, require_private_directory, write_private_file
from .store import parse

SPOOL_ID = re.compile(r"^[0-9a-f]{32}$")
SUFFIX = ".spool"
MAX_SPOOL_BYTES = 80_000
MAX_LIFETIME_SECONDS = 600


class SecretSpool:
    def __init__(self, directory):
        self.directory = Path(directory)

    def ensure(self):
        require_private_directory(self.directory, create=True)
        return self

    def _path(self, spool_id):
        if not SPOOL_ID.fullmatch(spool_id or ""):
            raise DomainError("validation_error", "Invalid spool reference")
        return self.directory / (spool_id + SUFFIX)

    def put(self, text, expires_at):
        spool_id = secrets.token_hex(16)
        payload = json.dumps({"expires_at": expires_at, "text": text}, ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_SPOOL_BYTES:
            raise DomainError("validation_error", "Sensitive input is too large")
        write_private_file(self.directory, spool_id + SUFFIX, payload)
        return spool_id

    def take(self, spool_id, now):
        """The spooled text, or None when the file is missing, expired or unreadable. It never resurrects expired data."""
        try:
            data = json.loads(read_private_file(self._path(spool_id), max_bytes=MAX_SPOOL_BYTES).decode("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("text"), str) or parse(data["expires_at"]) <= parse(now):
                self.delete(spool_id)
                return None
            return data["text"]
        except (DomainError, ValueError, KeyError, UnicodeError):
            return None

    def delete(self, spool_id):
        try:
            path = self._path(spool_id)
            try:
                size = path.stat().st_size
                with open(path, "r+b") as handle:
                    handle.write(b"\0" * size)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                pass
            path.unlink()
            return True
        except (DomainError, FileNotFoundError):
            return False

    def sweep(self, referenced, now, *, orphan_grace=60.0, wall=time.time):
        """Remove expired files and orphans (no queue pointer). Returns the number removed.

        A file younger than ``orphan_grace`` seconds may belong to an ingestion that has not committed yet, so only
        startup (grace 0, before any poll) removes a fresh orphan. ``referenced=None`` means the queue could not be read:
        every file is kept as if it were referenced, except that an expired one is always erased.
        """
        removed = 0
        for path in self.directory.glob("*" + SUFFIX):
            spool_id = path.name[:-len(SUFFIX)]
            if not SPOOL_ID.fullmatch(spool_id) or path.is_symlink() or not path.is_file():
                continue
            expired = False
            try:
                data = json.loads(path.read_bytes().decode("utf-8"))
                expired = parse(data["expires_at"]) <= parse(now)
            except (OSError, ValueError, KeyError, UnicodeError):
                expired = True
            try:
                fresh = wall() - path.stat().st_mtime < orphan_grace
            except OSError:
                continue
            if expired or (referenced is not None and spool_id not in referenced and not fresh):
                removed += self.delete(spool_id)
        for path in (*self.directory.glob("*.tmp"), *self.directory.glob("paste-*.paste")):
            try:
                if path.is_file() and not path.is_symlink() and (orphan_grace <= 0 or wall() - path.stat().st_mtime >= orphan_grace):
                    path.unlink(missing_ok=True)
            except OSError:
                continue
        return removed
