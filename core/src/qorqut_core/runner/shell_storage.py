"""Recorded-shell tables in the existing local runner/spool SQLite file."""

from contextlib import contextmanager
import fcntl
import os
import stat
import time

from .models import RunnerError
from .shell_models import ShellControl
from .storage import connect


def ensure_schema(database):
    with connect(database) as db:
        try:
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS runner_shell_terminals (
                    terminal_id TEXT PRIMARY KEY,
                    installation_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    spec_hash TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    launch_json TEXT NOT NULL,
                    runtime_root TEXT NOT NULL UNIQUE,
                    socket_path TEXT NOT NULL UNIQUE,
                    boot_id TEXT NOT NULL,
                    source_id TEXT NOT NULL UNIQUE,
                    source_epoch TEXT NOT NULL,
                    launch_marker TEXT NOT NULL UNIQUE,
                    tmux_json TEXT,
                    shell_json TEXT,
                    stopped INTEGER NOT NULL DEFAULT 0 CHECK(stopped IN (0,1)),
                    UNIQUE(installation_id, agent_id)
                );
                CREATE TABLE IF NOT EXISTS runner_shell_control (
                    terminal_id TEXT PRIMARY KEY REFERENCES runner_shell_terminals(terminal_id),
                    mode TEXT NOT NULL CHECK(mode IN ('unclaimed','human')),
                    epoch INTEGER NOT NULL CHECK(epoch >= 0),
                    viewer_id TEXT,
                    last_sequence INTEGER NOT NULL CHECK(last_sequence >= 0),
                    CHECK((mode='human' AND viewer_id IS NOT NULL)
                        OR (mode='unclaimed' AND viewer_id IS NULL))
                );
                COMMIT;
            """)
        except BaseException:
            db.rollback()
            raise


def terminal(database, terminal_id):
    with connect(database) as db:
        row = db.execute("SELECT * FROM runner_shell_terminals WHERE terminal_id=?",
                         (str(terminal_id),)).fetchone()
    if row is None:
        raise RunnerError("not_found", "recorded terminal does not exist")
    return dict(row)


def control(database, terminal_id):
    with connect(database) as db:
        row = db.execute("SELECT mode,epoch,viewer_id,last_sequence FROM runner_shell_control "
                         "WHERE terminal_id=?", (str(terminal_id),)).fetchone()
    if row is None:
        raise RunnerError("not_found", "recorded terminal does not exist")
    return ShellControl(**dict(row))


def save_binding(database, terminal_id, tmux, shell):
    with connect(database) as db, db:
        # Caller holds the per-terminal lock. A recorded fingerprint never changes.
        db.execute("UPDATE runner_shell_terminals SET tmux_json=COALESCE(tmux_json,?), "
                   "shell_json=COALESCE(shell_json,?) WHERE terminal_id=?",
                   (tmux.model_dump_json(), shell.model_dump_json() if shell else None,
                    str(terminal_id)))


def mark_stopped(database, terminal_id):
    with connect(database) as db, db:
        db.execute("UPDATE runner_shell_terminals SET stopped=1 WHERE terminal_id=?",
                   (str(terminal_id),))


@contextmanager
def path_lock(path):
    """Never unlink: serialize claims even from different configured databases."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise RunnerError("storage_blocked", "shell path lock is not owned")
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RunnerError("unavailable", "shell path lock is busy") from None
                time.sleep(0.01)
        yield
    finally:
        os.close(fd)
