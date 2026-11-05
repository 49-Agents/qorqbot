"""Test support: a process-local copy of the empty, fully migrated schema.

Applying every migration takes about 0.6s, and most tests only need a fresh, empty, current database. The first such
request builds one seed with the real initializer, closed and checkpointed; later requests copy that single file to
their own destination and open it with ``Database.open_existing``. Only the empty schema is shared: every caller gets
its own file, ``Database`` object and connections, and nothing a test writes ever reaches the seed.

Anything whose behavior depends on how initialization runs uses the real initializer instead: an existing destination,
a patched initializer/migration provider/hook, or an explicit ``real=True`` from a test of initialization itself.
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading

from bazarlyk.storage import Database
from bazarlyk.storage import database as storage

# What initialization is built from, captured while nothing is patched. A test that replaces any of it (typically to
# build a historical schema) never reuses the seed, whose schema would then be the wrong one.
_GUARDED = ("initialize", "migrations", "open_existing", "_connect")
_ORIGINALS = {name: Database.__dict__[name] for name in _GUARDED}
_HOOKS = tuple(sorted(storage.MIGRATION_HOOKS.items()))
_LOCK = threading.Lock()
_seed = None  # (fingerprint, folder, database path, owner pid): only the current template is ever kept


def _fingerprint(database):
    """The current ordered migration versions/checksums when real initialization would use only unpatched inputs."""
    if (type(database) is not Database
            or any(Database.__dict__.get(name) is not original for name, original in _ORIGINALS.items())
            or tuple(sorted(storage.MIGRATION_HOOKS.items())) != _HOOKS):
        return None
    return tuple((version, checksum) for version, checksum, _ in Database.migrations())


def _discard():
    global _seed
    if _seed is not None and _seed[3] == os.getpid():
        shutil.rmtree(_seed[1], ignore_errors=True)
    _seed = None


atexit.register(_discard)


def _seed_path(fingerprint):
    """The closed seed database for this exact migration set, replacing an outdated one."""
    global _seed
    with _LOCK:
        if _seed is not None and _seed[0] == fingerprint and _seed[2].is_file():
            return _seed[2]
        _discard()
        folder = Path(tempfile.mkdtemp(prefix="bazarlyk-schema-seed-"))
        path = folder / "state.sqlite3"
        Database(path).initialize()
        connection = sqlite3.connect(path)
        try:
            if connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                raise RuntimeError("Schema seed could not be checkpointed")
        finally:
            connection.close()
        if sorted(entry.name for entry in folder.iterdir()) != [path.name]:
            raise RuntimeError("Schema seed must be a single closed file")
        _seed = (fingerprint, folder, path, os.getpid())
        return path


def initialized(database, *, real=False):
    """Return ``database`` initialized to the current schema, like ``database.initialize()``.

    A fresh destination copies the seed; an existing file, a path through a symbolic link (``open_existing`` refuses
    it), patched initialization inputs, or ``real=True`` run the real initializer.
    """
    path = database.path
    fingerprint = None if real else _fingerprint(database)
    if fingerprint is None or path.exists() or any(entry.is_symlink() for entry in (path, *path.parents)):
        return database.initialize()
    seed = _seed_path(fingerprint)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as target, open(seed, "rb") as source:
        shutil.copyfileobj(source, target)
    path.chmod(0o600)
    database.open_existing()
    # The read-only open leaves -wal/-shm files behind; one ordinary connection removes them again, so the folder
    # holds exactly what real initialization leaves.
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA user_version").fetchone()
    finally:
        connection.close()
    return database
