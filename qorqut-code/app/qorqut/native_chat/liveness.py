"""The one precise answer to "is the process (or Codex thread writer) a native binding was made for still there?"

Registry, attachment and the selected-observation probe all use this, so none of them can read a failure to look as a death.
Three answers, never two:

* ``dead``     the process is verifiably absent (no ``/proc`` entry, a zombie, a different start tick), or, for a shared Codex
               thread, its writer lock is verifiably not held any more (descriptor closed or replaced, lock line absent while
               the descriptor table was readable).
* ``alive``    the process exists with the recorded start ticks and, for a shared thread, still holds that exact writer lock.
               A managed session is alive only while both its client process and the host process that drives it are.
* ``unknown``  anything that could not be read or understood (permission denied, I/O error, malformed fields). Unknown is not
               dead: nothing is ended, superseded or replayed on it.
"""

from __future__ import annotations

import os

from qorqut.bootstrap import codex_session
from qorqut_system import platform

ALIVE, DEAD, UNKNOWN = "alive", "dead", "unknown"


def state(pid, started, owner=None) -> str:
    """``alive`` / ``dead`` / ``unknown`` for ``pid`` with start ticks ``started``. A five-part ``owner``
    (``pid:ticks:fd:dev:inode``) is a shared Codex thread: its held, exact writer lock is part of being alive."""
    if type(pid) is not int or not isinstance(started, str):
        return UNKNOWN
    root = codex_session.PROC_ROOT
    try:
        if platform.family() == 'mac':
            from qorqut import process_info
            fields = ['Z' if process_info.is_zombie(pid) else 'S'] + ['0'] * 18 + [process_info.start_token(pid)]
        else:
            fields = (root / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z" or fields[19] != started:
            return DEAD
    except (FileNotFoundError, ProcessLookupError):
        return DEAD
    except (OSError, IndexError, ValueError):
        return UNKNOWN
    parts = owner.split(":") if isinstance(owner, str) else []
    if len(parts) != 5:
        return ALIVE
    if parts[0] != str(pid) or parts[1] != started or not all(part.isascii() and part.isdigit() for part in parts):
        return UNKNOWN
    if platform.family() == 'mac':
        return UNKNOWN  # A readable process/open file cannot prove a held per-thread writer lock.
    try:
        descriptor = root / str(pid) / "fd" / parts[2]
        current = descriptor.stat()
        if (current.st_dev, current.st_ino) != (int(parts[3]), int(parts[4])):
            return DEAD
        for line in (root / str(pid) / "fdinfo" / parts[2]).read_text().splitlines():
            fields = line.split()
            if fields[:1] == ["lock:"] and fields[2:6] == ["FLOCK", "ADVISORY", "WRITE", str(pid)]:
                major, minor, locked_inode = fields[6].split(":")
                held = (int(major, 16), int(minor, 16), int(locked_inode)) == (
                    os.major(current.st_dev), os.minor(current.st_dev), current.st_ino)
                return ALIVE if held else DEAD
        return DEAD
    except (FileNotFoundError, ProcessLookupError):
        return DEAD
    except (OSError, IndexError, ValueError):
        return UNKNOWN


def of_binding(binding: dict) -> str:
    """The answer for an owner row's ``binding_json``."""
    return state(binding.get("pid"), binding.get("started"), binding.get("owner"))


def managed_host(owner) -> tuple[int, str] | None:
    """The host process of a managed binding's owner, ``host:<pid>:<start ticks>``; ``None`` for anything else."""
    parts = owner.split(":") if isinstance(owner, str) else []
    if len(parts) != 3 or parts[0] != "host" or not all(part.isascii() and part.isdigit() for part in parts[1:]):
        return None
    return int(parts[1]), parts[2]


def worst(*answers: str) -> str:
    """The least favourable answer: one verifiably dead part makes the whole dead; otherwise one unreadable part makes it
    unknown; alive needs every part alive."""
    for answer in (DEAD, UNKNOWN):
        if answer in answers:
            return answer
    return ALIVE


def of_session(row: dict) -> str:
    """The answer for a ``native_sessions`` row: a managed session by its client process and its host process together, a row of an
    older transport (a terminal pane, a shared Codex server; history after the cutover) by the process identity alone. The client
    process alone is not enough for a managed session: one whose host is gone cannot be driven, so the verified death of either ends
    the binding."""
    if row["transport"] == "managed":
        host = managed_host(row["owner"])
        return worst(state(row["process_pid"], row["process_started"]), UNKNOWN if host is None else state(*host))
    return state(row["process_pid"], row["process_started"])


# ----- availability -----------------------------------------------------------------------------------------------------
# What a reader that says "online" or "offline" about a managed agent reports instead (``native_chat/opener.py`` has the rules):
# ``open`` a session is bound; ``asleep`` none is, and the next input opens one; ``off`` the owner (or Ordubashy) turned it off, so
# only the owner's own input (or Ordubashy's start) does; ``unavailable`` nothing can open it (paused, no managed runtime).
OPEN, ASLEEP, OFF, UNAVAILABLE = "open", "asleep", "off", "unavailable"


def availability(*, open_session: bool, desired_state: str | None, can_open: bool) -> str:
    """Where an agent stands for the readers: an open session is ``open``; otherwise ``off`` when its recorded intent is stopped,
    ``asleep`` when it can be opened (``can_open``) and is not paused, else ``unavailable``."""
    if open_session:
        return OPEN
    if desired_state == "stopped":
        return OFF
    if can_open and desired_state != "paused":
        return ASLEEP
    return UNAVAILABLE
