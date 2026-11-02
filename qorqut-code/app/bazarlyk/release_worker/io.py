"""Small, dependency-free durability and redaction primitives for releases."""

from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import threading
import time
from typing import Any, Iterator
import uuid

from .errors import ReleaseError, UncertainRemoteEffect

_SECRET_KEY = re.compile(
    r"(?:token|secret|password|passwd|credential|private[_-]?key|api[_-]?key|bearer|cookie)",
    re.I,
)
_SECRET_VALUE = re.compile(
    r"(?i)(\b(?:bearer|token|password|passwd|secret|api[_-]?key)\s*[=:]\s*)([^\s,;]+)"
)
_BEARER_VALUE = re.compile(r"(?i)(\b(?:bearer|basic)\s+)([^\s,;]+)")
_COOKIE_VALUE = re.compile(r"(?i)(\b(?:cookie):\s*)([^\r\n]+)")
_AUTH_VALUE = re.compile(r"(?i)(\b(?:authorization|proxy-authorization):)([^\r\n]*)")
_SECRET_FLAG = re.compile(
    r"(?i)^(?:--?|/)(?:token|secret|password|passwd|api[_-]?key|authorization|cookie|credential|private[_-]?key)$"
)
_HEADER_NAME = re.compile(r"(?i)^(?:authorization|proxy-authorization|cookie):?$")
_REDACTED = "<redacted>"


def _redact_text(value: str) -> str:
    value = _SECRET_VALUE.sub(lambda match: match.group(1) + _REDACTED, value)
    value = _BEARER_VALUE.sub(lambda match: match.group(1) + _REDACTED, value)
    value = _COOKIE_VALUE.sub(lambda match: match.group(1) + _REDACTED, value)

    def redact_auth(match: re.Match[str]) -> str:
        if re.match(r"\s*(?:bearer|basic)\s+", match.group(2), re.I):
            return match.group(0)
        return match.group(1) + " " + _REDACTED

    return _AUTH_VALUE.sub(redact_auth, value)


def redact(value: Any, *, key: str | None = None) -> Any:
    """Return a JSON-safe copy with credential-shaped values removed."""

    if key and _SECRET_KEY.search(key):
        return _REDACTED
    if isinstance(value, dict):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        result: list[Any] = []
        secret_values = 0
        header_pending: str | None = None
        for item in value:
            if secret_values:
                result.append(_REDACTED)
                secret_values -= 1
                continue
            if header_pending is not None:
                if isinstance(item, str) and item.strip().lower() in {"bearer", "basic"}:
                    result.append(item)
                    secret_values = 1
                else:
                    result.append(_REDACTED)
                header_pending = None
                continue
            if isinstance(item, str) and _SECRET_FLAG.fullmatch(item.strip()):
                result.append(item)
                secret_values = 1
            elif isinstance(item, str) and _HEADER_NAME.fullmatch(item.strip()):
                result.append(item)
                header_pending = item.strip().rstrip(":").lower()
            else:
                result.append(redact(item))
        return result
    if isinstance(value, tuple):
        return redact(list(value))
    if isinstance(value, str):
        return _redact_text(value)
    return value


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return "sha256:" + hasher.hexdigest()


def _plain_path(value: Any) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise ReleaseError("Release paths must be absolute and normalized")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ReleaseError("Release paths must be absolute and normalized")
    if path.parent == path:
        raise ReleaseError("Release paths may not be the filesystem root")
    # Do not resolve/follow a symlink: reject it before anything is read through it.
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ReleaseError("Release paths may not cross symbolic links")
    return path


def checked_path(value: Any, root: Path | None = None) -> Path:
    """Return an absolute, normalized, symlink-free path, inside ``root`` when one is selected.

    The explicit workspace ``root`` is held to the same rules as the path itself;
    there is no discovery of a root and no environment fallback.
    """

    path = _plain_path(value)
    if root is not None:
        root = _plain_path(root)
        if path != root and root not in path.parents:
            raise ReleaseError("Every release input/output must be inside the selected independent workspace")
    return path


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_makedirs(path: Path, *, anchor: Path | None = None, mode: int = 0o700) -> None:
    """Flush each new entry's parent; partial creation is retained and retried.

    This visits only the selected path's ancestors. An explicit ownership anchor
    must already exist, so a missing mountpoint is never provisioned as fallback.
    """
    path = Path(os.path.abspath(path))
    pending = []
    current = path
    if anchor is not None:
        anchor = Path(os.path.abspath(anchor))
        if not path.is_relative_to(anchor) or not anchor.is_dir():
            raise ReleaseError('directory must belong to an existing owned anchor')
        while current != anchor:
            pending.append(current)
            current = current.parent
    else:
        while not current.exists():
            pending.append(current)
            current = current.parent
        if current.is_symlink() or not current.is_dir():
            raise ReleaseError('directory creation has a nonregular ancestor')
        fsync_directory(current.parent)
    for directory in reversed(pending):
        try:
            directory.mkdir(mode=mode)
        except FileExistsError:
            if directory.is_symlink() or not directory.is_dir():
                raise ReleaseError('owned directory path is nonregular')
        fsync_directory(directory.parent)
    fsync_directory(path)


def atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write and fsync a file, then atomically replace it and its directory."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_json(path: Path, value: Any, *, mode: int = 0o600, redact_values: bool = True) -> None:
    serialized = redact(value) if redact_values else value
    atomic_write(path, canonical(serialized), mode=mode)


_LOCK_LEASE_SECONDS = 120.0
_LOCK_HEARTBEAT_SECONDS = 20.0
_LOCK_ACTIVE_GRACE_SECONDS = 600.0


def _process_start(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19]
    except (OSError, IndexError):
        return None


def _owner_process_live(owner: dict[str, Any], host: str) -> bool | None:
    if owner.get("host") != host:
        return None
    try:
        pid = int(owner["pid"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OverflowError:
        return None
    except PermissionError:
        return True
    recorded_start = owner.get("process_start")
    current_start = _process_start(pid)
    return not recorded_start or not current_start or recorded_start == current_start


_REMOTE_LOCK_ACQUIRE = r'''
import fcntl, json, os, shutil, signal, socket, sys, time, uuid

directory, token, lease = sys.argv[1], sys.argv[2], float(sys.argv[3])
owner_path = os.path.join(directory, "owner.json")
owner_lock_path = os.path.join(directory, "owner.lock")
parent = os.path.dirname(directory)
os.makedirs(parent, mode=0o700, exist_ok=True)

def process_start(pid):
    try:
        return open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None

def process_live(owner):
    if owner.get("host") != socket.gethostname():
        return True
    try:
        pid = int(owner["pid"])
    except (KeyError, TypeError, ValueError):
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OverflowError:
        return True
    except PermissionError:
        return True
    recorded = owner.get("process_start")
    current = process_start(pid)
    return not recorded or not current or recorded == current

def read_owner():
    with open(owner_lock_path, "a+") as owner_lock:
        fcntl.flock(owner_lock.fileno(), fcntl.LOCK_SH)
        with open(owner_path, encoding="utf-8") as stream:
            value = json.load(stream)
    if not isinstance(value, dict) or not value.get("owner"):
        raise ValueError("invalid owner record")
    return value

def write_owner(value):
    temporary = owner_path + ".tmp-" + uuid.uuid4().hex
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, owner_path)

def cancel_pending(expected_owner=None):
    try:
        with open(owner_lock_path, "a+") as owner_lock:
            fcntl.flock(owner_lock.fileno(), fcntl.LOCK_EX)
            with open(owner_path, encoding="utf-8") as stream:
                owner = json.load(stream)
            if expected_owner is not None and owner.get("owner") != expected_owner:
                return "owner-mismatch"
            now = time.time()
            if float(owner.get("heartbeat_at", 0)) + float(owner.get("lease_seconds", 0)) > now:
                return "lease-active"
            active = owner.get("active_command")
            if not isinstance(active, dict) or active.get("state") != "pending_launch":
                return "already-launched"
            if active.get("pid"):
                return "already-launched"
            active["state"] = "cancelled"
            active["cancelled_at"] = now
            owner["heartbeat_at"] = now
            write_owner(owner)
        return "cancelled"
    except (OSError, ValueError, TypeError):
        return "invalid"

def active_process_live(active):
    if not isinstance(active, dict):
        return False
    return process_live(active)

def remove_stale():
    stale = directory + ".stale-" + uuid.uuid4().hex
    try:
        os.rename(directory, stale)
    except FileNotFoundError:
        return
    shutil.rmtree(stale, ignore_errors=True)

for attempt in range(20):
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        try:
            owner = read_owner()
        except (OSError, ValueError):
            print("busy-corrupt")
            raise SystemExit(3)
        active = owner.get("active_command")
        if isinstance(active, dict) and active.get("state") == "pending_launch":
            outcome = cancel_pending()
            if outcome == "cancelled":
                remove_stale()
                continue
            if outcome == "lease-active":
                print("busy")
                raise SystemExit(2)
            try:
                owner = read_owner()
            except (OSError, ValueError):
                print("busy-corrupt")
                raise SystemExit(3)
            active = owner.get("active_command")
        active_dead = False
        if isinstance(active, dict):
            if active_process_live(active):
                print("busy-active")
                raise SystemExit(2)
            # The command identity was checked against the destination
            # process table above.  Once that process is confirmed dead, the
            # active marker is safe to reconcile even if the guardian that
            # originally owned the lease is still around.
            active_dead = True
        if float(owner.get("heartbeat_at", 0)) + float(owner.get("lease_seconds", 0)) > time.time():
            print("busy")
            raise SystemExit(2)
        if active_dead:
            remove_stale()
            continue
        if process_live(owner):
            # A live holder gets time to observe its expired lease and exit;
            # takeover is never forced while its process is still present.
            time.sleep(1)
            continue
        remove_stale()
        continue

    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child:
        os.close(write_fd)
        message = os.read(read_fd, 256).decode()
        os.close(read_fd)
        sys.stdout.write(message)
        sys.stdout.flush()
        os._exit(0 if message.startswith("acquired:") else 1)

    os.close(read_fd)
    try:
        os.setsid()
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        owner = {
            "protocol": 1,
            "owner": token,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "process_start": process_start(os.getpid()),
            "started_at": time.time(),
            "heartbeat_at": time.time(),
            "lease_seconds": lease,
        }
        temporary = owner_path + ".tmp-" + uuid.uuid4().hex
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(owner, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, owner_path)
        os.write(write_fd, f"acquired:{os.getpid()}".encode())
        os.close(write_fd)
        for fd in (0, 1, 2):
            try:
                os.close(fd)
            except OSError:
                pass
        while True:
            time.sleep(2)
            try:
                current = read_owner()
            except (OSError, ValueError):
                break
            if current.get("owner") != token:
                break
            if float(current.get("heartbeat_at", 0)) + float(current.get("lease_seconds", 0)) <= time.time():
                active = current.get("active_command")
                if isinstance(active, dict) and active.get("state") == "pending_launch":
                    if cancel_pending(token) == "cancelled":
                        remove_stale()
                        break
                    try:
                        current = read_owner()
                    except (OSError, ValueError):
                        break
                    active = current.get("active_command")
                if isinstance(active, dict) and active_process_live(active):
                    continue
                remove_stale()
                break
    except Exception:
        try:
            os.write(write_fd, b"failed")
        except OSError:
            pass
        os._exit(1)
    os._exit(0)

print("acquire-failed")
raise SystemExit(3)
'''

_REMOTE_LOCK_REFRESH = r'''
import fcntl, json, os, sys, time, uuid
directory, token, lease = sys.argv[1], sys.argv[2], float(sys.argv[3])
owner_path = os.path.join(directory, "owner.json")
owner_lock_path = os.path.join(directory, "owner.lock")
try:
    with open(owner_lock_path, "a+") as owner_lock:
        fcntl.flock(owner_lock.fileno(), fcntl.LOCK_EX)
        with open(owner_path, encoding="utf-8") as stream:
            owner = json.load(stream)
        if owner.get("owner") != token:
            print("owner-mismatch")
            raise SystemExit(2)
        now = time.time()
        owner["heartbeat_at"] = now
        active = owner.get("active_command")
        if isinstance(active, dict):
            active["expires_at"] = now + 600.0
        temporary = owner_path + ".tmp-" + uuid.uuid4().hex
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(owner, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, owner_path)
    print("refreshed")
except (OSError, ValueError):
    print("refresh-failed")
    raise SystemExit(1)
'''

_REMOTE_LOCK_ASSERT = r'''
import json, os, sys, time
directory, token = sys.argv[1], sys.argv[2]
try:
    with open(os.path.join(directory, "owner.json"), encoding="utf-8") as stream:
        owner = json.load(stream)
except (OSError, ValueError):
    print("lost")
    raise SystemExit(2)
if owner.get("owner") != token:
    print("lost")
    raise SystemExit(2)
if float(owner.get("heartbeat_at", 0)) + float(owner.get("lease_seconds", 0)) <= time.time():
    print("expired")
    raise SystemExit(3)
print("owned")
'''

_REMOTE_LOCK_MARK = r'''
import fcntl, json, os, sys, time, uuid
directory, token, command = sys.argv[1], sys.argv[2], sys.argv[3]
owner_path = os.path.join(directory, "owner.json")
owner_lock_path = os.path.join(directory, "owner.lock")
try:
    with open(owner_lock_path, "a+") as owner_lock:
        fcntl.flock(owner_lock.fileno(), fcntl.LOCK_EX)
        with open(owner_path, encoding="utf-8") as stream:
            owner = json.load(stream)
        now = time.time()
        if owner.get("owner") != token or float(owner.get("heartbeat_at", 0)) + float(owner.get("lease_seconds", 0)) <= now:
            print("lost")
            raise SystemExit(2)
        owner["heartbeat_at"] = now
        owner["active_command"] = {
            "id": command,
            "state": "pending_launch",
            "started_at": now,
            "expires_at": now + 600.0,
        }
        temporary = owner_path + ".tmp-" + uuid.uuid4().hex
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(owner, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, owner_path)
    print("marked")
except (OSError, ValueError):
    print("lost")
    raise SystemExit(2)
'''

_REMOTE_LOCK_CANCEL = r'''
import fcntl, json, os, sys, time, uuid
directory, token, command = sys.argv[1], sys.argv[2], sys.argv[3]
owner_path = os.path.join(directory, "owner.json")
owner_lock_path = os.path.join(directory, "owner.lock")
try:
    with open(owner_lock_path, "a+") as owner_lock:
        fcntl.flock(owner_lock.fileno(), fcntl.LOCK_EX)
        with open(owner_path, encoding="utf-8") as stream:
            owner = json.load(stream)
        if owner.get("owner") != token:
            print("owner-mismatch")
            raise SystemExit(2)
        active = owner.get("active_command")
        if not isinstance(active, dict) or active.get("id") != command:
            print("missing")
            raise SystemExit(0)
        if active.get("state") != "pending_launch":
            print("already-launched")
            raise SystemExit(0)
        now = time.time()
        active["state"] = "cancelled"
        active["cancelled_at"] = now
        owner["heartbeat_at"] = now
        temporary = owner_path + ".tmp-" + uuid.uuid4().hex
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(owner, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, owner_path)
    print("cancelled")
except (OSError, ValueError):
    print("cancel-failed")
    raise SystemExit(1)
'''

_REMOTE_LOCK_CLEAR = r'''
import fcntl, json, os, sys, time, uuid
directory, token, command = sys.argv[1], sys.argv[2], sys.argv[3]
owner_path = os.path.join(directory, "owner.json")
owner_lock_path = os.path.join(directory, "owner.lock")
try:
    with open(owner_lock_path, "a+") as owner_lock:
        fcntl.flock(owner_lock.fileno(), fcntl.LOCK_EX)
        with open(owner_path, encoding="utf-8") as stream:
            owner = json.load(stream)
        if owner.get("owner") != token:
            print("lost")
            raise SystemExit(2)
        active = owner.get("active_command")
        if isinstance(active, dict) and active.get("id") == command:
            owner.pop("active_command", None)
            owner["heartbeat_at"] = time.time()
            temporary = owner_path + ".tmp-" + uuid.uuid4().hex
            with open(temporary, "w", encoding="utf-8") as stream:
                json.dump(owner, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, owner_path)
    print("cleared")
except (OSError, ValueError):
    print("lost")
    raise SystemExit(2)
'''

_REMOTE_LOCK_RELEASE = r'''
import fcntl, json, os, shutil, signal, sys, uuid
directory, token = sys.argv[1], sys.argv[2]
owner_path = os.path.join(directory, "owner.json")
owner_lock_path = os.path.join(directory, "owner.lock")
if not os.path.isdir(directory):
    print("missing")
    raise SystemExit(0)
try:
    with open(owner_lock_path, "a+") as owner_lock:
        fcntl.flock(owner_lock.fileno(), fcntl.LOCK_EX)
        with open(owner_path, encoding="utf-8") as stream:
            owner = json.load(stream)
        if owner.get("owner") != token:
            print("owner-mismatch")
            raise SystemExit(2)
        try:
            os.kill(int(owner.get("pid", 0)), signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass
        releasing = directory + ".releasing-" + uuid.uuid4().hex
        try:
            os.rename(directory, releasing)
        except FileNotFoundError:
            print("missing")
            raise SystemExit(0)
        shutil.rmtree(releasing, ignore_errors=True)
except (OSError, ValueError):
    print("owner-invalid")
    raise SystemExit(2)
print("released")
'''


class InstallationLock:
    """An advisory per-installation lock.

    Read-only inspection and status never acquire this lock.  Mutating
    lifecycle actions do. ReleaseWorker derives the lock from the frozen
    destination configuration rather than the operator's records directory.
    Local and SSH callers use the same destination-side owner-recorded lock
    directory, with a renewable lease so interrupted remote holders can be
    reclaimed without taking over a live release.
    """

    def __init__(self, root: Path | dict[str, Any], installation: str, *, executor: Any = None):
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", installation)
        self.installation = installation
        self.executor = executor
        self.remote_path: str | None = None
        if isinstance(root, dict):
            configured = root.get("lock_path")
            release_root = root.get("release_root")
            if configured:
                self.remote_path = str(configured)
            elif release_root:
                self.remote_path = str(Path(str(release_root)) / ".release-worker" / f"{safe}.lock")
            else:
                self.remote_path = str(Path("/tmp/qorqut-release-locks") / f"{safe}.lock")
            self.path = Path(self.remote_path)
        else:
            # Compatibility for callers that do not have an installation
            # configuration yet. ReleaseWorker passes the canonical
            # destination configuration below.
            self.path = root / ".locks" / f"{safe}.lock"
        self.lock_directory = Path(str(self.path) + ".d")
        self.owner_path = self.lock_directory / "owner.json"
        self.owner_token = uuid.uuid4().hex
        self.host = socket.gethostname()
        self.lease_seconds = _LOCK_LEASE_SECONDS
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._owner_mutex = threading.Lock()
        self._remote_acquired = False
        self._local_acquired = False
        self._lease_lost = False
        self._heartbeat_failed = False
        self._command_depth = 0
        self._command_id: str | None = None
        self._uncertain_command = False
        self._retain_on_exit = False

    def _owner(self, pid: int) -> dict[str, Any]:
        now = time.time()
        return {
            "protocol": 1,
            "owner": self.owner_token,
            "pid": pid,
            "host": self.host,
            "process_start": _process_start(pid),
            "started_at": now,
            "heartbeat_at": now,
            "lease_seconds": self.lease_seconds,
        }

    def _read_owner(self) -> dict[str, Any]:
        try:
            value = json.loads(self.owner_path.read_text())
        except (OSError, ValueError) as exc:
            raise ReleaseError(f"lock owner record is missing or invalid for {self.installation}") from exc
        if not isinstance(value, dict) or not value.get("owner"):
            raise ReleaseError(f"lock owner record is invalid for {self.installation}")
        return value

    def _reclaim_local_stale(self) -> None:
        owner = self._read_owner()
        active = owner.get("active_command")
        if isinstance(active, dict) and float(active.get("expires_at", 0)) > time.time():
            raise ReleaseError(f"active destination command requires reconciliation for {self.installation}")
        try:
            expired = float(owner.get("heartbeat_at", 0)) + float(owner.get("lease_seconds", 0)) <= time.time()
        except (TypeError, ValueError) as exc:
            raise ReleaseError(f"lock owner lease is invalid for {self.installation}") from exc
        if not expired:
            raise ReleaseError(f"release already running for installation {self.installation}")
        live = _owner_process_live(owner, self.host)
        if live is not False:
            raise ReleaseError(f"stale lock owner is still live or cannot be verified for {self.installation}")
        stale = self.lock_directory.with_name(self.lock_directory.name + ".stale-" + uuid.uuid4().hex)
        try:
            os.rename(self.lock_directory, stale)
        except FileNotFoundError:
            return
        shutil.rmtree(stale, ignore_errors=True)

    def _write_local_owner(self) -> None:
        atomic_json(self.owner_path, self._owner(os.getpid()), redact_values=False)

    def _refresh_remote(self) -> bool:
        if self.remote_path is None:
            return False
        observed = self.executor.run(
            [
                "python3",
                "-I",
                "-c",
                _REMOTE_LOCK_REFRESH,
                self.remote_path + ".d",
                self.owner_token,
                str(self.lease_seconds),
            ],
            check=False,
        )
        return observed.strip() == "refreshed"

    def _set_lease_lost(self) -> None:
        self._lease_lost = True

    def assert_owned(self) -> None:
        if self._lease_lost:
            raise ReleaseError(f"release lock ownership was lost for installation {self.installation}")
        if self._remote_acquired and self.remote_path is not None:
            observed = self.executor.run(
                [
                    "python3",
                    "-I",
                    "-c",
                    _REMOTE_LOCK_ASSERT,
                    self.remote_path + ".d",
                    self.owner_token,
                ],
                check=False,
            ).strip()
            if observed != "owned":
                self._set_lease_lost()
                raise ReleaseError(f"release lock ownership was lost for installation {self.installation}")
            return
        if not self._local_acquired:
            raise ReleaseError(f"release lock is not held for installation {self.installation}")
        try:
            owner = self._read_owner()
            valid = owner.get("owner") == self.owner_token and (
                float(owner.get("heartbeat_at", 0)) + float(owner.get("lease_seconds", 0)) > time.time()
            )
        except (ReleaseError, TypeError, ValueError):
            valid = False
        if not valid:
            self._set_lease_lost()
            raise ReleaseError(f"release lock ownership was lost for installation {self.installation}")

    def _mark_command(self, command: str) -> None:
        if self._uncertain_command:
            raise ReleaseError(
                f"uncertain remote command requires reconciliation for installation {self.installation}"
            )
        self.assert_owned()
        if self._command_depth:
            self._command_depth += 1
            return
        command_id = f"{self.owner_token}:{uuid.uuid4().hex}:{command}"
        if self._remote_acquired and self.remote_path is not None:
            observed = self.executor.run(
                [
                    "python3",
                    "-I",
                    "-c",
                    _REMOTE_LOCK_MARK,
                    self.remote_path + ".d",
                    self.owner_token,
                    command_id,
                ],
                check=False,
            ).strip()
            if observed != "marked":
                self._set_lease_lost()
                raise ReleaseError(f"release lock ownership was lost for installation {self.installation}")
        else:
            with self._owner_mutex:
                owner = self._read_owner()
                if owner.get("owner") != self.owner_token:
                    self._set_lease_lost()
                    raise ReleaseError(f"release lock ownership was lost for installation {self.installation}")
                now = time.time()
                if float(owner.get("heartbeat_at", 0)) + float(owner.get("lease_seconds", 0)) <= now:
                    self._set_lease_lost()
                    raise ReleaseError(f"release lock ownership was lost for installation {self.installation}")
                owner["heartbeat_at"] = now
                owner["active_command"] = {
                    "id": command_id,
                    "started_at": now,
                    "expires_at": now + _LOCK_ACTIVE_GRACE_SECONDS,
                }
                atomic_json(self.owner_path, owner, redact_values=False)
        self._command_id = command_id
        self._command_depth = 1

    def _retain_command(self) -> None:
        self._uncertain_command = True
        self._retain_on_exit = True

    def _cancel_pending_command(self) -> bool:
        if not self._remote_acquired or self.remote_path is None or self._command_id is None:
            return False
        try:
            observed = self.executor.run(
                [
                    "python3",
                    "-I",
                    "-c",
                    _REMOTE_LOCK_CANCEL,
                    self.remote_path + ".d",
                    self.owner_token,
                    self._command_id,
                ],
                check=False,
            ).strip()
        except Exception:
            return False
        return observed == "cancelled"

    def _clear_command(self) -> None:
        if not self._command_depth:
            return
        self._command_depth -= 1
        if self._command_depth or self._command_id is None or self._lease_lost:
            return
        command_id = self._command_id
        self._command_id = None
        try:
            self.assert_owned()
            if self._remote_acquired and self.remote_path is not None:
                observed = self.executor.run(
                    [
                        "python3",
                        "-I",
                        "-c",
                        _REMOTE_LOCK_CLEAR,
                        self.remote_path + ".d",
                        self.owner_token,
                        command_id,
                    ],
                    check=False,
                ).strip()
                if observed != "cleared":
                    self._set_lease_lost()
            else:
                with self._owner_mutex:
                    owner = self._read_owner()
                    if owner.get("owner") == self.owner_token:
                        owner.pop("active_command", None)
                        owner["heartbeat_at"] = time.time()
                        atomic_json(self.owner_path, owner, redact_values=False)
        except ReleaseError:
            self._set_lease_lost()

    @contextmanager
    def command_scope(self, command: str) -> Iterator[None]:
        self._mark_command(command)
        uncertain = False
        try:
            yield
        except UncertainRemoteEffect:
            # A transport failure is not proof that the destination command
            # stopped.  A pending launch is first cancelled on the
            # destination while holding the owner-record lock; if the
            # bootstrap has already won that race, keep the active command
            # identity and guardian alive until reconciliation proves the
            # process is gone.
            uncertain = True
            if self._command_depth == 1 and self._cancel_pending_command():
                self._uncertain_command = False
                self._retain_on_exit = False
            else:
                self._retain_command()
            raise
        finally:
            if self._uncertain_command:
                if self._command_depth:
                    self._command_depth -= 1
            else:
                self._clear_command()
            if self._lease_lost and not uncertain:
                raise ReleaseError(f"release lock ownership was lost for installation {self.installation}")

    @contextmanager
    def hold_scope(self, operation: str) -> Iterator[None]:
        """Hold and fence the lock without reserving a process-launch identity."""

        self.assert_owned()
        uncertain = False
        try:
            yield
        except UncertainRemoteEffect:
            uncertain = True
            raise
        finally:
            if self._lease_lost and not uncertain:
                raise ReleaseError(
                    f"release lock ownership was lost for installation {self.installation}"
                )

    def guarded(self, executor: Any) -> "LeaseCheckedExecutor":
        return LeaseCheckedExecutor(self, executor)

    def _heartbeat(self) -> None:
        interval = min(_LOCK_HEARTBEAT_SECONDS, self.lease_seconds / 3)
        while not self._heartbeat_stop.wait(interval):
            try:
                if self._remote_acquired:
                    if not self._refresh_remote():
                        self._heartbeat_failed = True
                    else:
                        self._heartbeat_failed = False
                elif self._local_acquired:
                    with self._owner_mutex:
                        owner = self._read_owner()
                        if owner.get("owner") != self.owner_token:
                            self._set_lease_lost()
                            return
                        now = time.time()
                        owner["heartbeat_at"] = now
                        active = owner.get("active_command")
                        if isinstance(active, dict):
                            active["expires_at"] = now + _LOCK_ACTIVE_GRACE_SECONDS
                        atomic_json(self.owner_path, owner, redact_values=False)
                        self._heartbeat_failed = False
            except (OSError, ReleaseError):
                self._heartbeat_failed = True

    def _start_heartbeat(self) -> None:
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat, name="release-lock-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def _stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2)
            self._heartbeat_thread = None

    def _acquire_local(self) -> None:
        self.lock_directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for _ in range(3):
            try:
                os.mkdir(self.lock_directory, 0o700)
            except FileExistsError:
                self._reclaim_local_stale()
                continue
            try:
                self._write_local_owner()
            except Exception:
                shutil.rmtree(self.lock_directory, ignore_errors=True)
                raise
            self._local_acquired = True
            self._start_heartbeat()
            return
        raise ReleaseError(f"release already running for installation {self.installation}")

    def __enter__(self) -> "InstallationLock":
        if self.remote_path is not None and getattr(self.executor, "host", None):
            observed = self.executor.run(
                [
                    "python3",
                    "-I",
                    "-c",
                    _REMOTE_LOCK_ACQUIRE,
                    self.remote_path + ".d",
                    self.owner_token,
                    str(self.lease_seconds),
                ]
            )
            if not observed.strip().startswith("acquired:"):
                raise ReleaseError(f"release already running for installation {self.installation}")
            self._remote_acquired = True
            self._start_heartbeat()
            return self
        self._acquire_local()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop_heartbeat()
        if self._retain_on_exit:
            # Leave the destination guardian and active command marker in
            # place.  A later operator may take over only after the recorded
            # command is confirmed dead and the owner lease is reconciled.
            return
        if self._remote_acquired and self.remote_path is not None:
            self.executor.run(
                [
                    "python3",
                    "-I",
                    "-c",
                    _REMOTE_LOCK_RELEASE,
                    self.remote_path + ".d",
                    self.owner_token,
                ],
                check=False,
            )
            self._remote_acquired = False
            return
        if self._local_acquired:
            try:
                owner = self._read_owner()
                if owner.get("owner") != self.owner_token:
                    return
            except ReleaseError:
                return
            releasing = self.lock_directory.with_name(self.lock_directory.name + ".releasing-" + uuid.uuid4().hex)
            try:
                os.rename(self.lock_directory, releasing)
            except FileNotFoundError:
                pass
            else:
                shutil.rmtree(releasing, ignore_errors=True)
            self._local_acquired = False


class LeaseCheckedExecutor:
    """Executor facade that fences every destination operation by the lock."""

    def __init__(self, lock: InstallationLock, delegate: Any):
        self.lock = lock
        self.delegate = delegate
        if hasattr(delegate, "host"):
            self.host = delegate.host

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    @contextmanager
    def _delegate_scope(self) -> Iterator[None]:
        owner_context = getattr(self.delegate, "owner_context", None)
        if callable(owner_context) and self.lock.remote_path and self.lock._command_id:
            with owner_context(
                self.lock.remote_path + ".d",
                self.lock.owner_token,
                self.lock._command_id,
            ):
                yield
            return
        yield

    @contextmanager
    def _launch_scope(self, command: str) -> Iterator[None]:
        with self.lock.command_scope(command):
            with self._delegate_scope():
                yield

    def run(self, argv: list[str], *, check: bool = True) -> str:
        with self.lock.command_scope("run:" + (argv[0] if argv else "unknown")):
            with self._delegate_scope():
                return self.delegate.run(argv, check=check)

    def write_file(self, path: Path, data: bytes, *, mode: int = 0o600) -> None:
        with self.lock.command_scope("write:" + str(path)):
            with self._delegate_scope():
                self.delegate.write_file(path, data, mode=mode)

    def copy_tree(self, source: Path, destination: Path) -> None:
        if callable(getattr(self.delegate, "owner_context", None)):
            with self.lock.hold_scope("copy:" + str(destination)):
                self.delegate.copy_tree(
                    source,
                    destination,
                    launch_context=self._launch_scope,
                )
            return
        with self.lock.command_scope("copy:" + str(destination)):
            with self._delegate_scope():
                self.delegate.copy_tree(source, destination)

    def tree_digest(self, root: Path) -> str:
        with self.lock.command_scope("tree-digest:" + str(root)):
            with self._delegate_scope():
                return self.delegate.tree_digest(root)

    def file_digest(self, path: Path) -> str:
        with self.lock.command_scope("file-digest:" + str(path)):
            with self._delegate_scope():
                return self.delegate.file_digest(path)

    def read_file(self, path: Path) -> bytes:
        with self.lock.command_scope("read:" + str(path)):
            with self._delegate_scope():
                return self.delegate.read_file(path)

    def entry_digest(self, path: Path, relative: str = "") -> str:
        with self.lock.command_scope("entry-digest:" + str(path)):
            with self._delegate_scope():
                return self.delegate.entry_digest(path, relative)

    def entry_exists(self, path: Path) -> bool:
        with self.lock.command_scope("entry-exists:" + str(path)):
            with self._delegate_scope():
                return self.delegate.entry_exists(path)

    def verify_entries(self, destination: Path, expected_entries: list[list[Any]]) -> None:
        """Verify a whole manifest under one command scope, never one per entry.

        The scope writes owner-record bookkeeping once.  A local delegate gets
        the lock's read-only ownership check to run during its loop; an SSH
        delegate checks the destination-side owner record from its own
        process.  There is deliberately no per-entry fallback.
        """

        verify = getattr(self.delegate, "verify_entries", None)
        if not callable(verify):
            raise ReleaseError("destination executor cannot verify artifact entries")
        with self.lock.command_scope("verify-entries:" + str(destination)):
            with self._delegate_scope():
                verify(destination, expected_entries, ownership_check=self.lock.assert_owned)


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class RecordStore:
    """Separate immutable plan and durable, hash-chained execution record."""

    def __init__(self, root: Path, release_id: str):
        self.directory = root / release_id
        self.plan_path = self.directory / "plan.json"
        self.record_path = self.directory / "record.json"
        self.events_path = self.directory / "events.jsonl"

    def create(self, plan: dict[str, Any]) -> dict[str, Any]:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        expected = plan["plan_digest"]
        if self.plan_path.exists():
            existing = json.loads(self.plan_path.read_text())
            if existing.get("plan_digest") != expected:
                raise ReleaseError(f"frozen plan already exists with a different digest: {self.plan_path}")
            plan = existing
        else:
            # The frozen plan is executable input. Its digest was computed
            # over this exact value, so redacting it would make it unloadable.
            atomic_json(self.plan_path, plan, redact_values=False)
        if self.record_path.exists():
            return self.load()
        record = {
            "schema_version": 1,
            "release_id": plan["release_id"],
            "installation": plan["installation"],
            "plan_digest": expected,
            "state": "planned",
            "approvals": {},
            "steps": {},
            "observations": [],
            "failures": [],
            "recovery": [],
            "cleanup": [],
            "last_event_digest": None,
            "event_count": 0,
        }
        atomic_json(self.record_path, record)
        return record

    def plan(self) -> dict[str, Any]:
        if not self.plan_path.exists():
            raise ReleaseError(f"no frozen plan: {self.plan_path}")
        return json.loads(self.plan_path.read_text())

    def load(self) -> dict[str, Any]:
        if not self.record_path.exists():
            raise ReleaseError(f"no release record: {self.record_path}")
        state = json.loads(self.record_path.read_text())
        if state.get("plan_digest") != self.plan().get("plan_digest"):
            raise ReleaseError("release record is bound to a different frozen plan")
        snapshot_count = int(state.get("event_count", 0))
        if self.events_path.exists():
            chain: str | None = None
            lines = self.events_path.read_text().splitlines()
            for index, line in enumerate(lines):
                event = json.loads(line)
                event_digest = event.pop("event_digest", None)
                if not event_digest or digest(event) != event_digest or event.get("previous_event_digest") != chain:
                    raise ReleaseError(f"release event chain is corrupt: {self.events_path}:{index + 1}")
                chain = event_digest
                # The snapshot already contains every event through
                # snapshot_count.  Only replay a tail left by a crash between
                # the fsynced event append and the atomic snapshot replace.
                if index < snapshot_count:
                    continue
                state = _merge(state, event.get("record_patch", {}))
                state["last_event_digest"] = event_digest
                state["event_count"] = index + 1
            if lines and snapshot_count >= len(lines) and state.get("last_event_digest") != chain:
                raise ReleaseError(f"release snapshot does not match event chain: {self.record_path}")
        return state

    def append(self, event_type: str, *, record_patch: dict[str, Any] | None = None, **data: Any) -> dict[str, Any]:
        state = self.load()
        event = {
            "schema_version": 1,
            "event_type": event_type,
            "record_patch": redact(record_patch or {}),
            "data": redact(data),
            "previous_event_digest": state.get("last_event_digest"),
            "created_at": time.time(),
        }
        event["event_digest"] = digest(event)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.events_path.open("ab") as stream:
            stream.write(canonical(event))
            stream.flush()
            os.fsync(stream.fileno())
        updated = _merge(state, event["record_patch"])
        updated["last_event_digest"] = event["event_digest"]
        updated["event_count"] = int(state.get("event_count", 0)) + 1
        atomic_json(self.record_path, updated)
        return updated

    def step_started(self, step_id: str, *, before: Any) -> dict[str, Any]:
        return self.append(
            "step_started",
            record_patch={"state": "running", "steps": {step_id: {"status": "running", "before": redact(before)}}},
            step_id=step_id,
        )

    def step_finished(self, step_id: str, *, after: Any) -> dict[str, Any]:
        current = self.load().get("steps", {}).get(step_id, {})
        return self.append(
            "step_finished",
            record_patch={
                "state": "succeeded",
                "steps": {step_id: {**current, "status": "succeeded", "after": redact(after)}},
            },
            step_id=step_id,
        )

    def step_failed(self, step_id: str, *, error: str, after: Any = None) -> dict[str, Any]:
        current = self.load().get("steps", {}).get(step_id, {})
        failure = {"step_id": step_id, "error": redact(error), "after": redact(after)}
        return self.append(
            "step_failed",
            record_patch={
                "state": "failed",
                "steps": {step_id: {**current, "status": "failed", "after": redact(after), "error": redact(error)}},
                "failures": self.load().get("failures", []) + [failure],
            },
            step_id=step_id,
        )

    def observation(self, value: Any) -> dict[str, Any]:
        current = self.load()
        return self.append(
            "observation",
            record_patch={"observations": current.get("observations", []) + [redact(value)]},
            observation=redact(value),
        )

    def authorize(self, scope: str, evidence: dict[str, Any]) -> dict[str, Any]:
        plan = self.plan()
        if evidence.get("plan_digest") != plan["plan_digest"]:
            raise ReleaseError("authorization evidence does not bind the frozen plan")
        if evidence.get("scope") != scope:
            raise ReleaseError(f"authorization evidence scope is not {scope}")
        if not evidence.get("evidence"):
            raise ReleaseError("authorization evidence must describe the owner instruction")
        return self.append("authorization", record_patch={"approvals": {scope: redact(evidence)}}, scope=scope)


def walk_json_files(root: Path) -> Iterator[Path]:
    yield from sorted(path for path in root.rglob("*") if path.is_file())
