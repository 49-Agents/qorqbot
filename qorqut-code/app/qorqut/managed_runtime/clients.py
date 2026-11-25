"""The one small table of what differs between the agent clients a managed session can run: Claude Code and Codex.

Everything in ``manager.py``, ``host.py`` and ``native_chat/adapters.py`` that differs between the two clients asks this table,
keyed on the *effective client* of the agent (``options.effective_client``). Claude is driven by ``claude_driver.ClaudeSession``
(``claude -p`` over stream-json), Codex by ``codex_driver.CodexSession`` (``codex app-server`` over stdio).

Per client:

``provider``       the archive/selection provider name (``claude`` or ``codex``); also what a bound session's ``provider`` says
``runtime``        the profile ``runtime`` value that names this client (``claude-code`` or ``codex``)
``driver``         ``module:Class`` of the driver the host loads
``receipt``        the client name written in input receipts
``role``           the first message a fresh session gets (Claude ``/role``, Codex ``$role``)
``environment``    the name of the executable override environment variable (``agent_cli_install.EXECUTABLE_ENVIRONMENT``)
``efforts``        the effort levels the client's own command line accepts, and how the others are written (``minimal`` for Claude)
``own_compaction`` the variables that switch the client's own automatic compaction off (``launch_environment``); absent when
                   the client has no such switch here
``always``         the variables every session of the client is started with (``launch_environment``): Claude Code's own
                   memory is off, because what an agent has to keep is filed in Qorqbot's records
``always_arguments`` the same for a client that takes it on its command line (``always_arguments``): Codex's own memory
"""

from __future__ import annotations

import json
from pathlib import Path
import tomllib

CLAUDE, CODEX = "claude", "codex"
# Claude Code's own memory (notes it keeps for itself under its configuration folder) is off in everything Qorqbot starts.
CLAUDE_MEMORY_OFF = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
CLAUDE_MEMORY_SETTING = {"autoMemoryEnabled": False}
# Codex's own memory (``memories_1.sqlite`` under its folder), the same way, as command-line configuration.
CODEX_MEMORY_OFF = ("-c", "features.memories=false")
CLIENTS = (CLAUDE, CODEX)
NAMES = {CLAUDE: "Claude Code", CODEX: "Codex"}

