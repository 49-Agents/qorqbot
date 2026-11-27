"""The state directory of one managed agent host, and the process and socket facts every side of it needs.

Layout, ``<runtime>/managed-agents/<agent_id>/`` (0700). the host owns the files below (``options.json`` belongs to ``options.py``)::

    host.lock     held with flock for the host's whole life (a second copy exits 75); probed with a shared lock
    host.json     the host's public record (see HOST_FIELDS), rewritten on every state change
    token         the control token (0600), written by the host from launch.json
    control.sock  the control socket (0600); a short path elsewhere when this one would be too long, recorded in host.json
    events.jsonl  the bounded event log, one JSON object per line with a monotonic ``seq``
    sent.jsonl    the message uuid ledger: every accepted uuid and every fenced one
    host.log      the host's own diagnostics (never a token, an env value or message text)
    launch.json   transient (0600): written by the launcher, read and unlinked by the host at once

This module imports nothing from the rest of the application except the system layer, so the host, the client and the
launcher can all use it, and it can run in the detached host process.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

from qorqut_system import channel, locks, places, private_files, processes

SCHEMA_VERSION = 1
PROTOCOL = 1

EXIT_STOPPED, EXIT_FAILED, EXIT_BUSY, EXIT_CONFIG = 0, 1, 75, 78

HOST_MODULE = "qorqut.managed_runtime.host"
DEFAULT_DRIVER = "qorqut.managed_runtime.claude_driver:ClaudeSession"

LOCK_FILE, HOST_FILE, TOKEN_FILE = "host.lock", "host.json", "token"
SOCKET_FILE, EVENTS_FILE, SENT_FILE, LOG_FILE, LAUNCH_FILE = "control.sock", "events.jsonl", "sent.jsonl", "host.log", "launch.json"

STATES = ("starting", "ready", "closing", "closed", "failed")
LIVE_STATES = ("starting", "ready", "closing")
HOST_FIELDS = ("schema_version", "agent_id", "instance", "host_pid", "host_identity", "protocol", "state", "started_at",
               "session_id", "transcript_path", "claude_pid", "claude_identity", "last_activity_at", "socket_path",
               "socket_fallback", "exit", "client")

SOCKET_PATH_LIMIT = 104     # macOS; Linux allows 108
HOST_RECORD_LIMIT = 64 * 1024
TOKEN_LIMIT = 256
LAUNCH_LIMIT = 4 * 1024 * 1024
ANCESTOR_HOPS = 24


def state_root(paths) -> Path:
    return Path(paths.runtime) / "managed-agents"


def agent_state(paths, agent_id) -> Path:
    return state_root(paths) / str(agent_id)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# -- private files ---------------------------------------------------------------------------------------------------------

def private_directory(path) -> Path:
    """Create (if needed) and check a directory that only this user may use. A symlink in its place is refused."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("state path is a symlink")
    private_files.make_folder_path(path, mode=0o700, parents=True)
    info = private_files.stat_folder(path)
    if not stat.S_ISDIR(info.st_mode) or not private_files.is_mine(info) or not private_files.is_private(info):
        raise ValueError("state directory is not private to this user")
    return path


def write_private(path, data: bytes) -> None:
    """Atomic: a temporary file created 0600 with O_EXCL, fsynced, then renamed over ``path``."""
    path = Path(path)
    private_files.replace_entry(path.parent, path.name, data, prefix=f".{path.name}.{os.getpid()}.",
                                suffix=".tmp", token_bytes=4, chmod=False)


