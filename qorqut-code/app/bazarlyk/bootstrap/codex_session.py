"""Linux Codex hook lifecycle and native queue delivery for the shared watcher."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from bazarlyk.contracts import DomainError
from bazarlyk.inbox.watcher import watch
from .client import BootstrapClient, load_registration, path_has_symlink
from .transcript_snapshot import run as snapshot_transcript

PROC_ROOT = Path("/proc")
WATCHER_VERSION = '4:' + str(Path(__file__).resolve())


def watcher_binding(owner):
    return json.dumps({'owner': owner, 'version': WATCHER_VERSION}, sort_keys=True)


def process_identity(pid):
    """PID plus Linux start ticks prevents attachment to a recycled PID."""
    try:
        fields = (PROC_ROOT / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return f"{pid}:{fields[19]}"
    except (OSError, IndexError):
        return None


def owner_alive(owner):
    """Owners are PID:start-ticks, plus fd:device:inode for a shared-server thread."""
    parts = owner.split(":")
    if len(parts) not in {2, 5} or not all(part.isascii() and part.isdigit() for part in parts):
        return False
    if process_identity(int(parts[0])) != ":".join(parts[:2]):
        return False
    if len(parts) == 2:
        return True
    pid, _, descriptor, device, inode = parts
    try:
        process = PROC_ROOT / pid
        current = (process / "fd" / descriptor).stat()
        if (current.st_dev, current.st_ino) != (int(device), int(inode)):
            return False
        # The server outlives individual threads; its open, held writer lock does not.
        for line in (process / "fdinfo" / descriptor).read_text().splitlines():
            fields = line.split()
            if fields[:1] == ["lock:"] and fields[2:6] == ["FLOCK", "ADVISORY", "WRITE", pid]:
                major, minor, locked_inode = fields[6].split(":")
                return (int(major, 16), int(minor, 16), int(locked_inode)) == (
                    os.major(current.st_dev), os.minor(current.st_dev), current.st_ino)
    except (OSError, ValueError, IndexError):
        pass
    return False


def thread_owner(pid, session_id):
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve()
    expected = home / "thread-writer-locks" / (str(uuid.UUID(session_id)) + ".lock")
    identity = process_identity(pid)
    if not identity:
        return None
    for descriptor in (PROC_ROOT / str(pid) / "fd").iterdir():
        try:
            if descriptor.readlink() != expected:
                continue
            current = descriptor.stat()
            owner = f"{identity}:{descriptor.name}:{current.st_dev}:{current.st_ino}"
            if owner_alive(owner):
                return owner
        except OSError:
            continue
    return None


def session_process(session_id):
    pid = os.getppid()
    for _ in range(12):
        try:
            process = PROC_ROOT / str(pid)
            # An npm update can unlink the executable while its server is still running.
            executable = Path(str((process / "exe").readlink()).removesuffix(" (deleted)"))
            argv = (process / "cmdline").read_bytes().split(b"\0")
            if executable.name == "codex":
                if b"app-server" in argv:
                    owner = thread_owner(pid, session_id)
                    if owner:
                        return owner
                    break
                if b"exec" in argv or b"queue" in argv:
                    break
                identity = process_identity(pid)
                if identity:
                    return identity
            fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
            pid = int(fields[1])
            if pid <= 1:
                break
        except (OSError, ValueError, IndexError):
            break
    raise DomainError("inbox_delivery_unavailable", "Cannot identify a running local Codex session for inbox delivery")


def state_path(registration, session_id):
    session_id = str(uuid.UUID(session_id))
    path = Path(registration["workspace"]) / ".bazarlyk/codex-watchers" / (session_id + ".lock")
    if path_has_symlink(path):
        raise DomainError("projection_conflict", "Inbox lifecycle state cannot follow symlinks")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def ensure_watcher(hook, registration_path):
    registration = load_registration(registration_path)
    owner = session_process(hook["session_id"])
    path = state_path(registration, hook["session_id"])
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        for attempt in range(31):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if path.read_text() == watcher_binding(owner):
                    return
                # Change only our own session's binding. Both the legacy and
                # current watcher notice this within their one-second sleep and
                # release the lock/lease; no terminal or model is restarted.
                if attempt == 0:
                    os.ftruncate(descriptor, 0)
                if attempt == 30:
                    raise DomainError('inbox_delivery_unavailable', 'Previous session watcher has not exited; retry this hook')
                time.sleep(.1)
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, watcher_binding(owner).encode())
        from bazarlyk.installation import helper_launcher
        launcher = helper_launcher()
        command = [launcher,__name__] if launcher else [sys.executable,"-I","-m",__name__]
        process = subprocess.Popen([*command,
            "--registration", str(registration_path), "--watch", hook["session_id"],
            "--owner", owner, "--lock-fd", str(descriptor)],
            cwd=registration["workspace"], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            pass_fds=(descriptor,), start_new_session=True)
        if process is not None and process.poll() is not None:
            raise DomainError('inbox_delivery_unavailable', 'The session watcher exited during startup')
    finally:
        os.close(descriptor)


def deliver(message, *, session_id, registration, alive, report_client=None):
    if not alive():
        raise KeyboardInterrupt
    try:
        result = subprocess.run(["codex", "queue", "--thread", session_id, "--message", message],
            cwd=registration["workspace"], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    except subprocess.TimeoutExpired as exc:
        _report_queue_failure(registration, session_id, 'inbox_queue_timeout', report_client)
        raise DomainError("inbox_delivery_unavailable", "Codex queue timed out",
                          details={'technical_reported': True}) from exc
    except OSError as exc:
        _report_queue_failure(registration, session_id, 'inbox_queue_unavailable', report_client)
        raise DomainError("inbox_delivery_unavailable", "Codex queue is unavailable",
                          details={'technical_reported': True}) from exc
    if result.returncode:
        _report_queue_failure(registration, session_id, 'inbox_queue_refused', report_client)
        raise DomainError("inbox_delivery_unavailable", "Codex queue refused the notification",
                          details={'technical_reported': True})


def _report_queue_failure(registration, session_id, code, client=None):
    try:
        from .technical_reporting import record
        record(registration, session_id, 'inbox', 'queue', code, call_site='inbox.watcher.queue',
               retryable=False, terminal=True, client=client)
    except Exception:
        pass


def run_watcher(registration_path, session_id, owner, descriptor):
    registration = load_registration(registration_path)
    session_id = str(uuid.UUID(session_id))
    client = BootstrapClient(registration, timeout=5)
    path = state_path(registration, session_id)
    def alive():
        return owner_alive(owner) and not path_has_symlink(path) and path.is_file() and path.read_text() == watcher_binding(owner)
    def sleep(seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not alive():
                raise KeyboardInterrupt
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    def emit(message):
        deliver(message, session_id=session_id, registration=registration, alive=alive, report_client=client)
        try:
            from .technical_reporting import active, record
            if active(registration, 'inbox', 'queue', session_id):
                record(registration, session_id, 'inbox', 'queue', 'success',
                       call_site='inbox.watcher.queue', recovered=True, client=client)
        except Exception:
            pass
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    delegated = None
    try:
        if not alive():
            return 0
        if registration.get("durable_delegation_enabled") is True:
            from .delegated_runtime import DelegatedRuntime
            delegated = DelegatedRuntime(registration, session_id, alive=alive)
            delegated.start()
        return watch(client, session_id, emit=emit, sleep=sleep)
    except (KeyboardInterrupt, DomainError, OSError):
        return 0
    finally:
        if delegated is not None:
            delegated.stop()
        os.close(descriptor)


def finish(hook, registration_path, *, diagnostics=sys.stderr):
    if hook.get("hook_event_name") == "SessionEnd":
        registration = load_registration(registration_path)
        if hook.get("cwd") != registration["workspace"]:
            raise DomainError("registration_conflict", "Codex exit workspace does not match its registration")
        path = state_path(registration, hook["session_id"])
        # Keep the inode stable: the watcher owns its lock until it releases the lease.
        if path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
            try:
                os.ftruncate(descriptor, 0)
            finally:
                os.close(descriptor)
    snapshot_transcript(hook, registration_path, runtime="codex", diagnostics=diagnostics)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registration", type=Path, required=True)
    parser.add_argument("--watch")
    parser.add_argument("--owner")
    parser.add_argument("--lock-fd", type=int)
    args = parser.parse_args()
    if args.watch:
        return run_watcher(args.registration, args.watch, args.owner, args.lock_fd)
    try:
        raw = sys.stdin.read(262145)
        if len(raw) > 262144:
            raise ValueError("oversized hook")
        hook = json.loads(raw)
        if not isinstance(hook, dict) or hook.get("hook_event_name") not in {"Stop", "SessionEnd"}:
            raise ValueError("invalid lifecycle hook")
        finish(hook, args.registration)
    except (DomainError, OSError, ValueError, KeyError):
        sys.stderr.write("Qorqut could not finish this Codex lifecycle hook; stale inbox leases expire automatically.\n")
    sys.stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