TABLE = {
    CLAUDE: {"provider": "claude", "runtime": "claude-code", "driver": "qorqut.managed_runtime.claude_driver:ClaudeSession",
             "receipt": "claude", "role": "/role", "settings": ".qorqut/claude-launch-settings.json",
             # ``claude --effort`` takes low, medium, high, xhigh, max (checked against ``--help`` of 2.1.269 and 2.1.327);
             # Qorqbot's ``minimal`` is the lowest of them.
             "effort_flags": {"minimal": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max"},
             # Checked in Claude Code 2.1.328: with it set the client compacts neither at its limit nor while idle, whatever
             # its ``autoCompactEnabled`` setting says; the manual ``/compact`` stays.
             "own_compaction": {"DISABLE_AUTO_COMPACT": "1"},
             # Checked in Claude Code 2.1.329: with it set the client gets no memory folder in its prompt and writes no notes
             # there, whatever the owner's own Claude settings say. It is set on the process Qorqbot starts and nowhere else,
             # so the owner's own Claude Code keeps its memory. ``autoMemoryEnabled`` in the prepared launch settings
             # (``bootstrap/preparation.py``) says the same for a client started from the agent's terminal.
             "always": dict(CLAUDE_MEMORY_OFF)},
    # Codex: the first message is the ``$role`` skill text (the engine expands it; ``/role`` would be plain text). Model and effort
    # reach the driver as ``--model`` / ``--effort`` in the arguments (it reads and strips them); the permission mode comes from the
    # prepared ``.codex/config.toml`` (full access = ``bypass``), not from a Claude settings file. The host is ready only when the
    # driver announces the thread (``ready_seconds`` 0): ``session_started`` comes right after ``thread/start``.
    CODEX: {"provider": "codex", "runtime": "codex", "driver": "qorqut.managed_runtime.codex_driver:CodexSession",
            "receipt": "codex", "role": "$role", "settings": ".codex/config.toml", "ready_seconds": 0,
            # Checked in Codex 0.167.0 (``codex features list``): the override wins over ``memories = true`` in the
            # ``config.toml`` of the client's own folder. The feature is off unless a configuration turns it on.
            "always_arguments": list(CODEX_MEMORY_OFF),
            "effort_flags": {name: name for name in ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")}},
}
# Whether a driver exists for the client.
DRIVER_READY = {CLAUDE: True, CODEX: True}
CODEX_CONFIG = ".codex/config.toml"
CODEX_FULL_ACCESS = ":danger-full-access"
CODEX_DEFAULT_EFFORT = "medium"

BYPASS, DENY = "bypass", "deny"
CLAUDE_BYPASS_MODE = "bypassPermissions"


class UnknownClient(ValueError):
    """A client name that is not in the table."""


def entry(client) -> dict:
    try:
        return TABLE[client]
    except (KeyError, TypeError):
        raise UnknownClient(str(client)[:40]) from None


def known(client) -> bool:
    return isinstance(client, str) and client in TABLE


def available(client) -> bool:
    """Whether a managed session can be run with this client now (its driver exists)."""
    return bool(known(client) and DRIVER_READY.get(client))


def by_provider(provider):
    """The client whose archive provider is ``provider`` (a bound session's ``provider``), or None."""
    return provider if known(provider) and TABLE[provider]["provider"] == provider else None


def runtime_client(runtime):
    """The client a profile ``runtime`` names, or None."""
    for name, item in TABLE.items():
        if item["runtime"] == runtime:
            return name
    return None


def driver(client) -> str:
    return entry(client)["driver"]


def receipt_client(client) -> str:
    return entry(client)["receipt"]


def role_command(client) -> str:
    return entry(client)["role"]


def effort_argument(client, effort):
    """How ``effort`` is written for this client, or None when the client has no such level (it is then not passed)."""
    return entry(client)["effort_flags"].get(effort) if effort else None


def codex_defaults(workspace):
    """``(model, effort)`` of the agent's prepared ``.codex/config.toml`` (only those two keys are read); None for a missing or
    unusable one."""
    try:
        path = Path(workspace) / CODEX_CONFIG
        if path.stat().st_size > 1024 * 1024:
            return None, None
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None, None
    found = (document.get("model"), document.get("model_reasoning_effort"))
    return tuple(value if isinstance(value, str) and 0 < len(value) <= 100 and value.isprintable() else None for value in found)


def codex_model_effort(model, effort, workspace):
    """The pair Codex is started with: always both or neither. The owner's choice first, then the prepared configuration's own
    defaults for what is empty. A chosen model with no effort anywhere gets ``medium``; an effort with no model to go with is not
    passed (the prepared configuration still carries its own)."""
    default_model, default_effort = codex_defaults(workspace) if workspace else (None, None)
    model, effort = model or default_model, effort or default_effort
    if model and not effort:
        effort = CODEX_DEFAULT_EFFORT
    return (model, effort) if model and effort else (None, None)


def launch_arguments(client, model, effort, workspace=None) -> list:
    """The command-line flags a chosen model and effort add. Claude: ``--model <model>`` and ``--effort <level>``. Codex: both
    ``--model`` and ``--effort`` together (see ``codex_model_effort``; the driver reads and removes them), plus the permission
    mode of the prepared configuration, which the driver would otherwise read from the same file."""
    arguments = []
    if client == CODEX:
        model, effort = codex_model_effort(model, effort, workspace)
        if model:
            arguments += ["--model", model, "--effort", effort_argument(client, effort) or effort]
        arguments.append("--bypass-permissions" if permission_mode(client, workspace) == BYPASS else "--scoped-permissions")
        return arguments
    if model:
        arguments += ["--model", model]
    written = effort_argument(client, effort)
    if written:
        arguments += ["--effort", written]
    return arguments


def launch_environment(client, *, own_compaction=False) -> dict:
    """The variables this table adds to the client's environment: those every session of the client gets (``always``),
    and with ``own_compaction`` (Qorqbot compacts this session's conversation itself, ``native_chat/compaction.py`` decides
    that) those that switch the client's automatic compaction off. Nothing for a client with neither (Codex)."""
    item = entry(client)
    return {**(item.get("always") or {}), **((item.get("own_compaction") or {}) if own_compaction else {})}


def always_arguments(client) -> list:
    """The command-line arguments every session of the client is started with, before the others."""
    return list(entry(client).get("always_arguments") or ())


def settings_arguments(client, workspace) -> list:
    """``--settings <file>`` with the agent's prepared launch settings, for the client that takes one (Claude) and only when the file
    exists. Codex reads its prepared ``.codex/config.toml`` through the driver."""
    if client != CLAUDE:
        return []
    path = Path(workspace) / entry(client)["settings"]
    return ["--settings", str(path)] if path.is_file() else []


def ready_seconds(client):
    """How long the client's process must have stayed up before the host counts as ready, or None for the host's own default.
    0 (Codex) waits for the driver's ``session_started``, which comes right after ``thread/start``."""
    return entry(client).get("ready_seconds")


PROJECT_SETTINGS = ".claude/settings.json"
MAX_ALLOW_ENTRIES, MAX_ALLOW_LENGTH = 2000, 500


def allowed_tool_arguments(client, workspace) -> list:
    """``--allowedTools <rule>...`` carrying the ``permissions.allow`` rules of the agent's prepared ``.claude/settings.json``.

    Claude Code ignores those rules while the folder has not been trusted (the trust dialog is gone from managed sessions and
    ``~/.claude.json`` is not Qorqbot's to edit), which would leave a ``dontAsk`` agent unable to use any of its tools. Rules given on
    the command line apply whether or not the folder is trusted. Read afresh at every launch, so they always match what
    preparation wrote last. Nothing for another client, and nothing for a missing, oversized or malformed file (fail closed: the
    agent then only gets what its mode allows). Deny rules need no help: they are honoured from an untrusted folder."""
    if client != CLAUDE:
        return []
    try:
        path = Path(workspace) / PROJECT_SETTINGS
        if path.stat().st_size > 1024 * 1024:
            return []
        rules = json.loads(path.read_text())["permissions"]["allow"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    if not isinstance(rules, list):
        return []
    rules = [item for item in rules if isinstance(item, str) and item and not item.startswith("-") and len(item) <= MAX_ALLOW_LENGTH]
    return ["--allowedTools", *rules[:MAX_ALLOW_ENTRIES]] if rules else []


def permission_mode(client, workspace) -> str:
    """``bypass`` when the agent's prepared settings say so, otherwise ``deny``. Fails closed: a missing, unreadable or
    unrecognised file is ``deny``. Claude: ``.qorqut/claude-launch-settings.json`` ``permissions.defaultMode`` is
    ``bypassPermissions``; Codex: the prepared ``.codex/config.toml`` has ``default_permissions`` ``:danger-full-access``."""
    if not known(client) or not workspace:
        return DENY
    try:
        path = Path(workspace) / entry(client)["settings"]
        if path.stat().st_size > 1024 * 1024:
            return DENY
        if client == CODEX:
            return BYPASS if tomllib.loads(path.read_text(encoding="utf-8")).get("default_permissions") == CODEX_FULL_ACCESS else DENY
        value = json.loads(path.read_text())
        mode = value["permissions"]["defaultMode"]
    except (OSError, ValueError, KeyError, TypeError, UnknownClient):
        return DENY
    return BYPASS if mode == CLAUDE_BYPASS_MODE else DENY
