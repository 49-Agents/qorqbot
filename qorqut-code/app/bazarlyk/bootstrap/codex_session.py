"""Linux Codex hook lifecycle: the verified native process identity and the transcript snapshot at a turn or session end."""

import argparse
import json
import os
from pathlib import Path
import sys
import uuid

from qorqut_operations.contracts import DomainError
from .client import load_registration
from .transcript_snapshot import run as snapshot_transcript

PROC_ROOT = Path("/proc")


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
