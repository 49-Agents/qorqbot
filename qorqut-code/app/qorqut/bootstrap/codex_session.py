"""Linux Codex hook lifecycle: the verified native process identity and the transcript snapshot at a turn or session end."""

import argparse
import json
import os
from pathlib import Path
import sys
import uuid

from qorqut_operations.contracts import DomainError
from qorqut_system import platform, private_files
from .client import load_registration
from .transcript_snapshot import run as snapshot_transcript

PROC_ROOT = Path("/proc")
# What a managed host starts (``codex_driver.CodexSession._argv``): the app-server on stdio, nothing after it. A ``codex exec`` /
# ``codex queue`` / TUI / websocket server never has this tail, so a process that does is the only shape a host's client can have.
APP_SERVER_TAIL = (b"app-server", b"--listen", b"stdio://")
WRITER_LOCKS = "thread-writer-locks"


def runs_app_server(argv):
    """Whether ``argv`` ends exactly in ``app-server --listen stdio://`` (a trailing empty element of a raw ``cmdline`` is ignored)."""
    argv = list(argv)
    if argv and argv[-1] == b"":
        argv.pop()
    return len(argv) > len(APP_SERVER_TAIL) and tuple(argv[-len(APP_SERVER_TAIL):]) == APP_SERVER_TAIL and b"exec" not in argv[:-3] \
        and b"queue" not in argv[:-3]


def process_identity(pid):
    """PID plus kernel process birth identity prevents attaching to a recycled PID."""
    try:
        if platform.family() == "mac":
            from qorqut.process_info import pid_identity
            return pid_identity(pid)
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
    if platform.family() == "mac":
        # psutil open-files data cannot attest an exact per-thread held writer lock.
        return False
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


def owner_process(owner):
    """The ``pid:start ticks`` of an owner string: the whole of a plain owner, the first two parts of a held-lock owner. The managed
    binding's owner is always this two-part form (the app-server process); the five-part ``pid:ticks:fd:dev:inode`` is only the
    proof, taken at the hook and by the probe, that this process holds the thread's writer lock."""
    parts = owner.split(":") if isinstance(owner, str) else []
    return ":".join(parts[:2]) if len(parts) in {2, 5} and all(part.isascii() and part.isdigit() for part in parts) else None


def thread_owner(pid, session_id, home=None):
    """The held-lock owner of ``session_id`` in process ``pid``: its writer lock under ``home`` (default ``$CODEX_HOME`` or
    ``~/.codex``), or, with ``home=False``, under any Codex home (the process, not the folder, is what the caller anchors on)."""
    if platform.family() == "mac":
        return None
    try:
        name = str(uuid.UUID(session_id)) + ".lock"
    except (ValueError, AttributeError, TypeError):
        return None
    if home is False:
        expected = None
    else:
        home = Path(home or os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve()
        expected = home / WRITER_LOCKS / name
    identity = process_identity(pid)
    if not identity:
        return None
    try:
        descriptors = list((PROC_ROOT / str(pid) / "fd").iterdir())
    except OSError:
        return None
    for descriptor in descriptors:
        try:
            target = Path(private_files.read_link(descriptor))
            if (target != expected) if expected is not None else (target.name != name or target.parent.name != WRITER_LOCKS):
                continue
            current = descriptor.stat()
            owner = f"{identity}:{descriptor.name}:{current.st_dev}:{current.st_ino}"
            if owner_alive(owner):
                return owner
        except OSError:
            continue
    return None


def hosted_session_process(session_id, workspace, agent_id):
    """``(identity, host)`` when this hook runs under the app-server a managed host of THIS agent started, else ``None``.

    ``identity`` is ``pid:start ticks`` of that app-server (the managed binding's owner), ``host`` what ``native_binding.managed_host``
    verified (the host record, its pid and start ticks, its command line, the app-server as its client without another ``codex``
    between). Walking up from the hook, the nearest ``codex`` must be that app-server: a ``codex exec`` or a TUI an agent starts from
    its shell comes first and ends the search. On Linux the app-server must also hold the writer lock of exactly ``session_id`` (it takes
    it at ``thread/start``), so the session the hook names is a thread this process serves. This is a filter over facts the agent's
    shell can also produce; the service binds only what its own token-verified host identity names (``verify_managed``)."""
    from .native_binding import managed_host
    pid = os.getppid()
    for _ in range(12):
        try:
            process = PROC_ROOT / str(pid)
            if platform.family() == "mac":
                from qorqut import process_info
                observed_start = process_info.start_token(pid)
                executable = process_info.exe(pid)
                argv = [arg.encode() for arg in process_info.cmdline(pid)]
                if process_info.start_token(pid) != observed_start:
                    return None
            else:
                executable = Path(str(private_files.read_link(process / "exe")).removesuffix(" (deleted)"))
                argv = (process / "cmdline").read_bytes().split(b"\0")
            if executable.name == "codex":
                if not runs_app_server(argv):
                    return None
                host = managed_host(workspace, pid, agent_id, runtime="codex")
                identity = process_identity(pid)
                if host is None or identity is None or (platform.family() == "mac" and identity != f"{pid}:{observed_start}"):
                    return None
                if platform.family() != "mac" and owner_process(thread_owner(pid, session_id) or "") != identity:
                    return None
                return identity, host
            if platform.family() == "mac":
                pid = process_info.parent_pid(pid)
            else:
                pid = int((process / "stat").read_text().rsplit(")", 1)[1].split()[1])
            if pid <= 1:
                return None
        except (OSError, ValueError, IndexError):
            return None
    return None


def finish(hook, registration_path, *, diagnostics=sys.stderr):
    if hook.get("hook_event_name") == "SessionEnd":
        registration = load_registration(registration_path)
        if hook.get("cwd") != registration["workspace"]:
            raise DomainError("registration_conflict", "Codex exit workspace does not match its registration")
    snapshot_transcript(hook, registration_path, runtime="codex", diagnostics=diagnostics)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registration", type=Path, required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.read(262145)
        if len(raw) > 262144:
            raise ValueError("oversized hook")
        hook = json.loads(raw)
        if not isinstance(hook, dict) or hook.get("hook_event_name") not in {"Stop", "SessionEnd"}:
            raise ValueError("invalid lifecycle hook")
        finish(hook, args.registration)
    except (DomainError, OSError, ValueError, KeyError):
        sys.stderr.write("Qorqut could not finish this Codex lifecycle hook.\n")
    sys.stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
