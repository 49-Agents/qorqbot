"""Codex file formats for the shared prepared agent configuration."""

import tomllib
from pathlib import Path

import tomli_w
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage import safe_io

from qorqut.agents.roles import GLOBAL_WORKER_ROLE, ORDUBASHY_ROLE, SUPPORTED_GLOBAL_ROLES
from qorqut.agents.worker_models import LUNA_MODEL
from qorqut_agentic.behavior.instructions import BOOTSTRAP_BUDGET

# Owner-chosen Codex startup defaults: (model, reasoning effort) by supported global role.
ROLE_MODEL_DEFAULTS = {
    "ordubashy": (LUNA_MODEL, "high"),
    "learning-applier": ("gpt-6-astra", "low"),
}


def role_model_default(scope_kind, role_id):
    """The startup default of a supported global role in ecosystem scope; None for any other agent."""
    if scope_kind != "ecosystem" or role_id not in SUPPORTED_GLOBAL_ROLES:
        return None
    # A permanent worker starts with the model and effort Ordubashy starts with (only those two settings).
    return ROLE_MODEL_DEFAULTS.get(ORDUBASHY_ROLE if role_id == GLOBAL_WORKER_ROLE else role_id)


def persisted_model_settings(workspace):
    """The (model, effort) a workspace's own Codex configuration already carries; a missing or unusable key is None.

    Only these two keys are read: never credentials, server definitions or any other setting."""
    try:
        document = tomllib.loads(safe_io.read_bytes(Path(workspace) / ".codex/config.toml").decode())
    except (OSError, ValueError, DomainError):
        return None, None
    values = (document.get("model"), document.get("model_reasoning_effort"))
    return tuple(value if isinstance(value, str) and 0 < len(value) <= 128 and value.isprintable() else None for value in values)


def worker_model_default(workspace, manager_workspace, fallback):
    """A worker's startup model and effort, each key taking the first of: the worker's own persisted choice (kept across
    re-preparation and conversion), the configured Ordubashy's persisted choice (inherited by a new worker), the runtime default."""
    own = persisted_model_settings(workspace)
    manager = persisted_model_settings(manager_workspace) if manager_workspace is not None else (None, None)
    return tuple(own[index] or manager[index] or fallback[index] for index in range(2))


def add_activity_hooks(hooks, command, runtime):
    """Append the production session-activity hooks for one native runtime."""
    events = ['SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse',
        'PermissionRequest', 'Stop', 'SessionEnd']
    events += ['Interrupt'] if runtime == 'codex' else ['PostToolUseFailure']
    for event in events:
        hooks.setdefault(event, []).append({'hooks': [{'type': 'command', 'command': command, 'timeout': 15 if event == 'Stop' else 3}]})


def configuration(*, workspace, endpoint, header_command, command, snapshot_command,
                  protected, read_denied, write_roots=(), delegate_operations=False,
                  external_servers=None, permission_mode="scoped", model_default=None,
                  preserved_servers=None, mcp_command=None):
    if permission_mode not in {"scoped", "bypass"}:
        raise ValueError("Unknown Codex permission mode")
    workspace = Path(workspace)
    hooks = {
        "SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [{
            "type": "command", "command": command, "timeout": 240,
            "additionalContextLimit": BOOTSTRAP_BUDGET}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": command,
            "timeout": 240, "additionalContextLimit": BOOTSTRAP_BUDGET}]}],
    }
    for event in ("Stop", "SessionEnd"):
        # SessionEnd also stops this session's inbox delivery process.
        hooks[event] = [{"hooks": [{"type": "command", "command": snapshot_command,
                                    "timeout": 3}]}]
    filesystem = {"/": "read", str(workspace / "work"): "write", "/tmp": "write"}
    filesystem.update({str(path): "write" for path in write_roots})
    filesystem.update({str(path): "read" for path in protected})
    filesystem.update({str(path): "deny" for path in read_denied})
    # MCP and hook helpers run in the host, not in model-issued shell commands.
    config = {
        "default_permissions": "qorqut-agent",
        "approval_policy": "never",
        # Native current-view metadata for Chat/Terminal attachment. Unlike the
        # launch arguments, Codex updates this when the owner changes threads.
        "tui": {"terminal_title": ["session-id"]},
        # Preserve the operator-provided terminal environment for shell tools.
        # Never serialize credential values into this generated configuration.
        "shell_environment_policy": {"inherit": "all", "ignore_default_excludes": True},
        "permissions": {"qorqut-agent": {"filesystem": filesystem,
            "network": {"enabled": True}}},
        "mcp_servers": {"qorqut": ({"command": mcp_command[0], "args": mcp_command[1:],
            "startup_timeout_sec": 240, "tool_timeout_sec": 240, "required": True,
            "default_tools_approval_mode": "approve"} if mcp_command else {
            "url": endpoint + "/mcp", "http_headers_helper": header_command, "required": True,
            "default_tools_approval_mode": "approve"})},
    }
    if permission_mode == "bypass":
        # An explicit operator selection. Retaining deny rules here makes Codex
        # re-enable its filesystem sandbox even when Full Access is selected.
        config["default_permissions"] = ":danger-full-access"
        config["approval_policy"] = "never"
        del config["permissions"]
    if delegate_operations:
        config["agents"] = {
            "enabled": True,
            "default_subagent_model": LUNA_MODEL,
            "default_subagent_reasoning_effort": "low",
            "max_concurrent_threads_per_session": 3,
        }
    if model_default is not None:
        # Startup defaults only: never inject model flags into interactive launches.
        config["model"], config["model_reasoning_effort"] = model_default
    # Foreign servers found in the previous file are kept verbatim.
    for name, server in (preserved_servers or {}).items():
        if name not in {"qorqut", "bazarlyk"}:
            config["mcp_servers"][name] = server
    # Role-declared connections are required unless the entry marks itself
    # optional. Credentials are referenced by helper or variable name only.
    for name, server in (external_servers or {}).items():
        if name in {"qorqut", "bazarlyk"}:
            raise ValueError("External connections cannot replace the scoped Qorqut server")
        entry = dict(server)
        required = entry.pop("required", True)
        config["mcp_servers"][name] = {**entry, "required": required,
            "default_tools_approval_mode": "approve"}
    return tomli_w.dumps(config).encode(), hooks
