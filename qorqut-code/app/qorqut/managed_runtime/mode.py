"""Which runtime an agent uses: its tmux terminal (the default) or a managed headless session.

The choice is host-local and lives in one small file, ``<runtime>/managed-agents/<agent_id>/mode.json``::

    {"schema_version": 1, "mode": "managed", "changed_at": "..."}

A missing, unreadable or unrecognised file means ``terminal``: an agent nobody switched behaves exactly as before. The profile
schema is not involved (it is portable authority; this is a setting of this installation).

Reads are cached by the file's identity (inode, size, modification time), so asking for the mode on every request costs one
``stat``; ``set_mode`` replaces the file atomically (private, 0600) and refreshes the cache. Nothing here raises for a read.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import threading
from datetime import datetime, timezone

TERMINAL = "terminal"
MANAGED = "managed"
MODES = (TERMINAL, MANAGED)
SCHEMA_VERSION = 1
STATE_DIRECTORY = "managed-agents"
MODE_FILE = "mode.json"
# An agent identity is a UUID; anything that could name another path is not an identity.
_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

_lock = threading.Lock()
_cache: dict[str, tuple[tuple, str]] = {}


def state_root(paths) -> Path:
    return Path(paths.runtime) / STATE_DIRECTORY


def agent_state(paths, agent_id) -> Path:
    """``<runtime>/managed-agents/<agent_id>``. Raises ``ValueError`` for an identity that is not a plain name."""
    if not isinstance(agent_id, str) or not _AGENT_ID.fullmatch(agent_id):
        raise ValueError("A managed agent is named by its identity only")
    return state_root(paths) / agent_id


def mode_file(paths, agent_id) -> Path:
    return agent_state(paths, agent_id) / MODE_FILE


def _signature(path: Path):
    info = path.stat()
    return (info.st_ino, info.st_size, info.st_mtime_ns)


def mode_of(paths, agent_id) -> str:
    """``managed`` or ``terminal`` for this agent. Never raises: no paths, no file, a bad file or a bad identity read as
    ``terminal``, the behaviour every agent had before the switch existed."""
    if paths is None:
        return TERMINAL
    try:
        path = mode_file(paths, agent_id)
    except (ValueError, AttributeError, TypeError):
        return TERMINAL
    key = str(path)
    try:
        signature = _signature(path)
    except (OSError, ValueError):
        with _lock:
            _cache.pop(key, None)
        return TERMINAL
    with _lock:
        cached = _cache.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    try:
        # The file is a few dozen bytes; a larger one is not ours.
        if signature[1] > 4096:
            raise ValueError("oversized")
        value = json.loads(path.read_text())
        mode = value.get("mode") if isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION else None
    except (OSError, ValueError):
        mode = None
    mode = mode if mode in MODES else TERMINAL
    with _lock:
        _cache[key] = (signature, mode)
    return mode


def set_mode(paths, agent_id, mode) -> str:
    """Record the runtime of this agent and return it. The file is replaced atomically and is private to this user.

    This only records the choice. Whether the switch is allowed now (no open session, no waiting input, no running host or
    terminal) is the caller's decision, made under the agent's lock."""
    if mode not in MODES:
        raise ValueError("The runtime is terminal or managed")
    from qorqut.bootstrap.client import write_json_private
    path = mode_file(paths, agent_id)
    root = state_root(paths)
    for folder in (root, path.parent):
        if folder.is_symlink():
            raise ValueError("Managed state paths cannot follow symlinks")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(folder, 0o700)
    write_json_private(path, {"schema_version": SCHEMA_VERSION, "mode": mode,
                              "changed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")})
    with _lock:
        _cache.pop(str(path), None)
    return mode_of(paths, agent_id)


def is_managed(paths, agent_id) -> bool:
    return mode_of(paths, agent_id) == MANAGED
