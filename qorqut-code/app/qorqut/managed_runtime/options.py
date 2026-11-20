"""Per-agent launch options: which client, model and effort a managed agent's session starts with.

Host-local, one small file per agent next to ``host.json``: ``<runtime>/managed-agents/<agent_id>/options.json``::

    {"schema_version": 1, "client": "claude" | "codex" | null, "model": str | null, "effort": str | null}

Until now the owner chose by what they typed in a terminal, and the profile's ``runtime`` often does not match reality, so this is
the one place the choice lives. ``read`` is tolerant (a missing, foreign, oversized or malformed file is "no options"); ``write``
replaces the file atomically (0600) and validates every field.

``effective_client`` is the client an open of this agent uses: the stored ``client``; else the profile's runtime's client when it is
ready (installed and signed in); else the other client when that one is ready; else the profile's client. The first successful
open of an agent with no stored client stores the one used (``remember``), so the agent does not change client later because
another CLI became ready. Changing ``client`` while a session is open is the caller's refusal (``session_open``).

Nothing here starts a process or reads a credential: ``ready`` is supplied by the caller (``clients_ready`` is the usual one).
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from . import clients, state as layout

OPTIONS_FILE = "options.json"
SCHEMA_VERSION = 1
FIELDS = ("client", "model", "effort")
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
MAX_TEXT = 100
OPTIONS_LIMIT = 4096
_MODEL = re.compile(r"^[A-Za-z0-9._:/\[\]-]{1,%d}$" % MAX_TEXT)
_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_write_lock = threading.RLock()


class OptionsError(ValueError):
    """A change that is not valid. ``code`` is ``validation_error``; the message is safe to show the owner."""

    code = "validation_error"


@dataclass(frozen=True)
class Options:
    client: str | None = None
    model: str | None = None
    effort: str | None = None

    def public(self) -> dict:
        return {"client": self.client, "model": self.model, "effort": self.effort}


def options_file(paths, agent_id) -> Path:
    if not isinstance(agent_id, str) or not _AGENT_ID.fullmatch(agent_id):
        raise ValueError("An agent is named by its identity only")
    return layout.agent_state(paths, agent_id) / OPTIONS_FILE


def _valid(field, value):
    """The validated value, or None for "no value". Raises ``OptionsError``."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise OptionsError(f"{field} must be text or null")
    value = value.strip()
    if not value:
        return None                      # an empty box clears the setting
    if field == "client":
        if value not in clients.CLIENTS:
            raise OptionsError("client must be claude or codex")
    elif field == "effort":
        if value not in EFFORTS:
            raise OptionsError("effort must be one of " + ", ".join(EFFORTS))
    elif value.startswith("-") or not _MODEL.fullmatch(value):
        raise OptionsError(f"model must be at most {MAX_TEXT} characters of letters, digits and . _ : / [ ] -, and not start with -")
    return value


def read(paths, agent_id) -> Options:
    """The stored options. Never raises: anything unreadable is no options, and a stored field that does not validate is dropped."""
    if paths is None:
        return Options()
    try:
        value = layout.read_json(options_file(paths, agent_id), OPTIONS_LIMIT)
    except (ValueError, AttributeError, TypeError):
        return Options()
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return Options()
    found = {}
    for field in FIELDS:
        try:
            found[field] = _valid(field, value.get(field))
        except OptionsError:
            found[field] = None
    return Options(**found)


def write(paths, agent_id, **changes) -> Options:
    """Apply ``changes`` (any of ``client``, ``model``, ``effort``; ``None`` clears one) and return the options now stored.
    ``OptionsError`` for an unknown field or an invalid value; nothing is written then. ``OSError`` when the file cannot be saved."""
    unknown = sorted(set(changes) - set(FIELDS))
    if unknown:
        raise OptionsError("unknown option: " + ", ".join(str(item)[:30] for item in unknown))
    checked = {field: _valid(field, value) for field, value in changes.items()}
    path = options_file(paths, agent_id)
    with _write_lock:
        current = read(paths, agent_id).public()
        merged = {**current, **checked}
        for folder in (layout.state_root(paths), path.parent):     # as ``mode.set_mode`` makes them
            if folder.is_symlink():
                raise OSError("managed state paths cannot follow symlinks")
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(folder, 0o700)
        layout.write_json_private(path, {"schema_version": SCHEMA_VERSION, **merged})
    return Options(**merged)


def profile_client(profile):
    """The client a profile's ``runtime`` names (``claude-code`` -> ``claude``, ``codex`` -> ``codex``), or None. ``profile`` is a
    profile row (``profile_json`` text or object), or any mapping with ``runtime``."""
    runtime = None
    if isinstance(profile, dict):
        value = profile.get("profile_json")
        try:
            value = json.loads(value) if isinstance(value, str) else value
        except ValueError:
            value = None
        runtime = value.get("runtime") if isinstance(value, dict) else profile.get("runtime")
    return clients.runtime_client(runtime)


def choose(stored, profile, ready) -> str:
    """The effective client from the pieces: see the module text. ``ready(client)`` is asked only when it can matter."""
    if stored in clients.CLIENTS:
        return stored
    wanted = profile_client(profile) or clients.CLAUDE
    try:
        if ready(wanted):
            return wanted
        other = clients.CODEX if wanted == clients.CLAUDE else clients.CLAUDE
        if ready(other):
            return other
    except Exception:  # noqa: BLE001 - a status that cannot be read is "not ready": the profile's own client stays
        pass
    return wanted


def effective_client(paths, agent_id, profile, ready) -> str:
    return choose(read(paths, agent_id).client, profile, ready)


def remember(paths, agent_id, client) -> bool:
    """The sticky rule: after the first successful open of an agent with no stored client, store the client that was used. True
    when it was stored now. Never raises (a failed write only means the rule applies again at the next open)."""
    if client not in clients.CLIENTS:
        return False
    try:
        with _write_lock:
            if read(paths, agent_id).client is not None:
                return False
            write(paths, agent_id, client=client)
            return True
    except (OSError, ValueError):
        return False


def clients_ready(environ=None, run=None) -> dict:
    """``{"claude": {"ready": bool}, "codex": {"ready": bool}}``: each CLI is installed and signed in (the same status Settings
    shows). Reads credentials only through the existing status code; starts no sign-in."""
    try:
        from qorqut import owner_settings
        view = owner_settings.agent_clients_view(environ) if run is None else owner_settings.agent_clients_view(environ, run=run)
    except Exception:  # noqa: BLE001 - nothing is ready that could not be checked
        view = {}
    return {name: {"ready": bool(isinstance(view.get(name), dict) and view[name].get("installed") and view[name].get("signed_in"))}
            for name in clients.CLIENTS}


def ready_function(environ=None, run=None):
    """A ``ready(client)`` that reads the status once, on first use."""
    cache = {}

    def ready(client):
        if not cache:
            cache.update(clients_ready(environ, run))
        return bool(cache.get(client, {}).get("ready"))
    return ready