def write_json_private(path, value) -> None:
    write_private(path, json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def read_private(path, limit: int) -> bytes:
    """The bytes of a regular file that this user owns and nobody else can read. ``OSError`` or ``ValueError`` otherwise."""
    path = Path(path)
    descriptor = private_files.open_read(path.parent, path.name)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not private_files.is_mine(info) or not private_files.is_private(info):
            raise ValueError("not a private file")
        if info.st_size > limit:
            raise ValueError("file too large")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(map(len, chunks)) > limit:
                raise ValueError("file too large")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def read_json(path, limit: int = HOST_RECORD_LIMIT):
    """The JSON object in a private file, or ``None`` for anything else (missing, foreign, large, malformed)."""
    try:
        value = json.loads(read_private(path, limit))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def read_host(state) -> dict | None:
    record = read_json(Path(state) / HOST_FILE)
    if record is None or record.get("schema_version") != SCHEMA_VERSION or record.get("state") not in STATES:
        return None
    return record


def read_token(state) -> str | None:
    try:
        value = read_private(Path(state) / TOKEN_FILE, TOKEN_LIMIT).decode("ascii").strip()
    except (OSError, ValueError):
        return None
    return value or None


def read_events_file(state, since: int = 0, limit: int = 1000) -> list:
    """Events with ``seq > since`` from ``events.jsonl``, for when the host is gone (its last error, why it exited)."""
    try:
        text = read_private(Path(state) / EVENTS_FILE, 64 * 1024 * 1024).decode("utf-8", "replace")
    except (OSError, ValueError):
        return []
    found = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue          # a torn last line
        if isinstance(event, dict) and isinstance(event.get("seq"), int) and event["seq"] > since:
            found.append(event)
            if len(found) >= limit:
                break
    return found


# -- the lock ----------------------------------------------------------------------------------------------------------------

class _OpenedLockFile:
    """A lock file the caller opened itself, handed to the layer's lock operations, which take an open file."""

    def __init__(self, descriptor):
        self._descriptor = descriptor

    def fileno(self):
        return self._descriptor


def lock_held(path) -> bool:
    """Someone holds the host lock. A missing file means nobody does; this never creates it."""
    try:
        descriptor = locks.open_descriptor(Path(path).parent, Path(path).name)
    except FileNotFoundError:
        return False
    except OSError:
        return True            # cannot tell: treat as held, never as free
    try:
        locks.acquire(_OpenedLockFile(descriptor), exclusive=False, wait=False)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(descriptor)


def acquire_lock(descriptor, attempts: int = 20, delay: float = 0.05) -> bool:
    """Take the exclusive host lock on an open descriptor, retrying for about ``attempts * delay`` seconds: a probe
    (``lock_held``, which the service runs at 20 Hz while it waits for a host) holds a shared lock for microseconds and must not
    make a starting host think another host is running. ``False`` when someone really holds it."""
    stream = _OpenedLockFile(descriptor)
    for attempt in range(max(1, attempts)):
        try:
            locks.acquire(stream, wait=False)
            return True
        except BlockingIOError:
            if attempt + 1 < attempts:
                time.sleep(delay)
    return False


# -- processes ---------------------------------------------------------------------------------------------------------------

def process_identity(pid):
    return processes.identity(pid)


def process_alive(pid, identity) -> bool:
    """The exact process (boot identity and start time) still exists and is not a zombie."""
    try:
        if process_identity(pid) != identity:
            return False
        return not processes.is_zombie(pid)
    except (OSError, ValueError, IndexError, TypeError):
        return False


def terminate_process(pid, identity, term_wait: float = 3.0, kill_wait: float = 2.0) -> bool:
    """Stop exactly the process ``pid`` whose boot identity and start time are ``identity``: SIGTERM, then SIGKILL, to its process
    group when it leads one other than ours, otherwise to the process. The identity is checked again before every signal, so a
    reused pid is never touched. Returns True when it was running and is gone, False when it was not (or not that process)."""
    return processes.stop_identified_process(pid, identity, term_wait=term_wait, kill_wait=kill_wait)


def process_ticks(identity) -> str:
    return str(identity).rsplit(":", 1)[-1]


def host_owner(pid, identity) -> str:
    """The ``native_sessions.owner`` of a managed session: ``host:<host pid>:<host start ticks>``."""
    return "host:%d:%s" % (int(pid), process_ticks(identity))


def parse_host_owner(owner):
    """``(pid, ticks)`` from a ``host:<pid>:<ticks>`` owner string, or ``None``."""
    parts = str(owner or "").split(":")
    if len(parts) == 3 and parts[0] == "host" and parts[1].isdecimal() and parts[2].isdecimal():
        return int(parts[1]), parts[2]
    return None


def parent_of(pid):
    return processes.parent_pid(pid)


def descends_from(pid, ancestor, hops: int = ANCESTOR_HOPS) -> bool:
    """``ancestor`` is ``pid`` itself or one of its parents within ``hops`` steps."""
    try:
        current = int(pid)
        ancestor = int(ancestor)
        for _ in range(hops):
            if current == ancestor:
                return True
            if current <= 1:
                return False
            current = parent_of(current)
        return current == ancestor
    except (OSError, ValueError, TypeError, IndexError):
        return False


def host_command(pid):
    """The argv of ``pid``, or ``None`` when it cannot be read."""
    try:
        argv = processes.cmdline(pid)
    except (OSError, ValueError, TypeError, ImportError):
        return None
    return argv


def host_command_matches(pid, state) -> bool:
    """The process runs this module for exactly this state directory (the directory itself or the same one by another path)."""
    argv = host_command(pid)
    if not argv or HOST_MODULE not in argv or "--state" not in argv:
        return False
    index = argv.index("--state")
    if index + 1 >= len(argv):
        return False
    given = argv[index + 1]
    if given == str(state):
        return True
    try:
        return os.path.samefile(given, state)
    except OSError:
        return False


# -- the socket --------------------------------------------------------------------------------------------------------------

def socket_fits(path) -> bool:
    return len(os.fsencode(str(path))) < SOCKET_PATH_LIMIT


def short_socket_directory(state) -> Path:
    """A private directory with a short path for the control socket of an agent whose state directory is too deep for one.

    The user's runtime directory is private by construction; the shared temporary directory is the fallback, and there the
    directory must be ours alone. Anything else is refused, never adopted."""
    state = Path(state)
    runtime = places.run_folder()
    digest = hashlib.sha256(os.fsencode(str(state))).hexdigest()[:12]
    for base in ([runtime] if runtime else []) + [places.temporary_folder(), Path("/tmp")]:
        directory = base / ("qorqut-managed-" + digest)
        if not socket_fits(directory / SOCKET_FILE):
            continue
        try:
            private_files.make_folder_path(directory, mode=0o700)
        except FileExistsError:
            pass
        except OSError:
            continue
        info = private_files.stat_folder(directory)
        if stat.S_ISDIR(info.st_mode) and private_files.is_mine(info) and private_files.is_private(info):
            return directory
    raise RuntimeError("socket_directory_unsafe")


def socket_path_for(state):
    """``(path, fallback)``: the control socket inside the state directory, or a short private path when that is too long."""
    preferred = Path(state) / SOCKET_FILE
    if socket_fits(preferred):
        return preferred, False
    return short_socket_directory(state) / SOCKET_FILE, True


def peer_credentials(connection):
    """``(pid, uid)`` of the other end of a connected Unix socket; either is ``None`` when the platform cannot say."""
    return channel.peer(connection)


def clean_stale(state) -> bool:
    """Remove what a dead host left that must not be trusted again. The caller has seen the lock free.

    A host that was killed outright (SIGKILL, the OOM killer) cannot stop its Claude process; on Linux the CLI is started to die
    with its parent, but that does not cover every way out (and macOS has no such facility), so the Claude process recorded in
    ``host.json`` is terminated here when that exact process (pid and start time) is still alive. Without that a new host would
    start a second Claude in the same workspace. Returns True when such a process was stopped.

    Keeps ``options.json``, ``host.log``, ``events.jsonl`` and the final ``host.json`` (rewritten as failed when it still claims a
    live state), so the reason an agent's session ended stays readable."""
    state = Path(state)
    raw = read_json(state / HOST_FILE) or {}
    stopped = False
    if not lock_held(state / LOCK_FILE):          # nobody took over in the meantime
        stopped = terminate_process(raw.get("claude_pid"), raw.get("claude_identity"))
    record = read_host(state)
    for name in (TOKEN_FILE, LAUNCH_FILE):
        try:
            (state / name).unlink()
        except FileNotFoundError:
            pass
    socket_files = {state / SOCKET_FILE}
    if record and isinstance(record.get("socket_path"), str) and record.get("socket_fallback"):
        socket_files.add(Path(record["socket_path"]))
    for path in socket_files:
        try:
            if stat.S_ISSOCK(path.lstat().st_mode):
                path.unlink()
        except OSError:
            pass
    if record and record["state"] in LIVE_STATES:
        record.update(state="failed", exit={"code": None, "reason": "host_gone", **({"claude_terminated": True} if stopped else {})})
        try:
            write_json_private(state / HOST_FILE, record)
        except OSError:
            pass
    return stopped
