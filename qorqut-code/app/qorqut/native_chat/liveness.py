"""The one precise answer to "is the process (or Codex thread writer) a native binding was made for still there?"

Registry, attachment and the selected-observation probe all use this, so none of them can read a failure to look as a death.
Three answers, never two:

* ``dead``     the process is verifiably absent (no ``/proc`` entry, a zombie, a different start tick), or, for a shared Codex
               thread, its writer lock is verifiably not held any more (descriptor closed or replaced, lock line absent while
               the descriptor table was readable).
* ``alive``    the process exists with the recorded start ticks and, for a shared thread, still holds that exact writer lock.
* ``unknown``  anything that could not be read or understood (permission denied, I/O error, malformed fields). Unknown is not
               dead: nothing is ended, superseded or replayed on it.
"""

from __future__ import annotations

import os
import sys

from qorqut.bootstrap import codex_session

ALIVE, DEAD, UNKNOWN = "alive", "dead", "unknown"


def state(pid, started, owner=None) -> str:
    """``alive`` / ``dead`` / ``unknown`` for ``pid`` with start ticks ``started``. A five-part ``owner``
    (``pid:ticks:fd:dev:inode``) is a shared Codex thread: its held, exact writer lock is part of being alive."""
    if type(pid) is not int or not isinstance(started, str):
        return UNKNOWN
    root = codex_session.PROC_ROOT
    try:
        if sys.platform == 'darwin':
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
    if sys.platform == 'darwin':
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


def of_session(row: dict) -> str:
    """The answer for a ``native_sessions`` row: a shared Codex thread is judged by its owner tuple, anything else by the
    process identity alone. A shared-thread row that lost its owner cannot be judged."""
    if row["transport"] == "codex_queue":
        owner = row["owner"]
        if not owner:
            return UNKNOWN
        parts = owner.split(":")
        if len(parts) != 5 or not parts[0].isdigit():
            return UNKNOWN
        return state(int(parts[0]), parts[1], owner)
    return state(row["process_pid"], row["process_started"])
