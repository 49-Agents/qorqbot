"""Owned filesystem checks and the single durable browser receipt store."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from uuid import UUID, uuid4

from .contracts import (
    Binding, BrowserConfig, BrowserError, ControlStatus, EffectResult, Mount,
    Receipt, canonical,
)

OWNER_FILE = ".qorqut-browser-owner"


def mount_inventory() -> tuple[Mount, ...]:
    def unescape(value):
        return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)

    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        split = fields.index("-")
        mounts.append(Mount(path=unescape(fields[4]), mount_id=int(fields[0]),
                            device=fields[2], filesystem=fields[split + 1]))
    return tuple(mounts)


def checked_path(path: Path, uid: int, *, directory=False):
    try:
        if path.resolve(strict=True) != path:
            raise ValueError("symlink")
        info = path.lstat()
        if (info.st_uid != uid or info.st_mode & 0o022
                or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
            raise ValueError("ownership or type")
        return info
    except (OSError, ValueError) as error:
        raise BrowserError("not_ready", f"Owned path unavailable: {path}") from error


def read_private(path: Path, uid: int, limit=65536) -> bytes:
    checked_path(path, uid)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022:
            raise BrowserError("not_ready", "State ownership changed")
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise BrowserError("not_ready", "State exceeds its bounded format")
    return value


def fsync_directory(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def exclusive_file(path: Path, content: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    fsync_directory(path.parent)


def replace_file(path: Path, content: bytes, uid: int):
    """Durably replace an owned small state record without following symlinks."""
    if os.path.lexists(path):
        checked_path(path, uid)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        exclusive_file(temporary, content)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def check_paths(config: BrowserConfig, *, claims=True):
    """Never mkdir a missing HOME, profile or mount as a readiness fallback."""
    if config.uid != os.getuid():
        raise BrowserError("not_ready", "Service user does not own the configured paths")
    try:
        mounts = mount_inventory()
        for path in config.owned_directories:
            checked_path(path, config.uid, directory=True)
            actual = max((m for m in mounts if path.is_relative_to(m.path)),
                         key=lambda m: len(m.path.parts))
            if actual not in config.mounts:
                raise BrowserError("not_ready", f"Intended mount is missing or replaced: {path}")
            if claims:
                owner = read_private(path / OWNER_FILE, config.uid, 512).decode()
                if owner != str(config.binding.identity.agent_id):
                    raise BrowserError("target_changed", "Managed path belongs to another agent")
    except (OSError, ValueError) as error:
        raise BrowserError("not_ready", "Cannot verify configured mounts and ownership") from error


class Store:
    def __init__(self, config: BrowserConfig):
        self.config = config
        self.lock_path = config.database.with_suffix(".command.lock")

    def initialize(self):
        """Explicit provisioning only. Claim existing directories, never create profiles.

        Partial provisioning is conservative: existing ownership files are checked,
        not overwritten. No readiness record or desktop is created here.
        """
        check_paths(self.config, claims=False)
        owner = str(self.config.binding.identity.agent_id).encode()
        for path in self.config.owned_directories:
            marker = path / OWNER_FILE
            try:
                exclusive_file(marker, owner)
            except FileExistsError:
                if read_private(marker, self.config.uid, 512) != owner:
                    raise BrowserError("target_changed", "Managed path already claimed by another agent")
        for path in (self.lock_path, self.config.database):
            try:
                exclusive_file(path, b"")
            except FileExistsError:
                checked_path(path, self.config.uid)
        with self.lock(5):
            with self.connection(validate=False) as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1):
                    raise BrowserError("unsupported_capability", "Unsupported browser receipt schema")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS browser_state (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        resource_hash TEXT NOT NULL,
                        binding TEXT NOT NULL,
                        mode TEXT NOT NULL CHECK(mode IN ('automation','yielding','human','reconciling')),
                        ready INTEGER NOT NULL CHECK(ready IN (0,1)),
                        service_token TEXT
                    );
                    CREATE TABLE IF NOT EXISTS commands (
                        command_id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL CHECK(kind IN ('browser','control')),
                        binding TEXT NOT NULL,
                        actor_id TEXT NOT NULL,
                        payload_hash TEXT NOT NULL,
                        outcome TEXT NOT NULL CHECK(outcome IN ('intent','pending','completed','failed','uncertain')),
                        result TEXT NOT NULL,
                        error TEXT,
                        settled INTEGER NOT NULL CHECK(settled IN (0,1)),
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS unsettled_commands ON commands(kind, settled);
                    CREATE TABLE IF NOT EXISTS control_events (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        command_id TEXT REFERENCES commands(command_id),
                        actor_id TEXT,
                        previous TEXT NOT NULL,
                        current TEXT NOT NULL,
                        evidence TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    PRAGMA user_version=1;
                """)
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT OR IGNORE INTO browser_state VALUES(1, ?, ?, 'reconciling', 0, NULL)",
                           (self.config.resource_hash, canonical(self.config.binding)))
                self.validate(db)
                db.commit()
            fsync_directory(self.config.database.parent)

    @contextmanager
    def connection(self, *, validate=True):
        check_paths(self.config)
        checked_path(self.config.database, self.config.uid)
        for suffix in ("-wal", "-shm"):
            path = Path(str(self.config.database) + suffix)
            if os.path.lexists(path):
                checked_path(path, self.config.uid)
        try:
            db = sqlite3.connect(self.config.database.as_uri() + "?mode=rw", uri=True,
                                 timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA busy_timeout=5000")
                db.execute("PRAGMA foreign_keys=ON")
                if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                    raise BrowserError("storage_blocked", "Browser receipts require WAL")
                db.execute("PRAGMA synchronous=FULL")
                if validate:
                    self.validate(db)
                yield db
            finally:
                db.close()
        except sqlite3.Error as error:
            raise BrowserError("storage_blocked", "Browser receipt storage unavailable") from error

    def validate(self, db):
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise BrowserError("not_ready", "Browser receipt schema not initialized")
        row = db.execute("SELECT * FROM browser_state WHERE singleton=1").fetchone()
        if row is None:
            raise BrowserError("not_ready", "Browser state has not been provisioned")
        if row["resource_hash"] != self.config.resource_hash:
            raise BrowserError("target_changed", "Receipt database has another resource binding")

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @contextmanager
    def lock(self, timeout: float):
        check_paths(self.config)
        checked_path(self.lock_path, self.config.uid)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            original = os.fstat(fd)
            deadline = time.monotonic() + max(0, timeout)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise BrowserError("not_ready", "A managed browser command is still in flight")
                    time.sleep(min(.02, max(0, deadline - time.monotonic())))
            current = self.lock_path.stat()
            if (original.st_dev, original.st_ino) != (current.st_dev, current.st_ino):
                raise BrowserError("not_ready", "Command lock inode changed")
            yield fd
        finally:
            # Close-only by default: inherited helpers keep admission held after
            # a service crash. Gate explicitly unlocks a completed invocation only
            # after its settled receipt commits; lookup/control do not unlock it.
            os.close(fd)

    @staticmethod
    def status_in(db) -> ControlStatus:
        row = db.execute("SELECT * FROM browser_state WHERE singleton=1").fetchone()
        return ControlStatus(binding=Binding.model_validate_json(row["binding"]),
                             mode=row["mode"], ready=bool(row["ready"]))

    def status(self) -> ControlStatus:
        with self.connection() as db:
            return self.status_in(db)

    @staticmethod
    def get_receipt(db, command_id: UUID) -> Receipt | None:
        row = db.execute("SELECT * FROM commands WHERE command_id=?", (str(command_id),)).fetchone()
        if row is None:
            return None
        return Receipt(command_id=row["command_id"], kind=row["kind"],
                       binding=Binding.model_validate_json(row["binding"]), actor_id=row["actor_id"],
                       payload_hash=row["payload_hash"], outcome=row["outcome"],
                       result=EffectResult.model_validate_json(row["result"]), error=row["error"])

    @staticmethod
    def insert(db, receipt: Receipt, *, settled=False):
        db.execute("INSERT INTO commands VALUES(?,?,?,?,?,?,?,?,?,?)", (
            str(receipt.command_id), receipt.kind, canonical(receipt.binding), str(receipt.actor_id),
            receipt.payload_hash, receipt.outcome, canonical(receipt.result), receipt.error,
            int(settled), datetime.now(timezone.utc).isoformat()))

    @staticmethod
    def finish(db, receipt: Receipt, *, settled: bool):
        db.execute("UPDATE commands SET outcome=?, result=?, error=?, settled=? WHERE command_id=?", (
            receipt.outcome, canonical(receipt.result), receipt.error, int(settled), str(receipt.command_id)))

    @staticmethod
    def set_status(db, current: ControlStatus, *, command_id=None, actor_id=None, evidence: str):
        previous = Store.status_in(db)
        db.execute("UPDATE browser_state SET binding=?,mode=?,ready=? WHERE singleton=1",
                   (canonical(current.binding), current.mode, int(current.ready)))
        db.execute("INSERT INTO control_events(command_id,actor_id,previous,current,evidence,created_at) "
                   "VALUES(?,?,?,?,?,?)", (str(command_id) if command_id else None,
                   str(actor_id) if actor_id else None, canonical(previous), canonical(current),
                   evidence, datetime.now(timezone.utc).isoformat()))
