"""Process identity and owned process-group cleanup; never by name, port or substring."""

import os
from pathlib import Path
import signal
import subprocess
import time

from ..control import process_identity


def identity_alive(pid, identity):
    """The PID still runs the same boot-scoped process it did when it was recorded."""
    try:
        return process_identity(pid) == identity
    except (OSError, ValueError, IndexError, TypeError):
        return False


def netns_identity(pid="self"):
    """Kernel identity of a process's network namespace (e.g. ``net:[4026532001]``)."""
    return os.readlink(f"/proc/{pid}/ns/net")


def _group_of(pid):
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        rest = text[text.rindex(")") + 2:].split()
        return rest[0], int(rest[2])
    except (OSError, ValueError, IndexError):
        return None


def leader_exited(process):
    """Peek without reaping so the group id cannot be reused before cleanup ends."""
    if process.returncode is not None:
        return True
    try:
        return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True


def group_members(pgid):
    members = []
    for entry in Path("/proc").iterdir():
        fields = _group_of(entry.name) if entry.name.isdigit() else None
        if fields and fields[0] not in "ZX" and fields[1] == pgid:
            members.append(int(entry.name))
    return members


class OwnershipLost(RuntimeError):
    """A child leader was reaped elsewhere, so its group id can no longer be trusted."""


def stop_group(process, timeout, log=lambda message: None):
    """Stop the process group this supervisor created with ``start_new_session``.

    SIGTERM, then SIGKILL, only for a group whose unreaped leader we still hold.
    """
    if process is None:
        return
    if process.returncode is not None:
        raise OwnershipLost(f"pid {process.pid} was reaped before its group was cleaned; not signalling stale IDs")
    pgid = process.pid
    alive = lambda: not leader_exited(process) or bool(group_members(pgid))
    for number, limit in ((signal.SIGTERM, timeout), (signal.SIGKILL, 5)):
        if not alive():
            break
        if number == signal.SIGKILL:
            log(f"pid {pgid} did not stop within {timeout}s; killing its process group")
        try:
            os.killpg(pgid, number)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + limit
        while alive() and time.monotonic() < deadline:
            time.sleep(0.05)
    try:
        process.wait(5)
    except subprocess.TimeoutExpired:
        log(f"pid {pgid} could not be reaped after SIGKILL; leaving it to the service manager")
