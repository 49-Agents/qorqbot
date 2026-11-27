"""The owner's choice of Claude Code folder (``CLAUDE_CONFIG_DIR``) for every agent: Settings > Model/Provider.

Claude Code keeps its sign-in, settings, skills and memory in one folder, ``~/.claude`` unless ``CLAUDE_CONFIG_DIR`` names
another; on macOS its Keychain item is named after an explicitly named folder. An owner who keeps a second account in, say,
``~/.claude-personal`` (usually through a shell alias that agents never see) names that folder here, and every agent then
runs as that account.

The choice lives in ``<instance>/config/claude-config-dir.json``. The service applies it to its own environment at start and
whenever it changes, by rewriting ``CLAUDE_CONFIG_DIR`` in ``QORQUT_NATIVE_USER_ENV``: the record of the owner's own command
line that everything starting or checking a client reads (``agent_cli_launch.owner_variables`` for managed sessions,
``connectors._probe_environment`` for the sign-in check and button, ``agent_trust.user_environment`` and the terminal broker
for Terminals mode). Clearing the choice restores the value the service started with. Sessions already open keep their
folder until they are restarted (``restart`` in ``native_chat/account_switch.py``).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading

from qorqut.atomic_write import atomic_write
from qorqut_operations.contracts import DomainError, utc_now

FILE = "claude-config-dir.json"
VARIABLE = "CLAUDE_CONFIG_DIR"
NATIVE = "QORQUT_NATIVE_USER_ENV"
MAX_LENGTH = 1024
MAX_FILE_BYTES = 4096

_lock = threading.Lock()
# What the service started with, before any saved choice was applied: {"set": bool, "value": str | None}. Kept per environ
# mapping so tests that pass their own mapping never see the process's value.
_started: dict[int, tuple[bool, str | None]] = {}


def file_path(instance_root) -> Path:
    return Path(instance_root) / "config" / FILE


def _native(environ) -> dict | None:
    try:
        native = json.loads(environ.get(NATIVE) or "null")
    except ValueError:
        return None
    return native if isinstance(native, dict) else None


def _current(environ) -> tuple[bool, str | None]:
    """Whether the owner's record names the variable at all, and its value (None meaning unset)."""
    native = _native(environ)
    if native is not None and VARIABLE in native:
        value = native[VARIABLE]
        return True, value if isinstance(value, str) else None
    return False, environ.get(VARIABLE)


def _owner_home(environ) -> str:
    native = _native(environ) or {}
    home = native.get("HOME") if isinstance(native.get("HOME"), str) else None
    return home or environ.get("HOME") or str(Path.home())


def validate(value, environ=None) -> str:
    """The absolute folder ``value`` names. ``~`` is the owner's own home folder, not the service's. The folder must exist:
    a typo would otherwise send every agent to a fresh, signed-out Claude Code."""
    environ = os.environ if environ is None else environ
    if not isinstance(value, str) or not value.strip():
        raise DomainError("validation_error", "Enter a folder, or clear the setting to use the default")
    text = value.strip()
    if len(text) > MAX_LENGTH or any(ch in text for ch in "\x00\n\r"):
        raise DomainError("validation_error", f"The folder must be one line of at most {MAX_LENGTH} characters")
    if text == "~" or text.startswith("~/"):
        text = _owner_home(environ) + text[1:]
    path = Path(text)
    if not path.is_absolute():
        raise DomainError("validation_error", "Enter a full folder path, such as ~/.claude-personal")
    path = Path(os.path.normpath(path))
    if not path.is_dir():
        raise DomainError("validation_error", f"{path} is not a folder. Sign in to Claude Code with this folder once "
                                              "(for example CLAUDE_CONFIG_DIR=" + str(path) + " claude), then save it here.")
    return str(path)


def saved(instance_root) -> str | None:
    """The saved folder, or None when there is none or the file cannot be used (then the default applies)."""
    path = file_path(instance_root)
    try:
        if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    value = document.get("path") if isinstance(document, dict) and document.get("version") == 1 else None
    return value if isinstance(value, str) and value and Path(value).is_absolute() else None


def apply(folder, environ=None) -> None:
    """Make ``folder`` (or, for None, the value the service started with) the owner's ``CLAUDE_CONFIG_DIR``."""
    environ = os.environ if environ is None else environ
    with _lock:
        key = id(environ)
        if key not in _started:
            _started[key] = _current(environ)
        was_set, original = _started[key]
        native = _native(environ)
        if native is None:
            # Started without the launcher's record: the service's own variable is the owner's.
            if folder is not None:
                environ[VARIABLE] = folder
            elif original is None:
                environ.pop(VARIABLE, None)
            else:
                environ[VARIABLE] = original
            return
        if folder is not None:
            native[VARIABLE] = folder
        elif was_set:
            native[VARIABLE] = original
        else:
            native.pop(VARIABLE, None)
        environ[NATIVE] = json.dumps(native)


def apply_saved(instance_root, environ=None) -> None:
    """At service start: apply the saved choice, if any."""
    folder = saved(instance_root)
    if folder is not None:
        apply(folder, environ)


def view(instance_root, environ=None) -> dict:
    """``{"path", "effective", "default"}``: the saved folder (or None), the folder agents use now and the one used without a
    saved choice. Folders are shown as full paths; None for ``default`` means Claude Code's own ``~/.claude``."""
    environ = os.environ if environ is None else environ
    with _lock:
        _started.setdefault(id(environ), _current(environ))
        original = _started[id(environ)][1]
    current = _current(environ)[1]
    home = _owner_home(environ)
    return {"path": saved(instance_root), "effective": current or str(Path(home) / ".claude"),
            "default": original or str(Path(home) / ".claude")}


def change(instance_root, value, environ=None) -> dict:
    """Save (a folder) or clear (None or empty text) the choice and apply it. Returns ``view``."""
    environ = os.environ if environ is None else environ
    path = file_path(instance_root)
    if value is None or (isinstance(value, str) and not value.strip()):
        path.unlink(missing_ok=True)
        apply(None, environ)
    else:
        folder = validate(value, environ)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps({"version": 1, "path": folder, "changed_at": utc_now()}).encode(), mode=0o600)
        apply(folder, environ)
    return view(instance_root, environ)
