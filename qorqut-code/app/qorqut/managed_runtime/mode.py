"""How this installation runs its agents: managed sessions (the default) or tmux terminals (the legacy option).

The choice is one setting for the whole installation, never one per agent. It is host-local and lives in one small file,
``<runtime>/managed-agents/runtime.json``::

    {"schema_version": 1, "mode": "terminal", "changed_at": "...", "previous": "managed",
     "pending": {"to": "managed", "started": "...", "pid": 123, "force": false},     # only while a switch runs
     "last": {"at": "...", "to": "terminal", "ok": true, "message": ""}}            # the last switch, for Settings

``mode`` always names the last committed mode: a switch ends every session of the current mode first and writes the new mode
last, so a service that dies in the middle is still in the old mode. A missing, unreadable, oversized or unrecognised file
means ``managed``: a new installation and every upgrade run managed sessions until the owner chooses otherwise in Settings.

Reads are cached by the file's identity (inode, size, modification time), so asking for the mode on every request costs one
``stat``. Nothing here raises for a read. Only the owner's switch (``native_chat/runtime_switch.py``) writes.

While a switch runs, ``switching(paths)`` is true in the service process and nothing may open a session of either kind. That
flag lives in memory, so it cannot outlive the process; the ``pending`` entry in the file is the journal a later start reads
(``recover``).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from . import state as layout

MANAGED = "managed"
TERMINAL = "terminal"
MODES = (MANAGED, TERMINAL)
DEFAULT = MANAGED
SCHEMA_VERSION = 1
FILE = "runtime.json"
LIMIT = 4096                # the file is a few hundred bytes; a larger one is not ours
MESSAGE_LIMIT = 400

_lock = threading.Lock()
_cache: dict[str, tuple[tuple, dict]] = {}
_switching: set[str] = set()


def mode_file(paths) -> Path:
    return layout.state_root(paths) / FILE


def _signature(path: Path):
    info = path.stat()
    return (info.st_ino, info.st_size, info.st_mtime_ns)


def _document(paths) -> dict:
    """The file's content when it is ours, else ``{}``. Never raises."""
    if paths is None:
        return {}
    try:
        path = mode_file(paths)
        key = str(path)
        signature = _signature(path)
    except (OSError, ValueError, AttributeError, TypeError):
        try:
            with _lock:
                _cache.pop(str(mode_file(paths)), None)
        except (AttributeError, TypeError):
            pass
        return {}
    with _lock:
        cached = _cache.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    try:
        if signature[1] > LIMIT:
            raise ValueError("oversized")
        value = json.loads(path.read_text())
        ours = isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION and value.get("mode") in MODES
        document = value if ours else {}
    except (OSError, ValueError):
        document = {}
    with _lock:
        _cache[key] = (signature, document)
    return document


def current(paths) -> str:
    """``managed`` or ``terminal`` for this installation. Never raises; anything unreadable is ``managed``."""
    return _document(paths).get("mode", DEFAULT)


def is_terminal(paths) -> bool:
    return current(paths) == TERMINAL


def is_managed(paths) -> bool:
    return current(paths) == MANAGED


def mode_of(paths, agent_id=None) -> str:
    """The installation's mode. ``agent_id`` is accepted and ignored: there is no per-agent choice."""
    return current(paths)


def document(paths) -> dict:
    """A copy of what the file records (``mode``, ``changed_at``, ``previous``, ``pending``, ``last``); ``{}`` without a file."""
    return json.loads(json.dumps(_document(paths)))


def switching(paths) -> bool:
    """True while this process is switching the installation. Every opener refuses (retryably) until it is false again."""
    if paths is None:
        return False
    try:
        key = str(mode_file(paths))
    except (AttributeError, TypeError):
        return False
    with _lock:
        return key in _switching


def _write(paths, change) -> dict:
    """Apply ``change(document)`` to the file atomically (private, 0600) and return the new document."""
    path = mode_file(paths)
    layout.private_directory(path.parent)
    if path.is_symlink():
        raise ValueError("The runtime setting cannot be a symlink")
    value = document(paths)
    value.setdefault("schema_version", SCHEMA_VERSION)
    value.setdefault("mode", DEFAULT)
    change(value)
    layout.write_json_private(path, value)
    with _lock:
        _cache.pop(str(path), None)
    return document(paths)


def _checked(mode) -> str:
    if mode not in MODES:
        raise ValueError("Agents run as managed sessions or in terminals")
    return mode


def begin(paths, to, *, force=False) -> dict:
    """Journal a switch to ``to`` and close the gate. The mode itself does not change until ``commit``."""
    _checked(to)

    def change(value):
        value["pending"] = {"to": to, "started": layout.now_iso(), "pid": os.getpid(), "force": bool(force)}
    result = _write(paths, change)
    with _lock:
        _switching.add(str(mode_file(paths)))
    return result


def _open_gate(paths) -> None:
    with _lock:
        _switching.discard(str(mode_file(paths)))


def commit(paths, mode, *, message="") -> dict:
    """Record ``mode`` as the installation's mode, drop the journal and open the gate. The last step of a switch."""
    _checked(mode)

    def change(value):
        before = value.get("mode", DEFAULT)
        if before != mode:
            value["previous"] = before
        value["mode"] = mode
        value["changed_at"] = layout.now_iso()
        value.pop("pending", None)
        value["last"] = {"at": value["changed_at"], "to": mode, "ok": True, "message": str(message)[:MESSAGE_LIMIT]}
    try:
        return _write(paths, change)
    finally:
        _open_gate(paths)


def abort(paths, *, to=None, message="") -> dict:
    """A switch that did not finish: the mode stays, the journal goes, the gate opens. ``message`` is what Settings shows."""
    def change(value):
        pending = value.pop("pending", None) or {}
        target = to or pending.get("to")
        if target in MODES:
            value["last"] = {"at": layout.now_iso(), "to": target, "ok": False, "message": str(message)[:MESSAGE_LIMIT]}
    try:
        return _write(paths, change)
    finally:
        _open_gate(paths)


def recover(paths):
    """At service start: a journal left by a process that died in the middle of a switch is dropped (the mode on file is
    the truth). Returns the dropped entry, or ``None``. Never raises."""
    try:
        pending = _document(paths).get("pending")
        if not isinstance(pending, dict):
            return None
        abort(paths, message="Qorqbot stopped in the middle of the switch. Nothing was changed; switch again.")
        return pending
    except (OSError, ValueError, AttributeError, TypeError):
        return None
