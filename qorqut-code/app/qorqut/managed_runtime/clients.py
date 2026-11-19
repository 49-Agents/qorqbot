"""The one small table of what differs between the agent clients a managed session can run: Claude Code and Codex.

Everything in ``manager.py``, ``host.py`` and ``native_chat/adapters.py`` that used to assume Claude asks this table, keyed on the
*effective client* of the agent (``options.effective_client``). Claude's entry is the behaviour managed sessions always had.
Codex's entry is a set of clearly marked hooks (``TODO(codex)``): ``DRIVER_READY["codex"]`` is False, so opening a Codex agent
fails with ``driver_unavailable`` until ``managed_runtime/codex_driver.py`` is plugged in and that flag is switched on.

Per client:

``provider``       the archive/selection provider name (``claude`` or ``codex``); also what a bound session's ``provider`` says
``runtime``        the profile ``runtime`` value that names this client (``claude-code`` or ``codex``)
``driver``         ``module:Class`` of the driver the host loads
``receipt``        the client name written in input receipts
``role``           the first message a fresh session gets (Claude ``/role``, Codex ``$role``)
``environment``    the name of the executable override environment variable (``agent_cli_install.EXECUTABLE_ENVIRONMENT``)
``efforts``        the effort levels the client's own command line accepts, and how the others are written (``minimal`` for Claude)
"""

from __future__ import annotations

import json
from pathlib import Path

CLAUDE, CODEX = "claude", "codex"
CLIENTS = (CLAUDE, CODEX)
NAMES = {CLAUDE: "Claude Code", CODEX: "Codex"}

TABLE = {
    CLAUDE: {"provider": "claude", "runtime": "claude-code", "driver": "qorqut.managed_runtime.claude_driver:ClaudeSession",
             "receipt": "claude", "role": "/role", "settings": ".qorqut/claude-launch-settings.json",
             # ``claude --effort`` takes low, medium, high, xhigh, max (checked against ``--help`` of 2.1.269 and 2.1.327);
             # Qorqbot's ``minimal`` is the lowest of them.
             "effort_flags": {"minimal": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max"}},
    # TODO(codex): the driver is written elsewhere (``codex_driver.py``); ``role`` is the ``$role`` skill text, model and effort
    # go through ``thread/start`` and ``turn/start`` (the driver reads them from the launch spec's ``model`` and ``effort``), and
    # the permission mode comes from the agent's Codex permission mode (``scoped`` / ``bypass``), not from a Claude settings file.
    CODEX: {"provider": "codex", "runtime": "codex", "driver": "qorqut.managed_runtime.codex_driver:CodexSession",
            "receipt": "codex", "role": "$role", "settings": None, "effort_flags": {}},
}
# Whether a driver exists for the client. The Codex entry stays False until its driver is plugged in (stage 2).
DRIVER_READY = {CLAUDE: True, CODEX: False}

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


def launch_arguments(client, model, effort) -> list:
    """The command-line flags a chosen model and effort add. Claude: ``--model <model>`` and ``--effort <level>``. Nothing for a
    client that takes them another way (Codex: through the driver, from the launch spec)."""
    if client != CLAUDE:
        return []
    arguments = []
    if model:
        arguments += ["--model", model]
    written = effort_argument(client, effort)
    if written:
        arguments += ["--effort", written]
    return arguments


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
    unrecognised file, or a client with no way to tell, is ``deny``."""
    if client != CLAUDE:
        return DENY      # TODO(codex): the agent's Codex permission mode (``bypass`` for the ecosystem scope) once the driver exists
    try:
        path = Path(workspace) / entry(client)["settings"]
        if path.stat().st_size > 64 * 1024:
            return DENY
        value = json.loads(path.read_text())
        mode = value["permissions"]["defaultMode"]
    except (OSError, ValueError, KeyError, TypeError, UnknownClient):
        return DENY
    return BYPASS if mode == CLAUDE_BYPASS_MODE else DENY
